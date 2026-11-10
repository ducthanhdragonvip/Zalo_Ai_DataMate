import json
import tempfile
import unittest
from pathlib import Path

import duckdb


QUESTION = {"id": "q1", "business_id": "biz_1", "question": "Doanh thu hôm nay?", "as_of_date": "2026-09-07"}


def fixture(path, *, profiles=True):
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
    if profiles:
        from business_profile.profiling import build_profile, write_profile
        with duckdb.connect(str(path)) as conn:
            for business in ('biz_1', 'biz_2'):
                write_profile(build_profile(conn, business), path.parent / 'business_profiles')


class ContextTests(unittest.TestCase):
    def test_validate_question_rejects_invalid_fields(self):
        from question_sql.context import validate_question
        self.assertEqual(validate_question(QUESTION), QUESTION)
        for changes in ({'id': ''}, {'question': ' '}, {'business_id': '_catalog'}, {'business_id': 'x;drop'}, {'as_of_date': '2026-02-30'}, {'as_of_date': '20260907'}, {'as_of_date': None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_question({**QUESTION, **changes})
        with self.assertRaises(ValueError):
            validate_question([])

    def test_saved_grouped_profile_is_used_without_data_queries(self):
        from business_profile.profiling import build_profile, write_profile
        from question_sql.context import load_context
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path)
            profiles = path.parent / 'business_profiles'
            with duckdb.connect(str(path)) as conn:
                conn.execute('CREATE TABLE biz_1.sales2 AS SELECT * FROM biz_1.sales')
                conn.execute("INSERT INTO _catalog.tables VALUES ('biz_1', 'sales.xlsx', 'T10', 'sales2')")
                profile = build_profile(conn, 'biz_1')
                write_profile(profile, profiles)
                class MetadataOnly:
                    def execute(self, sql, *args):
                        if 'FROM biz_1.' in sql or 'FROM "biz_1".' in sql:
                            raise AssertionError('QA must not query business records')
                        return conn.execute(sql, *args)
                context = load_context(MetadataOnly(), 'biz_1', profiles)
                self.assertEqual(context['business_profile'], profile)
                self.assertEqual(profile['tables'][1]['names'], ['sales', 'sales2'])
                self.assertEqual(profile['tables'][1]['columns'][0]['distinct_count'], 2)

    def test_invalid_profiles_are_rejected(self):
        import copy
        from question_sql.context import load_context
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path)
            profiles = path.parent / 'business_profiles'
            file = profiles / 'biz_1.json'
            valid = json.loads(file.read_text(encoding='utf-8'))
            variants = []
            for mutate in (
                lambda p: p.update(business_id='biz_2'),
                lambda p: p.update(samples=['PRIVATE']),
                lambda p: p['tables'].pop(),
                lambda p: p['tables'].append(copy.deepcopy(p['tables'][0])),
                lambda p: p['tables'][0].update(names=['products']),
                lambda p: p['tables'][0].update(name='unknown'),
                lambda p: p['tables'][0].pop('name'),
                lambda p: (p['tables'][0].pop('name'), p['tables'][0].update(names=[])),
                lambda p: (p['tables'][0].pop('name'), p['tables'][0].update(names=['products', 'products'])),
                lambda p: p['tables'][1]['columns'].pop(),
                lambda p: p['tables'][1]['columns'].append(copy.deepcopy(p['tables'][1]['columns'][0])),
                lambda p: p['tables'][0]['columns'][0].update(name='bad\x00name'),
                lambda p: p['tables'][0]['columns'][0].update(distinct_count=-1),
                lambda p: p['tables'][0]['columns'][0].update(values_omitted='unknown'),
                lambda p: p['tables'][1]['columns'].reverse(),
                lambda p: p['tables'][1]['columns'][0].update(type='VARCHAR'),
                lambda p: p['tables'][0]['columns'][0].update(distinct_count=True),
                lambda p: p['tables'][0]['columns'][0].update(values=['x'], distinct_count=0),
                lambda p: p['tables'][0]['columns'][0].update(values=['x'] * 33),
                lambda p: p['tables'][0]['columns'][0].update(values=['x'*129]),
                lambda p: p['tables'][0]['columns'][0].update(values=[], values_omitted='more than 32 labels'),
                lambda p: p['tables'][0]['columns'][0].update(min='PRIVATE'),
                lambda p: p['tables'][0]['columns'][0].update(name='\ud800'),
            ):
                p = copy.deepcopy(valid)
                mutate(p)
                variants.append(json.dumps(p))
            variants += ['{', '{"business_id": NaN}', 'null']
            with duckdb.connect(str(path)) as conn:
                for content in variants:
                    file.write_text(content, encoding='utf-8')
                    with self.subTest(content=ascii(content)), self.assertRaisesRegex(ValueError, 'business_profile'):
                        load_context(conn, 'biz_1', profiles)
                file.unlink()
                with self.assertRaisesRegex(ValueError, 'business_profile'):
                    load_context(conn, 'biz_1', profiles)

    def test_profile_native_types_quoted_names_and_omitted_domains(self):
        from business_profile.profiling import build_profile, write_profile
        from question_sql.context import load_context
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path)
            profiles = path.parent / 'business_profiles'
            with duckdb.connect(str(path)) as conn:
                conn.execute('CREATE TABLE biz_1.native ("Nhãn ""lạ" VARCHAR, "Trạng thái" VARCHAR, f DOUBLE, b BOOLEAN, u UUID, t TIME, a INTEGER[])')
                conn.execute("INSERT INTO biz_1.native SELECT NULL, 'label' || i, 'NaN'::DOUBLE, true, uuid(), '12:00'::TIME, [1,2] FROM range(33) t(i)")
                conn.execute("INSERT INTO _catalog.tables VALUES ('biz_1', 'native.sqlite', 'native', 'native')")
                profile = build_profile(conn, 'biz_1')
                write_profile(profile, profiles)
                context = load_context(conn, 'biz_1', profiles)
                self.assertEqual(context['business_profile'], profile)
                columns = {c['name']: c for c in profile['tables'][0]['columns']}
                self.assertEqual(columns['Nhãn "lạ']['distinct_count'], 0)
                self.assertNotIn('values', columns['Nhãn "lạ'])
                self.assertEqual(columns['Trạng thái']['values_omitted'], 'more than 32 labels')
                self.assertEqual(columns['f']['nonfinite_count'], 33)
                self.assertEqual(columns['a']['stats_omitted'], 'unsupported type')
                # Empty complete domains are valid, not missing metadata.
                profile['tables'][1]['columns'][0]['values'] = []
                write_profile(profile, profiles)
                self.assertEqual(load_context(conn, 'biz_1', profiles)['business_profile'], profile)

    def test_context_contains_all_actual_columns_and_provenance(self):
        from question_sql.context import load_context
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path)
            with duckdb.connect(str(path), read_only=True) as conn:
                context = load_context(conn, 'biz_1', path.parent / 'business_profiles')
                self.assertIn('không cộng lặp', context['readme'])
                self.assertEqual([t['name'] for t in context['business_profile']['tables']], ['products', 'sales'])
                sales = context['business_profile']['tables'][1]
                self.assertEqual(sales['columns'], [{'name': 'Ngày', 'type': 'DATE', 'distinct_count': 2}, {'name': 'Tiền', 'type': 'DECIMAL(18,2)', 'distinct_count': 2}])
                self.assertEqual(context['sources'][1], {'name': 'sales', 'source_file': 'sales.xlsx', 'source_table': 'T9'})
                with self.assertRaises(ValueError):
                    load_context(conn, 'missing', path.parent / 'business_profiles')
            with duckdb.connect(str(path)) as conn:
                conn.execute('CREATE TABLE biz_1.unmapped (n INT)')
                with self.assertRaises(ValueError):
                    load_context(conn, 'biz_1', path.parent / 'business_profiles')
                conn.execute('DROP TABLE biz_1.unmapped')
                conn.execute("UPDATE _catalog.businesses SET readme='' WHERE business_id='biz_1'")
                with self.assertRaises(ValueError):
                    load_context(conn, 'biz_1', path.parent / 'business_profiles')


class GenerationTests(unittest.TestCase):
    def test_one_request_and_full_context(self):
        from types import SimpleNamespace
        from question_sql.llm import generate_sql
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"type":"number","sql":"SELECT 1"}'))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        profile = {'business_id': 'biz_1', 'tables': [
            {'names': ['sales', 'sales2'], 'columns': [
                {'name': 'Ngày', 'type': 'DATE', 'distinct_count': 2},
                {'name': 'Trạng thái', 'type': 'VARCHAR', 'distinct_count': 4,
                 'values': ['đã lấy', 'Đã lấy', ' đã lấy ', "Nhãn 'gốc'"]},
            ]},
        ]}
        context = {'business_id': 'biz_1', 'readme': 'Đơn hàng', 'business_profile': profile,
                   'sources': [{'name': 'sales', 'source_file': 'sales.xlsx', 'source_table': 'T9'}], 'warnings': '[]'}
        self.assertEqual(generate_sql(client, QUESTION, context), {'type': 'number', 'sql': 'SELECT 1'})
        self.assertEqual(len(calls), 1)
        payload = json.loads(calls[0]['messages'][1]['content'])
        self.assertEqual(set(payload), {'question', 'business_id', 'as_of_date', 'date_boundaries', 'readme', 'business_profile', 'sources', 'warnings'})
        self.assertEqual(payload['business_profile'], profile)
        self.assertEqual(payload['readme'], 'Đơn hàng')
        self.assertEqual(payload['sources'], context['sources'])
        self.assertEqual(payload['warnings'], '[]')
        self.assertEqual(payload['as_of_date'], '2026-09-07')
        self.assertEqual(payload['date_boundaries'], {
            'today': {'start': '2026-09-07', 'exclusive_end': '2026-09-08'},
            'yesterday': {'start': '2026-09-06', 'exclusive_end': '2026-09-07'},
            'last_week': {'start': '2026-08-31', 'exclusive_end': '2026-09-07'},
            'as_of_exclusive_end': '2026-09-08',
        })
        self.assertEqual(payload['question'], QUESTION['question'])
        for content in ('', '{}', '{"type":"number","sql":null}', '{"type":"unanswerable","sql":"SELECT 1"}', 'not json'):
            client.chat.completions.create = lambda **kw: SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=content))])
            with self.subTest(content=content), self.assertRaises(ValueError):
                generate_sql(client, QUESTION, context)

    def test_today_boundaries_follow_each_questions_as_of_date(self):
        from types import SimpleNamespace
        from question_sql.llm import generate_sql
        payloads = []
        def create(**kwargs):
            payloads.append(json.loads(kwargs['messages'][1]['content']))
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"type":"number","sql":"SELECT 1"}'))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        context = {'readme': '', 'business_profile': {'business_id': 'biz_1', 'tables': []}, 'sources': [], 'warnings': '[]'}
        for as_of, end in (('2024-02-29', '2024-03-01'), ('2025-12-31', '2026-01-01')):
            with self.subTest(as_of=as_of):
                generate_sql(client, {**QUESTION, 'as_of_date': as_of, 'question': 'hnay co bao nhieu don'}, context)
                self.assertEqual(payloads[-1]['date_boundaries'].get('today'), {'start': as_of, 'exclusive_end': end})
                self.assertEqual(payloads[-1]['question'], 'hnay co bao nhieu don')

    def test_request_binds_sql_namespace_to_each_business(self):
        from types import SimpleNamespace
        from question_sql.llm import generate_sql
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"type":"number","sql":"SELECT 1"}'))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        for business in ('biz_7', 'biz_88'):
            context = {'readme': '', 'business_profile': {'business_id': business, 'tables': []}, 'sources': [], 'warnings': '[]'}
            generate_sql(client, {**QUESTION, 'business_id': business}, context)
            instruction = calls[-1]['messages'][0]['content']
            self.assertIn(f'FROM "{business}"."table_name"', instruction)
            self.assertNotIn('FROM "biz_1".', instruction)
        self.assertEqual(len(calls), 2)

    def test_abstain_requires_null_sql(self):
        from types import SimpleNamespace
        from question_sql.llm import generate_sql
        for sql in (None, 'SELECT 1'):
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps({'type': 'abstain', 'sql': sql})))]))))
            if sql is None:
                self.assertEqual(generate_sql(client, QUESTION, {'readme': '', 'business_profile': {'business_id': 'biz_1', 'tables': []}, 'sources': [], 'warnings': '[]'}), {'type': 'abstain', 'sql': None})
            else:
                with self.assertRaises(ValueError):
                    generate_sql(client, QUESTION, {'readme': '', 'business_profile': {'business_id': 'biz_1', 'tables': []}, 'sources': [], 'warnings': '[]'})

    def test_timestamp_answers_use_calendar_dates_but_details_keep_time(self):
        from datetime import timedelta, timezone
        from question_sql.answers import diagnostic, format_answer
        with duckdb.connect() as conn:
            stamp = conn.execute("SELECT TIMESTAMP '2026-05-09 18:23:45.123456'").fetchone()[0]
        zoned = stamp.replace(tzinfo=timezone(timedelta(hours=7)))
        for value in (stamp, zoned):
            for kind in ('string', 'date'):
                with self.subTest(value=value, kind=kind):
                    self.assertEqual(format_answer('q', {'type': kind}, ['day'], [(value,)]),
                                     {'id': 'q', 'type': 'date', 'answer': '2026-05-09'})
            self.assertEqual(diagnostic(value), value.isoformat())
            for kind in ('number', 'percent', 'boolean'):
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    format_answer('q', {'type': kind}, ['day'], [(value,)])
        self.assertEqual(format_answer('q', {'type': 'list'}, ['day'], [(stamp,), (None,)]),
                         {'id': 'q', 'type': 'list', 'answer': ['2026-05-09', None]})
        self.assertEqual(format_answer('q', {'type': 'table'}, ['nested'], [({'days': [stamp]},)]),
                         {'id': 'q', 'type': 'table', 'answer': {'columns': ['nested'], 'rows': [[{'days': ['2026-05-09']}]]}})
        text = '2026-05-09T18:23:45'
        self.assertEqual(format_answer('q', {'type': 'string'}, ['text'], [(text,)])['answer'], text)

    def test_number_answers_round_to_integers_without_changing_percent_or_details(self):
        from decimal import Decimal
        from question_sql.answers import diagnostic, dumps, format_answer
        for value, expected in (
            (156347.57, 156348), (2.5, 3), (-2.5, -3), (47.0, 47),
            (Decimal('0.49'), 0), (Decimal('-0.5'), -1),
            (Decimal('123456789012345678901234567890.5'), 123456789012345678901234567891),
        ):
            with self.subTest(value=value):
                record = format_answer('q', {'type': 'number'}, ['n'], [(value,)])
                self.assertIs(type(record['answer']), int)
                self.assertEqual(record['answer'], expected)
                self.assertEqual(json.loads(dumps(record))['answer'], expected)
        for value in (1.66, Decimal('2.08')):
            self.assertEqual(format_answer('q', {'type': 'percent'}, ['p'], [(value,)])['answer'], Decimal(str(value)))
        self.assertEqual(diagnostic(Decimal('156347.57')), '156347.57')
        for value in (float('inf'), Decimal('NaN'), Decimal('Infinity')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                format_answer('q', {'type': 'number'}, ['n'], [(value,)])

    def test_percent_answers_round_to_two_decimals(self):
        from decimal import Decimal
        from question_sql.answers import diagnostic, dumps, format_answer
        for value, expected in (
            (-5.83497640565048, '-5.83'), (2.675, '2.68'),
            (Decimal('-2.675'), '-2.68'), (Decimal('1.004'), '1.00'),
            (Decimal('123456789012345678901234567890.125'), '123456789012345678901234567890.13'),
        ):
            with self.subTest(value=value):
                record = format_answer('q', {'type': 'percent'}, ['p'], [(value,)])
                self.assertEqual(record['answer'], Decimal(expected))
                self.assertEqual(json.loads(dumps(record), parse_float=Decimal)['answer'], Decimal(expected))
        self.assertEqual(diagnostic(-5.83497640565048), -5.83497640565048)
        for value in (float('nan'), float('inf'), Decimal('NaN'), Decimal('Infinity')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                format_answer('q', {'type': 'percent'}, ['p'], [(value,)])

    def test_reference_answer_types_and_exact_json_numbers(self):
        from datetime import date
        from decimal import Decimal
        from question_sql.answers import format_answer, dumps
        cases = [
            ('number', ['n'], [(Decimal('1234567890123456.1234'),)], 1234567890123456),
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
                if kind in ('unanswerable', 'abstain'):
                    expected_record['type'] = 'unanswerable'
                    expected_record['reason'] = 'Required data is unavailable.' if kind == 'unanswerable' else 'Cannot determine a reliable answer.'
                else:
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
    def test_mixed_identifier_closing_quote_uses_exact_catalog_column(self):
        from question_sql.execution import execute_sql
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path, profiles=False)
            prefix = 'SELECT COUNT(*) AS "số đơn" FROM "biz_1"."sales" WHERE '
            malformed = prefix + '\"Ngày\' = DATE \'2026-09-07\''
            result = execute_sql(path, 'biz_1', malformed, timeout=20)
            self.assertEqual(result['rows'], [(1,)])
            self.assertEqual(result['executed_sql'], prefix + '\"Ngày\" = DATE \'2026-09-07\'')
            valid = "SELECT 'Ngày'' = yesterday'"
            result = execute_sql(path, 'biz_1', valid, timeout=20)
            self.assertEqual(result['rows'], [("Ngày' = yesterday",)])
            self.assertNotIn('executed_sql', result)
            for invalid in (prefix + '\"Unknown\' = 1', prefix + '\"Ngày\' = \'unfinished',
                            prefix + '\"Ngày\' = 1; SELECT 2',
                            'DELETE FROM biz_1.sales WHERE \"Ngày\' = DATE \'2026-09-07\''):
                with self.subTest(sql=invalid), self.assertRaises(ValueError):
                    execute_sql(path, 'biz_1', invalid, timeout=20)

    def test_single_trailing_brace_is_corrected_after_parser_rejection(self):
        from decimal import Decimal
        from question_sql.execution import execute_sql
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path, profiles=False)
            sql = 'SELECT SUM("Tiền") FROM "biz_1"."sales" WHERE "Ngày" >= \'2026-09-07\''
            result = execute_sql(path, 'biz_1', sql + '} \n', timeout=20)
            self.assertEqual(result['rows'], [(Decimal('32.34'),)])
            self.assertEqual(result['executed_sql'], sql)
            valid = "SELECT {'brace': '}'}"
            result = execute_sql(path, 'biz_1', valid, timeout=20)
            self.assertEqual(result['rows'], [({'brace': '}'},)])
            self.assertNotIn('executed_sql', result)
            for invalid in ('SELECT 1}}', "SELECT 'unfinished}", 'SELECT FROM }',
                            'SELECT 1; SELECT 2}', 'DROP TABLE biz_1.sales}'):
                with self.subTest(sql=invalid), self.assertRaises(ValueError):
                    execute_sql(path, 'biz_1', invalid, timeout=20)

    def test_isolated_execution_and_limits(self):
        from decimal import Decimal
        from question_sql.execution import execute_sql
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fixture.duckdb'
            fixture(path, profiles=False)
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
            records = [QUESTION, {**QUESTION, 'id': 'q2', 'business_id': 'biz_2'}, {**QUESTION, 'id': 'q3', 'question': 'unanswerable', 'as_of_date': '2026-10-02'}, {**QUESTION, 'id': 'q4', 'question': 'abstain'}]
            questions.write_text(json.dumps(records[0]) + '\nnot json\n' + '\n'.join(json.dumps(r) for r in records[1:]) + '\n', encoding='utf-8')
            with patch('question_sql.runner.create_client') as factory:
                factory.return_value.with_options.return_value = client
                self.assertEqual(run(questions, output, db, timeout=20), 1)
                factory.return_value.with_options.assert_called_once_with(max_retries=0)
            answers = [json.loads(line, parse_float=Decimal) for line in output.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(answers, [
                {'id': 'q1', 'type': 'number', 'answer': 32},
                {'id': 'q2', 'type': 'number', 'answer': 32},
                {'id': 'q3', 'type': 'unanswerable', 'reason': 'Required data is unavailable.'},
                {'id': 'q4', 'type': 'unanswerable', 'reason': 'Cannot determine a reliable answer.'}])
            details = [json.loads(line) for line in (root/'answers.details.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(details), 5)
            self.assertEqual(details[-1]['status'], 'abstain')
            self.assertEqual(details[-1]['columns'], [])
            self.assertEqual(details[1]['error']['stage'], 'input')
            self.assertEqual(details[0]['sql'], 'SELECT sum("Tiền") FROM "biz_1".sales')
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[1]['business_profile']['business_id'], 'biz_2')
            self.assertNotIn('products', str(calls[1]['business_profile']))
            self.assertEqual(calls[2]['business_profile'], calls[0]['business_profile'])
            self.assertEqual(calls[2]['date_boundaries']['yesterday']['start'], '2026-10-01')
            self.assertEqual(calls[3]['date_boundaries'], calls[0]['date_boundaries'])
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
            answers = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(answers[0]['id'], 'q1')
            self.assertEqual(answers[0]['type'], 'unanswerable')
            self.assertTrue(answers[0]['reason'])
            self.assertEqual(answers[1], {'id': 'q2', 'type': 'number', 'answer': 7})
            details = [json.loads(line) for line in (root/'out.details.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(details), 2)
            self.assertEqual(details[0]['error']['stage'], 'serialization')

    def test_each_failed_question_emits_unanswerable_with_stage_reason(self):
        from unittest.mock import patch
        from question_sql.runner import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, questions = root/'db.duckdb', root/'q.jsonl'
            fixture(db)
            for stage in ('input', 'context', 'model', 'execution', 'serialization'):
                output = root / f'{stage}.jsonl'
                record = {**QUESTION, 'question': ''} if stage == 'input' else QUESTION
                questions.write_text(json.dumps(record), encoding='utf-8')
                with patch('question_sql.runner.create_client'), patch('question_sql.runner.load_context', return_value={}) as context, patch('question_sql.runner.generate_sql', return_value={'type': 'number', 'sql': 'SELECT NULL'}) as generate, patch('question_sql.runner.execute_sql', return_value={'columns': ['n'], 'rows': [(None,)]}) as execute:
                    if stage == 'context':
                        context.side_effect = ValueError('missing profile')
                    elif stage == 'model':
                        generate.side_effect = ValueError('invalid model response')
                    elif stage == 'execution':
                        execute.side_effect = ValueError('invalid SQL')
                    self.assertEqual(run(questions, output, db), 1)
                answer = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(set(answer), {'id', 'type', 'reason'})
                self.assertEqual(answer['id'], QUESTION['id'])
                self.assertEqual(answer['type'], 'unanswerable')
                self.assertEqual(answer['reason'], f'{stage}: unable to produce a valid answer; see details.')
                detail = json.loads((root / f'{stage}.details.jsonl').read_text(encoding='utf-8'))
                self.assertEqual(detail['status'], 'error')
                self.assertEqual(detail['error']['stage'], stage)

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

    def test_failed_profiles_never_create_model_client(self):
        from unittest.mock import patch
        from question_sql.runner import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, questions = root/'db.duckdb', root/'q.jsonl'
            fixture(db)
            questions.write_text(json.dumps(QUESTION), encoding='utf-8')
            profile = root / 'business_profiles' / 'biz_1.json'
            for i, content in enumerate((None, '{', '{"business_id":"biz_1","tables":[]}')):
                if content is None:
                    profile.unlink()
                else:
                    profile.write_text(content, encoding='utf-8')
                output = root / f'out{i}.jsonl'
                with patch('question_sql.runner.create_client') as factory:
                    self.assertEqual(run(questions, output, db), 1)
                    factory.assert_not_called()
                detail = json.loads((root / f'out{i}.details.jsonl').read_text(encoding='utf-8'))
                self.assertEqual(detail['error']['stage'], 'context')
                self.assertIn('python -m business_profile', detail['error']['message'])

    def test_cli_profile_override_and_context_failure_continuation(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        from question_sql.__main__ import main
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, questions, output = root/'db.duckdb', root/'q.jsonl', root/'out.jsonl'
            fixture(db)
            profiles = root / 'custom_profiles'
            (root / 'business_profiles').rename(profiles)
            (profiles / 'biz_1.json').unlink()
            questions.write_text('\n'.join(json.dumps(r) for r in [QUESTION, {**QUESTION, 'business_id': 'biz_2', 'id': 'q2'}]), encoding='utf-8')
            calls = []
            def create(**kw):
                calls.append(json.loads(kw['messages'][1]['content']))
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"type":"number","sql":"SELECT 7"}'))])
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            argv = ['question_sql', '--questions', str(questions), '--output', str(output), '--db', str(db), '--profile-dir', str(profiles)]
            with patch.object(sys, 'argv', argv), patch('question_sql.runner.create_client') as factory:
                factory.return_value.with_options.return_value = client
                self.assertEqual(main(), 1)
            self.assertEqual([c['business_id'] for c in calls], ['biz_2'])
            answers = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(answers[0]['id'], 'q1')
            self.assertEqual(answers[0]['type'], 'unanswerable')
            self.assertTrue(answers[0]['reason'])
            self.assertEqual(answers[1], {'id': 'q2', 'type': 'number', 'answer': 7})
            details = [json.loads(line) for line in (root/'out.details.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(details[0]['error']['stage'], 'context')
            self.assertIn('python -m business_profile', details[0]['error']['message'])

    def test_cli_help_and_positive_limits(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, '-m', 'question_sql', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--details', result.stdout)
        self.assertIn('--profile-dir', result.stdout)
        result = subprocess.run([sys.executable, '-m', 'question_sql', '--questions', 'q', '--output', 'o', '--max-rows', '0'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
