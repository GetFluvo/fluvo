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
| `partner_banks.csv` | `res.partner.bank` rows (one per partner with a bank) |
| `accounts.csv` | `account.account` rows (only those to **create**) |
| `journals.csv` | `account.journal` rows |
| `moves.csv` | `account.move` rows (draft), with their lines |
| `account_map.csv` | the **editable** account-typing proposals |
| `partner_aliases.csv` | every source id → surviving partner external id |
| `open_invoices.csv` | one draft invoice/bill per open AR/AP item (cutover) |
| `open_items.csv` | the netted open-item breakdown, for audit (cutover) |
| `preflight.json` | the structured preflight report |
| `flows.yml` | the generated flow that loads the CSVs in order |

Nothing is written to Odoo. Staging is deterministic: the same file always
produces an identical stage.

### Preflight is fail-fast

Before writing `flows.yml`, `stage` runs a preflight over both the source data
and the **generated moves** and prints a summary.

**Errors** always block — Odoo would reject the import, or the data is wrong:

- a source transaction, or a *generated* move (opening or history), that does
  not balance;
- a line referencing an account or journal not in the file;
- a duplicate move or move-line external id (two records collapsed onto one id);
- a **parse anomaly** — a source amount or date that was present but could not
  be parsed. It is recorded, never silently coerced to zero, and listed with the
  offending value so you can fix the file or the export.

**Warnings** block by default too (so you see them); review, then re-run with
`--allow-warnings`:

- a referenced customer/supplier that is not defined;
- a non-EUR file currency, or an individual line in a foreign currency, needing
  an FX review (only the base amount is staged);
- account types that came from the numbering heuristic, or an account whose type
  contradicts the source's balance/P&L kind;
- the file's own declared control totals not matching what was parsed (a
  completeness check — the file may be truncated or non-standard);
- in cutover, a control account whose opening subledger does not tie to its GL
  opening balance.

When preflight blocks, the data and `preflight.json` are still written for
inspection — only `flows.yml` is withheld, and a stale one from an earlier,
passing run is removed so nothing looks runnable that is not.

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
`customer_rank` and `supplier_rank`. Matching is **transitive**: a record known
only by KvK and one known only by VAT still merge when a third record ties the
two together. The merge is lossless and auditable: `partner_aliases.csv` records
every source id and the surviving partner it maps to, and transaction lines
resolve through it. Pass `--no-dedup` to keep every source record as its own
partner.

The partner **country** is staged (resolved to its Odoo external id, e.g.
`base.nl`), and a partner **bank account** becomes a `res.partner.bank` row
(`partner_banks.csv`), loaded as its own step after the partners. The bank step
is omitted when no partner carries an account.

## Step 3 — choose a mode

`--mode` controls what the transaction history becomes:

- **`full_history`** (default) — every transaction becomes a draft
  `account.move`, plus one opening-balance move carrying the GL opening balances
  as-is. Use it to carry the complete ledger across.
- **`cutover`** — the **closing position at the end of the period**: each
  account's opening balance plus the year's movement. Each open AR/AP item
  becomes a **real draft invoice or bill** (`out_invoice` / `in_invoice`, or
  `out_refund` / `in_refund` for a credit note or an unallocated payment), one
  per item, carrying the **residual** open amount as a single tax-free line on a
  dedicated **clearing account**, with the original number, date, derived due
  date and partner. The receivable/payable line lands on the **source control
  account** (the connector stages the partner's receivable/payable property to
  force it, rather than Odoo's default). The one opening move then carries each
  account's closing balance, with the clearing account holding the counter-amount
  of all those invoices (so it nets to zero) instead of the AR/AP totals. Open
  items are the prior-year opening subledger plus this year's lines, matched on
  an explicit reconciliation key, else the invoice reference, else the document
  reference, so a settled invoice drops out and a payment never leaves a phantom
  entry. Use it to start fresh in Odoo while keeping the open invoices as
  invoices — so they show in the aged balance and can be reconciled against
  incoming payments.

  The invoices land **draft**, like the moves: reminders and follow-up only see
  them once they are posted (ADR 0001). The original total is not reconstructed —
  only the residual — so do not expect a payment history on them. Because XAF
  carries no due date, each is derived from the invoice date plus
  `--default-payment-days` (30 by default) and preflight reports the count.

  Profit-and-loss accounts carry their closing balance by default. Pass
  `--result-account <code>` to close them into one result/equity account instead,
  for a clean balance-sheet-only opening. Pass `--clearing-account <code>` to
  choose the clearing account (a synthetic one is created otherwise). Every
  staged move balances, and preflight errors if the clearing account does not
  net to zero.

## Step 4 — load

Loading is the ordinary flow runner, pointed at the generated flow:

```bash
fluvo --flow-file ./stage/flows.yml
```

The flow loads **accounts → journals → partners → partner banks → moves → open
invoices** in that order (accounts before partners, because a partner's
receivable/payable property references an account; the bank and open-invoice
steps appear only when they have rows). The moves and open-invoice steps use
Odoo's one-to-many load format (`--o2m`) so each move/invoice and its lines are
created together. Because every record carries a deterministic external id, a
second run **updates** rather than duplicates — re-running a failed load
converges.

## What lands, and what is deferred

Everything lands as **draft** — the moves, and the cutover open-item invoices.
Posting a historical `account.move` or invoice is a separate, explicit,
deliberately non-idempotent step (see
[ADR 0001](../adr/0001-account-move-load-and-post.md)), and **auto-reconciliation
is not performed** by this connector yet. **Reminders and follow-up only see the
open-item invoices once they are posted.** The staged data is built so that a
later post/reconcile step slots in without reshaping anything.

**`full_history` lands plain journal entries, not invoices.** It creates
`account.move` records with lines, not `account.move` *invoices*: no invoice/bill
documents, no `payment_state`, no customer/vendor invoice lists. VAT codes are
read and surfaced, but the staged lines carry **no taxes or tax tags**, so Odoo's
VAT/tax reports are empty for imported history — the figures live in the ledger
balances, not the tax engine. (The `cutover` open-item invoices *are* real
invoices, but they too carry a single tax-free residual line, not the original
tax detail.) Recreating prior-period tax reports or full invoice documents is a
separate, heavier mapping this connector does not attempt.

**Money is handled as floating-point** throughout, compared with a fixed `0.01`
tolerance (the same tolerance the import engine and [ADR
0001](../adr/0001-account-move-load-and-post.md) use). Balances therefore tie out
to the cent, not to exact integer minor units; a file whose own rounding differs
by more than a cent per move is surfaced by preflight rather than absorbed.

## Targeting a company that already has a chart of accounts

By default the connector **creates** the accounts it finds, keyed by code. That
fits a company set up empty. On a target that already has a localisation chart
(for example `l10n_nl`), you instead want the file's accounts **matched** onto
the accounts already there, not re-created. Two ways, which combine:

- **`--match-accounts`** reads the target chart over `--connection-file` and, for
  each file account whose code already exists, references the existing account
  instead of creating one:

  ```bash
  fluvo auditfile stage company-2024.xaf --out ./stage \
      --connection-file prod.conf --match-accounts
  ```

  Only codes that are **unique** in the target and already carry an external id
  are matched; an ambiguous code (the same code on several accounts) or one with
  no external id is left unmatched and the account is created as usual. Matched
  accounts are **referenced, never modified** — the connector does not touch the
  name, type or any other field of an account that already exists. The reads are
  the only time `stage` talks to Odoo, and it only reads.

- **`target_xmlid` in `account_map.csv`** pins an account onto an existing
  external id by hand, with no live connection — useful to review or override
  the matches in the file you already edit. A filled `target_xmlid` wins.

Matched accounts are left out of `accounts.csv` (nothing to create) and every
move and opening line references the existing account's external id. `stage`
reports how many accounts were matched versus created.

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
`accounts`, `partners`, `journals`, `moves`, `lines`, `opening_lines`,
`vat_codes` and `anomalies`, each with the fixed schema documented in
{py:mod}`fluvo.auditfile.canonical`. Map your format's own element names onto
those columns and normalise its code sets onto the canonical vocabularies
(partner role, account kind, journal kind); record a present-but-unparseable
cell in `anomalies` rather than dropping it; the transform, preflight and stage
layers then work unchanged.

For large files, stream the parse (don't build a DOM) and flush rows in chunks —
see {py:mod}`fluvo.auditfile.formats.xaf` for the pattern.

Register a built-in reader in the format registry, or ship one **out of tree**:
advertise the entry-point group `fluvo.auditfile_formats` from your own
distribution and Fluvo loads it on first use. See the
[roadmap](../ROADMAP.md) for the formats we want next (FEC, the SAF-T family,
DATEV).
