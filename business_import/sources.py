import csv
import sqlite3
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import load_workbook


SUPPORTED = {".csv", ".xlsx", ".sqlite"}


def _unique(names):
    seen = Counter()
    result = []
    for index, name in enumerate(names, 1):
        base = str(name).strip() or f"column_{index}"
        seen[base.casefold()] += 1
        candidate = base if seen[base.casefold()] == 1 else f"{base}__{seen[base.casefold()]}"
        while candidate.casefold() in {x.casefold() for x in result}:
            seen[base.casefold()] += 1
            candidate = f"{base}__{seen[base.casefold()]}"
        result.append(candidate)
    return result


def discover_businesses(roots: list[Path]) -> list[dict]:
    result = []
    ids = set()
    for root in roots:
        if not root.exists() or not root.is_dir():
            raise ValueError(f"business data root does not exist or is not a directory: {root}")
        resolved_root = root.resolve()
        split = root.parent.name
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            if directory.is_symlink() or directory.resolve().parent != resolved_root:
                raise ValueError(f"business directory link escapes configured root: {directory}")
            if directory.name in ids:
                raise ValueError(f"duplicate business ID across roots: {directory.name}")
            ids.add(directory.name)
            readme_path = directory / "README.md"
            if readme_path.is_symlink():
                raise ValueError(f"README symlink is not allowed: {readme_path}")
            result.append({"id": directory.name, "split": split, "path": directory,
                           "readme": readme_path.read_text(encoding="utf-8-sig") if readme_path.exists() else "",
                           "files": []})
    return result


def _sqlite_tables(path):
    uri = path.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    try:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        output = []
        for name in names:
            escaped = name.replace('"', '""')
            info = db.execute(f'PRAGMA table_xinfo("{escaped}")').fetchall()
            columns = [{"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": bool(r[5]), "hidden": int(r[6])} for r in info if int(r[6]) in (0, 2, 3)]
            selected = ", ".join('"' + c["name"].replace('"', '""') + '"' for c in columns)
            rows = db.execute(f'SELECT {selected} FROM "{escaped}"')
            observed = [set() for _ in columns]
            count, preview = 0, []
            for row in rows:
                count += 1
                if len(preview) < 3:
                    preview.append([_value(value) for value in row])
                for i, value in enumerate(row):
                    if value is not None:
                        observed[i].add(type(value).__name__)
            for i, col in enumerate(columns):
                col["observed_types"] = sorted(observed[i])
            output.append({"name": name, "columns": columns, "row_count": count, "preview": preview})
        return output
    finally:
        db.close()


def inspect_business(path: Path) -> dict:
    files = []
    resolved_business = path.resolve()
    readme_path = path / "README.md"
    if readme_path.is_symlink():
        raise ValueError(f"README link is not allowed: {readme_path}")
    for file in sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED):
        if file.is_symlink() or file.resolve().parent != resolved_business:
            raise ValueError(f"source file link escapes business directory: {file}")
        info = {"name": file.name, "format": file.suffix.lower(), "tables": []}
        if file.suffix.lower() == ".csv":
            with file.open("r", encoding="utf-8-sig", newline="") as stream:
                sample = stream.read(8192)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","
            with file.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream, delimiter=delimiter, strict=True))
            info["delimiter"] = delimiter
            info["tables"] = [{"name": file.stem, "row_count": len(rows), "max_column": max((len(r) for r in rows), default=0),
                               "preview": [[_value(v) for v in row] for row in rows[:256]]}]
        elif file.suffix.lower() == ".xlsx":
            book = load_workbook(file, read_only=True, data_only=False)
            try:
                for sheet in book.worksheets:
                    preview = []
                    populated = False
                    row_count = 0
                    for row in sheet.iter_rows(values_only=True):
                        row_count += 1
                        values = [_value(v) for v in row]
                        populated = populated or any(v is not None for v in row)
                        if len(preview) < 256:
                            preview.append(values)
                    info["tables"].append({"name": sheet.title, "max_row": sheet.max_row, "max_column": sheet.max_column,
                        "row_count": row_count, "is_empty": not populated, "merged_ranges": [],
                        "formula_cells": None, "excel_epoch": book.epoch.date().isoformat(), "preview": preview})
            finally:
                book.close()
        else:
            info["tables"] = _sqlite_tables(file)
        files.append(info)
    if not files:
        raise ValueError(f"no supported source files found in business directory: {path}")
    return {"id": path.name, "path": path, "readme": readme_path.read_text(encoding="utf-8-sig") if readme_path.exists() else "", "files": files}


def _value(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<blob:{len(value)} bytes>"
    return value


def read_csv_rows(path: Path, delimiter=None, header_row=1, data_start_row=None):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(8192)
        stream.seek(0)
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","
        rows = list(csv.reader(stream, delimiter=delimiter, strict=True))
    if not rows:
        return [], []
    if header_row < 1 or header_row > len(rows):
        raise ValueError(f"header row outside CSV bounds: {header_row}")
    headers = _unique(rows[header_row - 1])
    start = data_start_row or header_row + 1
    if start < header_row + 1:
        raise ValueError("data_start_row must follow header_row")
    result = []
    for row in rows[start - 1:]:
        row = row + [""] * max(0, len(headers) - len(row))
        result.append(row[:len(headers)])
    return result, headers


def read_xlsx_rows(path: Path, sheet_name: str, header_row=1, data_start_row=None):
    book = load_workbook(path, read_only=True, data_only=False)
    try:
        sheet = book[sheet_name]
        rows = [[_value(v) for v in row] for row in sheet.iter_rows(values_only=True)]
    finally:
        book.close()
    if header_row < 1 or header_row > len(rows):
        if not rows and header_row == 1:
            return [], []
        raise ValueError(f"header row outside XLSX bounds: {header_row}")
    headers = _unique(rows[header_row - 1])
    start = data_start_row or header_row + 1
    if start < header_row + 1:
        raise ValueError("data_start_row must follow header_row")
    output = []
    for row in rows[start - 1:]:
        row = row + [None] * max(0, len(headers) - len(row))
        output.append([_value(v) for v in row[:len(headers)]])
    return output, headers


def read_sqlite_rows(path: Path, table: str):
    uri = path.resolve().as_uri() + "?mode=ro"
    escaped = table.replace('"', '""')
    db = sqlite3.connect(uri, uri=True)
    try:
        cursor = db.execute(f'SELECT * FROM "{escaped}"')
        columns = [d[0] for d in cursor.description or []]
        return [list(row) for row in cursor.fetchall()], columns
    finally:
        db.close()
