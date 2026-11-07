import json
import re
from datetime import datetime, timezone

from .sql import quote_identifier, validate_load_plan
from .column_types import convert_columns, validate_specs


def _schema(name):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) or name.casefold() == "_catalog":
        raise ValueError(f"invalid or reserved business schema: {name}")
    return name


def _ensure_catalog(conn):
    conn.execute('CREATE SCHEMA IF NOT EXISTS "_catalog"')
    conn.execute('''CREATE TABLE IF NOT EXISTS "_catalog".businesses (
      business_id VARCHAR PRIMARY KEY, split VARCHAR, readme VARCHAR,
      description_json VARCHAR, import_status VARCHAR, imported_at VARCHAR, warnings VARCHAR)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS "_catalog".tables (
      business_id VARCHAR, source_file VARCHAR, source_table VARCHAR, table_name VARCHAR,
      row_count BIGINT, column_mapping VARCHAR, preamble VARCHAR, load_sql VARCHAR,
      PRIMARY KEY (business_id, source_file, source_table))''')


def _sqlite_type(declared, observed=()):
    declared = (declared or "").upper()
    types = set(observed)
    if len(types) > 1:
        return "VARCHAR"
    if types == {"int"} or "INT" in declared:
        return "BIGINT"
    if types == {"float"} or any(token in declared for token in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE"
    if types == {"bytes"} or "BLOB" in declared:
        return "BLOB"
    return "VARCHAR"


def _sqlite_value(value, observed):
    if len(set(observed)) > 1 and value is not None:
        return json.dumps({"$type": type(value).__name__, "value": value.hex() if isinstance(value, bytes) else value}, ensure_ascii=False, separators=(",", ":"))
    return value


def _table_name(source_file, table, fmt, used):
    base = source_file.rsplit(".", 1)[0]
    if fmt in (".xlsx", ".sqlite"):
        base += "__" + table["name"]
    base = re.sub(r"[^\w]+", "_", base, flags=re.UNICODE).strip("_") or "table"
    candidate, n = base, 2
    while candidate.casefold() in used:
        candidate, n = f"{base}__{n}", n + 1
    used.add(candidate.casefold())
    return candidate


def load_business(conn, business, description, raw_tables, layout_by_source, load_plan):
    """Load one business atomically. raw_tables: [{source_file, source_table, format, columns, rows}]."""
    schema = _schema(business["id"])
    tables, used = [], set()
    descriptions = {(t["source_file"], t["source_table"]): t for t in description.get("tables", [])}
    warnings = [f"{file} / {table}: excluded {layout['repeated_header_count']} repeated header rows"
                for (file, table), layout in layout_by_source.items() if layout.get("repeated_header_count")]
    for raw in raw_tables:
        name = _table_name(raw["source_file"], {"name": raw["source_table"]}, raw["format"], used)
        tables.append({**raw, "stage": f"_stage_{len(tables)}", "destination": name})
    stage_spec = [{"name": t["stage"], "columns": t["columns"], "destination": t["destination"]} for t in tables]
    accepted_plan = validate_load_plan(load_plan, stage_spec, schema)
    conn.execute("BEGIN TRANSACTION")
    try:
        schemata = {row[0].casefold() for row in conn.execute("SELECT schema_name FROM information_schema.schemata").fetchall()}
        owned_schema = conn.execute('SELECT 1 FROM information_schema.tables WHERE table_schema=? AND table_name=?', ["_catalog", "businesses"]).fetchone()
        if schema.casefold() in schemata:
            owned = conn.execute('SELECT 1 FROM "_catalog".businesses WHERE business_id=?', [business["id"]]).fetchone() if owned_schema else None
            if not owned:
                raise ValueError(f"business schema {schema} exists and is not importer-owned")
        _ensure_catalog(conn)
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(schema)}")
        for table in tables:
            source_types = table.get("column_types", [])
            default_types = [_sqlite_type(table.get("declared_types", [""] * len(table["columns"]))[i], source_types[i])
                             if table["format"] == ".sqlite" and i < len(source_types) else "VARCHAR"
                             for i in range(len(table["columns"]))]
            proposal = descriptions.get((table["source_file"], table["source_table"]), {})
            specs = proposal.get("column_types")
            protected_warnings = list(proposal.get("type_hint_warnings", []))
            if specs is not None:
                validate_specs(specs, len(table["columns"]))
                specs = [dict(spec) for spec in specs]
                for i, observed in enumerate(source_types):
                    if len(observed) > 1 and specs[i]["type"] != "VARCHAR":
                        specs[i] = {"type": "VARCHAR"}
                        protected_warnings.append(f"{table['columns'][i]}: kept VARCHAR; mixed SQLite storage types")
            actual_types, values, type_warnings = convert_columns(table["columns"], table["rows"], specs, default_types,
                                                                 excel_epoch=table.get("excel_epoch") if table["format"] == ".xlsx" else None)
            type_warnings = protected_warnings + type_warnings
            proposal["actual_types"] = actual_types
            proposal["type_warnings"] = type_warnings
            warnings.extend(f"{table['source_file']} / {table['source_table']}: {warning}" for warning in type_warnings)
            definitions = ", ".join(quote_identifier(c) + " " + kind for c, kind in zip(table["columns"], actual_types))
            conn.execute(f"CREATE OR REPLACE TEMP TABLE {quote_identifier(table['stage'])} ({definitions})")
            if table["rows"]:
                placeholders = ",".join("?" for _ in table["columns"])
                source_types = table.get("column_types", [])
                conn.executemany(f"INSERT INTO {quote_identifier(table['stage'])} VALUES ({placeholders})", [
                    [_sqlite_value(value, source_types[i]) if table["format"] == ".sqlite" and i < len(source_types) else value for i, value in enumerate(row)]
                    for row in values])

        prior = conn.execute('SELECT table_name FROM "_catalog".tables WHERE business_id=?', [business["id"]]).fetchall()
        current_names = {entry["destination"].casefold() for entry in accepted_plan["tables"]}
        for (old_name,) in prior:
            if old_name.casefold() not in current_names:
                conn.execute(f"DROP TABLE IF EXISTS {quote_identifier(schema)}.{quote_identifier(old_name)}")
        for entry in accepted_plan["tables"]:
            dest = quote_identifier(entry["destination"])
            exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=? AND table_name=?", [schema, entry["destination"]]).fetchone()
            if exists:
                owned = conn.execute('SELECT 1 FROM "_catalog".tables WHERE business_id=? AND table_name=?', [business["id"], entry["destination"]]).fetchone()
                if not owned:
                    raise ValueError(f"destination {schema}.{entry['destination']} already exists and is not importer-owned")
                conn.execute(f"DROP TABLE {quote_identifier(schema)}.{dest}")
            conn.execute(entry["sql"])
            source_count = conn.execute(f"SELECT count(*) FROM {quote_identifier(entry['source'])}").fetchone()[0]
            target_count = conn.execute(f"SELECT count(*) FROM {quote_identifier(schema)}.{dest}").fetchone()[0]
            if source_count != target_count:
                raise RuntimeError(f"row count mismatch for {entry['destination']}: {source_count} != {target_count}")

        conn.execute('DELETE FROM "_catalog".tables WHERE business_id=?', [business["id"]])
        conn.execute('DELETE FROM "_catalog".businesses WHERE business_id=?', [business["id"]])
        conn.execute('INSERT INTO "_catalog".businesses VALUES (?, ?, ?, ?, ?, ?, ?)', [business["id"], business["split"], business.get("readme", ""),
            json.dumps(description, ensure_ascii=False, default=str), "loaded", datetime.now(timezone.utc).isoformat(), json.dumps(warnings, ensure_ascii=False)])
        tables_by_stage = {table["stage"]: table for table in tables}
        for entry in accepted_plan["tables"]:
            table = tables_by_stage[entry["source"]]
            count = conn.execute(f"SELECT count(*) FROM {quote_identifier(table['stage'])}").fetchone()[0]
            layout = layout_by_source.get((table["source_file"], table["source_table"]), {})
            mapping = layout.get("column_mapping", list(zip(table["columns"], table["columns"])))
            conn.execute('INSERT INTO "_catalog".tables VALUES (?, ?, ?, ?, ?, ?, ?, ?)', [business["id"], table["source_file"], table["source_table"], entry["destination"], count,
                json.dumps(mapping, ensure_ascii=False), json.dumps(layout.get("preamble", []), ensure_ascii=False, default=str), entry["sql"]])
        conn.execute("COMMIT")
        return {"business_id": business["id"], "tables": len(tables), "rows": sum(len(t["rows"]) for t in tables), "warnings": warnings}
    except Exception:
        conn.execute("ROLLBACK")
        raise
