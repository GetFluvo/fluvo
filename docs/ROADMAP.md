# Project Roadmap

Where Fluvo is headed. This is deliberately short: a roadmap that lists ten things
and ships two is worse than one that lists a few and delivers them. Everything here
is tracked in the public [issue tracker](https://github.com/GetFluvo/fluvo/issues) —
follow or contribute to the linked issues for detail and status.

Fluvo's north star is unchanged: **get data into Odoo reliably and reproducibly, and
prove it landed.** No silent data loss, re-runnable imports, and exit codes automation
can trust. The items below extend that, they don't pivot from it.

## Recently shipped

Everything in the previous "Near-term" list has landed, as of **0.1.0**:

- **Declarative flow runner** ([#251](https://github.com/GetFluvo/fluvo/issues/251)) —
  describe a whole migration as one reviewable `flows.yml` in git: an ordered set of
  import/export/write/migrate steps with explicit failure semantics, instead of a
  hand-maintained shell script. YAML parsing is an optional extra
  (`pip install fluvo[flows]`); the rest of the core does not need it.

- **Safer multi-company imports** ([#255](https://github.com/GetFluvo/fluvo/issues/255)) —
  preflight guards against silently importing into the wrong company, plus handling
  of company-dependent fields across several companies in one run.

- **First-class multi-language import** ([#254](https://github.com/GetFluvo/fluvo/issues/254)) —
  translated field values load cleanly across the languages installed in the target.

- **`fluvo assess`** — a fast, read-only pre-migration check that reports the shape and
  risk of a dataset against a target Odoo (models, fields, relations, likely problems)
  before you commit to a run. `--profile` goes further and reads the values themselves:
  fill rates, columns that hold more than one population — the classic column mixing two
  units — mixed and ambiguous date formats, and categories that differ only by case or
  whitespace. Read-only: it never writes.

- **`fluvo compare`** ([#298](https://github.com/GetFluvo/fluvo/issues/298),
  [#350](https://github.com/GetFluvo/fluvo/issues/350)) — diff a live Odoo model against a
  spreadsheet, or against another Odoo instance in a single run, so you can see what a run
  would change, or check what it did.

- **Excel-mangling repair** ([#332](https://github.com/GetFluvo/fluvo/issues/332)) — detect
  and fix reference columns a spreadsheet has silently rewritten (scientific notation,
  dropped leading zeros, dates) before they reach Odoo.

- **Audit-file import (XAF)** — import a company's finances straight from the Dutch
  tax-authority audit file (XAF 3.2 and 4.0). The connector is a *compiler*: it stages
  Odoo-shaped CSVs and a `flows.yml` that the existing flow runner loads, so the
  reconciliation contract is unchanged. `fluvo auditfile inspect` is a free, read-only
  overview; `fluvo auditfile stage` compiles the file, with partner dedup, an editable
  account-type mapping, and cutover/full-history modes. See
  [ADR 0002](adr/0002-audit-file-connectors.md) and the
  [guide](guides/importing_from_an_audit_file.md).

- **Correct external ids in every export header** ([#339](https://github.com/GetFluvo/fluvo/issues/339)) —
  a bare `id` column now stays the external id even when a selection or binary field is
  present in the header. Previously it silently returned the database id instead, so
  re-importing the file created duplicates rather than updating.

## Near-term

- **Named exception buckets** ([#299](https://github.com/GetFluvo/fluvo/issues/299)) —
  classify import failures into named groups with a summary plus detail, instead of
  one flat fail file you have to read by eye. The fail file is how you find out *what*
  went wrong; today it does not tell you *how many kinds* of wrong.

- **Narrower Pass 2** ([#226](https://github.com/GetFluvo/fluvo/issues/226)) — stop
  deferring m2m/o2m values that point at records which already exist. Less deferral
  means fewer passes, less state carried between them, and a faster run.

## Ongoing

- **Connectors** — the source/target connectors are open core and the primary place we
  want community contributions. More sources (legacy ERPs, spreadsheets, other Odoo
  instances) and better round-tripping over time. See
  [CONTRIBUTING.md](https://github.com/GetFluvo/fluvo/blob/master/CONTRIBUTING.md).

- **More audit-file formats** — the audit-file connector's canonical model is SAF-T
  shaped so new formats are thin readers. Next, in rough priority: **DATEV** (the de
  facto DACH standard), **FEC** (France — mandatory, a clean flat file), the **SAF-T
  family** (Portugal, Poland, Luxembourg FAIA, Norway — structurally close to XAF 4.0),
  and **XAS** (Dutch payroll audit file). Formats may also ship out of tree via the
  `fluvo.auditfile_formats` entry-point group. The paid *assurance profile* is the
  per-format reconciliation report that proves, to an auditor's standard, that the
  migration landed right (open-item tie-out, trial-balance proof, post/reconcile
  sign-off).

- **Reconciliation & data quality** — deepen the integrity guarantees that already
  back every import (the `created + failed + unaccounted == total` contract) and the
  end-to-end suite that proves them against real Odoo 16–20, including resilience
  under a flaky server ([#197](https://github.com/GetFluvo/fluvo/issues/197)).

## Supported versions & legacy

Fluvo targets **Odoo 16–20** over RPC. The old `InvoiceWorkflowV9` post-import workflow
(written for Odoo 9, using the removed `exec_workflow` API) is **legacy and unmaintained**
— it is retained only for reference and is excluded from the test/coverage gates. It is
not part of the roadmap above; if post-import workflows return, it will be as a new,
modern design, not a refactor of the v9 class.

---

Have a direction you'd like to see? Open an issue or start a discussion — early input
shapes what gets built.
