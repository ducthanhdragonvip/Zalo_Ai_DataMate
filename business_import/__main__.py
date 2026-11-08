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


def _read_tables(inventory, description, defer_types=False):
    online = "columns" in description
    documented = {layouts.normalize_label(column["name"]) for column in description.get("columns", [])} | {
        layouts.normalize_label(name)
        for name in re.findall(r"\*\*([^*\n]+)\*\*", inventory.get("readme", ""))
    }
    if online:
        description["tables"] = []
    descriptions = {(t["source_file"], t["source_table"]): t for t in description["tables"]}
    raw, validated_layouts = [], {}
    for file in inventory["files"]:
        for table in file["tables"]:
            key = (file["name"], table["name"])
            if online:
                proposal = {"source_file": file["name"], "source_table": table["name"],
                            "description": "", "type_hint_warnings": []}
                description["tables"].append(proposal)
            else:
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
            excel_epoch = None
            if fmt == ".csv":
                rows = _read_csv_grid(source_path, file["delimiter"])
            else:
                from openpyxl import load_workbook
                workbook = load_workbook(source_path, read_only=True, data_only=False)
                try:
                    excel_epoch = workbook.epoch.date().isoformat()
                    rows = [[sources._value(v) for v in row] for row in workbook[table["name"]].iter_rows(values_only=True)]
                finally:
                    workbook.close()
            if (online and rows and len(rows[0]) >= 2
                    and all(len(row) == len(rows[0]) and all(
                        value in (None, "") or isinstance(value, str) and layouts._cell_kind(value) == "text"
                        for value in row) for row in rows)
                    and not any(layouts.normalize_label(value) in documented for row in rows for value in row)
                    and not layouts.candidate_layout(rows, allow_preamble=True)):
                # ponytail: unknowable all-text headers retain every row; add explicit header evidence to name columns.
                columns = [f"column_{i}" for i in range(1, len(rows[0]) + 1)]
                proposal["type_hint_warnings"].append(
                    "no supported header evidence; preserved all rows with generated column names")
                raw.append({"source_file": file["name"], "source_table": table["name"], "format": fmt,
                            "columns": columns, "rows": [[_to_text(v, fmt) for v in row] for row in rows]})
                continue
            if online:
                try:
                    header_row = layouts.select_header(rows, documented)
                except ValueError as exc:
                    raise ValueError(f"{key[0]} / {key[1]}: {exc}") from exc
                proposal.update(header_row=header_row, data_start_row=header_row + 1,
                                columns=[{"position": i, "label": label}
                                         for i, label in enumerate(rows[header_row - 1], 1)])
            layout = layouts.validate_layout(rows, proposal, documented if online else None)
            validated_layouts[key] = layout
            columns = sources._unique(layout["headers"])
            header = rows[layout["header_row"] - 1]
            records = layout["records"]
            footer_notes = []
            # ponytail: only contiguous, explicitly labelled Excel footers; ambiguous/interior notes remain records.
            if fmt == ".xlsx":
                while records and isinstance(records[-1][0], str) and re.match(
                        r"^(?:ghi chú|note)\s*:", records[-1][0].strip(), re.IGNORECASE) and all(
                        value in (None, "") for value in records[-1][1:]):
                    footer_notes.append({"source_row": layout["data_start_row"] + len(records) - 1,
                                         "values": records.pop()})
            if footer_notes:
                proposal["footer_notes"] = list(reversed(footer_notes))
                proposal.setdefault("type_hint_warnings", []).append(
                    f"preserved {len(footer_notes)} trailing worksheet notes in catalog metadata, not transaction rows")
            values = [row for row in records if row != header]
            layout["repeated_header_count"] = len(records) - len(values)
            raw.append({"source_file": file["name"], "source_table": table["name"], "format": fmt,
                        "columns": columns, "excel_epoch": excel_epoch,
                        "rows": [[_to_text(v, fmt) for v in row] for row in values]})
    if online and not defer_types:
        _apply_recommendations(description, validated_layouts)
    return raw, validated_layouts


def _apply_recommendations(description, validated_layouts):
    recommendations, conflicts, matched = {}, set(), set()
    for column in description["columns"]:
        name = layouts.normalize_label(column["name"])
        spec = {key: value for key, value in column.items() if key != "name"}
        if name in recommendations and recommendations[name] != spec:
            conflicts.add(name)
        recommendations[name] = spec
    for proposal in description["tables"]:
        if (proposal["source_file"], proposal["source_table"]) in validated_layouts:
            _map_recommendations(proposal, [column["label"] for column in proposal["columns"]],
                                 recommendations, conflicts, matched)
    if description["tables"]:
        for column in description["columns"]:
            if layouts.normalize_label(column["name"]) not in matched:
                description["tables"][0]["type_hint_warnings"].append(
                    f"unused README recommendation: {column['name']}")


def _map_recommendations(proposal, labels, recommendations, conflicts, matched):
    names = [layouts.normalize_label(label) for label in labels]
    specs, warnings = [], proposal["type_hint_warnings"]
    for label, name in zip(labels, names):
        matched.add(name)
        if name in conflicts:
            specs.append({"type": "VARCHAR"})
            warnings.append(f"{label}: conflicting README types; kept original storage")
        elif name in recommendations:
            specs.append(dict(recommendations[name]))
        elif re.search(r"\b(?:ngày|ngay|thời\s+gian|thoi\s+gian|sinh\s+nhật|sinh\s+nhat|date|datetime|timestamp|birthdate|birthday)\b",
                       name.replace("_", " ")):
            # ponytail: keyword hints only; full-column validation decides conversion, never override explicit types.
            specs.append({"type": "TIMESTAMP"})
            warnings.append(f"{label}: no README recommendation; trying locally validated TIMESTAMP")
        else:
            specs.append({"type": "VARCHAR"})
            warnings.append(f"{label}: no README recommendation; kept original storage")
    proposal["column_types"] = specs


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
                if args.offline or all(file["format"] == ".sqlite" for file in inventory["files"]):
                    description = _offline_description(inventory)
                    raw, layout_by_source = _read_tables(inventory, description)
                else:
                    resolved = {"columns": []}
                    raw, layout_by_source = _read_tables(inventory, resolved, defer_types=True)
                    column_names = [column["label"] for table in resolved["tables"]
                                    for column in table.get("columns", [])
                                    if layouts.normalize_label(column["label"])]
                    description = llm.describe_business(llm.create_client(), inventory, column_names)
                    description["tables"] = resolved["tables"]
                    _apply_recommendations(description, layout_by_source)
                schema_name = business["id"]
                used = set()
                staging = [{"name": f"_stage_{i}", "columns": t["columns"], "destination": _dest(t, used)} for i, t in enumerate(raw)]
                load_plan = sql.build_load_plan(staging, schema_name)
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
