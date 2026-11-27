# ADR 0001 — Loading and posting `account.move` under the re-run/upsert contract

**Status:** Accepted (#359 verification done; the load guard is implemented, the
post step follows under #358)

**Issues:** #358 (this decision), #359 (the on-real-Odoo verification —
see [the findings](0001-verification-359-findings.md))

## Context

Historical journal entries are only useful once they are **posted**. A posted
`account.move` must not be changed by a re-run — but the decisive #359 finding
is that **Odoo does not enforce that over RPC.** Verified on real Odoo
16/17/18/19 ([findings](0001-verification-359-findings.md)): a plain `write` to
a posted move's `ref`, `name`, `date` or a line's `account_id` *succeeds and
mutates it* with no error (on 18/19 a line write even silently resets the move
to `draft`). The only place Odoo itself blocks is a **hash-locked / inalterable
journal** (`restrict_mode_hash_table`), which is not switched on by default; the
probe saw only `unlink` refused (on 16). So the engine cannot rely on Odoo to
protect posted books — it must read the target's state and refuse before writing.

This collides head-on with Fluvo's central contract:

- records upsert on their external id, so re-running an import converges;
- every re-run is safe;
- nothing is silently dropped — `created + failed + unaccounted == total` holds
  on every run (`tests/e2e/assertions.py::assert_reconciled`).

A row that cannot be loaded because its target is already posted is not
`created`; it must be counted somewhere explicit or the reconciliation identity
breaks or becomes a lie. Protecting that identity — and protecting posted books
from a blind re-run — is the point of this decision.

## Decision

**Load always lands moves as `draft`; posting is a separate, explicit,
deliberately non-idempotent step.** The load stays idempotent and converges like
every other model; posting is the one declared one-way action. On top of that,
an already-posted (or cancelled) target on re-run is refused *before any write*
and routed to an *explicit* reconciliation category — never a silent skip, and
never a blind write onto posted books.

**The load reads target state and refuses up front (the guard).** Before writing
`account.move` rows (and their lines), the load reads the live `state` of the
targets that already exist — resolved by external id, in batches, not per row
(`lib/immutable.py`). A target that is not `draft` is refused without any write:
its row goes to the fail file under a named reason (`posted_immutable`,
`cancelled_immutable`), counts as `failed`, and `created + failed + unaccounted
== total` still holds. Lines whose parent move is guarded are refused with it
(no orphan line writes; the `--o2m` continuation rows and any second-pass /
deferred writes read the same, already-filtered dataset). If the state cannot be
read, the run aborts rather than proceed blind (fail closed). New moves and draft
moves behave exactly as before, and there is no extra RPC when a batch has no
existing targets. The guarded set is a small internal registry keyed by model
(`account.move`, `account.move.line`), not an `if model ==` in the write path,
so other state-guarded models can register later.

The guard sits on **every write path**, not just `load`: `fluvo write` applies
the same registry before its `write()` calls (a plain write to a posted move's
fields mutates it, and un-posts it on 18/19, exactly as a load would). It covers
records identified by **external id or database id** (`.id`, and the write path's
own `id` column), and treats a row with no id of its own and empty non-relational
cells as a one2many continuation that follows its parent's fate regardless of the
`--o2m` flag — so a line can never be left to land on the previous *draft* move
when its real parent was guarded out. A dangling `ir.model.data` row (pointing at
a record since deleted) is treated as "not there", so the load recreates it
rather than aborting. `export`, `assess` and `compare` are read-only and need no
guard; `migrate` runs through the same `load`/`write` engine and is covered by it.

The move is the unit of reconciliation: `total` counts moves, not lines.

### Resolved positions

- **Numbering.** The legacy entry number is mapped to `name` on the **draft**
  (invoice/entry numbers must survive the migration). *Verified on 16–19:* a
  `name` consistent with the move's date **survives `action_post` unchanged**, so
  Odoo never *silently* renumbers a named draft. An *inconsistent* name (its year
  ≠ the move's date year) is **rejected** — at **create** on Odoo 16 (i.e. the
  *load* step) and at **post** on 17–19. The real failure is therefore a
  sequence/date `ValidationError`: reason `sequence_date_mismatch`, raised at load
  on 16 and at post on 17–19 (Odoo's message is mapped to it in both places). The
  post step still asserts `name` is unchanged after posting as a cheap
  post-condition (reason `name_changed_on_post`), but on 16–19 that assertion is
  **not expected to fire** — it is a belt-and-braces guard against a future
  Odoo that renames silently. The cutover connector sidesteps the whole case by
  leaving open-item `name` blank and letting Odoo number the move.

- **Lock dates.** Fluvo never edits lock dates. The post step reads the company
  lock dates first; a move dated on or before an applicable lock is **not
  attempted**: `failed`, reason `lock_date` (the message names which lock),
  bucket `client_decision`. *Verified on 16/18/19:* posting a move dated on or
  before `fiscalyear_lock_date` **silently shifts the accounting date forward**
  instead of raising — exactly why the move is blocked rather than attempted.
  (On 17 the shift could not be exercised because setting the lock over a plain
  RPC `write` raises a `RedirectWarning`; the behaviour is confirmed on the
  other three.) The **per-lock** behaviour was verified on 16-19 (see the
  [findings](0001-verification-359-findings.md)): `fiscalyear_lock_date` and the
  18+ `sale_lock_date` / `purchase_lock_date` **silently shift**;
  `tax_lock_date` and `period_lock_date` posted a historical move at its
  original date (no shift) for the adviser user a migration runs as;
  `hard_lock_date` cannot even be set over RPC while draft entries exist. So the
  pre-check blocks on the proven-shift locks — `fiscalyear_lock_date`,
  `tax_lock_date` (kept as a conservative block, since it shifts a *tax-bearing*
  move and the step cannot cheaply tell which moves carry tax), `hard_lock_date`
  (irreversible), and the journal-gated `sale_`/`purchase_lock_date` — reading
  only the fields that exist (`fields_get`) and inheriting a branch's
  parent-company locks (17+); the strictest applicable one names the block.
  `period_lock_date` is **deliberately not** pre-blocked: blocking it would
  wrongly refuse a whole migration's pre-period moves, when the adviser user
  posts past it with no shift. The net for it — and for any lock exception or
  future field the pre-check cannot see — is a **date post-condition**: each
  posted move's `date` is compared before and after, and a silent shift is
  `failed`, reason `date_changed_on_post`.

- **Lines.** `line_ids/id` is **required and deterministic**
  (`<move_xmlid>_l<n>`). Preflight rejects an `account.move` import that lacks
  it, because without stable line external ids a re-run duplicates lines. Lines
  removed from the source between runs are **not** deleted. A post-load
  line-count mismatch is `failed`, reason `stale_lines`, bucket
  `client_decision`.

- **Half-posted batch.** Posting runs in batches with per-move fallback on a
  batch error. *Verified on 16/17/18:* `action_post` over several moves is a
  single transaction — one unpostable move rolls the **whole batch** back (all
  stay draft), so the per-move fallback is necessary. On re-run of the **post**
  step, an already-posted move is `unaccounted`, reason `already_posted`, so the
  step converges. On re-run of the **load** step, a posted target stays `failed`,
  reason `posted_immutable`.

### Reconciliation categories introduced

| Reason | Step | Bucket | Counts as |
|---|---|---|---|
| `posted_immutable` | load | `client_decision` | `failed` |
| `cancelled_immutable` | load | `client_decision` | `failed` |
| `sequence_date_mismatch` | load (16) / post (17–19) | `client_decision` | `failed` |
| `already_posted` | post | — | `unaccounted` |
| `name_changed_on_post` | post | — | `failed` |
| `date_changed_on_post` | post | `client_decision` | `failed` |
| `lock_date` | post | `client_decision` | `failed` |
| `stale_lines` | post | `client_decision` | `failed` |

Buckets map onto the owner taxonomy in `lib/buckets.py` (#299).

## Settled: load exit code on a full-flow re-run after posting

Earlier this was left open: on a **full-flow re-run after posting**, should the
load exit non-zero *forever* (a posted target → `posted_immutable` → `failed`),
or treat an unchanged posted move as `unaccounted` so a converged migration exits
0? **Settled: `failed`.** The load has no safe way to tell "already migrated and
posted" from "someone edited this source row and the target is now posted"; the
honest, conservative answer is to surface it loudly rather than exit 0 and hide a
would-be change. Operational consequence: those rows sit in the fail file, so a
`--fail` re-run retries and re-fails them (it never silently loads onto posted
books) until the operator resolves the conflict — un-post in Odoo, or drop the
row. This is intended, not a bug.

## Consequences

- The load step keeps the "re-run is safe" promise verbatim, and now also keeps
  posted books safe from a blind re-run that Odoo would otherwise allow over RPC.
- Only posting is one-way, and it is called out as such.
- The reconciliation identity stays true because every non-updatable row lands in
  an explicit, named category.
- #359 verified the positions above on real Odoo 16–19; its findings are recorded
  in [the findings doc](0001-verification-359-findings.md).
- The load guard is implemented in this change (`lib/immutable.py`, wired into
  `import_data`, tests in `tests/test_immutable.py`). The post step and its
  reasons (`sequence_date_mismatch`, `name_changed_on_post`, `lock_date`,
  `stale_lines`, `already_posted`) follow under #358.
