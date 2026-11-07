import csv
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
from openpyxl import Workbook

from business_import.database import load_business
from business_import.layouts import validate_layout
from business_import.llm import describe_business
from business_import.__main__ import main, _read_tables
from business_import.sources import discover_businesses, inspect_business, read_csv_rows, read_sqlite_rows
from business_import.sql import build_load_plan, validate_load_plan


def model_client(result, prompts):
    def create(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def compact_description(file="sales.csv", table="sales", header=1):
    return {"summary": "Sales business", "tables": [{"source_file": file, "source_table": table,
            "header_row": header, "description": "One row per item"}]}


class CompactIngestionTests(unittest.TestCase):
    def inventory(self):
        return {"readme": "Business notes: 1 box = 12 units.", "files": [{"name": "sales.csv", "format": ".csv",
                "tables": [{"name": "sales", "preview": [["id", "amount"], ["001", "10"], ["002", "20"], ["003", "30"]]}]}]}

    def describe(self, inventory, response):
        prompts = []
        with tempfile.TemporaryDirectory() as temp, patch("business_import.llm.OUTPUT_DIR", Path(temp)):
            result = describe_business(model_client(response, prompts), inventory)
        return result, prompts[0]

    def test_compact_response_expands_exact_layout_and_sends_real_rows(self):
        inventory = self.inventory()
        before = deepcopy(inventory)
        output = StringIO()
        with redirect_stdout(output):
            result, prompt = self.describe(inventory, compact_description())
        payload = json.loads(prompt.split("Input inventory JSON follows:\n", 1)[1])
        self.assertEqual(payload["readme"], inventory["readme"])
        self.assertEqual(payload["files"][0]["tables"][0]["preview"],
                         [[1, ["id", "amount"]], [2, ["001", "10"]], [3, ["002", "20"]]])
        table = result["tables"][0]
        self.assertEqual(table["columns"], [{"position": 1, "label": "id"}, {"position": 2, "label": "amount"}])
        self.assertEqual(table["data_start_row"], 2)
        self.assertEqual(table["preamble_rows"], [])
        self.assertEqual(result["rules"], [])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(inventory, before)

    def test_fenced_json_and_structured_readme_rules_are_preserved(self):
        response = compact_description()
        response["rules"] = [{"fact": "README: unit conversion", "description": "1 box = 12 units"}]
        content = "```json\n" + json.dumps(response) + "\n```"
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content))]))))
        with tempfile.TemporaryDirectory() as temp, patch("business_import.llm.OUTPUT_DIR", Path(temp)):
            result = describe_business(client, self.inventory())
            self.assertEqual(next(Path(temp).glob("*.txt")).read_text(encoding="utf-8"), content)
        self.assertEqual(result["rules"], ["README: unit conversion: 1 box = 12 units"])
        self.assertEqual(result["tables"][0]["columns"][0], {"position": 1, "label": "id"})

    def test_model_request_enables_json_output_mode(self):
        from business_import.llm import _json_call
        from unittest.mock import Mock
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as temp, patch("business_import.llm.OUTPUT_DIR", Path(temp)):
            self.assertEqual(_json_call(client, "prompt"), {})
        self.assertFalse(create.call_args.kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        response_format = create.call_args.kwargs["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema["required"], ["tables"])
        self.assertFalse(schema["additionalProperties"])
        table = schema["properties"]["tables"]["items"]
        self.assertEqual(table["type"], "object")
        self.assertEqual(set(table["required"]), {"source_file", "source_table", "header_row", "column_types"})
        self.assertFalse(table["additionalProperties"])
        hints = table["properties"]["column_types"]["items"]["anyOf"]
        self.assertEqual(hints[0]["enum"], ["VARCHAR", "BIGINT", "DECIMAL", "BOOLEAN"])
        self.assertTrue(all(not hint["additionalProperties"] for hint in hints[1:]))
        self.assertTrue(any(hint.get("properties", {}).get("format", {}).get("enum") == ["VND"]
                            and hint["properties"]["type"]["enum"] == ["DECIMAL"] for hint in hints))

    def test_json_parser_rejects_incomplete_or_surrounding_prose(self):
        from business_import.llm import _json_call
        for content in ('```json\n{"summary":', 'Explanation\n{"ok": true}', '```json\n{}\n```\nextra'):
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=content))]))))
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp, patch("business_import.llm.OUTPUT_DIR", Path(temp)):
                with self.assertRaisesRegex(ValueError, "malformed|truncated"):
                    _json_call(client, "prompt")

    def test_monthly_tables_have_explicit_exact_output_template(self):
        inventory = self.inventory()
        first = inventory["files"][0]
        first["name"] = "BanHang_T1_2025.xlsx"
        first["format"] = ".xlsx"
        first["tables"][0]["name"] = "DonHang_ChiTiet"
        second = deepcopy(first)
        second["name"] = "BanHang_T2_2025.xlsx"
        inventory["files"].append(second)
        response = compact_description(first["name"], "DonHang_ChiTiet")
        response["tables"].append(compact_description(second["name"], "DonHang_ChiTiet")["tables"][0])
        result, prompt = self.describe(inventory, response)
        instructions = prompt.split("Input inventory JSON follows:\n", 1)[0]
        template = json.loads(instructions.split("Output template JSON follows:\n", 1)[1])
        self.assertEqual([(t["source_file"], t["source_table"]) for t in template["tables"]],
                         [("BanHang_T1_2025.xlsx", "DonHang_ChiTiet"), ("BanHang_T2_2025.xlsx", "DonHang_ChiTiet")])
        self.assertEqual(len(result["tables"]), 2)
        with tempfile.TemporaryDirectory() as temp, patch("business_import.llm.OUTPUT_DIR", Path(temp)):
            from unittest.mock import Mock
            create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)))]))
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            describe_business(client, inventory)
            array = create.call_args.kwargs["response_format"]["json_schema"]["schema"]["properties"]["tables"]
            self.assertEqual((array["minItems"], array["maxItems"]), (2, 2))
            branches = array["items"]["anyOf"]
            self.assertEqual(branches[0]["properties"]["source_file"]["enum"], [first["name"]])
            self.assertEqual(branches[0]["properties"]["header_row"]["enum"], [1])
            types = branches[0]["properties"]["column_types"]
            self.assertEqual((types["minItems"], types["maxItems"]), (2, 2))
            self.assertEqual(branches[2]["properties"]["source_file"]["enum"], [second["name"]])
            self.assertEqual(branches[1]["properties"]["header_row"]["enum"], [None])
            self.assertEqual(branches[1]["properties"]["column_types"]["maxItems"], 0)
        missing = compact_description(first["name"], "DonHang_ChiTiet")
        with self.assertRaisesRegex(ValueError, "missing.*BanHang_T2_2025"):
            self.describe(inventory, missing)
        wildcard = compact_description("BanHang_T*.xlsx", "DonHang_ChiTiet")
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.describe(inventory, wildcard)

    def test_minimal_response_loads_string_and_object_types(self):
        response = {"tables": [{"source_file": "sales.csv", "source_table": "sales", "header_row": 1,
                                "column_types": ["VARCHAR", {"type": "DECIMAL", "thousands_separator": ","}]}]}
        description, prompt = self.describe(self.inventory(), response)
        template = json.loads(prompt.split("Output template JSON follows:\n", 1)[1].split("\nInput inventory JSON follows:", 1)[0])
        self.assertEqual(set(template), {"tables"})
        self.assertEqual(set(template["tables"][0]), {"source_file", "source_table", "header_row", "column_types"})
        conn = duckdb.connect(":memory:")
        try:
            raw = [{"source_file": "sales.csv", "source_table": "sales", "format": ".csv",
                    "columns": ["id", "amount"], "rows": [["001", "35,000"]]}]
            plan = build_load_plan([{"name": "_stage_0", "columns": ["id", "amount"], "destination": "sales"}], "biz_1")
            load_business(conn, {"id": "biz_1", "split": "test"}, description, raw, {}, plan)
            self.assertEqual(conn.execute("SELECT id, CAST(amount AS BIGINT) FROM biz_1.sales").fetchall(), [("001", 35000)])
            self.assertEqual(description["tables"][0]["actual_types"], ["VARCHAR", "DECIMAL(38,10)"])
        finally:
            conn.close()

    def test_type_template_has_exact_source_widths(self):
        inventory = self.inventory()
        inventory["files"].append({"name": "ledger.xlsx", "format": ".xlsx", "tables": [
            {"name": "Sheet1", "preview": [["Report"], [], ["id", "qty", "price"], ["A", 2, 10]]}]})
        inventory["files"].append({"name": "native.sqlite", "format": ".sqlite", "tables": [
            {"name": "items", "row_count": 1, "columns": [{"name": "id"}], "preview": [[1]]}]})
        response = compact_description()
        response["tables"].extend([compact_description("ledger.xlsx", "Sheet1", 3)["tables"][0],
                                   compact_description("native.sqlite", "items", None)["tables"][0]])
        _, prompt = self.describe(inventory, response)
        template = json.loads(prompt.split("Output template JSON follows:\n", 1)[1].split("\nInput inventory JSON follows:", 1)[0])
        self.assertEqual([t["column_types"] for t in template["tables"]],
                         [["VARCHAR"] * 2, ["VARCHAR"] * 3, ["VARCHAR"]])

    def test_missing_excel_dimensions_uses_preview_width_without_header_candidate(self):
        inventory = {"files": [{"name": "catalog.xlsx", "format": ".xlsx", "tables": [
            {"name": "Sheet1", "max_column": None, "preview": [["code", "name"], ["A", "Coffee"]]}]}]}
        with patch("business_import.llm._json_call", side_effect=RuntimeError("stop before API")) as call:
            with self.assertRaisesRegex(RuntimeError, "stop before API"):
                describe_business(None, inventory)
        prompt = call.call_args.args[1]
        template = json.loads(prompt.split("Output template JSON follows:\n", 1)[1].split("\nInput inventory JSON follows:", 1)[0])
        self.assertEqual(template["tables"][0]["column_types"], ["VARCHAR"] * 2)

    def test_wrong_type_count_keeps_original_values_and_reports_warning(self):
        for count in (1, 3):
            response = compact_description()
            response["tables"][0]["column_types"] = [{"type": "BIGINT"}] * count
            description, _ = self.describe(self.inventory(), response)
            self.assertNotIn("column_types", description["tables"][0])
            conn = duckdb.connect(":memory:")
            try:
                raw = [{"source_file": "sales.csv", "source_table": "sales", "format": ".csv",
                        "columns": ["id", "amount"], "rows": [["001", "10"]]}]
                plan = build_load_plan([{"name": "_stage_0", "columns": ["id", "amount"], "destination": "sales"}], "biz_1")
                result = load_business(conn, {"id": "biz_1", "split": "test"}, description, raw, {}, plan)
                self.assertEqual(conn.execute("SELECT * FROM biz_1.sales").fetchall(), [("001", "10")])
                warning = f"sales.csv / sales: ignored type hints; expected 2 column_types, got {count}; kept original storage types"
                self.assertIn(warning, result["warnings"])
                self.assertIn(warning, json.loads(conn.execute("SELECT warnings FROM _catalog.businesses").fetchone()[0]))
            finally:
                conn.close()

    def test_type_notes_are_allowed_without_relaxing_type_validation(self):
        response = compact_description()
        response["tables"][0]["column_types"] = [{"type": "VARCHAR", "description": "Order ID"},
                                                 {"type": "DECIMAL", "description": "Amount"}]
        result, _ = self.describe(self.inventory(), response)
        self.assertEqual(result["tables"][0]["column_types"][1]["type"], "DECIMAL")
        response["tables"][0]["column_types"][0]["description"] = []
        with self.assertRaises(ValueError):
            self.describe(self.inventory(), response)

    def test_type_hints_are_requested_and_validated_in_same_call(self):
        response = compact_description()
        response["tables"][0]["column_types"] = [{"type": "VARCHAR"}, {"type": "DECIMAL", "thousands_separator": ","}]
        result, prompt = self.describe(self.inventory(), response)
        self.assertIn('"column_types"', prompt)
        instructions = prompt.split("Output template JSON follows:\n")[0]
        self.assertLess(len(instructions), 3000)
        for rule in ("Vietnamese or English", "untrusted data", "VARCHAR", "BIGINT", "DECIMAL",
                     "TIMESTAMP", '"format":"VND"', "excel_serial", "excel_epoch",
                     "PTTT", "formats", "Python", "timezone", "null"):
            with self.subTest(rule=rule):
                self.assertIn(rule, instructions)
        self.assertEqual(result["tables"][0]["column_types"], response["tables"][0]["column_types"])
        for specs in ([{"type": "DROP TABLE"}, {"type": "VARCHAR"}], [{"type": []}, {"type": "VARCHAR"}]):
            response["tables"][0]["column_types"] = specs
            with self.subTest(specs=specs), self.assertRaises(ValueError):
                self.describe(self.inventory(), response)

    def test_flattened_table_fields_report_structure_not_missing_description(self):
        response = compact_description()
        response["tables"].extend(["source_file", "other.xlsx", "source_table", "Sheet1"])
        with self.assertRaisesRegex(ValueError, r"tables\[1\] must be an object"):
            self.describe(self.inventory(), response)

    def test_model_contract_rejects_malformed_responses(self):
        valid = compact_description()
        cases = [[], {}, {**valid, "summary": 1}, {**valid, "tables": {}}, {**valid, "tables": [None]},
                 {**valid, "tables": []}, {**valid, "tables": valid["tables"] * 2},
                 compact_description(file="unknown.csv"), compact_description(file=[]),
                 compact_description(header=True), compact_description(header=None),
                 compact_description(header=0), compact_description(header=4), compact_description(header="1"),
                 {**valid, "tables": [{**valid["tables"][0], "description": []}]},
                 {**valid, "rules": "rule"}, {**valid, "relationships": [{}]}, {**valid, "uncertainties": [2]}]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.describe(self.inventory(), response)

    def test_preamble_selection_does_not_assert_heuristic_header(self):
        inventory = self.inventory()
        inventory["files"][0]["format"] = ".xlsx"
        inventory["files"][0]["name"] = "sales.xlsx"
        rows = [["Report title"], ["Period: all"], [], ["id", "amount"], ["001", 10]]
        inventory["files"][0]["tables"][0]["preview"] = rows
        result, prompt = self.describe(inventory, compact_description(file="sales.xlsx", header=4))
        payload = json.loads(prompt.split("Input inventory JSON follows:\n", 1)[1])
        self.assertEqual(payload["files"][0]["tables"][0]["preview"], [[1, ["Report title"]], [4, ["id", "amount"]], [5, ["001", 10]]])
        layout = validate_layout(rows, result["tables"][0])
        self.assertEqual(layout["preamble"], rows[:3])
        self.assertEqual(layout["records"], [["001", 10]])

    def test_excel_serial_metadata_reaches_timestamp_loading(self):
        from datetime import datetime
        from openpyxl.utils.datetime import CALENDAR_MAC_1904
        for epoch, serial, expected in ((None, 45717, datetime(2025, 3, 1)),
                                        (CALENDAR_MAC_1904, 1, datetime(1904, 1, 2))):
            with self.subTest(epoch=epoch), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "dates.xlsx"
                book = Workbook()
                if epoch is not None:
                    book.epoch = epoch
                sheet = book.active
                sheet.append(["Report"])
                sheet.append([])
                sheet.append(["id", "Ngày mua"])
                sheet.append(["A", serial])
                sheet.append(["B", "05/03/2025"])
                book.save(path)
                book.close()
                inventory = inspect_business(Path(temp))
                response = {"tables": [{"source_file": path.name, "source_table": "Sheet", "header_row": 3,
                    "column_types": ["VARCHAR", {"type": "TIMESTAMP", "formats": ["excel_serial", "%d/%m/%Y"]}]}]}
                description, prompt = self.describe(inventory, response)
                payload = json.loads(prompt.split("Input inventory JSON follows:\n", 1)[1])
                preview = payload["files"][0]["tables"][0]["preview"]
                self.assertEqual([row[0] for row in preview], [3, 4, 5])
                self.assertIn("excel_epoch", payload["files"][0]["tables"][0])
                raw, layouts = _read_tables(inventory, description)
                conn = duckdb.connect(":memory:")
                try:
                    plan = build_load_plan([{"name": "_stage_0", "columns": raw[0]["columns"], "destination": "dates__Sheet"}], "biz_1")
                    load_business(conn, {"id": "biz_1", "split": "test"}, description, raw, layouts, plan)
                    self.assertEqual(conn.execute('SELECT "Ngày mua" FROM biz_1.dates__Sheet').fetchall(),
                                     [(expected,), (datetime(2025, 3, 5),)])
                finally:
                    conn.close()

    def test_unknown_layout_is_not_silently_accepted(self):
        inventory = self.inventory()
        inventory["files"][0]["tables"][0]["preview"] = [["name", "email"], ["Alice", "a@example.org"]]
        result, prompt = self.describe(inventory, compact_description())
        self.assertIn("a@example.org", prompt)
        with self.assertRaisesRegex(ValueError, "structural"):
            validate_layout(inventory["files"][0]["tables"][0]["preview"], result["tables"][0])

    def test_sqlite_samples_are_bounded_and_blobs_are_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            with closing(sqlite3.connect(path / "native.sqlite")) as db:
                db.execute("CREATE TABLE t (id INTEGER, value BLOB)")
                db.executemany("INSERT INTO t VALUES (?, ?)", [(i, b"abc") for i in range(5)])
                db.commit()
            inventory = inspect_business(path)
            self.assertEqual(inventory["files"][0]["tables"][0]["preview"], [[0, "<blob:3 bytes>"], [1, "<blob:3 bytes>"], [2, "<blob:3 bytes>"]])
            result, prompt = self.describe(inventory, compact_description("native.sqlite", "t", None))
            self.assertIsNone(result["tables"][0]["header_row"])
            self.assertIn("<blob:3 bytes>", prompt)
            self.assertEqual(read_sqlite_rows(path / "native.sqlite", "t")[0][-1], [4, b"abc"])

    def test_online_cli_loads_all_formats_once_and_preserves_long_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            business = root / "biz_1"
            business.mkdir(parents=True)
            readme = "Business rules: 1 box = 12 units. " * 30
            (business / "README.md").write_text(readme, encoding="utf-8")
            long_header, long_value = "Header" * 50, "value" * 100
            with (business / "sales.csv").open("w", encoding="utf-8", newline="") as stream:
                csv.writer(stream).writerows([["id", long_header], ["001", long_value], ["002", "2"], ["003", "3"]])
            book = Workbook()
            book.active.title = "Sales"
            book.active.append(["id", "amount"])
            book.active.append([1, 10])
            book.create_sheet("Empty")
            book.save(business / "report.xlsx")
            (business / "empty.csv").touch()
            with closing(sqlite3.connect(business / "native.sqlite")) as db:
                db.execute("CREATE TABLE t (id INTEGER)")
                db.execute("INSERT INTO t VALUES (7)")
                db.commit()
            response = compact_description()
            response["tables"] += [compact_description("report.xlsx", "Sales")["tables"][0],
                                    compact_description("report.xlsx", "Empty", None)["tables"][0],
                                    compact_description("native.sqlite", "t", None)["tables"][0],
                                    compact_description("empty.csv", "empty", None)["tables"][0]]
            response["tables"][1]["column_types"] = [{"type": "BIGINT"}, {"type": "DECIMAL"}]
            prompts, output, errors = [], StringIO(), StringIO()
            db_path = Path(temp) / "out.duckdb"
            with patch.dict(os.environ, {"MODEL_API_KEY": "test"}), patch("business_import.llm.create_client", return_value=model_client(response, prompts)), patch("business_import.llm.OUTPUT_DIR", Path(temp) / "responses"), redirect_stdout(output), redirect_stderr(errors):
                status = main(["--roots", str(root), "--db", str(db_path)])
            self.assertEqual(status, 0, errors.getvalue())
            self.assertEqual(len(prompts), 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "ok")
            payload = json.loads(prompts[0].split("Input inventory JSON follows:\n", 1)[1])
            self.assertEqual(payload["readme"], readme)
            samples = next(f for f in payload["files"] if f["name"] == "sales.csv")["tables"][0]["preview"]
            self.assertLessEqual(len(samples), 3)
            self.assertLessEqual(len(samples[0][1][1]), 200)
            self.assertTrue(samples[0][1][1].endswith("[truncated]"))
            self.assertLessEqual(len(samples[1][1][1]), 200)
            with duckdb.connect(str(db_path)) as conn:
                self.assertEqual(conn.execute('SELECT * FROM biz_1.sales ORDER BY id').fetchall(), [("001", long_value), ("002", "2"), ("003", "3")])
                self.assertEqual(conn.execute('DESCRIBE biz_1.sales').fetchall()[1][0], long_header)
                self.assertEqual(conn.execute('SELECT * FROM biz_1.native__t').fetchall(), [(7,)])
                self.assertEqual(conn.execute('SELECT * FROM biz_1.report__Sales').fetchall(), [(1, 10)])
                self.assertEqual([row[1] for row in conn.execute('DESCRIBE biz_1.report__Sales').fetchall()], ["BIGINT", "DECIMAL(38,10)"])
                self.assertEqual(conn.execute('SELECT count(*) FROM biz_1.empty').fetchone(), (0,))
                self.assertEqual(conn.execute('SELECT count(*) FROM biz_1.report__Empty').fetchone(), (0,))
                stored = conn.execute('SELECT readme, description_json FROM _catalog.businesses').fetchone()
                self.assertEqual(stored[0], readme)
                self.assertEqual(json.loads(stored[1])["summary"], "Sales business")
            # An invalid reimport must leave the previous business untouched.
            response["tables"][0]["header_row"] = None
            with patch("business_import.llm.create_client", return_value=model_client(response, [])), patch("business_import.llm.OUTPUT_DIR", Path(temp) / "responses"), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(["--roots", str(root), "--db", str(db_path)]), 1)
            with duckdb.connect(str(db_path)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM biz_1.sales').fetchone(), (3,))

    def test_only_exact_repeated_headers_are_excluded_and_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "sales.csv").write_text("id,amount\n001,10\nid,amount\nid,20\n", encoding="utf-8")
            inventory = inspect_business(path)
            response = compact_description()
            result, _ = self.describe(inventory, response)
            raw, layouts = _read_tables(inventory, result)
            self.assertEqual(raw[0]["rows"], [["001", "10"], ["id", "20"]])
            self.assertEqual(layouts[("sales.csv", "sales")]["repeated_header_count"], 1)
            with duckdb.connect(":memory:") as conn:
                plan = build_load_plan([{"name": "_stage_0", "columns": ["id", "amount"], "destination": "sales"}], "biz_1")
                loaded = load_business(conn, {"id": "biz_1", "split": "train"}, result, raw, layouts, plan)
                self.assertTrue(any("1 repeated header" in warning for warning in loaded["warnings"]))

    def test_offline_cli_never_calls_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            business = root / "biz_1"
            business.mkdir(parents=True)
            (business / "sales.csv").write_text("id,amount\n001,10\n", encoding="utf-8")
            db_path = Path(temp) / "out.duckdb"
            with patch("business_import.llm.create_client", side_effect=AssertionError("offline contacted model")), redirect_stdout(StringIO()):
                self.assertEqual(main(["--offline", "--roots", str(root), "--db", str(db_path)]), 0)
            with duckdb.connect(str(db_path)) as conn:
                self.assertEqual(conn.execute('SELECT * FROM biz_1.sales').fetchall(), [("001", "10")])




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

    def test_xlsx_inspection_handles_missing_sheet_dimensions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.xlsx"
            path.touch()
            from types import SimpleNamespace
            sheet = SimpleNamespace(title="unknown_dimensions", max_row=None, max_column=2,
                                    iter_rows=lambda values_only: iter([("id", "value"), ("001", "x")]))
            with patch("business_import.sources.load_workbook") as load_workbook:
                workbook = load_workbook.return_value
                workbook.worksheets = [sheet]
                inventory = inspect_business(Path(temp))
            self.assertEqual(inventory["files"][0]["tables"][0]["row_count"], 2)
            self.assertFalse(inventory["files"][0]["tables"][0]["is_empty"])

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

    def test_load_plan_repairs_model_using_qualified_destination_as_staging_source(self):
        staging = [{"name": "_stage_0", "columns": ["id"], "destination": "ban_hang"}]
        plan = {"tables": [{"source": "biz_101.ban_hang", "destination": "biz_101.ban_hang", "columns": ["id"],
            "sql": 'CREATE TABLE "biz_101"."ban_hang" AS SELECT "id" FROM "biz_101"."ban_hang"'}]}
        result = validate_load_plan(plan, staging, "biz_101")
        self.assertEqual(result, build_load_plan(staging, "biz_101"))

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
