import argparse
import csv
import json
import re
import sys
from pathlib import Path

import duckdb

from . import database, layouts, llm, sources, sql

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOTS = [PROJECT_ROOT / "data" / split / "data" for split in ("train", "public_test")]


def _inventory(business):
    inventory = sources.inspect_business(business["path"])
    for file in inventory["files"]:
        for table in file["tables"]:
            table["source_file"] = file["name"]
            table["format"] = file["format"]
    return inventory


def _read_tables(inventory, description):
    descriptions = {(t["source_file"], t["source_table"]): t for t in description["tables"]}
    raw, validated_layouts = [], {}
    for file in inventory["files"]:
        for table in file["tables"]:
            key = (file["name"], table["name"])
            proposal = descriptions[key]
            fmt = file["format"]
            source_path = inventory["path"] / file["name"]
            if fmt == ".sqlite":
                rows, columns = sources.read_sqlite_rows(source_path, table["name"])
                column_types = [column.get("observed_types", []) for column in table["columns"]]
                declared_types = [column.get("type", "") for column in table["columns"]]
                raw.append({"source_file": file["name"], "source_table": table["name"], "format": fmt,
                            "columns": columns, "column_types": column_types, "declared_types": declared_types,
                            "rows": [[_sqlite_text(value) if len(column_types[i]) > 1 else value for i, value in enumerate(row)] for row in rows]})
                continue
            if fmt == ".csv" and table.get("row_count", 0) == 0:
                raw.append({"source_file": file["name"], "source_table": table["name"], "format": fmt, "columns": ["column_1"], "rows": []})
                continue
            if fmt == ".xlsx" and table.get("is_empty", False):
                raw.append({"source_file": file["name"], "source_table": table["name"], "format": fmt, "columns": ["column_1"], "rows": []})
                continue
            if fmt == ".csv":
                rows = _read_csv_grid(source_path, file["delimiter"])
            else:
                from openpyxl import load_workbook
                workbook = load_workbook(source_path, read_only=True, data_only=False)
                try:
                    rows = [[sources._value(v) for v in row] for row in workbook[table["name"]].iter_rows(values_only=True)]
                finally:
                    workbook.close()
            layout = layouts.validate_layout(rows, proposal)
            validated_layouts[key] = layout
            columns = sources._unique(layout["headers"])
            values = layout["records"]
            raw.append({"source_file": file["name"], "source_table": table["name"], "format": fmt,
                        "columns": columns, "rows": [[_to_text(v, fmt) for v in row] for row in values]})
    return raw, validated_layouts


def _read_csv_grid(path, delimiter):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream, delimiter=delimiter, strict=True))


def _to_text(value, fmt):
    if value is None:
        return None
    if fmt == ".csv":
        return value
    return str(value)


def _sqlite_text(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return json.dumps({"$type": "blob", "base64": __import__("base64").b64encode(value).decode("ascii")}, separators=(",", ":"))
    return json.dumps({"$type": type(value).__name__, "value": value}, ensure_ascii=False, separators=(",", ":"))


def _offline_description(inventory):
    descriptions = []
    for file in inventory["files"]:
        for table in file["tables"]:
            if file["format"] == ".sqlite":
                descriptions.append({"source_file": file["name"], "source_table": table["name"], "header_row": None,
                                     "data_start_row": None, "columns": [{"position": i, "label": c["name"]} for i, c in enumerate(table["columns"], 1)],
                                     "preamble_rows": [], "report_context": [], "description": "", "uncertainties": []})
                continue
            candidates = layouts.candidate_layout(table.get("preview", []))
            if not candidates:
                if file["format"] == ".csv" and table.get("row_count", 0) == 0:
                    descriptions.append({"source_file": file["name"], "source_table": table["name"], "header_row": None, "data_start_row": None,
                        "columns": [{"position": 1, "label": "column_1"}], "preamble_rows": [], "report_context": [], "description": "", "uncertainties": ["empty source"]})
                    continue
                if table.get("is_empty", False) or table.get("row_count", 0) == 0:
                    descriptions.append({"source_file": file["name"], "source_table": table["name"], "header_row": 1, "data_start_row": 2,
                        "columns": [{"position": 1, "label": None}], "preamble_rows": [], "report_context": [], "description": "", "uncertainties": []})
                    continue
                raise ValueError(f"offline mode cannot identify header for {file['name']} / {table['name']}; use model mode")
            candidate = candidates[0]
            row = table["preview"][candidate["header_row"] - 1]
            descriptions.append({"source_file": file["name"], "source_table": table["name"], "header_row": candidate["header_row"],
                                 "data_start_row": candidate["header_row"] + 1,
                                 "columns": [{"position": i, "label": value} for i, value in enumerate(row, 1)],
                                 "preamble_rows": list(range(1, candidate["header_row"])), "report_context": [], "description": "", "uncertainties": []})
    return {"summary": "Offline structural import; no LLM descriptions", "tables": descriptions,
            "rules": [], "relationships": [], "uncertainties": ["LLM analysis skipped"]}


def _dest(raw, used):
    base = raw["source_file"].rsplit(".", 1)[0]
    if raw["format"] in (".xlsx", ".sqlite"):
        base += "__" + raw["source_table"]
    base = re.sub(r"[^\w]+", "_", base, flags=re.UNICODE).strip("_") or "table"
    name, suffix = base, 2
    while name.casefold() in used:
        name, suffix = f"{base}__{suffix}", suffix + 1
    used.add(name.casefold())
    return name


def run(args):
    roots = args.roots or DEFAULT_ROOTS
    businesses = sources.discover_businesses([Path(p) for p in roots])
    if not businesses:
        raise ValueError("no businesses found in the configured data roots")
    if args.business:
        businesses = [b for b in businesses if b["id"] == args.business]
        if not businesses:
            raise ValueError(f"business not found: {args.business}")
    db_path = Path(args.db) if args.db else PROJECT_ROOT / "tmp" / "businesses.duckdb"
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    failed = 0
    try:
        for business in businesses:
            try:
                inventory = _inventory(business)
                client = None if args.offline else llm.create_client()
                description = _offline_description(inventory) if args.offline else llm.describe_business(client, inventory)
                raw, layout_by_source = _read_tables(inventory, description)
                schema_name = business["id"]
                used = set()
                staging = [{"name": f"_stage_{i}", "columns": t["columns"], "destination": _dest(t, used)} for i, t in enumerate(raw)]
                load_plan = sql.build_load_plan(staging, schema_name) if args.offline else sql.validate_load_plan(
                    llm.generate_load_sql(client, description, staging, schema_name), staging, schema_name)
                result = database.load_business(conn, business, description, raw, layout_by_source, load_plan)
                print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
            except Exception as exc:
                failed += 1
                print(json.dumps({"status": "error", "business_id": business["id"], "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
    finally:
        conn.close()
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import business source files into DuckDB schemas")
    parser.add_argument("--db", help="DuckDB path (default: project tmp/businesses.duckdb)")
    parser.add_argument("--business", help="Import one business ID")
    parser.add_argument("--offline", action="store_true", help="Skip Qwen; import only unambiguous layouts")
    parser.add_argument("--roots", nargs="*", help="Override source data roots")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
