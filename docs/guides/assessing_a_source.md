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

## Company audit

For **company-aware** models (those with a `company_id`), the report also breaks down how existing records are spread across companies — e.g. `by company: 1:4200, 2:0`. This makes wrong-company data that is *already in the database* visible at a glance: a company with a suspicious `0`, or everything sitting under a single company on a multi-company database, is the fingerprint of a migration that ran without choosing a company (the failure the import guard now prevents going forward). It appears in the risk-flags column and, in full, under `company_distribution` in the JSON/Markdown output.

## Value profiling with `--profile`

Everything above is **metadata**: field types, counts, flags. It never looks at the values, so it cannot see the problems that actually sink a migration. `--profile` reads a bounded sample of rows and profiles the values themselves:

```bash
fluvo assess --connection-file source.conf \
  --models product.template \
  --profile --group-by categ_id
```

It is opt-in because it is the only part of `assess` that transfers data rather than metadata. Each model is capped at `--profile-limit` rows (10,000 by default), and the report states what it was computed from — "all rows", or "a sample of 10,000 of 450,000 rows" — because a fill rate from a sample is an estimate and saying so is the point of an assessment.

### What it looks for

| Check | What it flags |
| --- | --- |
| **Fill rate** | How much of each column is actually populated. Null, blank and Odoo's `False`-for-unset all count as empty. |
| **Order-of-magnitude populations** | A numeric column whose values fall into two *separated* decade clusters — `multi-population`. |
| **Whole-vs-decimal split** | A numeric column whose integral rows sit two or more decades away from its fractional rows — `whole-vs-decimal`. |
| **Date-format drift** | More than one format variant in one column (`mixed-formats`), values that match none (`unrecognised-values`), and values where day-first and month-first both parse but disagree (`day-month-ambiguous`). |
| **Dirty enumerations** | For low-cardinality text: the tail of rarely-used values (`tail-values`) and members that fold together once case and whitespace are normalised (`case-space-collisions`). |
| **Unqualified values** | A well-populated measurement column whose qualifier — unit of measure, currency, language — is sparse or empty (`unqualified-values`). |

### Why two numeric tests

They catch the same problem from different directions, and neither alone is enough.

Suppose one numeric column holds the same quantity recorded in two different units — the base unit on most rows, a sub-unit on the rest. The sub-unit values are around a thousand times larger, so the **decade histogram** splits into two clusters with empty decades between them. That is the clean case.

But the two ranges often overlap enough to close the gap. What usually survives is that one unit is recorded as a whole number and the other carries decimals, so comparing the **median decade of the integral rows against the fractional rows** still separates them. A column is flagged if either test fires.

`--group-by` repeats the population test inside each value of a field, which matters because a mix that is invisible across a whole table is often obvious within one product category.

### Locale is a parameter, never a guess

Whether `1.234` means one thousand two hundred or one-and-a-bit decides every numeric finding on the page. Fluvo will not infer it:

```bash
# comma as the decimal separator (continental convention)
fluvo assess --connection-file source.conf --profile --decimal-comma
```

Getting this wrong shifts a whole column by a decade and invents findings that are not there, which is why there is a flag rather than a heuristic.

### A flag is a question, not a defect

`--profile` reports; it never changes anything. A flag says a column holds more than one kind of value, and somebody has to decide which kind is correct — that decision needs the business, not the tool. Where the problem is a *known* spreadsheet corruption rather than an open question, {doc}`the mangling detectors <../usage>` repair it; detect and fix are deliberately separate commands.

## Output as a handout

By default the report prints as a console table. For an artifact you can share, write it to a file:

```bash
# Machine-readable JSON (default when --output is given):
fluvo assess --connection-file source.conf --output assessment.json

# A Markdown table, e.g. to paste into a scoping document:
fluvo assess --connection-file source.conf --format markdown --output assessment.md
```

```{note}
`assess` is read-only. Without `--profile` it only calls `fields_get` and `search_count`; with it, it additionally calls `search_read` for the sampled rows. It never writes. Models it cannot read (access rules) are skipped with a warning rather than aborting the run, and a model whose rows cannot be counted is reported with an unknown (`?`) count.
```
