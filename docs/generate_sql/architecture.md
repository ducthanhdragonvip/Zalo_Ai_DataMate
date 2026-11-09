# SQL generation and answering architecture

`question_sql` is a sequential JSONL batch processor over an already-imported DuckDB database. The model chooses an answer type and generates SQL; local Python validates the response, executes eligible SQL in a separate worker, and formats results without a second model call.

For execution order and commands, see [the flow document](flow.md). Its database producer is [business_import](../business_import/architecture.md).

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| `question_sql/__main__.py` | Parse required input/output paths and positive execution limits, then invoke the runner. |
| `runner.py` | Validate batch paths, manage read-only DB access and exclusive output creation, cache business context, coordinate question stages, and report failures. |
| `context.py` | Validate question records and build business context after checking catalog metadata against actual database tables. |
| `llm.py` | Construct reference DDL and prompt context, make one structured request, and validate the returned answer type and SQL. |
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

The resulting context contains the README snapshot, all actual column/type definitions, source file/sheet mappings, and import warnings. The runner caches it per business within a batch.

This makes `_catalog` an explicit integration contract with the importer, not optional descriptive metadata. Source file names alone are insufficient to infer destination table names.

## Model boundary

The model receives the question, business ID, reference date, quoted reference DDL, and full business context. Source records are not sent as query samples; the README can itself contain sensitive content.

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

Successful answer records contain `id`, `type`, and `answer`, without `alias`. `unanswerable` and `abstain` contain only `id` and `type`.

The formatter enforces one row/column for scalars and one column for lists. Tables contain `columns` and `rows`. Date answers use `YYYY-MM-DD`; nested native dates/times become ISO strings. Exact decimals serialize as JSON numbers without conversion through binary float. Percent values represent percentage points.

### Details file

The separate diagnostics JSONL contains original input, line number, SQL, selected type, result columns/rows, status, and stage-specific errors. Diagnostic decimals become strings; other values unsupported by ordinary JSON receive explicit diagnostic representations. This file can contain business records.

Both files use exclusive creation and incremental flushing. They are not a single atomic transaction: an interrupted or aborted run can leave partial files, and rerunning needs new paths.

## Error boundaries

The runner tracks `input`, `context`, `model`, `execution`, and `serialization` stages. Question-level failures produce a details record, omit the answer, and allow the next line to run. They are not converted into `unanswerable` or `abstain`.

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
