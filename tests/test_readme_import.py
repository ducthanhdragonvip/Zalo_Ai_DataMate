import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
from openpyxl import Workbook

from business_import.__main__ import _read_tables, main
from business_import.llm import describe_business
from business_import.sources import inspect_business


def response_client(result, requests):
    def create(**kwargs):
        requests.append(kwargs)
        return nullcontext([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])
                            for part in json.dumps(result)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class ReadmeImportTests(unittest.TestCase):
    def describe(self, readme, columns, files=None):
        requests = []
        with tempfile.TemporaryDirectory() as temp, patch('business_import.llm.OUTPUT_DIR', Path(temp)):
            result = describe_business(response_client({'columns': columns}, requests),
                                       {'readme': readme, 'files': files or []})
        return result, requests[0]

    def test_csv_timestamp_columns_accept_excel_serial_dates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'data'
            path = root / 'biz_1'
            path.mkdir(parents=True)
            (path / 'README.md').write_text('- **Date**: event date\n- **Code**: identifier\n- **Qty**: count', encoding='utf-8')
            (path / 'dates.csv').write_text('Date,Code,Qty\n45692,45692,45692\n05/02/2025,002,2\n,003,3\n', encoding='utf-8')
            (path / 'invalid.csv').write_text('Date,Code,Qty\n45692,001,1\n60,002,2\n', encoding='utf-8')
            response = {'columns': [{'name': 'Date', 'type': 'TIMESTAMP'},
                                    {'name': 'Code', 'type': 'VARCHAR'}, {'name': 'Qty', 'type': 'BIGINT'}]}
            database = Path(temp) / 'out.duckdb'
            with patch('business_import.llm.create_client', return_value=response_client(response, [])), patch('business_import.llm.OUTPUT_DIR', Path(temp) / 'responses'), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(['--roots', str(root), '--db', str(database)]), 0)
            with duckdb.connect(str(database)) as conn:
                self.assertEqual(conn.execute('SELECT * FROM biz_1.dates').fetchall(), [
                    (datetime(2025, 2, 4), '45692', 45692), (datetime(2025, 2, 5), '002', 2), (None, '003', 3)])
                self.assertEqual(conn.execute('SELECT Date FROM biz_1.invalid').fetchall(), [('45692',), ('60',)])

    def test_source_headers_extend_unique_readme_candidates(self):
        requests = []
        columns = [{'name': 'Amount', 'type': 'DECIMAL'}, {'name': 'Mã', 'type': 'VARCHAR'}]
        with tempfile.TemporaryDirectory() as temp, patch('business_import.llm.OUTPUT_DIR', Path(temp)):
            result = describe_business(response_client({'columns': columns}, requests),
                {'readme': '- **Mã**: identifier\n- **README only**: documented field'},
                ['Amount', ' amount ', 'Mã', 'MÃ', 'Quote "field"\nlabel'])
        self.assertEqual(result['columns'], columns)
        schema = requests[0]['response_format']['json_schema']['schema']
        names = schema['properties']['columns']['items']['anyOf'][0]['properties']['name']['enum']
        self.assertEqual(names, ['Amount', 'Mã', 'Quote "field"\nlabel', 'README only'])
        self.assertIn(json.dumps(['Amount', 'Mã', 'Quote "field"\nlabel'], ensure_ascii=False),
                      requests[0]['messages'][0]['content'])

    def test_cli_collects_actual_grouped_headers_before_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'data'
            path = root / 'biz_1'
            path.mkdir(parents=True)
            (path / 'README.md').write_text('- **Khách hàng (tên)**: customer\n- **Giờ**: hour\n- **README only**: field', encoding='utf-8')
            labels = ['Khách hàng (tên)', 'Giờ', 'Thời gian', 'Ma don', 'SL bán', 'dvt',
                      'Hàng bán', 'Trạng thái xử lý', 'Giá 1 đv', 'Amount', 'Tổng phiếu']
            book = Workbook()
            sheet = book.active
            sheet.title = 'PRIVATE_SHEET'
            sheet.append(['Đơn', None, None, None, 'Hàng', None, None, 'Tiền'])
            sheet.merge_cells('A1:D1')
            sheet.merge_cells('E1:G1')
            sheet.merge_cells('H1:K1')
            sheet.append(labels)
            sheet.append(['SECRET_CUSTOMER', '09:00', 'morning', '001', 2, 'pcs', 'SECRET_PRODUCT', 'done', 10, 20, 20])
            book.save(path / 'PRIVATE.xlsx')
            book.close()
            (path / 'extra.csv').write_text('Amount,Qty\n30,3\n', encoding='utf-8')
            with closing(sqlite3.connect(path / 'native.sqlite')) as conn:
                conn.execute('CREATE TABLE native (SQLITE_PRIVATE TEXT)')
                conn.execute("INSERT INTO native VALUES ('001')")
                conn.commit()
            requests = []
            response = {'columns': [{'name': 'Amount', 'type': 'BIGINT'}, {'name': 'Qty', 'type': 'BIGINT'}]}
            database = Path(temp) / 'out.duckdb'
            with patch('business_import.llm.create_client', return_value=response_client(response, requests)), patch('business_import.llm.OUTPUT_DIR', Path(temp) / 'responses'), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(['--roots', str(root), '--db', str(database)]), 0)
            request = requests[0]
            names = request['response_format']['json_schema']['schema']['properties']['columns']['items']['anyOf'][0]['properties']['name']['enum']
            self.assertEqual(set(names), set(labels + ['Qty', 'README only']))
            prompt = request['messages'][0]['content']
            for private in ['SECRET_CUSTOMER', 'SECRET_PRODUCT', 'PRIVATE.xlsx', 'PRIVATE_SHEET', 'SQLITE_PRIVATE']:
                self.assertNotIn(private, prompt)
            with duckdb.connect(str(database)) as conn:
                self.assertEqual(conn.execute('SELECT "Ma don", Amount FROM biz_1.PRIVATE__PRIVATE_SHEET').fetchall(), [('001', 20)])
                self.assertEqual(conn.execute('DESCRIBE biz_1.PRIVATE__PRIVATE_SHEET').fetchall()[9][1], 'BIGINT')
                preamble = conn.execute("SELECT preamble FROM _catalog.tables WHERE source_file='PRIVATE.xlsx'").fetchone()[0]
                self.assertEqual(json.loads(preamble)[0][0], 'Đơn')
                self.assertEqual(conn.execute('SELECT * FROM biz_1.native__native').fetchall(), [('001',)])

    def test_undocumented_all_text_sheet_preserves_every_row(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            book = Workbook()
            sheet = book.active
            sheet.title = 'Lookup'
            records = [['Alias', 'Full name', 'Category'], ['A', 'Alpha', 'Text'],
                       ['Alias', 'Full name', 'Category'], ['B', 'Beta', 'Text']]
            for row in records:
                sheet.append(row)
            book.save(path / 'lookup.xlsx')
            book.close()
            description = {'columns': []}
            raw, _ = _read_tables(inspect_business(path), description)
            self.assertEqual(raw[0]['columns'], ['column_1', 'column_2', 'column_3'])
            self.assertEqual(raw[0]['rows'], records)
            self.assertTrue(any('all rows' in warning for warning in description['tables'][0]['type_hint_warnings']))

    def test_trailing_total_is_excluded_but_arbitrary_invalid_dates_remain(self):
        from business_import.column_types import convert_columns
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / 'README.md').write_text('- **Ngày**: date\n- **Amount**: money', encoding='utf-8')
            for last in [['Tổng cộng', 26000], ['invalid date', 13000]]:
                book = Workbook()
                sheet = book.active
                sheet.append(['Ngày', 'Amount'])
                sheet.append([45692, 13000])
                sheet.append(last)
                sheet.append(['Ghi chú: report', None])
                book.save(path / 'report.xlsx')
                book.close()
                description = {'columns': [{'name': 'Ngày', 'type': 'TIMESTAMP'}]}
                raw, _ = _read_tables(inspect_business(path), description)
                proposal = description['tables'][0]
                # Reading preserves the row; type validation decides exclusion during loading.
                self.assertEqual(raw[0]['rows'][-1], [str(value) for value in last])
                self.assertNotIn('footer_totals', proposal)

    def test_trailing_excel_notes_are_preserved_outside_date_records(self):
        from business_import.column_types import convert_columns
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            book = Workbook()
            sheet = book.active
            for row in [['Ngày', 'Item'], [46032, 'A'], ['10/01/2026', 'B'],
                        ['Ghi chú: số liệu ghi tay', None], ['Note: currency VND', None]]:
                sheet.append(row)
            book.save(path / 'dates.xlsx')
            book.close()
            description = {'columns': [{'name': 'Ngày', 'type': 'TIMESTAMP'}]}
            raw, _ = _read_tables(inspect_business(path), description)
            self.assertEqual(raw[0]['rows'], [['46032', 'A'], ['10/01/2026', 'B']])
            self.assertEqual(description['tables'][0]['footer_notes'], [
                {'source_row': 4, 'values': ['Ghi chú: số liệu ghi tay', None]},
                {'source_row': 5, 'values': ['Note: currency VND', None]}])
            types, values, _ = convert_columns(raw[0]['columns'], raw[0]['rows'],
                description['tables'][0]['column_types'], ['VARCHAR', 'VARCHAR'], raw[0]['excel_epoch'])
            self.assertEqual(types[0], 'TIMESTAMP')
            self.assertEqual([row[0] for row in values], [datetime(2026, 1, 10)] * 2)
            # A note-like cell with other values is still a source record.
            book = Workbook()
            sheet = book.active
            sheet.append(['Ngày', 'Item'])
            sheet.append(['Ghi chú: customer instruction', 'A'])
            sheet.append([46032, 'B'])
            book.save(path / 'dates.xlsx')
            book.close()
            raw, _ = _read_tables(inspect_business(path), {'columns': [
                {'name': 'Ngày', 'type': 'TIMESTAMP'}, {'name': 'Item', 'type': 'VARCHAR'}]})
            self.assertEqual(len(raw[0]['rows']), 2)
            self.assertEqual(raw[0]['rows'][0][1], 'A')

    def test_readme_headers_work_when_llm_omits_recommendations(self):
        from business_import.column_types import convert_columns
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            labels = ['Chương trình', 'Hàng áp dụng', 'Từ ngày', 'Đến ngày']
            (path / 'README.md').write_text('\n'.join(f'- **{label}**: documented field' for label in labels), encoding='utf-8')
            book = Workbook()
            sheet = book.active
            sheet.append(labels)
            sheet.append(['Sale', 'All', datetime(2025, 5, 1), datetime(2026, 6, 30)])
            book.save(path / 'lookup.xlsx')
            book.close()
            description = {'columns': []}
            raw, layouts = _read_tables(inspect_business(path), description)
            self.assertEqual(raw[0]['columns'], labels)
            self.assertEqual(len(raw[0]['rows']), 1)
            self.assertEqual(next(iter(layouts.values()))['header_row'], 1)
            types, values, _ = convert_columns(labels, raw[0]['rows'], description['tables'][0]['column_types'],
                                              ['VARCHAR'] * 4, raw[0]['excel_epoch'])
            self.assertEqual(types, ['VARCHAR', 'VARCHAR', 'TIMESTAMP', 'TIMESTAMP'])
            self.assertEqual(values[0][2:], [datetime(2025, 5, 1), datetime(2026, 6, 30)])

    def test_request_uses_full_readme_without_source_inventory(self):
        readme = 'Business context\n- **Ngày tháng**: ngày bán\n- **Mã**: mã hàng\n'
        columns = [{'name': 'Ngày tháng', 'type': 'TIMESTAMP'}, {'name': 'Mã', 'type': 'VARCHAR'}]
        files = [{'name': 'PRIVATE.xlsx', 'format': '.xlsx', 'tables': [
            {'name': 'PRIVATE_SHEET', 'preview': [['SECRET_VALUE']]}]}]
        result, request = self.describe(readme, columns, files)
        self.assertEqual(result['columns'], columns)
        prompt = request['messages'][0]['content']
        self.assertIn(readme, prompt)
        for private in ('PRIVATE.xlsx', 'PRIVATE_SHEET', 'SECRET_VALUE', 'header_row', 'source_file', 'source_table'):
            self.assertNotIn(private, prompt)
        schema = request['response_format']['json_schema']['schema']
        self.assertEqual(schema['required'], ['columns'])
        self.assertFalse(request['extra_body']['chat_template_kwargs']['enable_thinking'])

    def test_response_schema_constrains_exact_readme_names(self):
        result, request = self.describe('- **Khách hàng (tên)**: tên khách\n- **Giờ**: giờ đặt đơn',
                                        [{'name': 'Khách hàng (tên)', 'type': 'VARCHAR'}])
        branches = request['response_format']['json_schema']['schema']['properties']['columns']['items']['anyOf']
        for branch in branches:
            self.assertEqual(branch['properties']['name']['enum'], ['Khách hàng (tên)', 'Giờ'])
            self.assertNotIn('Khách hàng', branch['properties']['name']['enum'])
        self.assertEqual(result['columns'][0]['name'], 'Khách hàng (tên)')

    def test_empty_readme_names_constrain_empty_response(self):
        _, request = self.describe('Business notes without documented columns.', [])
        array = request['response_format']['json_schema']['schema']['properties']['columns']
        self.assertEqual(array['maxItems'], 0)

    def test_rejects_invalid_or_fabricated_recommendations(self):
        for columns in (None, ['name'], [{'name': 'Unknown', 'type': 'VARCHAR'}],
                        [{'name': 'Mã', 'type': 'SQL'}], [{'name': 'Mã', 'type': 'DATE'}],
                        [{'name': 'Mã', 'type': 'VARCHAR', 'source_file': 'x.csv'}],
                        [{'name': '', 'type': 'VARCHAR'}]):
            with self.subTest(columns=columns), self.assertRaises(ValueError):
                self.describe('- **Mã**: mã hàng', columns)

    def test_source_mapping_handles_all_text_and_undocumented_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / 'names.csv').write_text('Report\n\nTên,Trạng thái,Ghi chú\nAn,Đã bán,x\n', encoding='utf-8')
            description = {'columns': [{'name': 'Tên', 'type': 'VARCHAR'},
                                       {'name': 'Trạng thái', 'type': 'VARCHAR'}]}
            raw, layouts = _read_tables(inspect_business(path), description)
            self.assertEqual(raw[0]['columns'], ['Tên', 'Trạng thái', 'Ghi chú'])
            self.assertEqual(raw[0]['rows'], [['An', 'Đã bán', 'x']])
            self.assertEqual(layouts[('names.csv', 'names')]['header_row'], 3)
            self.assertTrue(any('Ghi chú' in warning for warning in description['tables'][0]['type_hint_warnings']))

    def test_undocumented_import_date_uses_safe_timestamp_fallback(self):
        from business_import.__main__ import _map_recommendations
        from business_import.column_types import convert_columns
        for recommendations, conflicts, expected in (
                ({}, set(), 'TIMESTAMP'),
                ({'ngày nhập': {'type': 'VARCHAR'}}, set(), 'VARCHAR'),
                ({}, {'ngày nhập'}, 'VARCHAR')):
            proposal = {'type_hint_warnings': []}
            _map_recommendations(proposal, ['Ngày nhập'], recommendations, conflicts, set())
            self.assertEqual(proposal['column_types'], [{'type': expected}])
            source = [['04/01/2025'], ['2025-01-04 00:00:00'], ['']]
            types, values, _ = convert_columns(['Ngày nhập'], source, proposal['column_types'], ['VARCHAR'])
            self.assertEqual(types, [expected])
            self.assertEqual(values, [[datetime(2025, 1, 4)], [datetime(2025, 1, 4)], [None]]
                             if expected == 'TIMESTAMP' else source)
            invalid = source + [['not a date']]
            types, values, _ = convert_columns(['Ngày nhập'], invalid, proposal['column_types'], ['VARCHAR'])
            self.assertEqual(types, ['VARCHAR'])
            self.assertEqual(values, invalid)

    def test_undocumented_date_keywords_use_validated_timestamps(self):
        from business_import.__main__ import _map_recommendations
        from business_import.column_types import convert_columns
        labels = ['Ngày bán', 'ngay_nhap', 'Thời gian giao', 'thoi_gian',
                  'Invoice Date', 'Sinh nhật', 'sinh nhat', 'birthdate', 'created_timestamp']
        for label in labels:
            with self.subTest(label=label):
                proposal = {'type_hint_warnings': []}
                _map_recommendations(proposal, [label], {}, set(), set())
                self.assertEqual(proposal['column_types'], [{'type': 'TIMESTAMP'}])
                types, values, warnings = convert_columns([label], [['04/01/2025']], proposal['column_types'], ['VARCHAR'])
                self.assertEqual(types, ['TIMESTAMP'])
                self.assertEqual(values, [[datetime(2025, 1, 4)]])
                self.assertFalse(warnings)
                source = [['30'], ['60']]
                types, values, warnings = convert_columns([label], source, proposal['column_types'], ['VARCHAR'])
                self.assertEqual(types, ['VARCHAR'])
                self.assertEqual(values, source)
                self.assertTrue(warnings)
                proposal = {'type_hint_warnings': []}
                _map_recommendations(proposal, [label], {label.casefold(): {'type': 'VARCHAR'}}, set(), set())
                self.assertEqual(proposal['column_types'], [{'type': 'VARCHAR'}])
        proposal = {'type_hint_warnings': []}
        _map_recommendations(proposal, ['update', 'candidate', 'Qty'], {}, set(), set())
        self.assertEqual(proposal['column_types'], [{'type': 'VARCHAR'}] * 3)

    def test_single_documented_column(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / 'names.csv').write_text('Tên\nAn\nBình\n', encoding='utf-8')
            description = {'columns': [{'name': 'Tên', 'type': 'VARCHAR'}]}
            raw, _ = _read_tables(inspect_business(path), description)
            self.assertEqual(raw[0]['rows'], [['An'], ['Bình']])

    def test_partial_dictionary_keeps_sixteen_columns_and_duplicate_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            labels = ['Mã', 'Mã', 'Tên', 'Giá', 'Nhóm', 'Đơn vị'] + ['extra' + str(i) for i in range(10)]
            with (path / 'items.csv').open('w', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerows([labels, ['001', '002', 'Bút', '12', 'A', 'cái'] + ['x'] * 10])
            description = {'columns': [{'name': label, 'type': 'VARCHAR'} for label in labels[:6]]}
            raw, _ = _read_tables(inspect_business(path), description)
            self.assertEqual(len(raw[0]['columns']), 16)
            self.assertEqual(raw[0]['columns'][:2], ['Mã', 'Mã__2'])
            self.assertEqual(len(raw[0]['rows'][0]), 16)

    def test_conflicting_types_and_unused_names_warn(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / 'items.csv').write_text('Mã,Qty\n001,2\n', encoding='utf-8')
            description = {'columns': [{'name': 'Mã', 'type': 'VARCHAR'}, {'name': 'Mã', 'type': 'BIGINT'},
                                       {'name': 'Qty', 'type': 'BIGINT'}, {'name': 'Missing', 'type': 'VARCHAR'}]}
            raw, _ = _read_tables(inspect_business(path), description)
            self.assertEqual(raw[0]['rows'], [['001', '2']])
            proposal = description['tables'][0]
            self.assertEqual(proposal['column_types'], [{'type': 'VARCHAR'}, {'type': 'BIGINT'}])
            warnings = ' '.join(proposal['type_hint_warnings'])
            self.assertIn('conflict', warnings)
            self.assertIn('Missing', warnings)

    def test_ambiguous_headers_and_lone_preamble_match_fail(self):
        for rows in ([['Tên', 'Mã'], ['An', 'A'], ['Mã', 'Tên'], ['B', 'Bình']],
                     [['Tên', 'report context'], [], ['Other', 'Unknown'], ['An', 'Bình']]):
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as temp:
                path = Path(temp)
                with (path / 'x.csv').open('w', encoding='utf-8', newline='') as stream:
                    csv.writer(stream).writerows(rows)
                with self.assertRaisesRegex(ValueError, 'header|layout'):
                    _read_tables(inspect_business(path), {'columns': [
                        {'name': 'Tên', 'type': 'VARCHAR'}, {'name': 'Mã', 'type': 'VARCHAR'}]})

    def test_later_stronger_match_cannot_discard_earlier_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / 'x.csv').write_text('Mã,Notes\n001,A\nTên,Qty\nB,2\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'header|layout'):
                _read_tables(inspect_business(path), {'columns': [
                    {'name': 'Mã', 'type': 'VARCHAR'}, {'name': 'Tên', 'type': 'VARCHAR'},
                    {'name': 'Qty', 'type': 'BIGINT'}]})

    def test_expense_preamble_with_mixed_dates_and_no_model_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            book = Workbook()
            sheet = book.active
            sheet.append(['SỔ CHI PHÍ CỬA HÀNG'])
            sheet.append([])
            sheet.append(['Ngày mua', 'Lưu ý', 'Tiền chi', 'Nhóm'])
            records = [[45717, None, '50000k', 'thuê'],
                       ['05/03/2025', None, '11,067tr', 'điện'],
                       [45721, None, '1,976tr', 'nước'],
                       ['31/03/2025', None, '78,361tr', 'lương']]
            for row in records:
                sheet.append(row)
            book.save(path / 'SoTay_ChiPhi.xlsx')
            book.close()
            raw, layouts = _read_tables(inspect_business(path), {'columns': []})
            self.assertEqual(layouts[('SoTay_ChiPhi.xlsx', 'Sheet')]['header_row'], 3)
            self.assertEqual(raw[0]['columns'], ['Ngày mua', 'Lưu ý', 'Tiền chi', 'Nhóm'])
            self.assertEqual(raw[0]['rows'], [
                ['45717', None, '50000k', 'thuê'],
                ['05/03/2025', None, '11,067tr', 'điện'],
                ['45721', None, '1,976tr', 'nước'],
                ['31/03/2025', None, '78,361tr', 'lương']])

    def test_header_after_preview_limit_and_exact_repeats(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            rows = [['Report']] + [[]] * 260 + [['Mã', 'Qty'], ['001', '2'], ['Mã', 'Qty'], ['002', '3']]
            with (path / 'x.csv').open('w', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerows(rows)
            description = {'columns': [{'name': 'Mã', 'type': 'VARCHAR'}, {'name': 'Qty', 'type': 'BIGINT'}]}
            raw, layouts = _read_tables(inspect_business(path), description)
            self.assertEqual(raw[0]['rows'], [['001', '2'], ['002', '3']])
            self.assertEqual(layouts[('x.csv', 'x')]['header_row'], 262)
            self.assertEqual(layouts[('x.csv', 'x')]['repeated_header_count'], 1)

    def test_sqlite_only_cli_skips_llm_and_preserves_native_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'data'
            path = root / 'biz_1'
            path.mkdir(parents=True)
            (path / 'README.md').write_text('**Qty**: quantity', encoding='utf-8')
            for filename in ('one.sqlite', 'two.sqlite'):
                with closing(sqlite3.connect(path / filename)) as db:
                    db.execute('CREATE TABLE items (Qty INTEGER, code TEXT, payload BLOB, mixed)')
                    db.executemany('INSERT INTO items VALUES (?, ?, ?, ?)',
                                   [(1, '001', b'abc', 1), (2, '002', None, 'one')])
                    db.commit()
            database = Path(temp) / 'out.duckdb'
            errors = StringIO()
            with patch('business_import.llm.create_client', side_effect=AssertionError('must skip client')), patch('business_import.llm.describe_business', side_effect=AssertionError('must skip LLM')), redirect_stdout(StringIO()), redirect_stderr(errors):
                self.assertEqual(main(['--roots', str(root), '--db', str(database)]), 0, errors.getvalue())
            with duckdb.connect(str(database)) as conn:
                for table in ('one__items', 'two__items'):
                    rows = conn.execute(f'SELECT Qty, code, payload FROM biz_1.{table}').fetchall()
                    self.assertEqual(rows, [(1, '001', b'abc'), (2, '002', None)])
                    self.assertEqual(conn.execute(f'DESCRIBE biz_1.{table}').fetchone()[1], 'BIGINT')

    def test_sqlite_ignores_readme_type_recommendations_in_mixed_business(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            with closing(sqlite3.connect(path / 'native.sqlite')) as db:
                db.execute('CREATE TABLE items (Qty TEXT)')
                db.execute("INSERT INTO items VALUES ('2')")
                db.commit()
            (path / 'items.csv').write_text('Qty\n3\n', encoding='utf-8')
            description = {'columns': [{'name': 'Qty', 'type': 'BIGINT'}]}
            raw, _ = _read_tables(inspect_business(path), description)
            native = next(t for t in description['tables'] if t['source_file'] == 'native.sqlite')
            csv_table = next(t for t in description['tables'] if t['source_file'] == 'items.csv')
            self.assertNotIn('column_types', native)
            self.assertEqual(native['type_hint_warnings'], [])
            self.assertEqual(csv_table['column_types'], [{'type': 'BIGINT'}])
            self.assertEqual(next(t for t in raw if t['format'] == '.sqlite')['rows'], [['2']])

    def test_cli_multisheet_dates_sqlite_rerun_and_failed_reimport(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'data'
            path = root / 'biz_1'
            path.mkdir(parents=True)
            readme = '- **Ngày**: ngày bán\n- **Mã**: mã đơn\n- **Qty**: số lượng\n'
            (path / 'README.md').write_text(readme, encoding='utf-8')
            book = Workbook()
            for i, sheet in enumerate([book.active, book.create_sheet('Tháng 2')]):
                sheet.append(['SỔ CHI TIẾT BÁN HÀNG'])
                sheet.append(['Kỳ báo cáo: toàn bộ', 'Kênh bán: Tất cả'])
                sheet.append([])
                sheet.append(['Ngày', 'Mã', 'Qty'])
                sheet.append(['01/03/2025', '001', 2 + i])
            book.create_sheet('Empty')
            book.save(path / 'sales.xlsx')
            book.close()
            (path / 'empty.csv').touch()
            with closing(sqlite3.connect(path / 'native.sqlite')) as db:
                db.execute('CREATE TABLE items (Qty INTEGER, mixed)')
                db.executemany('INSERT INTO items VALUES (?, ?)', [(1, 1), (2, 'one')])
                db.commit()
            columns = [{'name': 'Ngày', 'type': 'TIMESTAMP'}, {'name': 'Mã', 'type': 'VARCHAR'},
                       {'name': 'Qty', 'type': 'BIGINT'}]
            requests = []
            database = Path(temp) / 'out.duckdb'
            args = ['--roots', str(root), '--db', str(database)]
            for _ in range(2):
                errors = StringIO()
                with patch('business_import.llm.create_client', return_value=response_client({'columns': columns}, requests)), patch('business_import.llm.OUTPUT_DIR', Path(temp) / 'responses'), redirect_stdout(StringIO()), redirect_stderr(errors):
                    self.assertEqual(main(args), 0, errors.getvalue())
            self.assertEqual(len(requests), 2)
            self.assertNotIn('Kỳ báo cáo', requests[0]['messages'][0]['content'])
            with duckdb.connect(str(database)) as conn:
                self.assertEqual(conn.execute('SELECT * FROM biz_1.sales__Sheet').fetchall(), [(datetime(2025, 3, 1), '001', 2)])
                self.assertEqual([row[1] for row in conn.execute('DESCRIBE biz_1.sales__Sheet').fetchall()], ['TIMESTAMP', 'VARCHAR', 'BIGINT'])
                self.assertEqual(conn.execute('SELECT Qty FROM biz_1."sales__Tháng_2"').fetchall(), [(3,)])
                self.assertEqual(conn.execute('SELECT count(*) FROM biz_1.sales__Empty').fetchone(), (0,))
                self.assertEqual(conn.execute('SELECT count(*) FROM biz_1.empty').fetchone(), (0,))
                self.assertEqual(conn.execute('SELECT Qty FROM biz_1.native__items').fetchall(), [(1,), (2,)])
                self.assertEqual(conn.execute('SELECT count(*) FROM _catalog.tables').fetchone(), (5,))
            (path / 'bad.csv').write_text('Mã,Qty\n001,2\nQty,Mã\n3,002\n', encoding='utf-8')
            with patch('business_import.llm.create_client', return_value=response_client({'columns': columns}, [])), patch('business_import.llm.OUTPUT_DIR', Path(temp) / 'responses'), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(args), 1)
            with duckdb.connect(str(database)) as conn:
                self.assertEqual(conn.execute('SELECT Qty FROM biz_1.sales__Sheet').fetchall(), [(2,)])


if __name__ == '__main__':
    unittest.main()
