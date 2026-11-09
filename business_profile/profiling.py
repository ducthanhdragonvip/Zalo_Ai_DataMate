"""Local aggregate statistics; no source records or model calls."""

import json
import math
import os
import re
import tempfile
from pathlib import Path
import threading
import time
import unicodedata

from business_import.sql import quote_identifier


CATEGORIES = {
    'ket qua', 'trang thai', 'trang thai don hang', 'trang thai xu ly',
    'status', 'order status', 'category', 'product category', 'danh muc',
    'nhom hang', 'nhom san pham', 'unit', 'unit of measure', 'don vi tinh',
    'payment method', 'phuong thuc thanh toan', 'hinh thuc thanh toan',
}
NUMERIC = {
    'TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT', 'UTINYINT',
    'USMALLINT', 'UINTEGER', 'UBIGINT', 'UHUGEINT', 'FLOAT', 'DOUBLE',
}
TEMPORAL = {'DATE', 'TIMESTAMP', 'TIMESTAMP_S', 'TIMESTAMP_MS', 'TIMESTAMP_NS', 'TIMESTAMP WITH TIME ZONE'}
SENSITIVE = re.compile(
    r'\b(?:id|code|identifier|customer|khach hang|phone|telephone|dien thoai|sdt|email|'
    r'address|dia chi|birth|birthday|birthdate|sinh nhat|ngay sinh)\b|^ma\b'
)


def _business_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', value):
        raise ValueError('invalid business ID')
    return value


def _normalized(name):
    value = ''.join(c for c in unicodedata.normalize('NFD', name.casefold()) if not unicodedata.combining(c))
    return ' '.join(value.replace('đ', 'd').replace('_', ' ').split())


def build_profile(conn, business_id, *, timeout=60):
    """Profile one catalog-owned business from a consistent read transaction."""
    _business_id(business_id)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')
    deadline = time.monotonic() + timeout
    expired = threading.Event()

    def interrupt():
        expired.set()
        conn.interrupt()

    timer = threading.Timer(timeout, interrupt)
    timer.daemon = True
    conn.execute('BEGIN TRANSACTION')
    timer.start()
    try:
        business = conn.execute('SELECT import_status FROM _catalog.businesses WHERE business_id=?', [business_id]).fetchall()
        if business != [('loaded',)]:
            raise ValueError(f'{business_id}: missing loaded business')
        mappings = conn.execute('SELECT table_name FROM _catalog.tables WHERE business_id=? ORDER BY table_name', [business_id]).fetchall()
        actual = conn.execute('SELECT table_name, table_type FROM information_schema.tables WHERE table_schema=? ORDER BY table_name', [business_id]).fetchall()
        if not mappings or len(mappings) != len(actual) or {m[0] for m in mappings} != {t[0] for t in actual} or any(t[1] != 'BASE TABLE' for t in actual):
            raise ValueError(f'{business_id}: inconsistent catalog tables or unsupported views')
        columns = conn.execute('SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema=? ORDER BY table_name, ordinal_position', [business_id]).fetchall()
        groups = {}
        for (name,) in mappings:
            signature = tuple((column, kind) for source_table, column, kind in columns if source_table == name)
            if not signature:
                raise ValueError(f'{business_id}.{name}: missing columns')
            groups.setdefault(signature, []).append(name)
        tables = []
        for signature, names in groups.items():
            sources = [f'SELECT * FROM {quote_identifier(business_id)}.{quote_identifier(name)}' for name in names]
            table = '(' + ' UNION ALL '.join(sources) + ') AS combined'
            definitions, fields, domains = [], [], []
            for column, kind in signature:
                field = {'name': column, 'type': kind}
                expressions = {}
                quoted = quote_identifier(column)
                numeric = kind in NUMERIC or kind.startswith('DECIMAL(')
                supported = numeric or kind in TEMPORAL or kind in {'VARCHAR', 'BOOLEAN', 'UUID', 'TIME', 'TIME WITH TIME ZONE'}
                if supported:
                    expressions['distinct_count'] = f'count(DISTINCT {quoted})'
                else:
                    field['stats_omitted'] = 'unsupported type'
                normalized = _normalized(column)
                sensitive = bool(SENSITIVE.search(normalized))
                if kind == 'VARCHAR' and normalized in CATEGORIES and not sensitive:
                    domains.append(field)
                if kind in {'FLOAT', 'DOUBLE'}:
                    expressions['nonfinite_count'] = f'count(*) FILTER (WHERE NOT isfinite({quoted}))'
                fields.append((field, list(expressions)))
                definitions.extend(expressions.values())
            row = conn.execute(f'SELECT {", ".join(definitions)} FROM {table}').fetchone() if definitions else ()
            position = 0
            for field, keys in fields:
                for key, value in zip(keys, row[position:position + len(keys)]):
                    field[key] = value
                position += len(keys)
            for field in domains:
                quoted = quote_identifier(field['name'])
                # ponytail: bounded exact category domains only; add names when explicitly approved.
                values = conn.execute(f'SELECT DISTINCT {quoted} FROM {table} WHERE {quoted} IS NOT NULL ORDER BY 1 LIMIT 33').fetchall()
                if len(values) > 32:
                    field['values_omitted'] = 'more than 32 labels'
                elif any(len(value[0]) > 128 for value in values):
                    field['values_omitted'] = 'label exceeds 128 characters'
                else:
                    field['values'] = [value[0] for value in values]
            identity = {'name': names[0]} if len(names) == 1 else {'names': names}
            tables.append({**identity, 'columns': [field for field, _ in fields]})
        timer.cancel()
        timer.join()
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError(f'{business_id}: profiling timed out')
        conn.execute('COMMIT')
        return {'business_id': business_id, 'tables': tables}
    except BaseException:
        timer.cancel()
        timer.join()
        conn.execute('ROLLBACK')
        raise


def _valid_profile(profile):
    if not isinstance(profile, dict) or set(profile) != {'business_id', 'tables'} or not isinstance(profile['tables'], list):
        return False
    for table in profile['tables']:
        if not isinstance(table, dict) or set(table) - {'row_count'} not in ({'name', 'columns'}, {'names', 'columns'}):
            return False
        names = [table['name']] if 'name' in table else table['names']
        if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name for name in names):
            return False
        # Accept legacy row counts so existing generated files can be refreshed.
        if 'row_count' in table and (type(table['row_count']) is not int or table['row_count'] < 0):
            return False
        if len(set(names)) != len(names) or not isinstance(table['columns'], list):
            return False
        for field in table['columns']:
            if not isinstance(field, dict) or not {'name', 'type'} <= field.keys():
                return False
            if not isinstance(field['name'], str) or not isinstance(field['type'], str):
                return False
            for key in ('null_count', 'empty_string_count', 'distinct_count', 'nonfinite_count'):
                if key in field and (type(field[key]) is not int or field[key] < 0):
                    return False
            if 'row_count' in table and field.get('null_count', 0) > table['row_count']:
                return False
    return True


def write_profile(profile, output_dir):
    """Atomically replace only a matching business-profile JSON file."""
    if not _valid_profile(profile):
        raise ValueError('invalid business profile')
    business_id = _business_id(profile['business_id'])
    content = json.dumps(profile, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f'{business_id}.json'
    if path.is_symlink():
        raise ValueError(f'refusing symlink output: {path}')
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f'refusing unrelated output: {path}') from exc
        if not _valid_profile(existing) or existing['business_id'] != business_id:
            raise ValueError(f'refusing unrelated output: {path}')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=output_dir, prefix=f'.{business_id}.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
