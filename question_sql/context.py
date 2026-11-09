"""Question validation and imported business metadata."""

import json
import re
from datetime import date
import unicodedata

from business_import.sql import quote_identifier


def validate_question(record):
    if not isinstance(record, dict):
        raise ValueError('question must be an object')
    try:
        json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (UnicodeError, ValueError) as exc:
        raise ValueError('question must contain valid Unicode and finite JSON values') from exc
    for key in ('id', 'business_id', 'question', 'as_of_date'):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f'{key} must be a nonempty string')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', record['business_id']):
        raise ValueError('invalid business schema')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', record['as_of_date']):
        raise ValueError('as_of_date must be YYYY-MM-DD')
    date.fromisoformat(record['as_of_date'])
    return record


def load_context(conn, business_id):
    business = conn.execute('SELECT readme, import_status, warnings FROM _catalog.businesses WHERE business_id=?', [business_id]).fetchone()
    if not business or business[1] != 'loaded' or not business[0] or not business[0].strip():
        raise ValueError(f'{business_id}: missing loaded business or README')
    mappings = conn.execute('SELECT table_name, source_file, source_table FROM _catalog.tables WHERE business_id=? ORDER BY table_name', [business_id]).fetchall()
    actual = conn.execute('SELECT table_name, table_type FROM information_schema.tables WHERE table_schema=? ORDER BY table_name', [business_id]).fetchall()
    if not mappings or {m[0] for m in mappings} != {t[0] for t in actual} or len(mappings) != len(actual) or any(t[1] != 'BASE TABLE' for t in actual):
        raise ValueError(f'{business_id}: inconsistent catalog tables or unsupported views')
    columns = conn.execute('SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema=? ORDER BY table_name, ordinal_position', [business_id]).fetchall()
    tables = []
    for name, source_file, source_table in mappings:
        fields = [{'name': column, 'type': kind} for table, column, kind in columns if table == name]
        if not fields:
            raise ValueError(f'{business_id}.{name}: missing columns')
        for field in fields:
            normalized = ''.join(c for c in unicodedata.normalize('NFD', field['name'].casefold()) if not unicodedata.combining(c))
            normalized = ' '.join(normalized.replace('đ', 'd').replace('_', ' ').split())
            if field['type'] != 'VARCHAR' or normalized not in {
                'ket qua', 'trang thai', 'trang thai don hang', 'trang thai xu ly',
                'status', 'order status',
            }:
                continue
            column = quote_identifier(field['name'])
            # ponytail: only small status enums; extend the allowlist for documented status columns.
            values = conn.execute(
                f'SELECT DISTINCT {column} FROM {quote_identifier(business_id)}.{quote_identifier(name)} '
                f'WHERE {column} IS NOT NULL ORDER BY 1 LIMIT 33'
            ).fetchall()
            if len(values) > 32 or any(len(v[0]) > 128 for v in values):
                field['values_omitted'] = True
            else:
                field['values'] = [v[0] for v in values]
        tables.append({'name': name, 'source_file': source_file, 'source_table': source_table, 'columns': fields})
    return {'business_id': business_id, 'readme': business[0], 'warnings': business[2], 'tables': tables}
