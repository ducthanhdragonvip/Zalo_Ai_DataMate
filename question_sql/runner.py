"""Sequential JSONL batch orchestration."""

import json
import os
from pathlib import Path

import duckdb

from business_import.llm import create_client
from .answers import MAX_PAYLOAD, diagnostic, dumps, format_answer
from .context import load_context, validate_question
from .execution import execute_sql
from .llm import generate_sql


def run(input_path, output_path, db_path, *, details_path=None, limit=None, timeout=60, max_rows=10000):
    if (limit is not None and limit <= 0) or timeout <= 0 or max_rows <= 0:
        raise ValueError('limits must be positive')
    input_path, output_path, db_path = (Path(path).resolve() for path in (input_path, output_path, db_path))
    details_path = Path(details_path).resolve() if details_path else output_path.with_name(output_path.stem + '.details.jsonl')
    paths = [input_path, output_path, db_path, details_path]
    if len(set(os.path.normcase(str(path)) for path in paths)) != len(paths):
        raise ValueError('input, database, answers and details paths must differ')
    for path in (output_path, details_path):
        if path.exists():
            raise FileExistsError(f'refusing to overwrite {path}')
    if not db_path.is_file():
        raise ValueError(f'database not found: {db_path}')
    # Check DB access before creating output. Never stop another process to release a lock.
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error as exc:
        raise ValueError('cannot open database read-only; close DBeaver/other database connections: ' + str(exc)) from exc
    errors, contexts = 0, {}
    client = None
    try:
        with input_path.open(encoding='utf-8') as source, output_path.open('x', encoding='utf-8') as answers, details_path.open('x', encoding='utf-8') as details:
            for line_number, line in enumerate(source, 1):
                if limit is not None and line_number > limit:
                    break
                info = {'input': None, 'line_number': line_number, 'sql': None, 'type': None,
                        'columns': [], 'rows': [], 'status': 'error', 'error': None}
                answer, stage = None, 'input'
                try:
                    record = json.loads(line)
                    info['input'] = record
                    validate_question(record)
                    stage = 'context'
                    business_id = record['business_id']
                    if business_id not in contexts:
                        contexts[business_id] = load_context(conn, business_id)
                    stage = 'model'
                    if client is None:
                        client = create_client().with_options(max_retries=0)
                    generation = generate_sql(client, record, contexts[business_id])
                    info.update(generation)
                    if generation['type'] not in ('unanswerable', 'abstain'):
                        stage = 'execution'
                        info.update(execute_sql(db_path, business_id, generation['sql'], timeout=timeout, max_rows=max_rows))
                    stage = 'serialization'
                    answer = format_answer(record['id'], generation, info['columns'], info['rows'])
                    info['status'] = generation['type'] if generation['type'] in ('unanswerable', 'abstain') else 'ok'
                except Exception as exc:
                    errors += 1
                    info['error'] = {'stage': stage, 'message': f'{type(exc).__name__}: {exc}'}
                    if info['input'] is None:
                        info['raw_line'] = line.rstrip('\r\n')
                # Output errors must abort, not be swallowed as a question failure.
                detail_line = dumps(diagnostic(info))
                if len(detail_line.encode('utf-8')) > MAX_PAYLOAD:
                    errors += 1
                    answer = None
                    info.update(rows=[], status='error', error={'stage': 'serialization', 'message': 'details exceed 16 MiB payload limit'})
                    detail_line = dumps(diagnostic(info))
                details.write(detail_line + '\n')
                details.flush()
                if answer is not None:
                    answer_line = dumps(answer)
                    if len(answer_line.encode('utf-8')) > MAX_PAYLOAD:
                        raise ValueError('answer exceeds 16 MiB output limit')
                    answers.write(answer_line + '\n')
                    answers.flush()
    finally:
        conn.close()
        if client is not None:
            close = getattr(client, 'close', None)
            if close:
                close()
    return int(errors > 0)
