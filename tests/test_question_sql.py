import json
import tempfile
import unittest
from pathlib import Path

import duckdb


QUESTION = {"id": "q1", "business_id": "biz_1", "question": "Doanh thu hôm nay?", "as_of_date": "2026-09-07"}


def fixture(path):
    with duckdb.connect(str(path)) as conn:
        conn.execute('CREATE SCHEMA _catalog; CREATE SCHEMA biz_1; CREATE SCHEMA biz_2')
        conn.execute('CREATE TABLE _catalog.businesses (business_id VARCHAR, readme VARCHAR, import_status VARCHAR, warnings VARCHAR)')
        conn.execute('CREATE TABLE _catalog.tables (business_id VARCHAR, source_file VARCHAR, source_table VARCHAR, table_name VARCHAR)')
        for business, note in [('biz_1', 'Đơn hàng chia theo tháng; không cộng lặp tổng đơn.'), ('biz_2', 'Other business')]:
            conn.execute('INSERT INTO _catalog.businesses VALUES (?, ?, ?, ?)', [business, note, 'loaded', '[]'])
            conn.execute(f'CREATE TABLE {business}.sales ("Ngày" DATE, "Tiền" DECIMAL(18,2))')
            conn.execute(f"INSERT INTO {business}.sales VALUES ('2026-09-07', 12.34), ('2026-09-08', 20)")
            conn.execute('INSERT INTO _catalog.tables VALUES (?, ?, ?, ?)', [business, 'sales.xlsx', 'T9', 'sales'])
        conn.execute('CREATE TABLE biz_1.products (code VARCHAR)')
        conn.execute("INSERT INTO _catalog.tables VALUES ('biz_1', 'products.csv', 'products', 'products')")


class ContextTests(unittest.TestCase):
    def test_validate_question_rejects_invalid_fields(self):
        from question_sql.context import validate_question
        self.assertEqual(validate_question(QUESTION), QUESTION)
        for changes in ({'id': ''}, {'question': ' '}, {'business_id': '_catalog'}, {'business_id': 'x;drop'}, {'as_of_date': '2026-02-30'}, {'as_of_date': '20260907'}, {'as_of_date': None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_question({**QUESTION, **changes})
        with self.assertRaises(ValueError):
            validate_question([])

    def test_context_exposes_only_bounded_status_labels(self):
        from question_sql.context import load_context
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path)
            with duckdb.connect(str(path)) as conn:
                conn.execute('ALTER TABLE biz_1.sales ADD COLUMN "Kết quả" VARCHAR')
                conn.execute('ALTER TABLE biz_1.sales ADD COLUMN "Tên khách hàng" VARCHAR')
                conn.execute('UPDATE biz_1.sales SET "Kết quả" = \'Xong\', "Tên khách hàng" = \'PRIVATE\'')
                fields = load_context(conn, 'biz_1')['tables'][1]['columns']
                self.assertEqual(fields[2]['values'], ['Xong'])
                self.assertNotIn('values', fields[3])
                conn.execute('ALTER TABLE biz_1.sales RENAME COLUMN "Kết quả" TO "Trạng thái đơn hàng"')
                fields = load_context(conn, 'biz_1')['tables'][1]['columns']
                self.assertEqual(fields[2]['values'], ['Xong'])
                conn.execute('ALTER TABLE biz_1.sales RENAME COLUMN "Trạng thái đơn hàng" TO "Kết quả"')
                conn.execute('DELETE FROM biz_1.sales')
                conn.execute('INSERT INTO biz_1.sales ("Kết quả") SELECT i::VARCHAR FROM range(33) t(i)')
                field = load_context(conn, 'biz_1')['tables'][1]['columns'][2]
                self.assertNotIn('values', field)
                self.assertTrue(field['values_omitted'])

    def test_context_contains_all_actual_columns_and_provenance(self):
        from question_sql.context import load_context
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path)
            with duckdb.connect(str(path), read_only=True) as conn:
                context = load_context(conn, 'biz_1')
                self.assertIn('không cộng lặp', context['readme'])
                self.assertEqual([t['name'] for t in context['tables']], ['products', 'sales'])
                sales = context['tables'][1]
                self.assertEqual(sales['columns'], [{'name': 'Ngày', 'type': 'DATE'}, {'name': 'Tiền', 'type': 'DECIMAL(18,2)'}])
                self.assertEqual(sales['source_file'], 'sales.xlsx')
                self.assertEqual(sales['source_table'], 'T9')
                with self.assertRaises(ValueError):
                    load_context(conn, 'missing')
            with duckdb.connect(str(path)) as conn:
                conn.execute('CREATE TABLE biz_1.unmapped (n INT)')
                with self.assertRaises(ValueError):
                    load_context(conn, 'biz_1')
                conn.execute('DROP TABLE biz_1.unmapped')
                conn.execute("UPDATE _catalog.businesses SET readme='' WHERE business_id='biz_1'")
                with self.assertRaises(ValueError):
                    load_context(conn, 'biz_1')


class GenerationTests(unittest.TestCase):
    def test_one_request_and_full_context(self):
        from types import SimpleNamespace
        from question_sql.llm import generate_sql
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"type":"number","sql":"SELECT 1"}'))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        context = {'readme': 'Đơn hàng', 'tables': [
            {'name': 'sales', 'columns': [{'name': 'Ngày', 'type': 'DATE'}, {'name': 'Tiền', 'type': 'DECIMAL(18,2)'}]},
            {'name': 'products', 'columns': [{'name': 'Mã', 'type': 'VARCHAR'}]},
        ]}
        self.assertEqual(generate_sql(client, QUESTION, context), {'type': 'number', 'sql': 'SELECT 1'})
        self.assertEqual(len(calls), 1)
        payload = json.loads(calls[0]['messages'][1]['content'])
        self.assertEqual(payload['context'], context)
        self.assertEqual(payload['database_schema'], 'CREATE TABLE "biz_1"."sales" (\n  "Ngày" DATE,\n  "Tiền" DECIMAL(18,2)\n);\n\nCREATE TABLE "biz_1"."products" (\n  "Mã" VARCHAR\n);')
        self.assertEqual(payload['as_of_date'], '2026-09-07')
        self.assertEqual(payload['date_boundaries'], {
            'yesterday': {'start': '2026-09-06', 'exclusive_end': '2026-09-07'},
            'last_week': {'start': '2026-08-31', 'exclusive_end': '2026-09-07'},
            'as_of_exclusive_end': '2026-09-08',
        })
        self.assertEqual(payload['question'], QUESTION['question'])
        for content in ('', '{}', '{"type":"number","sql":null}', '{"type":"unanswerable","sql":"SELECT 1"}', 'not json'):
            client.chat.completions.create = lambda **kw: SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=content))])
            with self.subTest(content=content), self.assertRaises(ValueError):
                generate_sql(client, QUESTION, context)

    def test_abstain_requires_null_sql(self):
        from types import SimpleNamespace
        from question_sql.llm import generate_sql
        for sql in (None, 'SELECT 1'):
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps({'type': 'abstain', 'sql': sql})))]))))
            if sql is None:
                self.assertEqual(generate_sql(client, QUESTION, {'tables': []}), {'type': 'abstain', 'sql': None})
            else:
                with self.assertRaises(ValueError):
                    generate_sql(client, QUESTION, {'tables': []})

    def test_reference_answer_types_and_exact_json_numbers(self):
        from datetime import date
        from decimal import Decimal
        from question_sql.answers import format_answer, dumps
        cases = [
            ('number', ['n'], [(Decimal('1234567890123456.1234'),)], Decimal('1234567890123456.1234')),
            ('percent', ['p'], [(Decimal('2.08'),)], Decimal('2.08')),
            ('string', ['s'], [('Cà phê',)], 'Cà phê'),
            ('boolean', ['b'], [(False,)], False),
            ('date', ['d'], [(date(2025, 2, 15),)], '2025-02-15'),
            ('list', ['v'], [('one',)], ['one']),
            ('list', ['v'], [], []),
            ('table', ['a', 'a'], [(1, 2)], {'columns': ['a', 'a'], 'rows': [[1, 2]]}),
            ('unanswerable', [], [], None),
            ('abstain', [], [], None),
        ]
        for kind, columns, rows, expected in cases:
            with self.subTest(kind=kind):
                answer = format_answer('q', {'type': kind}, columns, rows)
                expected_record = {'id': 'q', 'type': kind}
                if kind not in ('unanswerable', 'abstain'):
                    expected_record['answer'] = expected
                self.assertEqual(answer, expected_record)
                encoded = dumps(answer)
                self.assertEqual(set(json.loads(encoded)), set(expected_record))
        encoded = dumps({'n': Decimal('1234567890123456.1234'), 'whole': Decimal('22.00')})
        self.assertEqual(json.loads(encoded, parse_float=Decimal), {'n': Decimal('1234567890123456.1234'), 'whole': 22})
        bad = [('number', ['x'], [(None,)]), ('number', ['x'], [(True,)]), ('number', ['x'], [(float('nan'),)]), ('string', ['x'], [(2,)]), ('boolean', ['x'], [(1,)]), ('date', ['x'], [('2026-02-30',)]), ('list', ['x', 'y'], [(1, 2)]), ('number', ['x'], [(1,), (2,)])]
        for kind, columns, rows in bad:
            with self.subTest(kind=kind, rows=rows), self.assertRaises(ValueError):
                format_answer('q', {'type': kind}, columns, rows)
        with self.assertRaises(ValueError):
            dumps({'x': b'unsupported'})


class ExecutionTests(unittest.TestCase):
    def test_isolated_execution_and_limits(self):
        from decimal import Decimal
        from question_sql.execution import execute_sql
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path)
            result = execute_sql(path, 'biz_1', 'WITH x AS (SELECT * FROM biz_1.sales) SELECT sum("Tiền") AS a, count(*) AS a FROM x', timeout=20, max_rows=10)
            self.assertEqual(result['columns'], ['a', 'a'])
            self.assertEqual(result['rows'], [(Decimal('32.34'), 2)])
            rejected = ['SELECT 1; SELECT 2', 'DROP TABLE biz_1.sales', 'SELECT * FROM biz_2.sales', 'SELECT * FROM _catalog.businesses', "SELECT * FROM read_csv('missing.csv')", "SELECT * FROM read_csv('https://example.com/data.csv')"]
            for sql in rejected:
                with self.subTest(sql=sql), self.assertRaises(ValueError):
                    execute_sql(path, 'biz_1', sql, timeout=20, max_rows=10)
            with self.assertRaisesRegex(ValueError, 'row limit'):
                execute_sql(path, 'biz_1', 'SELECT * FROM biz_1.sales', timeout=20, max_rows=1)
            with self.assertRaisesRegex(ValueError, 'timed out'):
                execute_sql(path, 'biz_1', 'SELECT sum(i) FROM range(100000000000) t(i)', timeout=0.1, max_rows=10)
            with self.assertRaisesRegex(ValueError, 'payload limit'):
                execute_sql(path, 'biz_1', "SELECT repeat('x', 17000000)", timeout=20, max_rows=10)


class BatchTests(unittest.TestCase):
    def test_outputs_reference_format_and_continues_after_errors(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from decimal import Decimal
        from question_sql.runner import run
        calls = []
        def create(**kwargs):
            payload = json.loads(kwargs['messages'][1]['content'])
            calls.append(payload)
            kind, sql = (payload['question'], None) if payload['question'] in ('unanswerable', 'abstain') else ('number', 'SELECT sum("Tiền") FROM "' + payload['business_id'] + '".sales')
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps({'type': kind, 'sql': sql})))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, questions, output = root/'db.duckdb', root/'questions.jsonl', root/'answers.jsonl'
            fixture(db)
            records = [QUESTION, {**QUESTION, 'id': 'q2', 'business_id': 'biz_2'}, {**QUESTION, 'id': 'q3', 'question': 'unanswerable'}, {**QUESTION, 'id': 'q4', 'question': 'abstain'}]
            questions.write_text(json.dumps(records[0]) + '\nnot json\n' + '\n'.join(json.dumps(r) for r in records[1:]) + '\n', encoding='utf-8')
            with patch('question_sql.runner.create_client') as factory:
                factory.return_value.with_options.return_value = client
                self.assertEqual(run(questions, output, db, timeout=20), 1)
                factory.return_value.with_options.assert_called_once_with(max_retries=0)
            answers = [json.loads(line, parse_float=Decimal) for line in output.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(answers, [
                {'id': 'q1', 'type': 'number', 'answer': Decimal('32.34')},
                {'id': 'q2', 'type': 'number', 'answer': Decimal('32.34')},
                {'id': 'q3', 'type': 'unanswerable'},
                {'id': 'q4', 'type': 'abstain'}])
            details = [json.loads(line) for line in (root/'answers.details.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(details), 5)
            self.assertEqual(details[-1]['status'], 'abstain')
            self.assertEqual(details[-1]['columns'], [])
            self.assertEqual(details[1]['error']['stage'], 'input')
            self.assertEqual(details[0]['sql'], 'SELECT sum("Tiền") FROM "biz_1".sales')
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[1]['context']['business_id'], 'biz_2')
            self.assertNotIn('products', str(calls[1]['context']))
            with self.assertRaises((ValueError, FileExistsError)):
                run(questions, output, db)
            self.assertEqual(len(output.read_text(encoding='utf-8').splitlines()), 4)

    def test_unsupported_query_value_does_not_abort_later_questions(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from question_sql.runner import run
        sqls = iter(['SELECT uuid()', 'SELECT 7'])
        def create(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps({'type': 'string' if len(calls) == 0 else 'number', 'sql': next(sqls)})))])
        calls = []
        def response(**kwargs):
            result = create(**kwargs)
            calls.append(kwargs)
            return result
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=response)))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, questions, output = root/'db.duckdb', root/'questions.jsonl', root/'out.jsonl'
            fixture(db)
            questions.write_text(json.dumps(QUESTION) + '\n' + json.dumps({**QUESTION, 'id': 'q2'}) + '\n', encoding='utf-8')
            with patch('question_sql.runner.create_client') as factory:
                factory.return_value.with_options.return_value = client
                self.assertEqual(run(questions, output, db, timeout=20), 1)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), {'id': 'q2', 'type': 'number', 'answer': 7})
            details = [json.loads(line) for line in (root/'out.details.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(details), 2)
            self.assertEqual(details[0]['error']['stage'], 'serialization')

    def test_lone_surrogate_input_is_reported_without_aborting_batch(self):
        from unittest.mock import patch
        from question_sql.runner import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, questions, output = root/'db.duckdb', root/'q.jsonl', root/'out.jsonl'
            fixture(db)
            questions.write_text(json.dumps({**QUESTION, 'id': '\ud800'}) + '\n', encoding='utf-8')
            with patch('question_sql.runner.create_client') as factory:
                self.assertEqual(run(questions, output, db), 1)
                factory.assert_not_called()
            detail = json.loads((root/'out.details.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(detail['error']['stage'], 'input')

    def test_cli_help_and_positive_limits(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, '-m', 'question_sql', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--details', result.stdout)
        result = subprocess.run([sys.executable, '-m', 'question_sql', '--questions', 'q', '--output', 'o', '--max-rows', '0'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
