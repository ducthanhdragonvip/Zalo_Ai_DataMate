# Business Data Importer

Import business datasets from CSV, Excel (`.xlsx`), and SQLite (`.sqlite`) files into DuckDB. Each business is loaded into its own schema. The importer preserves source records, stores business/file descriptions in an importer catalog, and generates source-preserving load SQL locally.

## Requirements

- Python 3.10 or newer
- Dependencies listed in `requirements.txt`
- For model-assisted imports: a compatible Qwen API endpoint and API key

Install dependencies from the project root:

```powershell
python -m pip install -r requirements.txt
```

## Data layout

By default, the importer reads businesses from:

```text
data/train/data/<business_id>/
data/public_test/data/<business_id>/
```

Each business directory should contain a `README.md` and one or more supported source files (`.csv`, `.xlsx`, or `.sqlite`). The importer skips unsupported files. Keep business IDs unique across both roots.

## Run with the model

Create a `.env` file in the project root and set the API key:

```text
MODEL_API_KEY=your_api_key
```

The model-assisted run makes one call per business:

```text
Python detects CSV/XLSX headers → unique source names + full README
                              → Qwen recommends business-wide types
                              → Python validates complete columns → DuckDB
```

Python first resolves actual headers locally, then combines their original names with bold README names, deduplicated by Unicode normalization, case folding and collapsed whitespace. Qwen receives the full README and these names and returns a business-wide list of recommended types, including for columns missing from the README, for example `{"columns":[{"name":"Ngày tháng","type":"TIMESTAMP"},{"name":"Mã","type":"VARCHAR"}]}`. Bold rule headings are not column definitions. No file names, sheet mappings, header positions or data samples are requested. Optional format hints are based only on README context. Qwen does not write SQL, join tables, or apply unit conversions. The original README, recommendations, locally resolved mappings and actual loaded types are stored in `_catalog`.

Python examines every CSV and Excel worksheet independently, including report titles, context and blank rows before the header. It scans the full local source, not just preview rows. Exact documented-name matches support header detection even for all-text tables; ambiguous or unsupported layouts fail rather than guessing. Matching uses Unicode normalization, case folding and collapsed whitespace, without fuzzy aliases. Original source labels and all columns are preserved. Undocumented columns and conflicting recommendations retain their original storage with CLI/catalog warnings; repeated names use the same recommendation across files and sheets.

Only final tables are stored; there is no duplicate raw-data schema. Supported model suggestions are `VARCHAR`, `BIGINT`, `DECIMAL` (loaded as `DECIMAL(38,10)`), `BOOLEAN`, and `TIMESTAMP`. Dates use an explicit Python `strptime` format when documented; formatless `TIMESTAMP` recommendations use the local formats described below. Numbers use validated decimal/thousands separators. Padded numeric IDs, invalid dates, malformed grouping, overflow, excessive precision, prevent conversion of the entire column: original values remain unchanged and a warning is emitted. `None` remains SQL NULL. Empty strings become NULL when converting numeric columns (including VND) or dates through multi-format parsing; VARCHAR preserves empty strings. Whitespace-only numeric strings remain invalid. Booleans accept true/false only. Timezone-aware timestamps stay in their original representation until an explicit timezone policy is added.

Date conversion is model-guided with no column-name overrides. A single documented encoding uses `format`; mixed encodings use, for example, `{"type":"TIMESTAMP","formats":["%d/%m/%Y","%d thg %m %Y"]}`. A formatless `TIMESTAMP` uses the Vietnamese day-first convention (`dd/mm/yyyy`, `dd-mm-yyyy`, `d thg m yyyy`), year-first dates and ISO datetimes; month-first is not inferred. Numeric Excel dates use the actual workbook's verified epoch. CSV TIMESTAMP columns also accept Excel serial dates, assuming the standard 1900 date system (`45692` → `2025-02-04 00:00:00`); CSV exports using the 1904 system are not supported. Explicit date formats remain authoritative unless they include `excel_serial`. Every value must match unambiguously; invalid or conflicting interpretations retain the original column with a warning. Times are preserved and dates use midnight. `VARCHAR` hints remain unchanged even for date-looking names. For money columns of any name, Qwen can suggest `{"type":"DECIMAL","format":"VND"}` based on README monetary context. This explicitly selects local VND parsing: `17k` → `17000`, `1,287tr` → `1287000`, `1.98 tr` → `1980000`, `13.000đ` → `13000`, comma-grouped amounts, and plain numbers. If Qwen selects plain `DECIMAL` but omits the VND format, a `k`, `tr`, `đ`, or `₫` suffix triggers the same whole-column VND validation, including for mixed plain amounts and currency notation. No money-column name whitelist is used; `VARCHAR` hints remain text. This relies on Qwen correctly identifying numeric measures, not identifiers or non-money quantities. Every value must convert safely or the entire column retains its original storage with a warning; expressions such as `25k + topping 5k` are never evaluated.

Rows exactly repeating the entire original CSV/XLSX header are excluded from records; partial matches are retained. The removed count is included in CLI/catalog warnings. Null records are not removed. For CSV/XLSX, one final text outlier in a date-recommended column is excluded if every preceding nonempty value safely converts to TIMESTAMP and at least one date exists. The excluded full row is preserved in catalog metadata with a warning. Interior/multiple failures and numeric/date-looking invalid values are not removed; no total-label matching is used.

Actual loaded types and fallback warnings are recorded in each table's description in `_catalog.businesses.description_json`; aggregate warnings are also stored in `_catalog.businesses.warnings` and printed in the CLI result. Missing type suggestions keep existing storage types. Offline mode does not request type suggestions.

The configured endpoint receives instructions, unique detected CSV/XLSX header names and the full README. Source records, file/sheet identities and header positions are not sent separately; anything already written in the README is sent as part of that text. Grouped title rows are preserved as preamble, not sent as actual headers. Unsupported all-text tables retain every row with generated names, which are not sent as source headers. SQLite-only businesses bypass Qwen, and SQLite tables in mixed businesses retain native storage without contributing header names. The README may contain sensitive information, so use an endpoint you trust. Raw model responses are saved under ignored `llm_out/` files and may also contain sensitive data. Full prompts are not printed to the terminal.

Import one business:

```powershell
python -m business_import --business biz_101
```

Import all discovered businesses:

```powershell
python -m business_import
```

The model defaults to `Qwen/Qwen3.5-9B` and the endpoint configured in `business_import/llm.py`. Optional `.env` overrides are:

```text
MODEL_BASE_URL=https://your-compatible-endpoint/v1
MODEL_NAME=your-model-name
```

Do not commit `.env` or share your API key.

## Run offline

Offline mode skips the model call and only imports layouts the local structural checks consider unambiguous. It does not create model-generated descriptions or relationship guidance. Ambiguous layouts fail instead of being guessed.

```powershell
python -m business_import --business biz_101 --offline
python -m business_import --offline
```

## Custom data roots and database path

Use `--roots` to override the default data directories. Pass both roots if importing the usual train and public-test splits:

```powershell
python -m business_import --business biz_101 `
  --roots "C:\path\to\data\train\data" "C:\path\to\data\public_test\data"
```

Choose a database path with `--db`:

```powershell
python -m business_import --db "tmp\businesses.duckdb" --offline
```

By default, the database is written to `tmp/businesses.duckdb` under the project root. The `tmp/` directory is ignored by Git.

## Inspect the output

The database contains one schema per business, plus the reserved `_catalog` schema with importer metadata. For example, using Python:

```python
import duckdb

conn = duckdb.connect("tmp/businesses.duckdb", read_only=True)
print(conn.execute("SHOW SCHEMAS").fetchall())
print(conn.execute('SHOW TABLES FROM "biz_101"').fetchall())
print(conn.execute('SELECT * FROM "biz_101"."ban_hang" LIMIT 5').fetchall())
conn.close()
```

Use the actual destination table names from the database; file and sheet names are normalized for SQL and collisions receive suffixes.

## Profile imported business data

Run the separate, offline `business_profile` tool **after importing**:

```powershell
python -m business_profile --business biz_101
python -m business_profile
python -m business_profile --db tmp/businesses.duckdb --output-dir tmp/profiles
```

The default database is `tmp/businesses.duckdb`. The default output folder is `<database parent>/business_profiles/`, with one readable UTF-8 JSON file per business, such as `tmp/business_profiles/biz_101.json`. Without `--business`, all loaded catalog businesses are profiled. The database is opened read-only; no model or source-file access is needed. `--timeout` defaults to 60 seconds of query work per business; this is not a memory bound or OS sandbox. Close DBeaver/other connections if DuckDB reports a file lock.

JSON contains only `business_id` and `tables`. Tables with identical ordered column names and types share one combined profile: `names` lists the member tables, and column counts and category domains are computed across their combined rows using `UNION ALL`. Distinct counts are recomputed, not summed; overlapping rows are not deduplicated. Single-table profiles retain `name`. Column definitions/statistics appear only once per group. Supported scalar columns include distinct counts (excluding NULL). Floating NaN/infinities are reported via `nonfinite_count`. Unsupported types have an omission reason. No `row_count`, `null_count`, `empty_string_count`, `min`, `max`, `profile_version`, `source`, timestamps, README copies or sample rows are added.

Only allowlisted status/category/unit/payment text fields expose complete distinct `values`: at most 32 labels, each at most 128 characters. Larger domains are entirely omitted with `values_omitted`, not truncated. Identifier/contact/birth-date values and arbitrary text examples are excluded. Name-based rules are **not a PII detector**; misleading column names can still expose sensitive data, so review JSON before sharing it.

Reruns atomically replace matching business-profile JSON files, refuse unrelated files/symlink outputs, and preserve the previous file if generation or publication fails. Errors are reported per business; the batch continues and exits nonzero if any fail. Use a separate output directory for each database: JSON intentionally has no source identity or freshness metadata. **Rerun after reimport or manual data changes.** Profiling is manual; `question_sql` requires these saved files as described below.

## Generate SQL and answer questions

The separate `question_sql` package reads `questions.jsonl`. First generate profiles for the imported businesses. Each question with valid context makes **one model request** with a flat JSON payload: `question`, `business_id`, `as_of_date`, Python-computed `date_boundaries`, the complete imported `readme`, saved `business_profile`, `sources` mappings, and import `warnings`. The profile is the single schema representation; no duplicate DDL or live status-label queries are added. Grouped `names` represent separate tables with statistics/domains over their combined `UNION ALL`, not joins or per-table label guarantees.

The **full validated saved profile**, including actual bounded category labels, plus README/source mappings/warnings goes to the configured endpoint (`MODEL_API_KEY`, `MODEL_BASE_URL`, `MODEL_NAME`, with importer defaults). No transaction samples or training answers are added. Review this input before sharing: README and labels can be sensitive, and the profiler's name-based exclusions are not guaranteed PII detection. Category coverage still uses the profiler's existing allowlist; this integration does not discover semantics for arbitrary column names. README/status interpretation remains the model's job; valid SQL is not proof of accurate answers.

```powershell
python -m business_profile --business biz_101
python -m question_sql --questions data/train/questions.jsonl --output tmp/profile_answers.jsonl --limit 5

# Generate all loaded profiles when questions cover multiple businesses:
python -m business_profile
python -m question_sql --questions data/public_test/questions.jsonl --output tmp/public_answers.jsonl

python -m business_profile --db tmp/custom.duckdb --output-dir tmp/custom_profiles
python -m question_sql --db tmp/custom.duckdb --profile-dir tmp/custom_profiles --questions data/train/questions.jsonl --output tmp/custom_answers.jsonl --limit 5
```

The default DB is `tmp/businesses.duckdb`; override with `--db`. Profiles default to `<resolved database parent>/business_profiles`; override with `--profile-dir`. Each requested business needs `<business_id>.json` matching all actual tables and ordered column names/types. Missing, invalid, legacy-extra-field or schema-mismatched profiles fail at the context stage **before model access**, with a regeneration command; later questions continue. QA never generates profiles automatically.

**Rerun profiling after reimport or data changes and use separate profile directories per database.** Schema matching cannot detect stale statistics or a different database with the same schema: the format contains no freshness/source identity. Close DBeaver's database connection if DuckDB reports a lock. Both output paths must be new; existing files are never overwritten.

Successful answers contain `id`, `type`, and `answer`, without `alias`. Model `unanswerable`/`abstain` and failed questions with a valid ID are emitted as `unanswerable` with a `reason`:

```json
{"id":"q_001883280e83","type":"number","answer":89526400}
{"id":"q_example_missing","type":"unanswerable","reason":"Required data is unavailable."}
{"id":"q_example_uncertain","type":"unanswerable","reason":"Cannot determine a reliable answer."}
```

Supported types: `number`, `percent` (percentage points), `string`, `boolean`, `date` (`YYYY-MM-DD`), `list`, `table` (`{"columns": [...], "rows": [...]}`), `unanswerable` (required data unavailable), and `abstain` (model cannot confidently determine a correct query). The last two skip SQL execution. Numeric answers remain JSON numbers, including exact decimals. The same request selects the answer type and SQL; Python formats the executed results without another model call.

SQL, raw result rows, original questions, and stage-specific errors are saved separately to `<output stem>.details.jsonl`; override with `--details`. This diagnostics file can contain business data, so handle it accordingly. Failed questions with a valid nonempty Unicode ID also get an `unanswerable` answer with a stage-specific reason directing readers to details; malformed input without a usable ID remains details-only. Details retain the original error/status, so operational failures remain distinguishable from model nonanswers. The batch continues and exits nonzero if any question fails. Model failures are not retried or repaired; `--limit` caps input lines.

SQL runs in a spawned worker containing an in-memory copy of only the requested business's tables. The original DB is read-only and detached before model SQL runs. Exactly one SELECT/CTE is allowed; external access, extension autoload/install, and disk spilling are disabled, and configuration is locked. This is defense in depth, **not a hardened OS sandbox**. Default limits: `--timeout 60` seconds including copying, `--max-rows 10000`, 256 MiB DuckDB memory, and 16 MiB result payload. Limits fail explicitly, never silently truncate. Large businesses may exceed copy/memory limits; DuckDB's memory setting does not bound all Python allocations.

Review generated SQL before relying on answers: successful execution does not prove correct joins, date cutoffs, units, or aggregation. Relative dates use `as_of_date`, not the current machine date. Run `python -m question_sql --help` for options.

## Run tests

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary fixtures and mocked model responses; they do not call the model API or require the business datasets.

## Notes and limitations

- CSV/Excel columns use validated model-suggested types when available and safe; otherwise they remain text (native Excel dates are represented as ISO strings before validation). Leading-zero identifiers are not coerced to numbers.
- SQLite columns with a single observed storage type retain queryable native values where supported. Mixed-type columns use a tagged text representation to avoid silently coercing distinct source values.
- CSV/XLSX inspection considers the first 256 rows; the model sees at most three selected rows around a candidate header, or the first three if none is found. Header choices must refer to shown rows and pass local structural checks. All-text, single-column, header-only, or other unsupported layouts may fail; inspect them rather than weaken validation. Null/uncertain headers on nonempty sources fail safely.
- Imports are transactional per business. Existing importer-owned tables are replaced on successful reimport; unrelated/unowned schemas and tables are not overwritten.
- A business that fails validation or import is reported as an error; the CLI continues attempting other discovered businesses and exits nonzero if any failed.
- Run against your data and review the emitted per-business results before relying on the database.
