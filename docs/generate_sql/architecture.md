# SQL generation and answering architecture

`question_sql` is a sequential JSONL batch processor over an already-imported DuckDB database. The model chooses an answer type and generates SQL; local Python validates the response, executes eligible SQL in a separate worker, and formats results without a second model call.

For execution order and commands, see [the flow document](flow.md). Its database producer is [business_import](../business_import/architecture.md).

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| `question_sql/__main__.py` | Parse required input/output paths and positive execution limits, then invoke the runner. |
| `runner.py` | Validate batch paths, manage read-only DB access and exclusive output creation, cache business context, coordinate question stages, and report failures. |
| `context.py` | Validate question records, catalog/schema consistency and required saved profile JSON; provide catalog checks independently to the worker. |
| `llm.py` | Construct one flat profile/README payload, make one structured request, and validate the returned answer type and SQL. |
| `execution.py` | Spawn a worker, copy the requested business into memory, restrict DuckDB, enforce query/result limits, and return results to the parent. |
| `answers.py` | Validate result shapes/types and serialize answer records and diagnostics, preserving exact decimal answers. |

The package reuses `business_import.llm.create_client`, its model defaults, and `business_import.sql.quote_identifier`; it does not implement a second provider configuration layer.

## Input and context contracts

### Question record

Each JSONL line is an independent object requiring nonempty string fields `id`, `business_id`, `question`, and `as_of_date`. The business ID must be a valid schema identifier, and `as_of_date` must be a valid `YYYY-MM-DD` calendar date. Validation also rejects invalid Unicode and nonfinite JSON values.

`as_of_date` is the reference date supplied to the model for relative-date reasoning. Training answers are not an input.

### Imported business context

The parent process opens the database read-only. `context.py` requires:

- A loaded business with a nonempty imported README.
- Catalog mappings that match the actual schema's base tables.
- Actual columns and DuckDB types for every mapped table.
- A saved `<business_id>.json` profile under `--profile-dir` (default `<resolved database parent>/business_profiles`) matching every table and its ordered columns/types.

Profiles use exactly `{business_id, tables}`. Table entries have `name` or grouped `names`, plus `columns`. Groups share an ordered schema and describe combined `UNION ALL` rows; they do not imply joins or per-member label presence. Supported scalar columns have `distinct_count` excluding NULL, FLOAT/DOUBLE add `nonfinite_count`, and unsupported types have `stats_omitted`. VARCHAR domains are complete bounded `values` or an explicit omission reason. Unknown/legacy extras, invalid names/counts/domains and schema mismatches are rejected before model access with a regeneration command. QA does not query transaction records or regenerate statistics.

The resulting context contains the README snapshot, compact validated profile, source file/sheet mappings without duplicate columns, and import warnings. The runner caches it per business; dates are computed per question. Rerun the standalone profiler after data changes and keep directories separate per database: schema equality cannot detect stale or cross-database statistics because profiles store no freshness/source identity.

This makes `_catalog` an explicit integration contract with the importer, not optional descriptive metadata. Source file names alone are insufficient to infer destination table names.

## Model boundary

The model receives a flat JSON payload with exactly `question`, `business_id`, `as_of_date`, `date_boundaries`, `readme`, `business_profile`, `sources` and `warnings`. The full saved validated profile, including actual bounded category labels, is sent without duplicate DDL/nested schema or live status queries. Python supplies yesterday, previous Monday–Sunday week and as-of exclusive-end boundaries.

No transaction samples or reference answers are added. README, mappings, warnings and labels can contain sensitive content; review them before sending to the configured endpoint. The profiler's name-based exclusions do not guarantee PII detection, and its existing category-name allowlist still limits domain coverage. This integration does not discover arbitrary-column semantics.

The response contains exactly:

```json
{"type":"number","sql":"SELECT COUNT(*) FROM \"biz_101\".\"ban_hang\""}
```

Supported answer types are `number`, `percent`, `string`, `boolean`, `date`, `list`, `table`, `unanswerable`, and `abstain`. The latter two require null SQL and skip execution. Other types require a nonempty query string.

The runner disables client retries. Invalid or truncated responses fail explicitly; there is no SQL repair loop or second request for answer formatting. Prompt instructions about units, joins, date cutoffs, and schema usage guide generation but do not establish semantic correctness.

## Execution isolation

Each executable query gets a new multiprocessing worker using `spawn`:

1. Validate the requested business against the read-only original database.
2. Copy only that business's tables into an in-memory DuckDB.
3. Detach the original database before running model SQL.
4. Disable external access, extension autoload/install, and disk spilling, then lock configuration.
5. Require exactly one parsed SELECT statement; SELECT queries using CTEs are allowed.
6. Return bounded results over a multiprocessing pipe.

The parent drains the pipe before joining the worker so a larger result does not deadlock the child. On deadline expiry, it terminates the worker.

| Boundary | Default |
| --- | --- |
| Worker deadline, including copy time | 60 seconds |
| Result rows | 10,000 |
| DuckDB memory | 256 MiB |
| Serialized result payload | 16 MiB |

Row and payload overflow are errors, not silent truncation. Copying a large business can exhaust limits before query execution. These controls are defense in depth, not a hardened OS sandbox; DuckDB's memory limit does not bound all Python allocations.

## Output contracts

### Answer file

Successful answer records contain `id`, `type`, and `answer`, without `alias`. Model `unanswerable` and `abstain` both serialize as `{id, type: "unanswerable", reason}` with a local explanation; their original type remains in details.

The formatter enforces one row/column for scalars and one column for lists. Tables contain `columns` and `rows`. Date answers use `YYYY-MM-DD`; nested native dates/times become ISO strings. Exact decimals serialize as JSON numbers without conversion through binary float. Percent values represent percentage points.

### Details file

The separate diagnostics JSONL contains original input, line number, SQL, selected type, result columns/rows, status, and stage-specific errors. Diagnostic decimals become strings; other values unsupported by ordinary JSON receive explicit diagnostic representations. This file can contain business records.

Both files use exclusive creation and incremental flushing. They are not a single atomic transaction: an interrupted or aborted run can leave partial files, and rerunning needs new paths.

## Error boundaries

The runner tracks `input`, `context`, `model`, `execution`, and `serialization` stages. Question-level failures produce a details error record and an `unanswerable` answer with a stage-specific reason when the input has a valid nonempty Unicode ID. Inputs without a usable ID remain details-only. The next line still runs; error details distinguish operational failures from model nonanswers.

Output-writing failures abort the batch to avoid hiding data-loss errors. Any question failure makes the final exit status nonzero. `--limit` counts input lines rather than successful answers.

## Extension and verification boundaries

- Schema, catalog, or provenance changes must stay compatible with [business_import](../business_import/architecture.md).
- New answer types require agreement between the model response schema and local result formatter.
- Execution restrictions belong in the worker, not only in model instructions.
- Successful SQL execution is not proof of correct business reasoning; inspect SQL and diagnostics when evaluating answers.

Tests use temporary databases and mocked model responses, including process-based query execution:

```powershell
python -m unittest discover -s tests -p "test_question_sql.py" -v
```
