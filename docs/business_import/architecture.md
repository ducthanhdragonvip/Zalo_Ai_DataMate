# Business import architecture

`business_import` is a CLI pipeline that transforms CSV, Excel, and SQLite business datasets into DuckDB. Python owns layout detection, conversion validation, SQL generation, and persistence. The model supplies business-wide column type recommendations, not executable SQL.

For execution order and commands, see [the flow document](flow.md). The downstream consumer is [question_sql](../generate_sql/architecture.md).

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| `business_import/__main__.py` | Coordinate discovery, choose online/offline handling, resolve source layouts, map recommendations, construct destination names, and report per-business outcomes. |
| `sources.py` | Discover businesses, read README/source metadata, inspect supported formats, and preserve source values. |
| `layouts.py` | Normalize labels, identify local header candidates, and validate source layouts without silently dropping columns or skipping records. |
| `llm.py` | Configure the shared model client and request/validate business-wide type recommendations using README and column names. |
| `column_types.py` | Validate allowlisted conversion specifications and convert complete columns only when every retained value is safe. |
| `sql.py` | Quote identifiers and build locally controlled one-to-one copy SQL from staging tables to final destinations. |
| `database.py` | Validate the load plan, enforce ownership rules, stage values, manage per-business transactions, and maintain provenance/catalog records. |

## Data boundaries

### Source inventory and local layout

A business contains its identifier, source directory, dataset split, and README. Inspection produces an inventory of source files and their tables or worksheets. Local layout metadata identifies headers, data start, preambles, original labels, and warnings.

CSV/XLSX data grids stay local. Online header resolution can use README labels as evidence, but the model does not choose header positions. Unsupported layouts fail unless a specific preservation fallback applies.

SQLite follows a separate native-storage path: homogeneous values retain supported native types; heterogeneous storage uses tagged text so distinct values are not silently coerced into the same representation.

### Model recommendation boundary

The importer sends the full README and deduplicated source/README column names to the configured endpoint. It does not separately send source records, file/sheet identities, or header positions. README text itself can contain sensitive information.

Recommendations attach to normalized column names across the business, not to file-scoped aliases. Conflicting recommendations retain original storage with warnings. Missing recommendations normally retain text, with limited local date-name fallback. An explicit VARCHAR recommendation remains text.

SQLite-only businesses and offline runs bypass the model. Mixed-business SQLite tables do not contribute source header names.

### Conversion and load-plan boundary

Source tables and their validated layouts are mapped to destination names. CSV destinations derive from file names; XLSX/SQLite names also include worksheet/table identity. Names are normalized and collisions receive suffixes.

The load plan is generated locally and checked before execution. It copies staged tables one-to-one; it does not introduce joins or model-authored transformations.

Column conversion is all-or-nothing over the retained values. Unsafe types, invalid dates, ambiguous interpretations, overflow, or excessive precision preserve original storage and produce warnings. Supported parsing includes validated date formats, workbook epochs, numeric grouping, and VND notation; expressions are never evaluated.

## Persistence and ownership

One DuckDB file contains:

| Location | Purpose |
| --- | --- |
| `<business_id>.<destination_table>` | Final queryable business data. |
| `_catalog.businesses` | Business identity, imported README snapshot, description, status, and warnings. |
| `_catalog.tables` | Source-to-destination mappings, table metadata, and row counts. |

Staging tables are temporary. There is no persistent duplicate raw-data schema.

The transaction boundary is one business, not the entire batch. A successful reimport replaces importer-owned tables and updates catalog metadata. Ownership checks prevent overwriting unrelated schemas/tables. Failures roll back the business transaction; later businesses can still run.

## Preservation policy

Preservation does not mean every physical row becomes a transaction record:

- Preambles remain layout metadata.
- Exact repeated headers are excluded and counted.
- Narrowly recognized trailing Excel notes are retained in catalog metadata instead of transaction rows.
- A strictly validated final date-text outlier can be excluded with its full row preserved in metadata.

These exceptions must remain visible through catalog metadata and warnings. Generic filtering, joins, or silent row dropping are not part of the importer.

## Contract with question answering

`question_sql` relies on the loaded status, README snapshot, source mappings, and warnings in `_catalog`, then obtains actual table/column types from DuckDB's information schema. It checks that catalog mappings and physical tables agree.

Changes to catalog shape, ownership rules, table naming, or README persistence can therefore affect both packages. The SQL generator must use the imported state, not assume source file labels are destination identifiers.

## Configuration and verification

Both packages share `business_import.llm.create_client` and `.env` settings (`MODEL_API_KEY`, optional `MODEL_BASE_URL` and `MODEL_NAME`). Importer raw model responses are saved under ignored `llm_out/` files.

Importer, README, and conversion tests use temporary fixtures and mocked model responses. Run the commands in [flow.md](flow.md#checks), or run the full suite:

```powershell
python -m unittest discover -s tests -v
```
