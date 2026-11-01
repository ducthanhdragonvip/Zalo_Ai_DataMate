import re


def quote_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid SQL identifier")
    return '"' + value.replace('"', '""') + '"'


def build_load_plan(staging: list[dict], schema: str) -> dict:
    used = set()
    tables = []
    for item in staging:
        source = item["name"]
        base = item.get("destination") or re.sub(r"[^\w]+", "_", source, flags=re.UNICODE).strip("_") or "table"
        destination = base
        suffix = 2
        while destination.casefold() in used:
            destination = f"{base}__{suffix}"
            suffix += 1
        used.add(destination.casefold())
        columns = list(item["columns"])
        sql = f"CREATE TABLE {quote_identifier(schema)}.{quote_identifier(destination)} AS SELECT " + ", ".join(quote_identifier(c) for c in columns) + f" FROM {quote_identifier(source)}"
        tables.append({"source": source, "destination": destination, "columns": columns, "sql": sql})
    return {"tables": tables}


def validate_load_plan(plan: dict, staging: list[dict], schema: str) -> dict:
    if not isinstance(plan, dict) or not isinstance(plan.get("tables"), list):
        raise ValueError("load plan must contain a tables list")
    expected_sources = {item["name"]: item for item in staging}
    if len(plan["tables"]) != len(expected_sources):
        raise ValueError("load plan must include every staging table exactly once")
    normalized = []
    seen_sources = set()
    seen_destinations = set()
    for proposed in plan["tables"]:
        if not isinstance(proposed, dict):
            raise ValueError("invalid table mapping")
        source = proposed.get("source")
        destination = proposed.get("destination")
        repaired_source = False
        if source not in expected_sources and isinstance(source, str) and source == destination:
            # Repair the model's common mistake of echoing the qualified final
            # table as both source and destination, but only for a unique mapping.
            matches = [name for name, item in expected_sources.items()
                       if source == f"{schema}.{item.get('destination')}"]
            if len(matches) == 1:
                source = matches[0]
                destination = expected_sources[source]["destination"]
                repaired_source = True
        if source not in expected_sources or source in seen_sources:
            raise ValueError("unknown or duplicate staging source")
        seen_sources.add(source)
        if destination != expected_sources[source].get("destination"):
            raise ValueError(f"destination does not match approved source mapping for {source}")

        if not isinstance(destination, str) or not destination.strip() or destination.startswith("_"):
            raise ValueError("invalid destination table name")
        if destination.casefold() in seen_destinations:
            raise ValueError("duplicate destination table name")
        seen_destinations.add(destination.casefold())
        columns = proposed.get("columns")
        if columns != expected_sources[source]["columns"]:
            raise ValueError("destination columns must exactly preserve the staging columns")
        expected_sql = f"CREATE TABLE {quote_identifier(schema)}.{quote_identifier(destination)} AS SELECT " + ", ".join(quote_identifier(c) for c in columns) + f" FROM {quote_identifier(source)}"
        if not repaired_source and proposed.get("sql") != expected_sql:
            raise ValueError("generated SQL is not the exact source-preserving canonical statement")
        normalized.append({"source": source, "destination": destination, "columns": columns, "sql": expected_sql})
    if seen_sources != set(expected_sources):
        raise ValueError("load plan omits staging tables")
    return {"tables": normalized}
