# Guide: Choosing the Right Options

`fluvo`'s `import` command has many options for performance, resilience, relational
data, and messy sources. This guide maps **common problems to the features that solve
them**, with links to the in-depth guides. For the complete, always-up-to-date option
list, see the [CLI reference](../usage.md).

## "My import is slow"

| Reach for | What it does |
|---|---|
| `--resolve-relation` | Resolve relation columns in Polars so Odoo runs **no `name_search`** ([Performance Tuning](performance_tuning.md)) |
| `--skip-unchanged` | On a re-import, send only new/changed rows via a vectorized anti-join |
| `--size` | Bigger batches (default 500) — fewer round-trips |
| `--worker` with `--groupby` | Parallel import without write deadlocks ([Performance Tuning](performance_tuning.md)) |
| JSON-RPC protocol | Faster transport than XML-RPC |

## "I'm importing relational data"

| Reach for | What it does |
|---|---|
| (automatic two-pass) | Relational fields are deferred to a second pass to avoid inverse-relation rewrites ([Performance Tuning](performance_tuning.md)) |
| `--deferred-fields` / `--auto-defer` | Manually or automatically defer specific relational fields |
| `--check-refs fail\|warn\|skip` | Pre-import validation that referenced XML IDs exist |
| `--auto-create-refs` | Auto-create missing related records for many2one fields |
| `--on-missing-ref field:action` | Per-field handling of a missing reference (create/skip/...) |
| `--set-empty-on-missing` | Set a relation to empty rather than failing when its target is missing |

## "I'm re-running an import (idempotency)"

| Reach for | What it does |
|---|---|
| `--skip-unchanged` | Send only rows whose values actually changed |
| `--skip-existing` | Skip records that already exist (by external id) |

## "My server is unstable, or the import is huge"

| Reach for | What it does |
|---|---|
| `--adaptive-throttle` | Auto-scale batch size to the server's health ([Performance Tuning](performance_tuning.md)) |
| `--resume` / `--no-checkpoint` | Resume an interrupted import from its checkpoint (or disable checkpointing) |
| `--max-batch-bytes` | Cap each batch's payload size |
| `--stream` | Stream very large CSVs without loading them fully into memory |

## "My source data is messy"

| Reach for | What it does |
|---|---|
| `--dry-run` | Validate the file against the model without importing |
| `--auto-clean` | Safe, type-aware coercions (whitespace, null tokens, booleans) before load |
| `--fallback-values field:value` | Defaults for invalid selection/boolean values |
| `--fail` | Route bad rows to a fail file instead of aborting the batch |

## "I'm hitting concurrent-update deadlocks"

| Reach for | What it does |
|---|---|
| `--groupby` | Partition work so workers never update the same parent concurrently ([Performance Tuning](performance_tuning.md)) |
| `--auto-groupby` | Let fluvo pick the partition column |

## "I'm importing hierarchical / nested data"

| Reach for | What it does |
|---|---|
| `--defer-parent-store` | Defer `parent_left`/`parent_right` recomputation for nested models |
| `--deferred-fields parent_id` | Defer the self-referencing field to a second pass |

## Product-specific

| Reach for | What it does |
|---|---|
| `--fix-missing-variants` | After a `product.template` import, create default variants for templates that have none (Odoo's `load()` doesn't) ([Odoo Workflows](odoo_workflows.md)) |
| `fluvo workflow create-missing-variants` | The same fix, runnable any time as a standalone command |

---

Every option above is also documented in full in the [CLI reference](../usage.md), and
the [Performance Tuning](performance_tuning.md) guide explains the speed-related ones in
depth.
