import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import duckdb


def catalog(conn):
    conn.execute('CREATE SCHEMA _catalog; CREATE SCHEMA biz_1')
    conn.execute('CREATE TABLE _catalog.businesses (business_id VARCHAR, import_status VARCHAR)')
    conn.execute("INSERT INTO _catalog.businesses VALUES ('biz_1', 'loaded')")
    conn.execute('CREATE TABLE _catalog.tables (business_id VARCHAR, table_name VARCHAR)')


def register(conn, name='sales'):
    conn.execute('INSERT INTO _catalog.tables VALUES (?, ?)', ['biz_1', name])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.conn = duckdb.connect(':memory:')
        self.addCleanup(self.conn.close)
        catalog(self.conn)

    def build(self, **kwargs):
        from business_profile.profiling import build_profile
        return build_profile(self.conn, 'biz_1', **kwargs)

    def test_counts_without_row_count_extrema_or_identifier_values(self):
        self.conn.execute('CREATE TABLE biz_1.sales (id INTEGER, amount DECIMAL(20,2), status VARCHAR, note VARCHAR, day DATE)')
        self.conn.execute("INSERT INTO biz_1.sales VALUES (1, 10.01, 'Xong', '', '2026-01-01'), (2, 20.02, 'Hủy', NULL, '2026-02-01'), (3, 123456789012345678.90, 'Xong', ' ', NULL), (4, NULL, NULL, 'PRIVATE', NULL)")
        register(self.conn)
        profile = self.build()
        self.assertEqual(set(profile), {'business_id', 'tables'})
        table = profile['tables'][0]
        self.assertEqual(table['name'], 'sales')
        self.assertNotIn('row_count', table)
        for field in table['columns']:
            self.assertNotIn('min', field)
            self.assertNotIn('max', field)
            self.assertNotIn('null_count', field)
            self.assertNotIn('empty_string_count', field)
        fields = {f['name']: f for f in table['columns']}
        self.assertEqual(fields['amount'], {'name': 'amount', 'type': 'DECIMAL(20,2)', 'distinct_count': 3})
        self.assertNotIn('min', fields['id'])
        self.assertNotIn('values', fields['id'])
        self.assertEqual(fields['note']['distinct_count'], 3)
        self.assertNotIn('values', fields['note'])
        self.assertEqual(fields['status']['values'], ['Hủy', 'Xong'])
        self.assertEqual(fields['day']['distinct_count'], 2)
        self.assertNotIn('PRIVATE', json.dumps(profile, ensure_ascii=False, allow_nan=False))

    def test_empty_all_null_and_unsupported_columns(self):
        self.conn.execute('CREATE TABLE biz_1.sales (status VARCHAR, amount DOUBLE, raw BLOB, items INTEGER[])')
        register(self.conn)
        fields = self.build()['tables'][0]['columns']
        self.assertEqual(fields[0]['values'], [])
        self.assertNotIn('empty_string_count', fields[0])
        self.assertEqual(fields[1]['distinct_count'], 0)
        self.assertNotIn('null_count', fields[2])
        self.assertIn('stats_omitted', fields[2])
        self.assertNotIn('distinct_count', fields[3])
        self.conn.execute('INSERT INTO biz_1.sales VALUES (NULL, NULL, NULL, NULL)')
        fields = self.build()['tables'][0]['columns']
        self.assertEqual(fields[0]['distinct_count'], 0)
        self.assertNotIn('null_count', fields[0])
        self.conn.execute('DROP TABLE biz_1.sales')
        self.conn.execute('CREATE TABLE biz_1.sales (raw BLOB, items INTEGER[])')
        self.assertEqual(self.build()['tables'][0]['columns'], [
            {'name': 'raw', 'type': 'BLOB', 'stats_omitted': 'unsupported type'},
            {'name': 'items', 'type': 'INTEGER[]', 'stats_omitted': 'unsupported type'},
        ])

    def test_nonfinite_values_are_counted_but_not_extrema(self):
        self.conn.execute('CREATE TABLE biz_1.sales (amount DOUBLE)')
        self.conn.execute("INSERT INTO biz_1.sales VALUES (2.5), ('NaN'), ('Infinity'), ('-Infinity'), (NULL)")
        register(self.conn)
        field = self.build()['tables'][0]['columns'][0]
        self.assertEqual(field['nonfinite_count'], 3)
        self.assertEqual(field['distinct_count'], 4)
        json.dumps(field, allow_nan=False)
        self.conn.execute('DELETE FROM biz_1.sales WHERE isfinite(amount)')
        field = self.build()['tables'][0]['columns'][0]
        self.assertEqual(field['nonfinite_count'], 3)

    def test_quoted_names_temporal_values_and_sensitive_extrema(self):
        self.conn.execute('CREATE TABLE biz_1."a""table" ("Trạng_thái" VARCHAR, "Mã khách hàng" BIGINT, "Ngày sinh" DATE, event_time TIMESTAMP, phone BIGINT)')
        self.conn.execute('INSERT INTO biz_1."a""table" VALUES (?, ?, ?, ?, ?)', [' Xong ', 123, '1990-01-01', '2026-02-03 04:05:06.123456', 123456])
        register(self.conn, 'a"table')
        fields = self.build()['tables'][0]['columns']
        self.assertEqual(fields[0]['values'], [' Xong '])
        self.assertNotIn('min', fields[1])
        self.assertNotIn('min', fields[2])
        self.assertEqual(fields[3]['distinct_count'], 1)
        self.assertNotIn('min', fields[4])

    def test_timezone_counts_need_no_optional_timezone_dependency(self):
        self.conn.execute('CREATE TABLE biz_1.sales (moment TIMESTAMP WITH TIME ZONE, precise TIMESTAMP_NS)')
        self.conn.execute("INSERT INTO biz_1.sales VALUES ('2026-01-01 12:00:00+07', '2026-01-01 00:00:00.123456789')")
        register(self.conn)
        fields = self.build()['tables'][0]['columns']
        self.assertEqual(fields[0]['distinct_count'], 1)
        self.assertEqual(fields[1]['distinct_count'], 1)

    def test_domains_are_complete_or_omitted(self):
        self.conn.execute('CREATE TABLE biz_1.sales (category VARCHAR, "Tên khách hàng" VARCHAR)')
        register(self.conn)
        self.conn.execute("INSERT INTO biz_1.sales SELECT i::VARCHAR, 'PRIVATE' FROM range(32) t(i)")
        fields = self.build()['tables'][0]['columns']
        self.assertEqual(len(fields[0]['values']), 32)
        self.assertNotIn('values', fields[1])
        self.conn.execute("INSERT INTO biz_1.sales VALUES ('32', 'PRIVATE')")
        field = self.build()['tables'][0]['columns'][0]
        self.assertNotIn('values', field)
        self.assertIn('values_omitted', field)
        self.conn.execute('DELETE FROM biz_1.sales')
        for length in (128, 129):
            self.conn.execute('DELETE FROM biz_1.sales')
            self.conn.execute('INSERT INTO biz_1.sales VALUES (?, NULL)', ['é' * length])
            field = self.build()['tables'][0]['columns'][0]
            self.assertEqual('values' in field, length == 128)

    def test_matching_tables_share_exact_combined_statistics(self):
        for name in ('sales_2025', 'sales_2026', 'sales_empty'):
            self.conn.execute(f'CREATE TABLE biz_1.{name} (amount INTEGER, status VARCHAR, day DATE)')
            register(self.conn, name)
        self.conn.execute("INSERT INTO biz_1.sales_2025 VALUES (10, 'Xong', '2025-01-01'), (NULL, '', NULL)")
        self.conn.execute("INSERT INTO biz_1.sales_2026 VALUES (10, 'Xong', '2026-01-01'), (20, 'Hủy', '2026-02-01')")
        tables = self.build()['tables']
        self.assertEqual(len(tables), 1)
        group = tables[0]
        self.assertEqual(group['names'], ['sales_2025', 'sales_2026', 'sales_empty'])
        self.assertNotIn('name', group)
        self.assertNotIn('row_count', group)
        amount, status, day = group['columns']
        self.assertEqual(amount['distinct_count'], 2)
        self.assertEqual(status['values'], ['', 'Hủy', 'Xong'])
        self.assertNotIn('empty_string_count', status)
        self.assertEqual(day['distinct_count'], 3)
        with tempfile.TemporaryDirectory() as temp:
            from business_profile.profiling import write_profile
            path = write_profile(self.build(), temp)
            write_profile(self.build(), temp)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['tables'], tables)

    def test_different_column_names_types_or_order_are_not_combined(self):
        for name, definition in [('a', 'amount INTEGER, status VARCHAR'), ('b', 'amount BIGINT, status VARCHAR'), ('c', 'price INTEGER, status VARCHAR'), ('d', 'status VARCHAR, amount INTEGER')]:
            self.conn.execute(f'CREATE TABLE biz_1.{name} ({definition})')
            register(self.conn, name)
        self.assertEqual([t['name'] for t in self.build()['tables']], ['a', 'b', 'c', 'd'])

    def test_combined_domain_limit_applies_across_all_members(self):
        for name, start in [('a', 0), ('b', 20)]:
            self.conn.execute(f'CREATE TABLE biz_1.{name} (status VARCHAR)')
            self.conn.execute(f'INSERT INTO biz_1.{name} SELECT i::VARCHAR FROM range({start}, {start + 20}) t(i)')
            register(self.conn, name)
        tables = self.build()['tables']
        self.assertEqual(len(tables), 1)
        field = tables[0]['columns'][0]
        self.assertEqual(field['distinct_count'], 40)
        self.assertNotIn('values', field)
        self.assertIn('values_omitted', field)

    def test_validation_does_not_read_unowned_tables(self):
        from business_profile.profiling import build_profile
        for business in ('../escape', '_catalog', 'missing'):
            with self.subTest(business=business), self.assertRaises(ValueError):
                build_profile(self.conn, business)
        self.conn.execute('CREATE TABLE biz_1.sales (n INTEGER)')
        with self.assertRaises(ValueError):
            self.build()
        register(self.conn)
        self.assertEqual(self.build()['tables'][0]['name'], 'sales')
        self.conn.execute("UPDATE _catalog.businesses SET import_status='failed'")
        with self.assertRaises(ValueError):
            self.build()

    def test_timeout_rolls_back_and_connection_remains_usable(self):
        from business_profile.profiling import build_profile
        self.conn.execute('CREATE TABLE biz_1.sales (n INTEGER)')
        register(self.conn)
        with self.assertRaises((TimeoutError, duckdb.InterruptException)):
            build_profile(self.conn, 'biz_1', timeout=1e-9)
        self.assertEqual(self.conn.execute('SELECT 42').fetchone()[0], 42)
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.build(timeout=timeout)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = {'business_id': 'biz_1', 'tables': []}

    def test_write_is_readable_repeatable_and_minimal(self):
        from business_profile.profiling import write_profile
        path = write_profile(self.profile, self.root / 'profiles')
        self.assertEqual(path.name, 'biz_1.json')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), self.profile)
        self.assertTrue(path.read_bytes().endswith(b'\n'))
        self.assertIn('\n  "business_id"', path.read_text(encoding='utf-8'))
        write_profile(self.profile, path.parent)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_legacy_profile_is_replaced_without_removed_fields(self):
        from business_profile.profiling import write_profile
        legacy = {'business_id': 'biz_1', 'tables': [{'name': 'sales', 'row_count': 2, 'columns': [{'name': 'amount', 'type': 'INTEGER', 'null_count': 0, 'distinct_count': 2, 'min': 1, 'max': 2}]}]}
        path = self.root / 'biz_1.json'
        path.write_text(json.dumps(legacy), encoding='utf-8')
        profile = {'business_id': 'biz_1', 'tables': [{'name': 'sales', 'columns': [{'name': 'amount', 'type': 'INTEGER', 'distinct_count': 2}]}]}
        write_profile(profile, self.root)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), profile)

    def test_failed_replace_preserves_old_json_and_cleans_temporary_file(self):
        from business_profile.profiling import write_profile
        path = write_profile(self.profile, self.root)
        before = path.read_bytes()
        with patch('business_profile.profiling.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                write_profile(self.profile, self.root)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_unrelated_files_and_unsafe_ids_are_not_overwritten(self):
        from business_profile.profiling import write_profile
        path = self.root / 'biz_1.json'
        for content in ('private notes', '{"business_id":"biz_2","tables":[]}', '{"business_id":"biz_1","tables":"PRIVATE"}', '{"business_id":"biz_1","tables":[],"private":"record"}'):
            path.write_text(content, encoding='utf-8')
            with self.subTest(content=content), self.assertRaises(ValueError):
                write_profile(self.profile, self.root)
            self.assertEqual(path.read_text(encoding='utf-8'), content)
        for business in ('../escape', '_catalog', 'a/b', ''):
            with self.subTest(business=business), self.assertRaises(ValueError):
                write_profile({**self.profile, 'business_id': business}, self.root)

    def test_symlink_output_is_rejected(self):
        from business_profile.profiling import write_profile
        target = self.root / 'target.json'
        target.write_text(json.dumps(self.profile), encoding='utf-8')
        link = self.root / 'biz_1.json'
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(ValueError):
            write_profile(self.profile, self.root)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), self.profile)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'businesses.duckdb'
        with duckdb.connect(str(self.db)) as conn:
            catalog(conn)
            conn.execute('CREATE TABLE biz_1.sales (status VARCHAR, amount INTEGER)')
            conn.execute("INSERT INTO biz_1.sales VALUES ('Xong', 5)")
            register(conn)
        self.out, self.err = StringIO(), StringIO()

    def run_cli(self, *args):
        from business_profile.__main__ import main
        with redirect_stdout(self.out), redirect_stderr(self.err):
            return main(['--db', str(self.db), *args])

    def test_cli_profiles_read_only_and_refreshes_default_output_without_model(self):
        with patch('business_import.llm.create_client', side_effect=AssertionError('model contacted')):
            self.assertEqual(self.run_cli('--business', 'biz_1'), 0)
        path = self.root / 'business_profiles' / 'biz_1.json'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['tables'][0]['columns'][0]['values'], ['Xong'])
        with duckdb.connect(str(self.db)) as conn:
            conn.execute("INSERT INTO biz_1.sales VALUES ('Hủy', 10)")
        self.assertEqual(self.run_cli(), 0)
        profile = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(profile['tables'][0]['columns'][0]['values'], ['Hủy', 'Xong'])
        self.assertNotIn('row_count', profile['tables'][0])
        self.assertEqual(set(profile), {'business_id', 'tables'})
        self.assertEqual(len(list(path.parent.iterdir())), 1)
        self.assertEqual(json.loads(self.out.getvalue().splitlines()[0])['status'], 'ok')

    def test_batch_continues_after_business_error(self):
        with duckdb.connect(str(self.db)) as conn:
            conn.execute("INSERT INTO _catalog.businesses VALUES ('biz_0', 'loaded'), ('biz_2', 'failed')")
        destination = self.root / 'custom'
        self.assertEqual(self.run_cli('--output-dir', str(destination)), 1)
        self.assertEqual([p.name for p in destination.iterdir()], ['biz_1.json'])
        self.assertIn('biz_0', self.err.getvalue())

    def test_missing_database_business_and_bad_timeout_fail(self):
        self.assertEqual(self.run_cli('--business', 'biz_404'), 1)
        self.assertEqual(self.run_cli('--db', str(self.root / 'missing.duckdb')), 1)
        self.assertFalse((self.root / 'missing.duckdb').exists())
        for timeout in ('0', '-1', 'nan', 'inf'):
            with self.subTest(timeout=timeout), self.assertRaises(SystemExit):
                self.run_cli('--timeout', timeout)


if __name__ == '__main__':
    unittest.main()
