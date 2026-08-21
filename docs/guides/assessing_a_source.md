# Guide: Assessing a Source with `fluvo assess`

Before you migrate anything, you want to know what you are dealing with: which entities exist, how much data there is, and where the hard parts are. `fluvo assess` connects to a source Odoo and produces a migration-readiness report — an inventory you can read at a glance or hand to a stakeholder in place of a paid discovery call.

## Usage

```bash
fluvo assess --connection-file source.conf
```

With no `--models`, it assesses a curated set of common migration models that exist on the database. To scope it, pass a comma-separated list:

```bash
fluvo assess --connection-file source.conf \
  --models "res.partner,product.template,account.move"
```

## What it reports

For each model:

- **Rows** — the record volume (`search_count`).
- **Fields** — the total field count (a proxy for mapping effort).
- **Risk flags** — the things that make a migration harder than a flat copy:
  - **company-dependent** fields — need per-company handling (see [`field@company`](importing_data.md)).
  - **translated** fields — need per-language passes (see [`field@lang`](importing_data.md)); dropping them is silent data loss.
  - **relational** density (many2one / many2many / one2many) — drives two-pass import complexity.
  - **required** fields — must be present in the source data.
  - **computed (not importable)** — read-only, non-stored fields an import cannot set.
  - **binary** fields — large payloads (images, attachments).

## Output as a handout

By default the report prints as a console table. For an artifact you can share, write it to a file:

```bash
# Machine-readable JSON (default when --output is given):
fluvo assess --connection-file source.conf --output assessment.json

# A Markdown table, e.g. to paste into a scoping document:
fluvo assess --connection-file source.conf --format markdown --output assessment.md
```

```{note}
`assess` is read-only — it only calls `fields_get` and `search_count`. Models it cannot read (access rules) are skipped with a warning rather than aborting the run, and a model whose rows cannot be counted is reported with an unknown (`?`) count.
```
