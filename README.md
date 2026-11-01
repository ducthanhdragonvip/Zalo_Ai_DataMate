# Business Data Importer

Import business datasets from CSV, Excel (`.xlsx`), and SQLite (`.sqlite`) files into DuckDB. Each business is loaded into its own schema. The importer preserves source records, stores business/file descriptions in an importer catalog, and validates model-proposed SQL before loading tables.

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

The model-assisted run makes two calls: one to describe the business and identify table layouts, then another to propose the source-preserving load SQL. The importer validates proposed layouts and SQL locally before loading. Each raw model response is saved as a uniquely named text file under `llm_out/` for debugging; these files may contain model output and are ignored by Git.

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

Offline mode skips both model calls and only imports layouts the local structural checks consider unambiguous. It does not create model-generated descriptions or relationship guidance. Ambiguous layouts fail instead of being guessed.

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

- CSV values are loaded as text; Excel values are loaded as text, with native date/time values represented as ISO strings. This avoids guessing types and preserves identifiers such as leading-zero codes.
- SQLite columns with a single observed storage type retain queryable native values where supported. Mixed-type columns use a tagged text representation to avoid silently coercing distinct source values.
- The importer masks likely records in model previews. Ambiguous or unsupported layouts should be inspected rather than forced through by changing validation.
- Imports are transactional per business. Existing importer-owned tables are replaced on successful reimport; unrelated/unowned schemas and tables are not overwritten.
- A business that fails validation or import is reported as an error; the CLI continues attempting other discovered businesses and exits nonzero if any failed.
- Run against your data and review the emitted per-business results before relying on the database.
