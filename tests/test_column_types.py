import json
import unittest
from datetime import date, datetime
from decimal import Decimal

import duckdb

from business_import.database import load_business
from business_import.sql import build_load_plan


class ColumnTypeTests(unittest.TestCase):
    def load(self, rows, specs):
        conn = duckdb.connect(":memory:")
        self.addCleanup(conn.close)
        columns = [f"c{i}" for i in range(len(specs))]
        raw = [{"source_file": "sales.csv", "source_table": "sales", "format": ".csv", "columns": columns, "rows": rows}]
        description = {"tables": [{"source_file": "sales.csv", "source_table": "sales", "column_types": specs}]}
        plan = build_load_plan([{"name": "_stage_0", "columns": columns, "destination": "sales"}], "biz_1")
        load_business(conn, {"id": "biz_1", "split": "train"}, description, raw, {}, plan)
        return conn

    def test_loads_validated_types_without_raw_duplicate(self):
        conn = self.load([["1 thg 6 2025", "1.234,50", "12", "true", "2025-06-01 12:30:00"]], [
            {"type": "DATE", "format": "%d thg %m %Y"},
            {"type": "DECIMAL", "decimal_separator": ",", "thousands_separator": "."},
            {"type": "BIGINT"}, {"type": "BOOLEAN"}, {"type": "TIMESTAMP", "format": "%Y-%m-%d %H:%M:%S"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchone(),
                         (datetime(2025, 6, 1), Decimal("1234.50"), 12, True, datetime(2025, 6, 1, 12, 30)))
        types = [row[1] for row in conn.execute("DESCRIBE biz_1.sales").fetchall()]
        self.assertEqual(types, ["TIMESTAMP", "DECIMAL(38,10)", "BIGINT", "BOOLEAN", "TIMESTAMP"])
        self.assertEqual(conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='biz_1'").fetchall(), [("sales",)])
        stored, warnings = conn.execute("SELECT description_json, warnings FROM _catalog.businesses").fetchone()
        self.assertEqual(json.loads(stored)["tables"][0]["actual_types"], types)
        self.assertEqual(json.loads(warnings), [])

    def test_invalid_late_values_and_padded_ids_keep_entire_column(self):
        rows = [["2025-01-01", "00123", "10"], ["2025-02-30", "00456", "oops"]]
        conn = self.load(rows, [{"type": "DATE", "format": "%Y-%m-%d"}, {"type": "BIGINT"}, {"type": "BIGINT"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(), [tuple(row) for row in rows])
        self.assertEqual([row[1] for row in conn.execute("DESCRIBE biz_1.sales").fetchall()], ["VARCHAR"] * 3)
        warnings = json.loads(conn.execute("SELECT warnings FROM _catalog.businesses").fetchone()[0])
        self.assertEqual(len(warnings), 3)

    def test_no_loss_from_blanks_overflow_precision_or_ambiguous_grouping(self):
        values = ["", "9223372036854775808", "0.12345678901", "12,34", "NaN", "01"]
        specs = [{"type": "BIGINT"}, {"type": "BIGINT"}, {"type": "DECIMAL"},
                 {"type": "DECIMAL", "thousands_separator": ","}, {"type": "DECIMAL"}, {"type": "DECIMAL"}]
        conn = self.load([values], specs)
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchone(), tuple(values))
        self.assertEqual([row[1] for row in conn.execute("DESCRIBE biz_1.sales").fetchall()], ["VARCHAR"] * len(values))

    def test_repeated_grouping_corrects_reversed_model_separators(self):
        rows = [["35,000"], ["65,000"], ["1,000,000"]]
        conn = self.load(rows, [{"type": "DECIMAL", "decimal_separator": ",", "thousands_separator": "."}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(),
                         [(Decimal("35000"),), (Decimal("65000"),), (Decimal("1000000"),)])
        self.assertEqual(conn.execute("DESCRIBE biz_1.sales").fetchone()[1], "DECIMAL(38,10)")
        warnings = json.loads(conn.execute("SELECT warnings FROM _catalog.businesses").fetchone()[0])
        self.assertTrue(any("separator" in warning for warning in warnings))

    def test_grouping_correction_still_checks_every_value(self):
        rows = [["35,000"], ["1,000,000"], ["12,34"]]
        conn = self.load(rows, [{"type": "DECIMAL", "decimal_separator": ",", "thousands_separator": "."}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(), [tuple(row) for row in rows])
        self.assertEqual(conn.execute("DESCRIBE biz_1.sales").fetchone()[1], "VARCHAR")

    def test_mixed_date_encodings_normalize_to_date_not_timestamp(self):
        from business_import.column_types import convert_columns
        rows = [["01/10/2025"], ["2025-10-02T00:00:00"], [None]]
        for spec in ({"type": "DATE", "formats": ["%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"]},):
            types, values, warnings = convert_columns(["Ngày đặt hàng"], rows, [spec], ["VARCHAR"])
            self.assertEqual(types, ["TIMESTAMP"])
            self.assertEqual(values, [[datetime(2025, 10, 1)], [datetime(2025, 10, 2)], [None]])
            self.assertFalse(warnings)

    def test_import_date_accepts_mixed_vietnamese_formats(self):
        from business_import.column_types import convert_columns
        source = [["01/03/2025"], ["1 thg 3 2025"], [None]]
        for spec in ({"type": "DATE", "formats": ["%d/%m/%Y", "%d thg %m %Y"]},):
            with self.subTest(spec=spec):
                types, values, warnings = convert_columns(["Received on"], source, [spec], ["VARCHAR"])
                self.assertEqual(types, ["TIMESTAMP"])
                self.assertEqual(values, [[datetime(2025, 3, 1)], [datetime(2025, 3, 1)], [None]])
                self.assertFalse(warnings)
                invalid = source + [["31/02/2025"]]
                types, values, warnings = convert_columns(["Received on"], invalid, [spec], ["VARCHAR"])
                self.assertEqual(types, ["VARCHAR"])
                self.assertEqual(values, invalid)
                self.assertTrue(warnings)

    def test_excel_serial_requires_verified_epoch(self):
        from business_import.column_types import convert_columns
        spec = {"type": "TIMESTAMP", "formats": ["excel_serial", "%d/%m/%Y"]}
        source = [["45717"], ["05/03/2025"], [None]]
        types, rows, warnings = convert_columns(["anything"], source, [spec], ["VARCHAR"], excel_epoch="1899-12-30")
        self.assertEqual(types, ["TIMESTAMP"])
        self.assertEqual(rows, [[datetime(2025, 3, 1)], [datetime(2025, 3, 5)], [None]])
        self.assertFalse(warnings)
        types, rows, warnings = convert_columns(["anything"], source, [spec], ["VARCHAR"])
        self.assertEqual(types, ["VARCHAR"])
        self.assertEqual(rows, source)
        self.assertTrue(warnings)
        types, rows, warnings = convert_columns(["anything"], [["1.5"]], [spec], ["VARCHAR"], excel_epoch="1904-01-01")
        self.assertEqual(rows, [[datetime(1904, 1, 2, 12)]])
        for value in ("60", "-1", "NaN", "1e400"):
            types, rows, warnings = convert_columns(["anything"], [[value]], [spec], ["VARCHAR"], excel_epoch="1899-12-30")
            self.assertEqual(types, ["VARCHAR"])

    def test_date_keywords_override_non_temporal_hints_locally(self):
        from business_import.column_types import convert_columns
        source = [["01/03/2025"], ["1 thg 3 2025"], ["2025-03-01T06:02:07"], [None]]
        for column in ("Ngày nhập", "Ghi nhận THỜI GIAN", "Giờ tạo", "Sinh nhật",
                       "Hạn sử dụng", "purchase_date", "created_at", "NGAY MUA", "thoi gian tao"):
            for specs in (None, [{"type": "VARCHAR"}], [{"type": "BIGINT"}]):
                with self.subTest(column=column, specs=specs):
                    types, rows, warnings = convert_columns([column], source, specs, ["VARCHAR"])
                    self.assertEqual(types, ["TIMESTAMP"])
                    self.assertEqual(rows, [[datetime(2025, 3, 1)], [datetime(2025, 3, 1)],
                                            [datetime(2025, 3, 1, 6, 2, 7)], [None]])
                    self.assertFalse(warnings)

    def test_date_keyword_rule_preserves_unsafe_columns(self):
        from business_import.column_types import convert_columns
        for source in ([["01/03/2025"], ["bad"]], [["06:02:07"]], [["2025-03-01T06:02:07+07:00"]]):
            types, rows, warnings = convert_columns(["Ngày nhập"], source, None, ["VARCHAR"])
            self.assertEqual(types, ["VARCHAR"])
            self.assertEqual(rows, source)
            self.assertTrue(warnings)
        self.assertEqual(convert_columns(["candidate"], [["001"]], None, ["VARCHAR"]),
                         (["VARCHAR"], [["001"]], []))

    def test_date_keyword_rule_uses_verified_excel_epoch(self):
        from business_import.column_types import convert_columns
        source = [["45717"], ["05/03/2025"]]
        types, rows, warnings = convert_columns(["Ngày mua"], source, None, ["VARCHAR"], excel_epoch="1899-12-30")
        self.assertEqual(types, ["TIMESTAMP"])
        self.assertEqual(rows, [[datetime(2025, 3, 1)], [datetime(2025, 3, 5)]])
        self.assertFalse(warnings)

    def test_date_formats_validation_and_ambiguity(self):
        from business_import.column_types import validate_specs, convert_columns
        for formats in ([], "bad", [1], [""], ["x" * 101]):
            with self.subTest(formats=formats), self.assertRaises(ValueError):
                validate_specs([{"type": "DATE", "formats": formats}], 1)
        for spec in ({"type": "VARCHAR", "formats": ["%Y-%m-%d"]},
                     {"type": "DATE", "format": "%Y-%m-%d", "formats": ["%Y-%m-%d"]}):
            with self.assertRaises(ValueError):
                validate_specs([spec], 1)
        source = [["01/03/2025"]]
        types, values, warnings = convert_columns(["anything"], source,
            [{"type": "DATE", "formats": ["%d/%m/%Y", "%m/%d/%Y"]}], ["VARCHAR"])
        self.assertEqual(types, ["VARCHAR"])
        self.assertEqual(values, source)
        self.assertTrue(warnings)

    def test_real_time_is_never_discarded_by_date_normalization(self):
        from business_import.column_types import convert_columns
        types, rows, _ = convert_columns(["Ngày đặt hàng"], [["2025-10-01T12:30:00"]],
                                         [{"type": "TIMESTAMP", "format": "%Y-%m-%dT%H:%M:%S"}], ["VARCHAR"])
        self.assertEqual(types, ["TIMESTAMP"])
        self.assertEqual(rows, [[datetime(2025, 10, 1, 12, 30)]])

    def test_vietnamese_money_notation_in_one_column(self):
        from business_import.column_types import convert_columns
        rows = [["135k"], ["51.000đ"], ["35,000"], ["53500"], ["50.500đ"], ["1.5k"], [None]]
        types, values, warnings = convert_columns(["Tổng tiền"], rows, [{"type": "DECIMAL", "format": "VND"}], ["VARCHAR"])
        self.assertEqual(types, ["DECIMAL(38,10)"])
        self.assertEqual(values, [[Decimal(v)] if v is not None else [None]
                                 for v in ("135000", "51000", "35000", "53500", "50500", "1500", None)])
        self.assertFalse(warnings)

    def test_million_suffix_uses_decimal_comma(self):
        from business_import.column_types import convert_columns
        types, rows, warnings = convert_columns(
            ["Tổng tiền"], [["1,287tr"], ["2tr"], ["1.5TR"], [None]],
            [{"type": "DECIMAL", "format": "VND"}], ["VARCHAR"])
        self.assertEqual(types, ["DECIMAL(38,10)"])
        self.assertEqual(rows, [[Decimal("1287000")], [Decimal("2000000")], [Decimal("1500000")], [None]])
        self.assertFalse(warnings)

    def test_money_columns_normalize_currency_suffixes(self):
        from business_import.column_types import convert_columns
        source = [["17k"], ["1,287tr  "], ["1.98 tr"], ["13.000đ"], [None]]
        expected = [[Decimal("17000")], [Decimal("1287000")], [Decimal("1980000")], [Decimal("13000")], [None]]
        for column in ("Chi phí nguyên liệu", "Tiền phải trả", "Purchase cost"):
            with self.subTest(column=column):
                spec = {"type": "DECIMAL", "format": "VND"}
                types, values, warnings = convert_columns([column], source, [spec], ["VARCHAR"])
                self.assertEqual(types, ["DECIMAL(38,10)"])
                self.assertEqual(values, expected)
                self.assertFalse(warnings)
                invalid = source + [["25k + 5k"]]
                types, values, warnings = convert_columns([column], invalid, [spec], ["VARCHAR"])
                self.assertEqual(types, ["VARCHAR"])
                self.assertEqual(values, invalid)
                self.assertTrue(warnings)

    def test_decimal_hint_without_vnd_format_loads_currency_notation(self):
        source = [["17k"], ["1,287tr  "], ["1.98 tr"], ["13.000đ"], ["35000"], [None]]
        expected = [(Decimal(v),) if v is not None else (None,)
                    for v in ("17000", "1287000", "1980000", "13000", "35000", None)]
        for spec in ({"type": "DECIMAL"},
                     {"type": "DECIMAL", "decimal_separator": ",", "thousands_separator": ""}):
            with self.subTest(spec=spec):
                conn = self.load(source, [spec])
                self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(), expected)
                self.assertEqual(conn.execute("DESCRIBE biz_1.sales").fetchone()[1], "DECIMAL(38,10)")

    def test_automatic_currency_parsing_preserves_unsafe_whole_columns(self):
        from business_import.column_types import convert_columns
        for invalid in ("25k + 5k", "USD 10", "00123", "", "1e40tr"):
            source = [["17k"], [invalid]]
            with self.subTest(invalid=invalid):
                types, values, warnings = convert_columns(["any name"], source, [{"type": "DECIMAL"}], ["VARCHAR"])
                self.assertEqual(types, ["VARCHAR"])
                self.assertEqual(values, source)
                self.assertTrue(warnings)

    def test_money_names_and_suffixes_do_not_override_varchar(self):
        from business_import.column_types import convert_columns
        for column in ("Tổng tiền", "Thành tiền", "Giá nhập", "Mã hàng"):
            rows = [["17K"], ["1,287tr"]]
            types, values, warnings = convert_columns([column], rows, [{"type": "VARCHAR"}], ["VARCHAR"])
            self.assertEqual(types, ["VARCHAR"])
            self.assertEqual(values, rows)
            self.assertFalse(warnings)

    def test_vnd_grouped_amounts_and_zero_load_as_numbers(self):
        conn = self.load([["17.000"], ["8,000.000"], ["8.000,000đ"], ["0"], [0], [None]],
                         [{"type": "DECIMAL", "format": "VND"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(),
                         [(Decimal("17000"),), (Decimal("8000000"),), (Decimal("8000000"),),
                          (Decimal("0"),), (Decimal("0"),), (None,)])
        self.assertEqual(conn.execute("DESCRIBE biz_1.sales").fetchone()[1], "DECIMAL(38,10)")

    def test_plain_decimal_missing_separator_accepts_comma_grouping(self):
        conn = self.load([["11,000"], ["9,200"], ["0"], [None]], [{"type": "DECIMAL"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(),
                         [(Decimal("11000"),), (Decimal("9200"),), (Decimal("0"),), (None,)])

    def test_missing_separator_accepts_grouped_decimal_places(self):
        conn = self.load([["8,000.0000"], ["2,070,000.0000"], ["8,377.1250"], ["0"], [None]],
                         [{"type": "DECIMAL"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(),
                         [(Decimal("8000"),), (Decimal("2070000"),), (Decimal("8377.125"),), (Decimal("0"),), (None,)])

    def test_date_hint_with_iso_time_promotes_whole_column_to_timestamp(self):
        conn = self.load([["2025-03-01T06:02:07"], ["02/03/2025"], [None]],
                         [{"type": "DATE", "format": "%d/%m/%Y"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(),
                         [(datetime(2025, 3, 1, 6, 2, 7),), (datetime(2025, 3, 2),), (None,)])
        self.assertEqual(conn.execute("DESCRIBE biz_1.sales").fetchone()[1], "TIMESTAMP")

    def test_iso_timestamp_promotion_still_validates_whole_column(self):
        for invalid in ("2025-02-30T06:02:07", "2025-03-01T06:02:07+07:00", "oops"):
            with self.subTest(invalid=invalid):
                source = [["2025-03-01T06:02:07.123456"], [invalid]]
                conn = self.load(source, [{"type": "DATE", "format": "%Y-%m-%d"}])
                self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(), [tuple(row) for row in source])

    def test_quantity_units_are_not_discarded(self):
        conn = self.load([["1 lon"], ["2 chai"], ["1 kg"]], [{"type": "BIGINT"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(),
                         [("1 lon",), ("2 chai",), ("1 kg",)])

    def test_zero_only_numeric_columns_remain_numeric(self):
        for spec in ({"type": "DECIMAL", "format": "VND"}, {"type": "DECIMAL"}, {"type": "BIGINT"}):
            with self.subTest(spec=spec):
                conn = self.load([["0"], [0], [None]], [spec])
                self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(), [(0,), (0,), (None,)])
                self.assertNotEqual(conn.execute("DESCRIBE biz_1.sales").fetchone()[1], "VARCHAR")

    def test_vnd_malformed_mixed_grouping_preserves_column(self):
        from business_import.column_types import convert_columns
        for value in ("8,00.000", "8,000.0.00", "08,000.000"):
            source = [["17.000"], [value], ["0"]]
            with self.subTest(value=value):
                types, rows, warnings = convert_columns(["cost"], source,
                    [{"type": "DECIMAL", "format": "VND"}], ["VARCHAR"])
                self.assertEqual(types, ["VARCHAR"])
                self.assertEqual(rows, source)
                self.assertTrue(warnings)

    def test_vnd_hint_loads_into_database(self):
        conn = self.load([["17k"], ["1.98 tr"], [None]], [{"type": "DECIMAL", "format": "VND"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(),
                         [(Decimal("17000"),), (Decimal("1980000"),), (None,)])

    def test_invalid_numeric_formats_are_rejected(self):
        from business_import.column_types import validate_specs
        for spec in ({"type": "BIGINT", "format": "VND"}, {"type": "VARCHAR", "format": "VND"},
                     {"type": "DECIMAL", "format": "USD"},
                     {"type": "DECIMAL", "format": "VND", "thousands_separator": ","}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                validate_specs([spec], 1)

    def test_k_suffix_does_not_round_large_decimal(self):
        from business_import.column_types import convert_columns
        value = "1234567890123456789012345.1234567k"
        types, rows, _ = convert_columns(["Tổng tiền"], [[value]], [{"type": "DECIMAL", "format": "VND"}], ["VARCHAR"])
        self.assertEqual(types, ["DECIMAL(38,10)"])
        self.assertEqual(rows, [[Decimal("1234567890123456789012345123.4567")]])

    def test_invalid_currency_expression_keeps_whole_column(self):
        from business_import.column_types import convert_columns
        rows = [["135k"], ["51.000đ"], ["25k + topping 5k"]]
        types, values, warnings = convert_columns(["Tổng tiền"], rows, [{"type": "DECIMAL", "format": "VND"}], ["VARCHAR"])
        self.assertEqual(types, ["VARCHAR"])
        self.assertEqual(values, rows)
        self.assertTrue(warnings)

    def test_excel_style_date_format_and_nulls_load_as_date(self):
        conn = self.load([["01/04/2025"], [None], ["31/12/2025"]], [{"type": "DATE", "format": "dd/mm/yyyy"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(),
                         [(datetime(2025, 4, 1),), (None,), (datetime(2025, 12, 31),)])
        self.assertEqual(conn.execute("DESCRIBE biz_1.sales").fetchone()[1], "TIMESTAMP")
        self.assertEqual(json.loads(conn.execute("SELECT warnings FROM _catalog.businesses").fetchone()[0]), [])

    def test_invalid_date_with_excel_format_still_preserves_entire_column(self):
        conn = self.load([["01/04/2025"], [None], ["31/02/2025"]], [{"type": "DATE", "format": "dd/mm/yyyy"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(), [("01/04/2025",), (None,), ("31/02/2025",)])
        self.assertEqual(conn.execute("DESCRIBE biz_1.sales").fetchone()[1], "VARCHAR")

    def test_date_format_cannot_discard_trailing_time(self):
        conn = self.load([["2025-06-01 12:30"]], [{"type": "DATE", "format": "%Y-%m-%d %H:%M"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchone(), (datetime(2025, 6, 1, 12, 30),))

    def test_malformed_date_format_keeps_text_with_warning(self):
        conn = self.load([["2025-06-01 2025"]], [{"type": "DATE", "format": "%Y-%m-%d %Y"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchone(), ("2025-06-01 2025",))
        self.assertTrue(json.loads(conn.execute("SELECT warnings FROM _catalog.businesses").fetchone()[0]))

    def test_mixed_sqlite_types_ignore_numeric_hint_with_warning(self):
        conn = duckdb.connect(":memory:")
        self.addCleanup(conn.close)
        raw = [{"source_file": "mixed.sqlite", "source_table": "t", "format": ".sqlite", "columns": ["v"],
                "column_types": [["int", "str"]], "declared_types": [""], "rows": [[1], ["2"]]}]
        description = {"tables": [{"source_file": "mixed.sqlite", "source_table": "t", "column_types": [{"type": "BIGINT"}]}]}
        plan = build_load_plan([{"name": "_stage_0", "columns": ["v"], "destination": "mixed__t"}], "biz_1")
        load_business(conn, {"id": "biz_1", "split": "train"}, description, raw, {}, plan)
        values = [json.loads(row[0]) for row in conn.execute("SELECT * FROM biz_1.mixed__t").fetchall()]
        self.assertEqual(values, [{"$type": "int", "value": 1}, {"$type": "str", "value": "2"}])
        self.assertEqual(description["tables"][0]["actual_types"], ["VARCHAR"])
        self.assertTrue(description["tables"][0]["type_warnings"])

    def test_invalid_type_specification_cannot_be_sql(self):
        with self.assertRaises(ValueError):
            self.load([["1"]], [{"type": "INTEGER); DROP SCHEMA biz_1 CASCADE; --"}])

    def test_varchar_proposal_keeps_original_text(self):
        conn = self.load([["001"]], [{"type": "VARCHAR"}])
        self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchone(), ("001",))
