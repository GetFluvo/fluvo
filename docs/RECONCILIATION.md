# Fork-branch reconciliation matrix

Status of unmerged `bosd` fork feature branches relative to the release branch
`feature/production-ready-etl`. This is the **implement-or-drop gate** before the planned
big-bang squash-merge into `master`. Verified with `git cherry -v` (patch-id) plus content
inspection of each branch's unique commits.

All fork branches diverged Aug–Sep 2025 and are 124–157 commits behind the release branch, so any
salvage requires a fresh re-apply, not a clean merge.

| Branch | Unique commits | What it adds | Already in release? | Verdict |
|--------|---------------:|--------------|---------------------|---------|
| `bosd/feature/prevent-defer-required-fields-v3` | 1 | Don't defer **required** relational fields; CLI safeguard ignoring `--deferred-fields` for required fields | **Partial** — release `_plan_deferrals_and_strategies` checks `is_required` for auto-defer m2o (`preflight.py:491`) but **not** for m2m/o2m/self-ref paths; **no** importer.py safeguard | **IMPLEMENT (gap-fill)** |
| `bosd/feat/data-quality-module-final` + `odf-data-quality-dashboard` | 17 | Full Odoo addon `modules/odf_data_quality_dashboard/` (models, security, views, scheduled actions, readme) | **No** — not a Python-library concern; never present | **KEEP AS SEPARATE MODULE** — strongest open-core/premium candidate; home decided with monetization |
| `bosd/feat-hybrid-xmlid-export` | 6 (→ ~95 net lines in `export_threaded.py`) | Export XMLID in "hybrid read" mode | **Unknown** — #179 work was import-side Pass-2 resolution; this is export-side. Needs functional check | **VERIFY → IMPLEMENT** if not superseded |
| `bosd/feature/improve-import-coverage` | 3 (→ 676 test lines) | Coverage tests for importer/import_threaded/relational_import **+ real bug fix** in `_read_data_file` (skip-lines-before-header) | **No** | **SALVAGE** — port the `_read_data_file` fix + useful tests; coverage is 81% vs 85% gate |

## Detail & actions

### prevent-defer-required-fields → IMPLEMENT (gap-fill)
Release partially does this. Concrete gaps to close (proven by **e2e Scenario 5**):
1. In `_plan_deferrals_and_strategies` (`src/odoo_data_flow/lib/preflight.py:480`), extend the
   `not is_required` guard to the many2many, one2many, and self-referential many2one append paths,
   not just the non-self m2o auto-defer path.
2. Add the safeguard from the fork to `src/odoo_data_flow/importer.py`: if a user explicitly passes a
   required field via `--deferred-fields`, drop it from the deferral set and log a warning.
Source commit: `360f0a6` (also touches `tests/test_preflight.py`, `tests/test_importer.py`).

### data-quality dashboard → KEEP AS SEPARATE MODULE
`modules/odf_data_quality_dashboard/` is a self-contained OCA-style addon (own models/security/views/
cron). It does not belong in the `odoo_data_flow` Python package and must not block the library release.
It is the cleanest premium/open-core lever — park it on its own branch; decide repo home when
monetization is decided. No action needed for the production-ready milestone.

### hybrid-xmlid-export → VERIFY then IMPLEMENT
Adds an export mode emitting XMLIDs. Distinct direction from the #179 import-side work, so probably
genuinely missing. Before cherry-pick: confirm the release `export_threaded.py` has no equivalent
XMLID export path. If absent, port `2a28369`-series onto the release branch and cover with an
export→import roundtrip e2e (overlaps Scenario 4). Lower priority than the integrity suite.

### improve-import-coverage → SALVAGE
141 commits behind, so cherry-pick will conflict; treat as a reference. Port the genuine
`_read_data_file` header/skip-lines bug fix and any tests not duplicated by the new e2e suite. The e2e
integrity suite is the primary route back above the 85% coverage gate; harvest unit tests opportunistically.

## Squash-merge gate

The release branch may proceed to the big-bang squash-merge into `master` **after**: (a) the
prevent-defer gap-fill lands (or Scenario 5 confirms it's harmless), and (b) the e2e integrity suite is
green. The data-quality module and hybrid-xmlid-export are **non-blocking** follow-ups. The 96-branch
prune happens after the merge, once these salvage decisions are executed.
