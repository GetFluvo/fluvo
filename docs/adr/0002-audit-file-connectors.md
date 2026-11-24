# ADR 0002 — Audit-file source connectors: a compiler, not a loader

## Status

Accepted. Implemented first for the Dutch XAF (3.2 and 4.0); the structure is
designed to extend to the wider SAF-T family (FEC, FAIA, Norwegian SAF-T) and
DATEV.

## Context

Tax-authority audit files are the highest-fidelity, most reliably available
source for migrating a company's finances into Odoo: a standardised, mandated
export of the chart of accounts, parties, journals and every transaction. We
want to import them without compromising the guarantees the rest of Fluvo
already provides — upsert on external id, fail files, and the
`created + failed + unaccounted == total` reconciliation identity — and without
a parser having to know anything about Odoo.

A naive design parses the file and writes to Odoo directly. That would duplicate
the import engine's retry, batching, external-id and reconciliation logic inside
the connector, and couple each new format to the Odoo write path.

## Decision

**An audit-file connector is a compiler, not a loader.** It turns an audit file
into a *stage directory* — ordinary Odoo-shaped CSVs plus a generated
`flows.yml` — that the existing flow runner loads with `fluvo --flow-file`. The
connector never writes to Odoo. The pipeline has four stable layers:

```text
audit file ──reader──▶ canonical ledger ──transform──▶ stage dir ──▶ fluvo --flow-file
 (per format)           (format-neutral)   (Odoo-shaped)   CSVs + flows.yml
```

1. **Canonical model** (`fluvo.auditfile.canonical`). A format-neutral,
   **columnar** representation (Polars frames: `header`, `accounts`, `partners`,
   `journals`, `moves`, `lines`, `opening_lines`, `vat_codes`, `anomalies`),
   modelled on the
   OECD **SAF-T** concepts of which XAF is the ancestor. Columnar, not a tree of
   per-record dicts: files exceed 100 MB and must not become Python objects. The
   schema is fixed and documented, so a reader and the transform agree at a
   checked seam.

2. **Format readers + registry** (`fluvo.auditfile.formats`). A reader answers
   two questions — *can you read this?* and *read it* — returning the canonical
   ledger. XAF streams with `iterparse` and bounded memory. The registry also
   loads the entry-point group `fluvo.auditfile_formats`, so a proprietary or
   country-specific format can ship **out of tree** without a change here.

3. **Transform** (`fluvo.auditfile.transform`). Pure functions, canonical →
   Odoo-shaped frames: deterministic external ids, transitive partner dedup
   (KvK/VAT) with a lossless alias map, account typing via a mapper chain
   (override → RGS → numbering heuristic, heuristic results flagged as
   proposals), and two modes (`cutover`, `full_history`).

4. **Preflight + stage** (`fluvo.auditfile.preflight`, `.stage`). Checks run over
   both the source and the **generated moves** and produce a structured,
   fail-fast report; stage writes the CSVs, the maps, `preflight.json` and the
   generated `flows.yml`.

### Cutover is the closing position

`cutover` stages the **closing position at the end of the period** — each
account's opening balance plus the year's movement — as one balanced opening
move, not a mix of year-start balances and this-year activity. Receivable and
payable control accounts are itemised **per open item**: the prior-year opening
subledger plus this year's control-account lines, netted per party and matching
key (explicit reconciliation key, else invoice reference, else document
reference), so a settled invoice drops out and a payment leaves no phantom entry;
any opening balance a subledger did not itemise is carried as a lumped remainder.
Profit-and-loss accounts carry by default or close into a chosen result account.
The move balances by construction, and preflight re-checks the generated move.

### Parse anomalies and completeness are reported, not hidden

A source amount or date that is present but unparseable is recorded in the
`anomalies` frame (never coerced silently to zero) and raised as a preflight
error. The file's own declared control totals (`openingBalance` and
`transactions` `linesCount`/`totalDebit`/`totalCredit`) are tied out against the
parse as the cheapest proof it is complete.

### Moves land draft; posting is a separate step

Historical moves are staged as **draft**, exactly as
[ADR 0001](0001-account-move-load-and-post.md) requires: move lines carry the
deterministic `<move_xmlid>_l<n>` external id so a re-run converges, and posting
— the one deliberately non-idempotent action — is left to the explicit post step
ADR 0001 defines. **Auto-reconciliation is not part of the connector.** The
staged data and the per-open-item `open_items.csv` are shaped so that a later
post/reconcile step slots in without reshaping anything.

### The XSDs are not vendored

The official Belastingdienst XAF schemas carry a "© XML Platform" notice and no
explicit reuse grant (the website's CC0 covers website text only). We therefore
do **not** vendor the XSDs; the reader validates structurally from its own code.
Readers verify element paths against the official schemas during development and
record the corrections in the pull request, but ship no copied schema file.

### Proposed: matching an existing chart

**Status: proposed, not implemented.** Today the transform *creates* every
account it finds, keyed by code. That is correct for a company set up empty, but
a company that already has a localisation chart (for example `l10n_nl`) already
owns those codes: creating them collides, and the file's accounts are never
matched onto the existing ones. Spec item C.1 ("map to the existing Odoo account
or create") is therefore only half-met, and the end-to-end test only exercises a
chart-less database.

The proposed design, to be built and validated on a charted database before it
is enabled:

- A new `stage --match-accounts` mode that, given a connection, reads the target
  company's `account.account` codes over RPC (read-only, during stage) and, for
  each file account whose code already exists, emits a **reference** to the
  existing record (by its external id, or by an `xmlid`/`id` resolved from the
  code) instead of a create. Accounts with no match are still created, and
  preflight reports the split ("N matched, M to create") so it is reviewable
  before load.
- Equivalently, an optional `target_xmlid` column in `account_map.csv`, so the
  mapping can be pinned and reviewed in the file the user already edits, without
  a live connection at stage time.
- The same question exists in principle for journals and for taxes; accounts are
  the blocking case and are addressed first.
- A second end-to-end scenario on a database with `l10n_nl` installed, asserting
  that staging matches the existing chart rather than colliding, is part of the
  acceptance for this work.

This is deliberately left as a proposal here rather than built, because the
matching rules (code normalisation, what to do on a type mismatch between the
file and the existing account, multi-company charts) want a design review first.

### Open core / premium line

The connector and preflight are **open core** — moving and validating data is
the core promise, and connectors are the primary place we want community
contributions. The line to a paid **assurance profile** is the per-format,
sign-off-grade **reconciliation report**: tying every open item out to its
control account, proving the migrated trial balance against the source file's
own declared totals, and the post/reconcile certification that a migration can
be signed off. The connector gets the data in and flags what needs a human; the
assurance profile is what proves, to an auditor's standard, that it landed right.

## Consequences

- A new format is a reader plus a mapping onto the canonical columns; the
  transform, preflight, stage and the entire Odoo write path are reused. Formats
  can also ship out of tree via the entry-point group.
- The reconciliation identity and idempotency are inherited from the flow runner,
  not re-implemented — there is one write path to reason about.
- Every stage is inspectable and deterministic: the staged CSVs and
  `account_map.csv` can be reviewed and edited before anything reaches Odoo, and
  the same file always produces an identical stage.
- Audit files cannot carry what they do not contain. XAF has **no fixed-asset
  register**; a faithful asset migration needs a separate source and is reported,
  not faked.
- **History imports as plain journal entries, not invoices.** There are no
  invoice documents and no `payment_state`; VAT codes are surfaced but lines
  carry no `tax_ids`/tax tags, so Odoo's VAT reports are empty for imported
  history. Invoice-level fidelity is a separate, heavier mapping, out of scope.
- **Money is floating-point** with a fixed `0.01` tolerance (ADR 0001's), not
  integer minor units; balances tie to the cent, and a file whose own rounding
  drifts further is flagged, not absorbed. Moving to integer cents is a possible
  future change, noted here as the known trade-off.
- **A pre-charted target is not yet supported** (see *Proposed: matching an
  existing chart*). The connector creates accounts by code, which collides with
  an installed localisation chart; matching is designed but not built.
- The post/reconcile step still wants validation against a real `l10n_nl` stack
  before production sign-off — tracked with the end-to-end work. Cutover's
  opening-balance semantics are now fixed (the closing position at period end).
