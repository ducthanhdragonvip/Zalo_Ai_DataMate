"""Question validation and required profile-backed business context."""

import json
import re
from datetime import date
from pathlib import Path

from business_profile.profiling import NUMERIC, TEMPORAL


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


def _load_profile(profile_dir, business_id, actual_columns):
    """Validate the current profiler contract before sending any saved fields."""
    def require(condition):
        if not condition:
            raise ValueError('invalid or schema-mismatched profile')

    def name(value):
        return isinstance(value, str) and bool(value.strip()) and '\x00' not in value

    try:
        profile = json.loads((Path(profile_dir) / f'{business_id}.json').read_text(encoding='utf-8'))
        json.dumps(profile, ensure_ascii=False, allow_nan=False).encode('utf-8')
        require(isinstance(profile, dict) and set(profile) == {'business_id', 'tables'})
        require(profile['business_id'] == business_id and isinstance(profile['tables'], list))
        expanded = {}
        for table in profile['tables']:
            require(isinstance(table, dict) and set(table) in ({'name', 'columns'}, {'names', 'columns'}))
            names = [table['name']] if 'name' in table else table['names']
            require(isinstance(names, list) and bool(names) and all(name(n) for n in names))
            require(len(names) == len(set(names)) and not set(names).intersection(expanded))
            require(isinstance(table['columns'], list) and bool(table['columns']))
            signature = []
            for field in table['columns']:
                require(isinstance(field, dict) and {'name', 'type'} <= field.keys())
                require(name(field['name']) and name(field['type']))
                kind = field['type']
                supported = kind in NUMERIC | TEMPORAL | {'VARCHAR', 'BOOLEAN', 'UUID', 'TIME', 'TIME WITH TIME ZONE'} or kind.startswith('DECIMAL(')
                required = {'name', 'type', 'distinct_count'} if supported else {'name', 'type', 'stats_omitted'}
                if kind in {'FLOAT', 'DOUBLE'}:
                    required.add('nonfinite_count')
                optional = {'values', 'values_omitted'} if kind == 'VARCHAR' else set()
                require(required <= field.keys() <= required | optional)
                for key in ('distinct_count', 'nonfinite_count'):
                    if key in field:
                        require(type(field[key]) is int and field[key] >= 0)
                if not supported:
                    require(field['stats_omitted'] == 'unsupported type')
                require(not {'values', 'values_omitted'} <= field.keys())
                if 'values' in field:
                    values = field['values']
                    require(isinstance(values, list) and len(values) <= 32)
                    require(all(isinstance(v, str) and len(v) <= 128 for v in values))
                    require(len(values) == len(set(values)) == field['distinct_count'])
                if 'values_omitted' in field:
                    require(field['values_omitted'] in ('more than 32 labels', 'label exceeds 128 characters'))
                signature.append((field['name'], kind))
            require(len(signature) == len({column for column, _ in signature}))
            for table_name in names:
                expanded[table_name] = signature
        expected = {}
        for table_name, column, kind in actual_columns:
            expected.setdefault(table_name, []).append((column, kind))
        require(expanded == expected)
        return profile
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(
            f'{business_id}: missing, invalid or schema-mismatched business_profile JSON; '
            f'regenerate with python -m business_profile --business {business_id} '
            f'--output-dir "{profile_dir}" (use the same --db as question_sql)'
        ) from exc


def load_catalog(conn, business_id):
    if not isinstance(business_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', business_id):
        raise ValueError('invalid business schema')
    business = conn.execute('SELECT readme, import_status, warnings FROM _catalog.businesses WHERE business_id=?', [business_id]).fetchone()
    if not business or business[1] != 'loaded' or not business[0] or not business[0].strip():
        raise ValueError(f'{business_id}: missing loaded business or README')
    mappings = conn.execute('SELECT table_name, source_file, source_table FROM _catalog.tables WHERE business_id=? ORDER BY table_name', [business_id]).fetchall()
    actual = conn.execute('SELECT table_name, table_type FROM information_schema.tables WHERE table_schema=? ORDER BY table_name', [business_id]).fetchall()
    if not mappings or {m[0] for m in mappings} != {t[0] for t in actual} or len(mappings) != len(actual) or any(t[1] != 'BASE TABLE' for t in actual):
        raise ValueError(f'{business_id}: inconsistent catalog tables or unsupported views')
    columns = conn.execute('SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema=? ORDER BY table_name, ordinal_position', [business_id]).fetchall()
    if {table for table, _, _ in columns} != {table for table, _ in actual}:
        raise ValueError(f'{business_id}: missing columns')
    sources = [{'name': name, 'source_file': source_file, 'source_table': source_table} for name, source_file, source_table in mappings]
    return {'business_id': business_id, 'readme': business[0], 'warnings': business[2], 'sources': sources}, columns


def load_context(conn, business_id, profile_dir):
    context, columns = load_catalog(conn, business_id)
    context['business_profile'] = _load_profile(profile_dir, business_id, columns)
    return context
