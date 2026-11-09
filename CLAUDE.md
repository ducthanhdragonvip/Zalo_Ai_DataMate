# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
python -m unittest discover -s tests -p "test_business_import.py" -k test_import_is_rerunnable_without_duplicates -v
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

Generate answers from an already-imported database (output and details paths must not exist):

```powershell
python -m question_sql --questions data/train/questions.jsonl --output tmp/train_answers.jsonl --limit 5
python -m question_sql --help
```

Both CLIs accept `--db` (default `tmp/businesses.duckdb`); the importer also accepts `--roots`. Close DBeaver/other database connections if DuckDB reports a file lock.

There is no separate build or lint command configured in the repository. Tests use temporary fixtures and mocked LLM responses, so they do not require dataset files or API access.

## Architecture

### Business import

`business_import/__main__.py` coordinates discovery and CSV/XLSX/SQLite inspection (`sources.py`), local header resolution across full CSV grids and Excel sheets (`layouts.py`), business-wide type recommendations (`llm.py`), validated column conversion (`column_types.py`), locally generated copy SQL (`sql.py`), and transactional DuckDB writes (`database.py`). The default roots are `data/train/data` and `data/public_test/data`.

The importer makes one model call per online business with the full README and deduplicated source/README column names. It does not send source records, file/sheet identities or header positions separately, and does not ask the model to write SQL. Recommendations match normalized names across files, not file-scoped aliases. SQLite-only businesses skip the model; mixed-business SQLite tables retain native storage without contributing header names. Supported online all-text fallbacks preserve rows under generated names; ambiguous layouts otherwise fail rather than guess.

CSV/Excel default to text; SQLite retains native homogeneous types or tagged text for mixed storage. Whole-column conversions must validate every value; unsafe conversions retain original storage with warnings. Explicit VARCHAR recommendations remain text. Date parsing supports explicit formats, Vietnamese day-first/year-first defaults, verified workbook epochs and worksheet-period context for yearless dates. Numeric parsing includes validated grouping, VND suffix expansion and leading quantities with alphabetic units, without evaluating expressions.

Preambles and excluded source rows are tracked in metadata rather than silently lost. Exact repeated CSV/XLSX headers and narrowly recognized trailing worksheet notes are excluded from records; date cleanup can also exclude a single trailing text outlier under strict validation. Do not assume every physical source row becomes a database record. Imports do not join tables. Only final tables are stored, without raw duplicates. `database.py` validates load plans and replaces importer-owned tables transactionally per business; unrelated tables are not overwritten. `_catalog` stores the README snapshot, provenance, actual types and warnings and is also the contract consumed by `question_sql`.

### Question answering

`question_sql/runner.py` processes JSONL sequentially, validating questions and caching per-business context from `context.py`. Context checks `_catalog` against actual database tables and columns. `llm.py` makes one structured request selecting both answer type and SQL from the question, `as_of_date`, full imported README, actual schema/types, provenance and warnings. Source records and training answers are not model input. Relative dates use `as_of_date`, not the machine date; there are no model retries or repair calls.

`execution.py` spawns a worker, copies only the requested business into an in-memory DuckDB, detaches the read-only original, disables external access/extensions/spilling and locks configuration before executing exactly one SELECT/CTE. Defaults are 60 seconds including copying, 10,000 result rows, 256 MiB DuckDB memory and a 16 MiB payload. Limits fail explicitly rather than truncate. This is defense in depth, not an OS sandbox or a bound on all Python allocations.

`answers.py` validates result shapes and serializes exact decimals as JSON numbers. `unanswerable`/`abstain` skip execution. Answer records omit `alias`; SQL, raw results and stage-specific errors go to a separate `<output stem>.details.jsonl`. Both output files are created exclusively. Failed questions are omitted from answers, recorded in details, and make the batch exit nonzero while other questions continue.

### Configuration

Both packages share `business_import.llm.create_client` and `.env` settings: `MODEL_API_KEY`, optional `MODEL_BASE_URL` and `MODEL_NAME` (default `Qwen/Qwen3.5-9B`). Importer raw responses go to ignored `llm_out/`; question diagnostics can contain business records. See `README.md` for setup, custom paths and database inspection; check code when its older limitations conflict with current behavior.
