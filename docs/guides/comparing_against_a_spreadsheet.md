# Comparing Odoo against a spreadsheet

`fluvo compare` answers a question that comes up on nearly every project:

> *Does what's in Odoo match this file?*

— and, with `--against-connection`, the same question about
[two live Odoo instances](#comparing-two-odoo-instances).

It is **read-only**. It never writes to Odoo. It can hand you a file that
`fluvo import` would load, but loading it stays a separate, deliberate step.

```bash
fluvo compare \
  --connection-file conf/connection.conf \
  --model res.partner \
  --file klanten.xlsx \
  --key vat \
  --fields name,street,city,country_id
```

## Why not a generic diff tool

Because a generic diff does not know Odoo. `country_id` *displays*
`Netherlands` and *stores* `166`. `active` is `True` in Odoo and `WAAR` in a
Dutch workbook. And the spreadsheet has usually damaged the data on its way out:
the account code `007` became `7`, the quantity `1` became `1.0`, the date became
the number `45000`.

Compared naively, every one of those is a difference, and a report that flags
everything tells you nothing. The diff is the easy part — **the normalisation is
the product.** `fluvo compare` reduces both sides to a canonical form first:

| Trap | The file | Odoo | Treated as |
|---|---|---|---|
| Numbers | `1.0`, `2,50`, `1.234.567,89` | `1`, `2.5`, `1234567.89` | equal |
| Leading zeros | `7` | `007` | equal |
| Booleans | `WAAR`, `Ja`, `1`, `X` | `True` | equal |
| Dates | `05-01-2024`, `45000` | `2024-01-05` | equal |
| many2one | `Netherlands` | `(166, 'Netherlands')` | equal |
| Selection | `Confirmed` | `sale` | equal |

## Two things it will not guess

**The locale.** Whether the comma is a decimal point or a thousands separator
changes what `1,100` means by a factor of a thousand, and the mistake is silent.
So you state it:

```bash
--locale dutch   # 1.234,56  (the default)
--locale anglo   # 1,234.56
```

The separator is only stripped where the number is genuinely grouped, so the
`1.0` a spreadsheet writes for the integer `1` is still read as one, not ten.

**The date order.** `05-01-2024` is the 5th of January or the 1st of May:

```bash
--date-order dmy   # the default
--date-order mdy
--date-order iso   # accept ISO dates only
```

The Odoo side is always read as Odoo writes it — dot decimals, ISO dates —
whatever you pass here.

## The key

`--key` is the business key both sides share: a VAT number, an SKU, an email, or
several columns together.

```bash
--key default_code
--key code,company_id      # composite
```

A key that Excel de-zeroed (`007` → `7`) still matches: the key goes through the
same repair as the values. A composite key matches only when **every** part is
filled on both sides; a row with a partial key is reported as unkeyed rather than
matched against the wrong record.

## Reading the result

Every row of both sides lands in exactly one bucket, so nothing disappears:

| Bucket | Meaning |
|---|---|
| Matched / identical | Found on both sides, same values |
| Differing | Found on both sides, at least one field differs |
| Only in file | No record in Odoo with that key |
| Only in Odoo | No row in the file with that key |
| Duplicate keys | The key is not unique on that side, so no comparison is possible |
| Unkeyed | The key is empty or partial |

The *differences* are reported one cell at a time — key, field, the file's value
and Odoo's value — so you can sort and filter them. The JSON output also carries
the two **normalised** values that were actually compared, which is what to look
at when you disagree with a verdict.

`Equal after numeric coercion` counts the cells that matched only because both
sides parsed as the same number — the `007`/`7` case. It is never hidden. If
leading zeros are meaningful to you and you want them reported, turn the
behaviour off:

```bash
--no-numeric-coercion
```

## Mapping differently-named columns

When the sheet's headers are not Odoo field names:

```bash
--map "BTW nummer=vat" --map "Klantnaam=name"
```

With `--fields` omitted, every column of the file that the model declares as a
field is compared.

## Comparing two Odoo instances

A migration asks the same question with two live systems instead of a file: *did
everything arrive, and did it arrive unchanged?* You can always answer it with
`fluvo export` followed by `fluvo compare --file`, and `--against-connection`
removes the intermediate file:

```bash
fluvo compare \
  --connection-file conf/new.conf \
  --against-connection conf/old.conf \
  --model res.partner \
  --key ref \
  --fields name,street,city,vat,country_id
```

Both sides are read over RPC. The source instance is the one named by
`--against-connection`, so *only in source Odoo* means **not migrated**, and
*only in target Odoo* means **created in the new system**.

Three differences from the file case:

**`--fields` is required.** There are no file columns to infer from, and comparing
every field of a model reports churn — `write_date`, computed values, sequence
numbers — instead of drift.

**The locale options do not apply,** and passing them is an error rather than a
no-op. Odoo writes dot decimals and ISO dates whatever the user's language, so
both sides are read that way.

**Each side can have its own filter.** `--against-domain` and `--against-context`
default to `--domain` and `--context`, which is usually what you want; pass
`--against-domain "[]"` to leave the source unfiltered.

An older source often needs a different transport, which `--against-protocol`
covers:

```bash
fluvo compare ... --protocol jsonrpc --against-protocol xmlrpc
```

A field the source instance does not have is reported as a setup error rather
than compared against nothing — the two schemas differing is exactly what you are
trying to find out about. A field whose *type* differs between the instances is
compared as the target's type, with a warning.

## Then fixing

```bash
fluvo compare ... --write-fixes diff.csv
```

writes a loadable CSV: the external id of each differing record plus the file's
values, in the canonical form Odoo accepts (`1`/`0` for a boolean, an ISO date, a
selection key). **Review it before loading it** — the spreadsheet is usually the
side that is wrong — then:

```bash
fluvo import --connection-file conf/connection.conf --model res.partner --file diff.csv
```

## In a pipeline

```bash
fluvo compare ... --fail-on-difference --format json --output report.json
```

exits `1` when anything at all does not line up, which makes the comparison
usable as a gate in CI or a nightly reconciliation check.

## Reading spreadsheets

`.csv` works out of the box. Reading `.xlsx` / `.ods` needs an engine:

```bash
pip install 'fluvo[excel]'
```

Every column is read as text on purpose. Letting a reader infer types would
re-apply the very mangling the comparison exists to see through.

## Related

- {doc}`assessing_a_source` — what is in the source Odoo before you migrate it.
- `fluvo.lib.mangling` — detectors and fixers for Excel-mangled reference
  columns, for when you want the damage *reported* rather than normalised away.
