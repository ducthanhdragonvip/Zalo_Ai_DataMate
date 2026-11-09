"""Reference answer formatting and lossless JSON/diagnostic serialization."""

import json
import math
import re
from datetime import date, datetime, time
from decimal import Decimal


MAX_PAYLOAD = 16 * 1024 * 1024


def dumps(value):
    """Encode Decimal as a JSON number without a binary-float round trip."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('non-finite decimal')
        return str(int(value)) if value == value.to_integral_value() else str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(dumps(item) for item in value) + ']'
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return '{' + ','.join(dumps(key) + ':' + dumps(item) for key, item in value.items()) + '}'
    raise ValueError(f'unsupported JSON value: {type(value).__name__}')


def format_answer(question_id, generation, columns, rows):
    kind = generation['type']
    if kind in ('unanswerable', 'abstain'):
        return {'id': question_id, 'type': kind}
    if kind == 'table':
        answer = {'columns': columns, 'rows': rows}
    elif kind == 'list':
        if len(columns) != 1 or any(len(row) != 1 for row in rows):
            raise ValueError('list requires one result column')
        answer = [row[0] for row in rows]
    else:
        if len(columns) != 1 or len(rows) != 1 or len(rows[0]) != 1:
            raise ValueError('scalar answer requires one row and column')
        answer = rows[0][0]
        if kind in ('number', 'percent'):
            if isinstance(answer, bool) or not isinstance(answer, (int, float, Decimal)):
                raise ValueError('numeric answer required')
        elif kind == 'boolean':
            if not isinstance(answer, bool):
                raise ValueError('boolean answer required')
        elif kind == 'string':
            if not isinstance(answer, str):
                raise ValueError('text answer required')
        elif kind == 'date':
            if isinstance(answer, date) and not isinstance(answer, datetime):
                answer = answer.isoformat()
            if not isinstance(answer, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', answer):
                raise ValueError('date answer must be YYYY-MM-DD')
            date.fromisoformat(answer)
        else:
            raise ValueError('unsupported answer type')
    # Native dates in tables/lists are still calendar strings, not arbitrary repr().
    def dates(value):
        if isinstance(value, (date, time)):
            return value.isoformat()
        if isinstance(value, (list, tuple)):
            return [dates(item) for item in value]
        if isinstance(value, dict):
            return {key: dates(item) for key, item in value.items()}
        return value
    result = {'id': question_id, 'type': kind, 'answer': dates(answer)}
    dumps(result)
    return result


def diagnostic(value):
    if isinstance(value, str):
        # Keep invalid escaped Unicode representable in an input-error record.
        return value.encode('utf-8', errors='backslashreplace').decode('utf-8')
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {'hex': value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return {'nonfinite': str(value)}
    if isinstance(value, (list, tuple)):
        return [diagnostic(item) for item in value]
    if isinstance(value, dict):
        return {diagnostic(str(key)): diagnostic(item) for key, item in value.items()}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {'unsupported_type': type(value).__name__, 'representation': diagnostic(str(value))}
