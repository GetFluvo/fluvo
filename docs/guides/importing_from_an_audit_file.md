# Guide: Importing from an audit file (XAF)

A tax-authority **audit file** is a complete, standardised export of a company's
financial year — the chart of accounts, every customer and supplier, the
journals, and every transaction. Many jurisdictions mandate it, so it is
reliably available, and it is the highest-fidelity source for migrating a
company's history into Odoo.

Fluvo imports the Dutch **XAF** (*XML Auditfile Financieel*), versions **3.2**
and **4.0**, today. The design generalises to the wider SAF-T family (see
[Adding a new format](#adding-a-new-format-for-contributors)).

## The idea: a compiler, not a loader

Fluvo treats an audit file as something to **compile**, not to load directly:

```text
audit file ──reader──▶ canonical ledger ──transform──▶ stage dir ──▶ fluvo --flow-file
 (per format)           (format-neutral)   (Odoo-shaped)   CSVs + flows.yml
```

The connector produces a **stage directory** of ordinary Odoo-shaped CSVs plus a
generated `flows.yml`. Loading is then the flow runner you already use, so the
reconciliation contract — upsert on external id, fail files, and
`created + failed + unaccounted == total` — comes from the existing engine
unchanged. There is no new write path to Odoo.

This also means every step is inspectable: you can read the staged CSVs, edit the
account mapping, and re-run, all before a single record reaches Odoo.

## Step 1 — inspect (no Odoo needed)

Start by looking at the file. `inspect` is read-only and needs no connection:

```bash
fluvo auditfile inspect company-2024.xaf
```

It reports the format and version, the producing software, the reporting period
and currency, the entity counts, and whether the file balances. Add `--json` for
a machine-readable summary.

## Step 2 — stage

`stage` compiles the file into a directory of CSVs and a `flows.yml`:

```bash
fluvo auditfile stage company-2024.xaf --out ./stage --connection-file prod.conf
```

This writes, into `./stage`:

| File | What it is |
|------|------------|
| `partners.csv` | `res.partner` rows (deduplicated) |
| `accounts.csv` | `account.account` rows |
| `journals.csv` | `account.journal` rows |
| `moves.csv` | `account.move` rows (draft), with their lines |
| `account_map.csv` | the **editable** account-typing proposals |
| `partner_aliases.csv` | every source id → surviving partner external id |
| `open_items.csv` | the per-partner open AR/AP breakdown (cutover) |
| `preflight.json` | the structured preflight report |
| `flows.yml` | the generated flow that loads the CSVs in order |

Nothing is written to Odoo. Staging is deterministic: the same file always
produces an identical stage.

### Preflight is fail-fast

Before writing `flows.yml`, `stage` runs a preflight over the data and prints a
summary. **Errors** (a move that does not balance; a line referencing an account
or journal that is not in the file) always block staging — Odoo would reject the
import. **Warnings** (a referenced customer/supplier that is not defined; a
non-EUR currency needing an FX review; account types that came from the numbering
heuristic; open items that do not tie out to the control balance) block by
default too, so you see them. Once you have reviewed them, re-run with
`--allow-warnings` to proceed. When preflight blocks, the data and
`preflight.json` are still written for inspection — only `flows.yml` is withheld.

### Account typing and `account_map.csv`

Fluvo proposes an Odoo `account_type` for every account through a chain, most
authoritative first:

1. **Your override** — an edited `account_map.csv` (see below). Always wins.
2. **RGS** — for XAF 4.0, a curated table maps the RGS reference code of the
   common control and liquidity accounts.
3. **The Dutch numbering heuristic** — e.g. `13xx` → receivable, `16xx` →
   payable, `10xx–12xx` → bank/cash, `8xxx` → revenue.

Heuristic and fallback results are **proposals**, never applied silently: they
are written to `account_map.csv` with a `source` column, and flagged in
preflight. To correct them, edit the `account_type` column of `account_map.csv`
and re-stage with it:

```bash
fluvo auditfile stage company-2024.xaf --out ./stage \
    --connection-file prod.conf --account-map ./stage/account_map.csv
```

Accounts typed receivable or payable are set `reconcile = True` automatically.

### Partner deduplication

Dutch source systems often hold one company as both a customer and a supplier,
as two records. Fluvo merges records that share a normalised Chamber-of-Commerce
(KvK) number or VAT number into a single `res.partner` carrying both
`customer_rank` and `supplier_rank`. The merge is lossless and auditable:
`partner_aliases.csv` records every source id and the surviving partner it maps
to, and transaction lines resolve through it.

## Step 3 — choose a mode

`--mode` controls what the transaction history becomes:

- **`full_history`** (default) — every transaction becomes a draft
  `account.move`, plus one opening-balance move. Use it to carry the complete
  ledger across.
- **`cutover`** — only an opening-balance move, but the AR/AP control totals are
  broken out **per open item** (customer/supplier + document reference), so open
  invoices carry over individually and can later be reconciled against incoming
  payments. Use it to start fresh in Odoo while keeping the open-invoice
  position.

## Step 4 — load

Loading is the ordinary flow runner, pointed at the generated flow:

```bash
fluvo --flow-file ./stage/flows.yml
```

The flow loads **partners → accounts → journals → moves** in that order. The
moves step uses Odoo's one-to-many load format (`--o2m`) so each move and its
lines are created together. Because every record carries a deterministic
external id, a second run **updates** rather than duplicates — re-running a
failed load converges.

## What lands, and what is deferred

Moves always land as **draft**. Posting a historical `account.move` is a separate,
explicit, deliberately non-idempotent step (see
[ADR 0001](../adr/0001-account-move-load-and-post.md)), and **auto-reconciliation
is not performed** by this connector yet. The staged data and the `open_items.csv`
breakdown are built so that a later post/reconcile step slots in without
reshaping anything.

## What XAF cannot give you

Verified against the official Belastingdienst XSDs, XAF (3.2 and 4.0) carries **no
fixed-asset register** — no purchase value, depreciation method, or depreciation
history. Asset book values appear only as ordinary ledger balances. Transferring
those alone yields a correct balance sheet but an incorrect depreciation state,
so a faithful asset migration needs a **separate source** (for example a fixed
-asset export from the source system); it cannot come from the audit file.

## Adding a new format (for contributors)

The canonical model is SAF-T–shaped on purpose, so a second format reader is
thin. A reader is any object with:

```python
class MyReader:
    format_name = "fec"  # the --format value

    def detect(self, path: str) -> bool:
        """Cheaply sniff whether this reader recognises the file."""
        ...

    def read(self, path: str) -> CanonicalLedger:
        """Parse the file into the canonical frames."""
        ...
```

`read` returns a
{py:class}`~fluvo.auditfile.canonical.CanonicalLedger` — the frames `header`,
`accounts`, `partners`, `journals`, `moves`, `lines`, `opening_lines` and
`vat_codes`, each with the fixed schema documented in
{py:mod}`fluvo.auditfile.canonical`. Map your format's own element names onto
those columns and normalise its code sets onto the canonical vocabularies
(partner role, account kind, journal kind); the transform, preflight and stage
layers then work unchanged.

For large files, stream the parse (don't build a DOM) and flush rows in chunks —
see {py:mod}`fluvo.auditfile.formats.xaf` for the pattern.

Register a built-in reader in the format registry, or ship one **out of tree**:
advertise the entry-point group `fluvo.auditfile_formats` from your own
distribution and Fluvo loads it on first use. See the
[roadmap](../ROADMAP.md) for the formats we want next (FEC, the SAF-T family,
DATEV).
