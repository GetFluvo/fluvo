# ADR 0001 — Loading and posting `account.move` under the re-run/upsert contract

**Status:** Proposed (records the intended semantics; no behaviour has changed yet)

**Issues:** #358 (this decision), #359 (the e2e proof that is blocked on it)

## Context

Historical journal entries are only useful once they are **posted**, and a
posted `account.move` is **immutable** in Odoo. That collides head-on with
Fluvo's central contract:

- records upsert on their external id, so re-running an import converges;
- every re-run is safe;
- nothing is silently dropped — `created + failed + unaccounted == total` holds
  on every run (`tests/e2e/assertions.py::assert_reconciled`).

None of that holds for a posted move: a re-run cannot update it. It can only
fail, skip, or create a duplicate. A row skipped because it is already posted is
not `created` and not `failed`; if it falls into neither and is not counted
somewhere explicit, the reconciliation identity either breaks or becomes a lie.
Protecting that identity is the point of this decision.

## Decision

**Load always lands moves as `draft`; posting is a separate, explicit,
deliberately non-idempotent step.** The load stays idempotent and converges like
every other model; posting is the one declared one-way action. On top of that,
an already-posted target on re-run is routed to an *explicit* reconciliation
category — never a silent skip.

The move is the unit of reconciliation: `total` counts moves, not lines.

### Resolved positions

- **Numbering.** The legacy entry number is mapped to `name` on the **draft**
  (invoice/entry numbers must survive the migration). The post step asserts
  `name` is unchanged after posting; a mismatch is `failed`, reason
  `name_changed_on_post`. *(Pending #359 verification across Odoo 16–19.)*

- **Lock dates.** Fluvo never edits lock dates. The post step reads the company
  lock dates first; a move dated on or before a lock date is **not attempted**:
  `failed`, reason `lock_date`, bucket `client_decision`. It is blocked rather
  than attempted because Odoo may silently shift the accounting date instead of
  raising. *(Pending #359 verification — believed true for invoices, unverified.)*

- **Lines.** `line_ids/id` is **required and deterministic**
  (`<move_xmlid>_l<n>`). Preflight rejects an `account.move` import that lacks
  it, because without stable line external ids a re-run duplicates lines. Lines
  removed from the source between runs are **not** deleted. A post-load
  line-count mismatch is `failed`, reason `stale_lines`, bucket
  `client_decision`.

- **Half-posted batch.** Posting runs in batches with per-move fallback on a
  batch error. On re-run of the **post** step, an already-posted move is
  `unaccounted`, reason `already_posted`, so the step converges. On re-run of the
  **load** step, a posted target stays `failed`, reason `posted_immutable`.

### Reconciliation categories introduced

| Reason | Step | Bucket | Counts as |
|---|---|---|---|
| `posted_immutable` | load | `client_decision` | `failed` |
| `already_posted` | post | — | `unaccounted` |
| `name_changed_on_post` | post | — | `failed` |
| `lock_date` | post | `client_decision` | `failed` |
| `stale_lines` | post | `client_decision` | `failed` |

Buckets map onto the owner taxonomy in `lib/buckets.py` (#299).

## Open question (owner decision — not settled here)

On a **full-flow re-run after posting**, should the run exit non-zero *forever*
(load sees a posted target → `posted_immutable` → `failed`), or should the load
step treat an unchanged posted move as `unaccounted` so a converged migration
exits 0 on subsequent runs? This is client-facing exit-code policy and is left
for the project owner. Until it is settled, the table above applies
(`posted_immutable` = `failed`).

## Consequences

- The load step keeps the "re-run is safe" promise verbatim; only posting is
  one-way, and it is called out as such.
- The reconciliation identity stays true because every non-updatable row lands in
  an explicit, named category.
- #359 can now assert against these categories; its results confirm the two
  positions marked *pending* above.
- No code changes accompany this ADR. It is the contract #359 and the
  accounting load/post implementation will be built against.
