import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb
from openpyxl import Workbook

from business_import.database import load_business
from business_import.layouts import validate_layout
from business_import.llm import describe_business, generate_load_sql
from business_import.sources import discover_businesses, inspect_business, read_csv_rows, read_sqlite_rows
from business_import.sql import build_load_plan, validate_load_plan
from business_import.__main__ import _read_tables


class SourceTests(unittest.TestCase):
    def test_missing_roots_fail_instead_of_importing_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "root"):
                discover_businesses([Path(temp) / "missing"])

    def test_symlinked_business_outside_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            root.mkdir()
            outside = Path(temp) / "outside"
            outside.mkdir()
            (outside / "README.md").write_text("private")
            try:
                (root / "biz_1").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "outside|link"):
                discover_businesses([root])

    def test_csv_preserves_empty_string_distinct_from_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.csv"
            path.write_text('id,value\n1,""\n2,\n', encoding="utf-8")
            rows, _ = read_csv_rows(path)
            self.assertEqual(rows, [["1", ""], ["2", ""]])

    def test_sqlite_mixed_values_remain_distinguishable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mixed.sqlite"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE t (v)")
                db.executemany("INSERT INTO t VALUES (?)", [(1,), ("1",), (sqlite3.Binary(b"a"),), ("base64:YQ==",)])
                db.commit()
            finally:
                db.close()
            rows, _ = read_sqlite_rows(path, "t")
            from business_import.__main__ import _sqlite_text
            encoded = [_sqlite_text(row[0]) for row in rows]
            self.assertEqual(len(set(encoded)), 4)

    def test_empty_csv_can_be_loaded_without_header_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            from business_import.__main__ import _read_tables, _offline_description
            path = Path(temp) / "empty.csv"
            path.write_text("", encoding="utf-8")
            inventory = {"path": Path(temp), "files": [{"name": path.name, "format": ".csv", "delimiter": ",", "tables": [{"name": "empty", "row_count": 0, "preview": []}]}]}
            description = _offline_description(inventory)
            raw, _ = _read_tables(inventory, description)
            self.assertEqual(raw[0]["rows"], [])
            self.assertEqual(raw[0]["columns"], ["column_1"])

    def test_sqlite_values_keep_native_python_types_in_destination(self):
        conn = duckdb.connect(":memory:")
        business = {"id": "biz_1", "split": "train", "readme": ""}
        raw = [{"source_file": "native.sqlite", "source_table": "sample", "format": ".sqlite", "columns": ["value"], "column_types": [["int"]], "declared_types": ["INTEGER"], "rows": [[1], [2]]}]
        plan = build_load_plan([{"name": "_stage_0", "columns": ["value"], "destination": "native__sample"}], "biz_1")
        load_business(conn, business, {}, raw, {}, plan)
        self.assertEqual(conn.execute("SELECT value + 1 FROM biz_1.native__sample ORDER BY value").fetchall(), [(2,), (3,)])
        conn.close()

    def test_layout_rejects_column_loss_and_skipped_records(self):
        rows = [["id", "amount"], ["001", "10"], ["002", "20"]]
        with self.assertRaises(ValueError):
            validate_layout(rows, {"header_row": 1, "data_start_row": 2, "columns": [{"position": 1, "label": "id"}], "preamble_rows": []})
        with self.assertRaises(ValueError):
            validate_layout(rows, {"header_row": 1, "data_start_row": 3, "columns": [{"position": 1, "label": "id"}, {"position": 2, "label": "amount"}], "preamble_rows": []})

    def test_layout_rejects_records_wider_than_header(self):
        rows = [["id", "name"], ["001", "Alice", "private-note"]]
        with self.assertRaisesRegex(ValueError, "width|column"):
            validate_layout(rows, {"header_row": 1, "data_start_row": 2,
                "columns": [{"position": 1, "label": "id"}, {"position": 2, "label": "name"}], "preamble_rows": []})

    def test_layout_rejects_data_row_misidentified_as_header(self):
        rows = [["id", "amount"], ["001", "10"], ["002", "20"]]
        with self.assertRaisesRegex(ValueError, "candidate|structural"):
            validate_layout(rows, {"header_row": 2, "data_start_row": 3,
                "columns": [{"position": 1, "label": "001"}, {"position": 2, "label": "10"}], "preamble_rows": [1]})

    def test_offline_blank_xlsx_header_uses_original_cell_value(self):
        from business_import.__main__ import _offline_description
        inventory = {"path": Path("."), "files": [{"name": "x.xlsx", "format": ".xlsx", "tables": [{"name": "s", "preview": [["id", None, "amount"], ["1", "x", "2"]]}]}]}
        description = _offline_description(inventory)
        layout = validate_layout(inventory["files"][0]["tables"][0]["preview"], description["tables"][0])
        self.assertEqual(layout["headers"], ["id", "column_2", "amount"])

    def test_layout_derives_preamble_instead_of_trusting_model_list(self):
        rows = [["report title"], ["period: all"], [], ["date", "amount"], ["2024-01-01", "5"]]
        proposal = {"header_row": 4, "data_start_row": 5,
                    "columns": [{"position": i, "label": value} for i, value in enumerate(rows[3], 1)],
                    "preamble_rows": [1]}
        result = validate_layout(rows, proposal)
        self.assertEqual(result["preamble"], rows[:3])
        self.assertEqual(result["records"], [rows[4]])

    def test_layout_reports_header_preamble_and_context_fixture(self):
        rows = [["SỔ CHI TIẾT BÁN HÀNG"], ["Kỳ báo cáo: toàn bộ", "Kênh bán: Tất cả"], [],
                ["Ngày", "Mã đơn hàng", "Tên hàng hóa", "Đơn vị tính", "Số lượng", "Đơn giá", "Thành tiền", "Trạng thái", "Số lượng quy đổi"], ["d", "1", "x", "cái", "1", "2", "2", "ok", "1"]]
        proposal = {"header_row": 4, "data_start_row": 5, "columns": [{"position": i, "label": value} for i, value in enumerate(rows[3], 1)], "preamble_rows": [1,2,3]}
        result = validate_layout(rows, proposal)
        self.assertEqual(result["preamble"], rows[:3])
        self.assertEqual(result["records"], [rows[4]])

    def test_empty_header_positions_are_named_not_dropped(self):
        result = validate_layout([["id", "", "amount"], ["1", "note", "2"]], {"header_row": 1, "data_start_row": 2,
            "columns": [{"position": 1, "label": "id"}, {"position": 2, "label": ""}, {"position": 3, "label": "amount"}], "preamble_rows": []})
        self.assertEqual(result["positions"], [1, 2, 3])

    def test_discovery_rejects_duplicate_business_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = []
            for split in ("train", "public_test"):
                data = Path(temp) / split / "data"
                (data / "biz_1").mkdir(parents=True)
                roots.append(data)
            with self.assertRaisesRegex(ValueError, "duplicate business"):
                discover_businesses(roots)

    def test_csv_preserves_text_and_duplicate_headers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sales.csv"
            path.write_text('Mã;Mã;Ghi chú\n001;002;"có; ghi chú"\n003;;NULL\n', encoding="utf-8-sig")
            rows, columns = read_csv_rows(path, delimiter=";")
            self.assertEqual(columns, ["Mã", "Mã__2", "Ghi chú"])
            self.assertEqual(rows, [["001", "002", "có; ghi chú"], ["003", "", "NULL"]])

    def test_xlsx_reads_all_sheets_and_keeps_native_values_and_formula_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "Tháng 1"
            sheet.append(["Ngày", "Mã"])
            sheet.append([date(2026, 1, 2), "001"])
            sheet["C1"] = "extra"
            sheet["C2"] = "=1+1"
            book.create_sheet("empty")
            book.save(path)
            inventory = {**inspect_business(path.parent), "path": path.parent}
            desc = {"tables": [{"source_file": "report.xlsx", "source_table": "Tháng 1", "header_row": 1, "data_start_row": 2,
                                 "columns": [{"position": i, "label": x} for i, x in enumerate(["Ngày", "Mã", "extra"], 1)], "preamble_rows": []},
                                {"source_file": "report.xlsx", "source_table": "empty", "header_row": 1, "data_start_row": 2,
                                 "columns": [{"position": 1, "label": "empty"}], "preamble_rows": []}]}
            raw, _ = _read_tables(inventory, desc)
            self.assertEqual(raw[0]["rows"], [["2026-01-02T00:00:00", "001", "=1+1"]])
            self.assertEqual(raw[0]["columns"], ["Ngày", "Mã", "extra"])
            self.assertEqual(raw[1]["rows"], [])

    def test_sqlite_reader_preserves_mixed_values_and_blobs(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pos.sqlite"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE sample (value)")
                db.executemany("INSERT INTO sample VALUES (?)", [(1,), ("one",), (sqlite3.Binary(b"\\x00\\xff"),)])
                db.commit()
            finally:
                db.close()
            rows, columns = read_sqlite_rows(path, "sample")
            self.assertEqual(columns, ["value"])
            self.assertEqual(rows, [[1], ["one"], [b"\\x00\\xff"]])

    def test_sqlite_reader_aligns_generated_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "generated.sqlite"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE sample (base INTEGER, derived INTEGER GENERATED ALWAYS AS (base * 2) STORED)")
                db.execute("INSERT INTO sample (base) VALUES (3)")
                db.commit()
            finally:
                db.close()
            rows, columns = read_sqlite_rows(path, "sample")
            self.assertEqual(columns, ["base", "derived"])
            self.assertEqual(rows, [[3, 6]])
            inspected = inspect_business(Path(temp))
            table = inspected["files"][0]["tables"][0]
            self.assertEqual([column["name"] for column in table["columns"]], columns)
            self.assertEqual(table["columns"][1]["hidden"], 3)
            self.assertEqual(table["columns"][1]["observed_types"], ["int"])


    def test_default_offline_header_detection_accepts_ordinary_csv_rows(self):
        from business_import.__main__ import _offline_description
        inventory = {"path": Path("."), "files": [{"name": "x.csv", "format": ".csv", "delimiter": ",", "tables": [{"name": "x", "preview": [["id", "amount"], ["001", "10"], ["002", "20"]]}]}]}
        description = _offline_description(inventory)
        self.assertEqual(description["tables"][0]["header_row"], 1)

    def test_layout_accepts_report_title_context_then_headers(self):
        rows = [["SỔ CHI TIẾT BÁN HÀNG"], ["Kỳ báo cáo: toàn bộ", "Kênh bán: Tất cả"], [],
                ["Ngày", "Mã đơn hàng", "Tên hàng hóa", "Đơn vị tính", "Số lượng", "Đơn giá", "Thành tiền", "Trạng thái", "Số lượng quy đổi"],
                ["2026-01-01", "001", "Bút", "cái", 2, 1000, 2000, "Hoàn tất", 2]]
        proposal = {"header_row": 4, "data_start_row": 5, "columns": [{"position": i, "label": label} for i, label in enumerate(rows[3], 1)], "preamble_rows": [1, 2, 3]}
        layout = validate_layout(rows, proposal)
        self.assertEqual(layout["headers"], rows[3])
        self.assertEqual(layout["records"], [rows[4]])
        self.assertEqual(layout["preamble"], rows[:3])

    def test_layout_rejects_fabricated_headers(self):
        with self.assertRaisesRegex(ValueError, "header"):
            validate_layout([["title"], ["real", "header"], [1, 2]], {"header_row": 1, "data_start_row": 2, "columns": [{"position": 1, "label": "invented"}]})


class SqlTests(unittest.TestCase):
    def test_load_plan_is_narrow_and_sql_matches_python_renderer(self):
        staging = [{"name": "_stage_0", "columns": ["Mã", "Amount"], "destination": "sales"}]
        plan = {"tables": [{"source": "_stage_0", "destination": "sales", "columns": ["Mã", "Amount"], "sql": 'CREATE TABLE "biz_1"."sales" AS SELECT "Mã", "Amount" FROM "_stage_0"'}]}
        self.assertEqual(validate_load_plan(plan, staging, "biz_1"), build_load_plan(staging, "biz_1"))

    def test_load_plan_rejects_redirected_destination(self):
        staging = [{"name": "_stage_0", "columns": ["id"], "destination": "sales"}]
        plan = {"tables": [{"source": "_stage_0", "destination": "payroll", "columns": ["id"], "sql": 'CREATE TABLE "biz_1"."payroll" AS SELECT "id" FROM "_stage_0"'}]}
        with self.assertRaisesRegex(ValueError, "destination"):
            validate_load_plan(plan, staging, "biz_1")

    def test_load_plan_rejects_sql_transformations_and_missing_tables(self):
        staging = [{"name": "_stage_0", "columns": ["id"]}]
        for statement in ['CREATE TABLE "biz_1"."sales" AS SELECT id FROM "_stage_0" WHERE id > 0', 'CREATE TABLE "other"."sales" AS SELECT "id" FROM "_stage_0"; DROP TABLE x']:
            plan = {"tables": [{"source": "_stage_0", "destination": "sales", "columns": ["id"], "sql": statement}]}
            with self.subTest(sql=statement), self.assertRaises(ValueError):
                validate_load_plan(plan, staging, "biz_1")
        with self.assertRaises(ValueError):
            validate_load_plan({"tables": []}, staging, "biz_1")


class LlmTests(unittest.TestCase):
    def test_uncertain_customer_rows_are_never_sent_to_model(self):
        inventory = {"files": [{"name": "x.csv", "format": ".csv", "tables": [{"name": "x", "preview": [["Alice", "alice@example.org"], ["Bob", "42"], ["Carol", "43"]]}]}]}
        response = {"tables": [{"source_file": "x.csv", "source_table": "x"}]}
        with patch("business_import.llm._json_call", return_value=response) as call:
            describe_business(object(), inventory)
        prompt = call.call_args.args[1]
        self.assertNotIn("Alice", prompt)
        self.assertNotIn("alice@example.org", prompt)

    def test_description_rejects_fabricated_source(self):
        inventory = {"files": [{"name": "sales.csv", "tables": [{"name": "sales"}]}]}
        with patch("business_import.llm._json_call", return_value={"tables": [{"source_file": "other.csv", "source_table": "sales"}]}):
            with self.assertRaisesRegex(ValueError, "unknown"):
                describe_business(object(), inventory)

    def test_csv_header_preview_exposes_only_local_candidate(self):
        inventory = {"files": [{"name": "x.csv", "format": ".csv", "tables": [{"name": "x", "preview": [["id", "email"], ["001", "alice@example.org"]]}]}]}
        response = {"tables": [{"source_file": "x.csv", "source_table": "x"}]}
        with patch("business_import.llm._json_call", return_value=response) as call:
            describe_business(object(), inventory)
        prompt = call.call_args.args[1]
        self.assertIn('"id"', prompt)
        self.assertIn('"email"', prompt)
        self.assertNotIn("alice@example.org", prompt)

    def test_description_sends_header_and_one_masked_data_row_only(self):
        inventory = {"files": [{"name": "sales.csv", "format": ".csv", "tables": [{"name": "sales", "preview": [
            ["id", "customer"], ["001", "FIRST_PRIVATE"], ["002", "SECOND_PRIVATE"], ["003", "THIRD_PRIVATE"]
        ]}]}]}
        response = {"tables": [{"source_file": "sales.csv", "source_table": "sales"}]}
        with patch("business_import.llm._json_call", return_value=response) as call:
            describe_business(object(), inventory)
        prompt = call.call_args.args[1]
        self.assertIn('"id"', prompt)
        self.assertIn('"customer"', prompt)
        self.assertIn('"row": 2', prompt)
        self.assertNotIn("FIRST_PRIVATE", prompt)
        self.assertNotIn("SECOND_PRIVATE", prompt)
        self.assertNotIn("THIRD_PRIVATE", prompt)
        self.assertNotIn('"row": 3', prompt)
        self.assertNotIn('"row": 4', prompt)

    def test_description_masks_row_values_and_validates_coverage(self):
        inventory = {"files": [{"name": "sales.csv", "tables": [{"name": "sales", "preview": [["id", "customer"], ["001", "private"]]}]}]}
        response = {"tables": [{"source_file": "sales.csv", "source_table": "sales"}]}
        with patch("business_import.llm._json_call", return_value=response) as call:
            result = describe_business(object(), inventory)
        self.assertNotIn("private", call.call_args.args[1])
        self.assertEqual(len(result["tables"]), 1)

    def test_second_call_proposes_sql(self):
        response = {"tables": [{"source": "_stage_0", "destination": "sales", "columns": ["id"], "sql": 'CREATE TABLE "biz_1"."sales" AS SELECT "id" FROM "_stage_0"'}]}
        with patch("business_import.llm._json_call", return_value=response):
            result = generate_load_sql(object(), {"summary": "x"}, [{"name": "_stage_0", "columns": ["id"]}], "biz_1")
        self.assertEqual(result, response)

    def test_each_model_call_saves_raw_output_to_llm_out(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from types import SimpleNamespace
        from business_import.llm import _json_call
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
        with TemporaryDirectory() as temp, patch("business_import.llm.OUTPUT_DIR", Path(temp)):
            result = _json_call(client, "test prompt")
            saved = list(Path(temp).glob("*.txt"))
            self.assertEqual(result, {"ok": True})
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_text(encoding="utf-8"), '{"ok": true}')


class DatabaseTests(unittest.TestCase):
    def _setup(self, conn, value="001"):
        business = {"id": "biz_1", "split": "train", "readme": "notes"}
        raw = [{"source_file": "sales.csv", "source_table": "sales", "format": ".csv", "columns": ["id"], "rows": [[value], ["002"]]}]
        spec = [{"name": "_stage_0", "columns": ["id"], "destination": "sales"}]
        return business, raw, build_load_plan(spec, "biz_1")

    def test_existing_unowned_schema_is_rejected_even_without_collision(self):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE SCHEMA "biz_1"')
        conn.execute('CREATE TABLE "biz_1"."private" (id VARCHAR)')
        business, raw, plan = self._setup(conn)
        with self.assertRaisesRegex(ValueError, "not importer-owned"):
            load_business(conn, business, {}, raw, {}, plan)
        self.assertEqual(conn.execute('SELECT count(*) FROM "biz_1"."private"').fetchone()[0], 0)

    def test_reimport_removes_stale_owned_tables(self):
        conn = duckdb.connect(":memory:")
        business, raw, plan = self._setup(conn)
        extra = {"source_file": "old.csv", "source_table": "old", "format": ".csv", "columns": ["id"], "rows": [["old"]]}
        raws = raw + [extra]
        staging = [{"name": "_stage_0", "columns": ["id"], "destination": "sales"}, {"name": "_stage_1", "columns": ["id"], "destination": "old"}]
        load_business(conn, business, {}, raws, {}, build_load_plan(staging, "biz_1"))
        load_business(conn, business, {}, raw, {}, plan)
        self.assertIsNone(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='biz_1' AND table_name='old'").fetchone())

    def test_reordered_plan_keeps_catalog_source_mapping(self):
        conn = duckdb.connect(":memory:")
        business, raw, _ = self._setup(conn)
        raw2 = {"source_file": "returns.csv", "source_table": "returns", "format": ".csv", "columns": ["id"], "rows": [["R"]]}
        raws = raw + [raw2]
        staging = [{"name": "_stage_0", "columns": ["id"], "destination": "sales"}, {"name": "_stage_1", "columns": ["id"], "destination": "returns"}]
        plan = build_load_plan(staging, "biz_1")
        plan["tables"].reverse()
        load_business(conn, business, {}, raws, {}, plan)
        mapping = conn.execute('SELECT source_file, table_name FROM "_catalog".tables ORDER BY source_file').fetchall()
        self.assertEqual(mapping, [("returns.csv", "returns"), ("sales.csv", "sales")])

    def test_import_is_rerunnable_without_duplicates(self):
        conn = duckdb.connect(":memory:")
        business, raw, plan = self._setup(conn)
        for _ in range(2):
            load_business(conn, business, {"summary": "sales"}, raw, {}, plan)
        self.assertEqual(conn.execute('SELECT * FROM "biz_1"."sales" ORDER BY id').fetchall(), [("001",), ("002",)])
        self.assertEqual(conn.execute('SELECT count(*) FROM "_catalog".tables').fetchone()[0], 1)

    def test_existing_unowned_destination_is_not_overwritten(self):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE SCHEMA "biz_1"')
        conn.execute('CREATE TABLE "biz_1"."sales" (secret VARCHAR)')
        conn.execute('INSERT INTO "biz_1"."sales" VALUES (\'keep\')')
        business, raw, plan = self._setup(conn)
        with self.assertRaisesRegex(ValueError, "not importer-owned"):
            load_business(conn, business, {}, raw, {}, plan)
        self.assertEqual(conn.execute('SELECT * FROM "biz_1"."sales"').fetchall(), [("keep",)])

    def test_failed_reimport_rolls_back_previous_table(self):
        conn = duckdb.connect(":memory:")
        business, raw, plan = self._setup(conn)
        load_business(conn, business, {"summary": "old"}, raw, {}, plan)
        invalid = {"tables": [{**plan["tables"][0], "sql": "DROP TABLE x"}]}
        with self.assertRaises(ValueError):
            load_business(conn, business, {"summary": "new"}, [{**raw[0], "rows": [["new"]]}], {}, invalid)
        self.assertEqual(conn.execute('SELECT * FROM "biz_1"."sales" ORDER BY id').fetchall(), [("001",), ("002",)])


if __name__ == "__main__":
    unittest.main()
