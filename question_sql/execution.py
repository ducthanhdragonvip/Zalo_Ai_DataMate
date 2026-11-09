"""Isolated DuckDB worker with query time and result-size limits."""

import multiprocessing
import pickle
import time as clock
from pathlib import Path

import duckdb

from business_import.sql import quote_identifier
from .answers import MAX_PAYLOAD
from .context import load_context


def _query_worker(pipe, db_path, business_id, sql, max_rows):
    try:
        with duckdb.connect(db_path, read_only=True) as source:
            context = load_context(source, business_id)
        with duckdb.connect(config={'memory_limit': '256MiB', 'temp_directory': '',
                                    'autoload_known_extensions': 'false', 'autoinstall_known_extensions': 'false'}) as conn:
            # Only trusted, locally constructed copy SQL sees the original database.
            literal = "'" + db_path.replace("'", "''") + "'"
            conn.execute(f'ATTACH {literal} AS source_db (READ_ONLY)')
            schema = quote_identifier(business_id)
            conn.execute(f'CREATE SCHEMA {schema}')
            for table in context['tables']:
                name = quote_identifier(table['name'])
                conn.execute(f'CREATE TABLE {schema}.{name} AS SELECT * FROM source_db.{schema}.{name}')
            conn.execute('DETACH source_db')
            conn.execute('SET enable_external_access=false')
            conn.execute('SET lock_configuration=true')
            statements = conn.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise ValueError('expected exactly one SELECT statement')
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(max_rows + 1)
            if len(rows) > max_rows:
                raise ValueError('result exceeds row limit')
            result = {'columns': [column[0] for column in cursor.description], 'rows': rows}
            payload = pickle.dumps({'result': result}, protocol=pickle.HIGHEST_PROTOCOL)
            if len(payload) > MAX_PAYLOAD:
                raise ValueError('result exceeds 16 MiB payload limit')
            pipe.send_bytes(payload)
    except Exception as exc:
        pipe.send_bytes(pickle.dumps({'error': f'{type(exc).__name__}: {exc}'}))
    finally:
        pipe.close()


def execute_sql(db_path, business_id, sql, *, timeout=60, max_rows=10000):
    if timeout <= 0 or max_rows <= 0:
        raise ValueError('timeout and max_rows must be positive')
    ctx = multiprocessing.get_context('spawn')
    reader, writer = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_query_worker, args=(writer, str(Path(db_path).resolve()), business_id, sql, max_rows))
    deadline = clock.monotonic() + timeout
    try:
        worker.start()
        writer.close()
        # Drain the pipe before joining: a large result otherwise blocks the child.
        while not reader.poll(min(0.05, max(0, deadline - clock.monotonic()))):
            if clock.monotonic() >= deadline:
                raise ValueError('query worker timed out')
            if not worker.is_alive():
                raise ValueError(f'query worker exited without result ({worker.exitcode})')
        try:
            message = pickle.loads(reader.recv_bytes(MAX_PAYLOAD))
        except (EOFError, OSError) as exc:
            raise ValueError('query worker returned no valid bounded result') from exc
        if 'error' in message:
            raise ValueError(message['error'])
        return message['result']
    finally:
        reader.close()
        writer.close()
        if worker.pid is not None:
            worker.join(timeout=0.2)
            if worker.is_alive():
                worker.terminate()
                worker.join()
