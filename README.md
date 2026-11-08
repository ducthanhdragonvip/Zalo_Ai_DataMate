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

Only final tables are stored; there is no duplicate raw-data schema. Supported model suggestions are `VARCHAR`, `BIGINT`, `DECIMAL` (loaded as `DECIMAL(38,10)`), `BOOLEAN`, and `TIMESTAMP`. Dates use an explicit Python `strptime` format when documented; formatless `TIMESTAMP` recommendations use the local formats described below. Numbers use validated decimal/thousands separators. Padded numeric IDs, invalid dates, malformed grouping, overflow, excessive precision, and empty strings prevent conversion of the entire column: original values remain unchanged and a warning is emitted. `None` remains SQL NULL; blank strings are not silently changed to NULL. Booleans accept true/false only. Timezone-aware timestamps stay in their original representation until an explicit timezone policy is added.

Date conversion is model-guided with no column-name overrides. A single documented encoding uses `format`; mixed encodings use, for example, `{"type":"TIMESTAMP","formats":["%d/%m/%Y","%d thg %m %Y"]}`. A formatless `TIMESTAMP` uses the Vietnamese day-first convention (`dd/mm/yyyy`, `dd-mm-yyyy`, `d thg m yyyy`), year-first dates and ISO datetimes; month-first is not inferred. Numeric Excel dates require the actual workbook's verified epoch. Every value must match unambiguously; invalid or conflicting interpretations retain the original column with a warning. Times are preserved and dates use midnight. `VARCHAR` hints remain unchanged even for date-looking names. For money columns of any name, Qwen can suggest `{"type":"DECIMAL","format":"VND"}` based on README monetary context. This explicitly selects local VND parsing: `17k` → `17000`, `1,287tr` → `1287000`, `1.98 tr` → `1980000`, `13.000đ` → `13000`, comma-grouped amounts, and plain numbers. If Qwen selects plain `DECIMAL` but omits the VND format, a `k`, `tr`, `đ`, or `₫` suffix triggers the same whole-column VND validation, including for mixed plain amounts and currency notation. No money-column name whitelist is used; `VARCHAR` hints remain text. This relies on Qwen correctly identifying numeric measures, not identifiers or non-money quantities. Every value must convert safely or the entire column retains its original storage with a warning; expressions such as `25k + topping 5k` are never evaluated.

Rows exactly repeating the entire original CSV/XLSX header are excluded from records; partial matches are retained. The removed count is included in CLI/catalog warnings. Null records are not removed.

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
