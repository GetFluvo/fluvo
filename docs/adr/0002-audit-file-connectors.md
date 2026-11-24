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
   `journals`, `moves`, `lines`, `opening_lines`, `vat_codes`), modelled on the
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
   Odoo-shaped frames: deterministic external ids, partner dedup with a lossless
   alias map, account typing via a mapper chain (override → RGS → numbering
   heuristic, heuristic results flagged as proposals), and two modes
   (`cutover`, `full_history`).

4. **Preflight + stage** (`fluvo.auditfile.preflight`, `.stage`). Canonical-level
   checks produce a structured, fail-fast report; stage writes the CSVs, the
   maps, `preflight.json` and the generated `flows.yml`.

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
- Cutover's opening-balance semantics (which trial balance; period-start vs
  cutover date) and the post/reconcile step still want validation against a real
  `l10n_nl` stack before production sign-off — tracked with the end-to-end work.
