# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## Commands

Install dependencies (Python 3.10+):

```powershell
python -m pip install -r requirements.txt
```

Run the full test suite:

```powershell
python -m unittest discover -s tests -v
```

Run one test module or one test case:

```powershell
python -m unittest discover -s tests -p "test_business_import.py" -v
python -m unittest tests.test_business_import.ImportTests.test_import_is_rerunnable_without_duplicates -v
```

Run the importer for one business or all discovered businesses:

```powershell
python -m business_import --business biz_101
python -m business_import
```

Add `--offline` to skip the LLM call; offline imports only locally unambiguous file layouts:

```powershell
python -m business_import --business biz_101 --offline
```

There is no separate build or lint command configured in the repository. Tests use temporary fixtures and mocked LLM responses, so they do not require dataset files or API access.

## Architecture

The CLI in `business_import/__main__.py` coordinates a source-preserving pipeline: discover business directories and inspect CSV/XLSX/SQLite layouts (`sources.py`), resolve headers locally across full CSV grids and every Excel sheet, including report preambles (`layouts.py`), send the unique original header names plus the full README in one model call for business-wide type/format suggestions (`llm.py`), then match normalized column names without file scopes, then stage source data and generate canonical one-to-one copy SQL locally (`sql.py`). No source records, file/sheet identities or header positions are sent to the model. Unsupported all-text tables preserve every row with generated names, excluded from source-header input. SQLite-only businesses skip the model; SQLite in mixed businesses also retains native storage and contributes no header names. Undocumented or conflicting recommendations retain original storage with warnings. Formatless TIMESTAMP hints use local Vietnamese day-first/year-first formats and verified Excel epochs; explicit VARCHAR is never overridden by column-name heuristics. `column_types.py` validates every value before converting whole columns to allowlisted types; unsafe columns retain their original storage and produce catalog/CLI warnings. Only final tables are stored, without raw duplicates. `database.py` validates the load plan, writes each business transactionally into a DuckDB schema, and maintains the `_catalog` provenance schema. No model-written SQL is requested.

The default data roots are `data/train/data` and `data/public_test/data`; the default database is `tmp/businesses.duckdb`. Online mode uses `MODEL_API_KEY`, with optional `MODEL_BASE_URL` and `MODEL_NAME` overrides. Raw model responses are written to ignored `llm_out/` files. No records are filtered or joined. CSV/Excel default to text and SQLite retains native homogeneous types/tagged mixed types; safe model type proposals can convert columns. Actual types and fallback warnings are stored with the business description; no unit conversions are applied.

See `README.md` for setup, custom roots/database options, and database inspection examples.
