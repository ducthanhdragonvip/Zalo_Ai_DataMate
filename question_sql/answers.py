"""Reference answer formatting and lossless JSON/diagnostic serialization."""

import json
import math
import re
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP, localcontext


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
        reason = 'Required data is unavailable.' if kind == 'unanswerable' else 'Cannot determine a reliable answer.'
        return {'id': question_id, 'type': 'unanswerable', 'reason': reason}
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
        if kind in ('string', 'date') and isinstance(answer, datetime):
            kind, answer = 'date', answer.date().isoformat()
        if kind in ('number', 'percent'):
            if isinstance(answer, bool) or not isinstance(answer, (int, float, Decimal)):
                raise ValueError('numeric answer required')
            numeric = answer if isinstance(answer, Decimal) else Decimal(str(answer))
            if not numeric.is_finite():
                raise ValueError('finite numeric answer required')
            if kind == 'number':
                answer = int(numeric.to_integral_value(rounding=ROUND_HALF_UP))
            else:
                with localcontext() as context:
                    context.prec = max(context.prec, numeric.adjusted() + 4)
                    answer = numeric.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
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
    # Answer timestamps use their returned calendar date; diagnostics retain the full value.
    def dates(value):
        if isinstance(value, datetime):
            return value.date().isoformat()
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
