# Project Roadmap

Where Fluvo is headed. This is deliberately short: a roadmap that lists ten things
and ships two is worse than one that lists a few and delivers them. Everything here
is tracked in the public [issue tracker](https://github.com/GetFluvo/fluvo/issues) —
follow or contribute to the linked issues for detail and status.

Fluvo's north star is unchanged: **get data into Odoo reliably and reproducibly, and
prove it landed.** No silent data loss, re-runnable imports, and exit codes automation
can trust. The items below extend that, they don't pivot from it.

## Near-term

- **Declarative flow runner** ([#251](https://github.com/GetFluvo/fluvo/issues/251)) —
  describe a whole migration as one reviewable `flows.yml` in git (an ordered set of
  import/export/write/migrate steps with explicit failure semantics) instead of a
  hand-maintained shell script. Until it lands, `--flow-file` fails loudly rather than
  pretending to run.

- **Safer multi-company imports** ([#255](https://github.com/GetFluvo/fluvo/issues/255)) —
  guard against silently importing into the wrong company (done), then first-class
  handling of company-dependent fields across several companies in one run.

- **First-class multi-language import** ([#254](https://github.com/GetFluvo/fluvo/issues/254)) —
  import translated field values cleanly across the languages installed in the target.

- **`fluvo assess`** — a fast, read-only pre-migration check that reports the shape and
  risk of a dataset against a target Odoo (models, fields, relations, likely problems)
  before you commit to a run. Read-only: it never writes.

## Ongoing

- **Connectors** — the source/target connectors are open core and the primary place we
  want community contributions. More sources (legacy ERPs, spreadsheets, other Odoo
  instances) and better round-tripping over time. See
  [CONTRIBUTING.md](https://github.com/GetFluvo/fluvo/blob/master/CONTRIBUTING.md).

- **Reconciliation & data quality** — deepen the integrity guarantees that already
  back every import (the `created + failed + unaccounted == total` contract) and the
  end-to-end suite that proves them against real Odoo 16–19.

## Supported versions & legacy

Fluvo targets **Odoo 16–19** over RPC. The old `InvoiceWorkflowV9` post-import workflow
(written for Odoo 9, using the removed `exec_workflow` API) is **legacy and unmaintained**
— it is retained only for reference and is excluded from the test/coverage gates. It is
not part of the roadmap above; if post-import workflows return, it will be as a new,
modern design, not a refactor of the v9 class.

---

Have a direction you'd like to see? Open an issue or start a discussion — early input
shapes what gets built.
