# Guide: Exporting Data from Odoo

In addition to importing, `fluvo` provides a powerful command-line utility for exporting data directly from Odoo into a structured CSV file. This is ideal for creating backups, feeding data into other systems, or for analysis.

```{mermaid}
---
config:
  theme: redux
---
flowchart TD
    ExportA["Odoo Instance"] L_ExportA_ExportB_0@--> ExportB{"fluvo export"}
    ExportC["Configuration<br>(CLI Options)"] --> ExportB
    ExportB L_ExportB_ExportD_0@--> ExportD["Output File<br>(e.g., exported_partners.csv)"]
    ExportA@{ shape: cyl}
    ExportD@{ shape: doc}
    style ExportA fill:#AA00FF
    style ExportB fill:#BBDEFB
    style ExportD fill:#FFF9C4
    L_ExportA_ExportB_0@{ animation: slow }
    L_ExportB_ExportD_0@{ animation: slow }
```


## The `fluvo export` Command

```text
Odoo Instance ---> (fluvo export) ---> Output File
      ^                      ^
      |                      |
(Database)      (Configuration / CLI Options)
```


The export process is handled by the `export` sub-command. It's a single-step operation designed for performance, reliability, and intelligent data handling.

### Smart Export Mode: Automatic Method Selection

The exporter features a **smart mode** that automatically uses the best Odoo API method for the data you request. This ensures you get the most accurate and performant export without manual configuration.

The tool will automatically use the high-performance **`read` method** if any of the following are true:

1.  You request a raw database ID using special syntax (e.g., `.id` or `country_id/.id`).
2.  You request a **`selection`** field (to get the raw key, e.g., `done`, instead of the label "Done").
3.  You request a **`binary`** field (as `export_data` cannot handle these).
4.  You manually force it with the `--technical-names` flag.

Otherwise, it defaults to the human-readable `export_data` method.

```{note}
Both export methods honour the `--context` you pass — including `lang` for translated fields. (Earlier versions ignored the context on the `read` path, so a `--context "{'lang': 'nl_NL'}"` combined with `.id`/`field/.id` or a technical field silently returned default-language values; that path now respects it.)
```


### High-Performance, Streaming Exports

The `export` command is built for scalability. To handle massive datasets, it uses a streaming pipeline:

* **Low Memory Usage:** Records are fetched in batches, processed, and written directly to the output file, ensuring a low memory footprint even for huge exports.
* **Type-Aware Cleaning:** The tool inspects Odoo field types to correct common data inconsistencies, like converting `False` values to empty strings for non-boolean fields.
* **Automatic Batch Resizing:** If the Odoo server runs out of memory on a large batch, the tool automatically splits the batch and retries, making the export process highly resilient.
* **Record Count Validation**: After a successful export, the tool automatically verifies that the number of rows in the output CSV file matches the number of records found in Odoo, providing an extra layer of data integrity.

### Command-Line Options

| Option | Description |
| :--- | :--- |
| `--config` | **Required**. Path to your `connection.conf` file. |
| `--model` | **Required**. The technical name of the Odoo model to export (e.g., `res.partner`). |
| `--fields` | **Required**. A comma-separated list of fields to export, with support for special ID specifiers. |
| `--output` | **Required**. The path and filename for the output CSV file. |
| `--domain` | A filter to select which records to export, using Odoo's domain syntax as a string. Defaults to `[]` (all records). |
| `--worker` | The number of parallel processes to use. Defaults to `1`. |
| `--size` | The number of records to fetch in a single batch. Defaults to `1000`. |
| `--sep` | The character separating columns. Defaults to a semicolon (`;`). |
| `--technical-names` | A flag to force the use of the high-performance raw export mode. Often enabled automatically. |
| `--languages` | Comma-separated language codes (e.g. `nl_NL,fr_FR`) to export translations for as `field@lang` columns. See [Exporting translations](#exporting-translations-fieldlang). |
| `--streaming` | A flag to enable streaming mode for very large datasets. Slower but uses minimal memory. |
| `--resume-session` | The ID of a failed export session to resume. The tool will append records to the existing output file. |
| `--since` | Weekly-delta: export only records changed since a timestamp (e.g. `2026-08-01`). Sugar that ANDs `(write_date >= …)` onto `--domain`. See [Weekly-delta pipelines](#weekly-delta-pipelines-since). |
| `--since-field` | The datetime field `--since` filters on. Defaults to `write_date` (use e.g. `create_date` for insert-only feeds). |


### Resuming Failed Exports

When exporting extremely large datasets, network outages or server restarts can interrupt the process. Starting over from the beginning is inefficient. To solve this, `fluvo` includes a session-based resume feature.

**How It Works**

1.  **Session ID Generation**: Every time a new export is started, a unique **Session ID** is generated based on the export parameters (model, domain, and fields). This ID is logged to the console.
2.  **State Tracking**: The tool creates a session directory inside `.fluvo_cache/sessions/`. It stores two files:
    *   `all_ids.json`: A complete list of all record IDs that match the export domain.
    *   `completed_ids.txt`: A list of record IDs that have been successfully exported and written to the CSV file. This file is updated after each batch.
3.  **Resuming**: If the export fails, you can restart it using the `--resume-session <session_id>` flag. The tool will:
    *   Read the two state files.
    *   Calculate the set of remaining IDs that still need to be exported.
    *   Continue the export process, fetching only the missing records and appending them to the output CSV file without a header.
4.  **Automatic Cleanup**: Upon a fully successful export, the corresponding session directory is automatically deleted to prevent clutter. If the job fails, the directory is kept, making it available for you to resume.

**Example Usage**

First, start a large export:

```bash
fluvo export \
    --config conf/connection.conf \
    --model "account.move.line" \
    --fields "id,name,move_id/.id,account_id/.id,debit,credit" \
    --output "data/all_journal_entries.csv"
```

The console will log the session ID:
`INFO - Starting new export session: a1b2c3d4e5f6a7b8`

If the process fails midway, you can find the session ID in the logs or in the final error message. To resume, simply add the `--resume-session` flag:

```bash
fluvo export \
    --config conf/connection.conf \
    --model "account.move.line" \
    --fields "id,name,move_id/.id,account_id/.id,debit,credit" \
    --output "data/all_journal_entries.csv" \
    --resume-session "a1b2c3d4e5f6a7b8"
```

The tool will then calculate the remaining records and continue where it left off.

### Understanding the `--domain` Filter

The `--domain` option allows you to precisely select which records to export. It uses Odoo's standard domain syntax, which is a list of tuples formatted as a string.

A domain is a list of search criteria. Each criterion is a tuple `('field_name', 'operator', 'value')`.

**Simple Domain Example:**
To export only companies (not individual contacts), the domain would be `[('is_company', '=', True)]`. You would pass this to the command line as a string:

`--domain "[('is_company', '=', True)]"`

**Complex Domain Example:**
To export all companies from the United States, you would combine two criteria:

`--domain "[('is_company', '=', True), ('country_id.code', '=', 'US')]"`

### Parquet output

Give `--output` a **`.parquet`** filename and Fluvo writes Parquet instead of CSV — a typed, compact intermediate for a downstream Polars-native transform (and the counterpart to importing a `.parquet` [source](importing_data.md#parquet-sources)). Parquet holds the whole result in memory to write it, so it cannot be combined with `--streaming`; use a `.csv` output for streaming exports. `--languages` translations work with Parquet output too.

### Weekly-delta pipelines (`--since`)

For a recurring migration or sync, you rarely want to move every record every run — only what changed. `--since` is convenience sugar for exactly that: it ANDs a change-timestamp term onto whatever `--domain` you pass, so you don't have to hand-write `write_date` filters.

```bash
# Only records changed on or after 2026-08-01, combined with any other --domain.
fluvo export --connection-file src.conf --model res.partner \
  --fields "id,name,email" --since 2026-08-01 --output partners_delta.csv
```

- The timestamp accepts a date (`2026-08-01`) or a datetime (`2026-08-01 09:00:00`).
- It filters on `write_date` by default; use `--since-field create_date` for insert-only feeds (records that are created but never updated).
- Because a term appended to any Odoo domain is implicitly AND-ed with the whole, `--since` composes correctly even with a `--domain` that contains `|` (OR).

**The re-runnable delta pattern.** Pair a delta export with an idempotent import so a re-run only touches what moved:

```bash
# 1. Extract only what changed since the last run.
fluvo export --connection-file src.conf --model res.partner \
  --fields "id,name,email" --since "$LAST_RUN" --output partners_delta.csv

# 2. Load idempotently — unchanged rows are skipped, changed rows updated.
fluvo import --connection-file dst.conf --model res.partner \
  --file partners_delta.csv --skip-unchanged
```

Record `$LAST_RUN` after a successful run (e.g. the run's start time) and feed it back next time. Both steps drop cleanly into a [`flows.yml`](importing_data.md) as `since:` / `skip_unchanged:` step options, so the whole delta pipeline is one `fluvo --flow-file` invocation.

### Specifying Fields with `--fields`

The `--fields` option is a simple comma-separated list of the field names you want in your output file. You can also access fields on related records using slash notation (/). The tool will log a warning if you request a field that does not exist on the Odoo model, and an empty column will be created in the output.

- Simple fields: `name,email,phone`
- Relational fields: `name,parent_id/id,parent_id/name` (This would get the contact's name, their parent company's XML ID, and their parent company's name).


It now has special syntax for handling different ID formats, making it powerful for data migration.

| Specifier | Mode Used | Resulting Value | Example |
| :--- | :--- | :--- | :--- |
| `id` | `export_data` | The record's XML ID (External ID) | `__export__.res_partner_123` |
| `.id` | `read` | The record's database ID (integer) | `123` |
| `field/id` | `export_data` | The related record's XML ID | `__export__.res_country_5` |
| `field/.id` | `read` | The related record's database ID (integer) | `5` |

The tool is smart: if you use `.id` or `field/.id`, it automatically switches to a high-performance "raw" export mode (using Odoo's `read` method). Otherwise, it defaults to a human-readable mode (using `export_data`).

### Exporting translations (`field@lang`)

Export the translations of translatable fields as extra `field@lang` columns — the exact same wide convention the [importer](importing_data.md) reads, so `export → import` is a lossless round-trip.

Two ways to request them, and they compose:

- **Explicit columns**: list `field@lang` tokens in `--fields`, e.g. `--fields "id,name,name@nl_NL,name@fr_FR"`.
- **`--languages` flag**: `--languages nl_NL,fr_FR` auto-expands *every* translatable field in `--fields` into one `field@lang` column per language. So `--fields "id,name,description" --languages nl_NL,fr_FR` emits `name@nl_NL`, `name@fr_FR`, `description@nl_NL`, `description@fr_FR` alongside the base columns.

```bash
fluvo export --connection-file conn.conf --model res.partner.category \
  --fields "id,name" --languages "nl_NL,fr_FR" --output categories.csv
```

produces:

```csv
id,name,name@nl_NL,name@fr_FR
__export__.tag_1,Customer,Klant,Client
__export__.tag_2,Vendor,Leverancier,Fournisseur
```

How it works and what to expect:

* The base (untranslated) columns are exported once; each language is then read in its own pass and stitched onto the same records. The records are selected **once** and the language passes are aligned by database id, so a `--domain` that filters a translated field still exports a consistent record set.
* It validates up front (like the importer): each base field must be translatable and each language must be installed — an unknown language or a non-translatable field aborts the export before writing anything.
* `--fields` must include an `id` (or `.id`) column: it is the join/re-import key. The export refuses without one.
* `--streaming` and `--resume-session` are not supported together with translations (the per-language columns are merged in memory before writing).
* **Round-trip caveat:** the round-trip is lossless only for records that have an external ID. Records without one export with an empty `id` (Odoo assigns none), and re-importing such a row **creates** a new record instead of updating the original — this is standard export/import behaviour, not specific to translations. Export `.id` (or ensure external IDs exist) if you need updates.

## Full Export Example

Let's combine these concepts into a full example. We want to export the name, email, and city for all individual contacts (not companies) located in Belgium.

Here is the full command you would run from your terminal:

```bash
fluvo export \
    --config conf/connection.conf \
    --model "res.partner" \
    --domain "[('is_company', '=', False), ('country_id.code', '=', 'BE')]" \
    --fields "id,name,email,city,country_id/id" \
    --output "data/belgian_contacts.csv"
```

### Result

This command will:

1.  Connect to the Odoo instance defined in `conf/connection.conf`.
2.  Search the `res.partner` model for records that are not companies and have their country set to Belgium.
3.  For each matching record, it will retrieve the `name`, `email`, `city`, and the `name` of the related country.
4.  It will save this data into a new CSV file located at `data/belgian_contacts.csv`.


#### Forcing Raw Export Mode

You can force the high-performance raw export mode using the `--technical-names` flag. This is useful if you need the raw values of `Many2one` fields (which will return the database ID) without explicitly using the `/.id` syntax.

**Example Usage:**

```bash
# Standard export with human-readable Many2one fields
fluvo export \
  --model "res.partner" \
  --fields "name,country_id"

# Export with the raw database ID for the country
fluvo export \
  --model "res.partner" \
  --fields "name,country_id/.id"

# Force raw export mode for all fields
fluvo export \
  --model "res.partner" \
  --fields "name,country_id" \
  --technical-names
```

### Automatic Batch Resizing

When exporting very large datasets, the Odoo server can sometimes run out of memory while preparing the data, causing the export of that batch to fail.

To make the process more resilient, this tool includes an **automatic batch resizing** feature. If the export of a specific batch fails due to a server-side `MemoryError`, the tool will not quit. Instead, it will:

1.  Automatically split the failed batch in half.
2.  Retry exporting each of the new, smaller sub-batches.
3.  This process continues recursively until the batch size is small enough for the server to process successfully.

This feature makes the export much more reliable and reduces the need to perfectly tune the `--batch-size` argument. However, for best performance, starting with a reasonable batch size (e.g., 1000-5000) is still recommended to avoid the small overhead of the retry mechanism.
