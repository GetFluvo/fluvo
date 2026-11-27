# #359 verification findings — `account.move` posting on Odoo 16–19

Status: **verification complete; the contradiction was reported and resolved.**
The owner's decision (load reads state and refuses posted/cancelled targets before
any write, counting them as `failed`; add `sequence_date_mismatch`) is recorded in
[ADR 0001](0001-account-move-load-and-post.md), which this doc now supports.
Produced by `tests/e2e/probe_post_359.py` (a live-Odoo RPC probe), run on a fresh
Odoo per version. These are observations, not decisions.

## Per-version results

| Probe | 16.0 | 17.0 | 18.0 | 19.0 |
|---|---|---|---|---|
| **A1** preset `name` consistent with date survives `action_post` | survives | survives | survives | survives |
| **A2** preset `name` whose year ≠ date year | **raises at _create_** | raises at _post_ | raises at _post_ | raises at _post_ |
| **B** post a move dated ≤ `fiscalyear_lock_date` | **silent date-shift** (→ next period), posts | (field write blocked by RedirectWarning over RPC; see note) | **silent date-shift**, posts | **silent date-shift**, posts |
| **C** batch `action_post` with one unpostable move | all-or-nothing: **whole batch raises, all stay draft** | same | same | n/a (`deprecated` field removed — trigger unavailable) |
| **D** write to a **posted** move over RPC | ref/name/date/line-account **all mutate, no error**; stays posted; `unlink` blocked | same shape | ref/name/date/line-account **all mutate, no error**; writing a line **silently un-posts** (→ draft) | same as 18 |
| **E** `payment_reference` + forced receivable survive posting | both survive | both survive | both survive | both survive |

Exact messages (A2): 16 — *"The Date (12/31/2024) doesn't match the sequence number
of the related Journal Entry (2023-900)…"*; 18/19 — *"The Date … isn't aligned with
the existing sequence number…"*. (C): *"…account … is deprecated, you cannot post it."*

## What this confirms in ADR 0001

- **Lock dates (was pending).** Confirmed: posting a move dated on/before the
  fiscal-year lock date **silently shifts the accounting date** instead of raising
  (entries and invoices, 16/18/19). This is exactly why the ADR blocks such moves
  rather than attempting them. ✔ Keep as written.
- **Half-posted batch.** Confirmed: `action_post` over several moves is a single
  transaction — one unpostable move rolls the **whole batch** back (all stay
  draft). The ADR's per-move fallback is necessary. ✔
- **`payment_reference` + forced receivable/payable survive posting.** Confirmed on
  all four versions (relevant to the cutover invoices). ✔

## What contradicted ADR 0001 — now resolved in the ADR

1. **Posted moves are NOT immutable over RPC (foundational premise).**
   ADR 0001 Context: *"a posted `account.move` is immutable in Odoo … a re-run
   cannot update it. It can only fail, skip, or create a duplicate."* The probe
   shows the opposite over RPC: writing `ref`, `name`, `date`, and even a line's
   `account_id` to a **posted** move **succeeds and mutates it**, with no error —
   on 16 the move stays posted (its number/date/account silently rewritten); on
   18/19 a line write **silently resets the move to draft**. Only `unlink` is
   blocked (16).
   **Consequence:** the idempotent load writes over RPC, so on a re-run against a
   posted target it would **silently corrupt** posted accounting data (or un-post
   it) and count it as a success — the `posted_immutable → failed` classification
   never triggers, because Odoo never raises. The load step must therefore
   **read the target's `state` and refuse before writing** — it cannot rely on
   Odoo to protect posted entries. This is a design change to the load path, not
   just a reason-code rename, so it is bosd's call.

2. **`name_changed_on_post` guards a case that does not occur.**
   Odoo never *silently* renumbers a named draft on post. A `name` consistent with
   the date survives unchanged; an inconsistent one (year ≠ date) is **rejected**
   — at **create** on 16 (i.e. the *load* step, not post) and at **post** on
   17/18/19. So the real failure is a sequence/date `ValidationError`, which on 16
   surfaces during load. `name_changed_on_post` (post succeeds but name differs)
   would never fire. Suggest replacing it with a `sequence_date_mismatch` reason
   that can occur at load (16) or post (17–19); the cutover connector already
   avoids it by leaving open-item `name` blank.

## Side notes (not blocking)

- **Odoo 17**: setting `fiscalyear_lock_date` via a plain RPC `write` raises a
  `RedirectWarning` (Odoo wants its wizard), so the lock-shift couldn't be
  exercised on 17 through this path; `period_lock_date` writes fine but the admin
  user bypasses it. The shift behaviour is confirmed on 16/18/19.
- **Lock-date fields**: 16/17 expose both `fiscalyear_lock_date` and
  `period_lock_date`; 18/19 expose only `fiscalyear_lock_date`.
- **Odoo 19**: `account.account.deprecated` has been **removed**; any code reading
  or writing it (preflight/match/transform) must guard for its absence.
