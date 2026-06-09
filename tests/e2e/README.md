# End-to-end data-integrity suite

These tests exercise odoo-data-flow against a **real Odoo** to prove the thing that
unit tests can't: that no record is ever silently lost, mis-related, or
type-mangled during an import/export. They reproduce the failure classes we hit on
real migrations.

## Running

```bash
# Small tier (managed disposable Odoo 18 stack via podman/docker):
nox -s e2e

# Directly, keeping the stack up between runs for fast iteration:
ODF_E2E_SCALE=200 pytest tests/e2e -o addopts= -m "not large" --keep-stack

# Large stress tier (opt-in):
ODF_E2E_SCALE=100000 pytest tests/e2e -o addopts= -m large

# Against an existing Odoo (e.g. a doodba stack) instead of containers:
ODF_E2E_ODOO_URL=http://localhost:8069 ODF_E2E_DB=mydb pytest tests/e2e -o addopts=
```

The suite is excluded from the default `pytest` / `nox -s tests` run.

## Contract & knobs

- **Fresh target DB per run.** Assertions count records by a per-test name prefix and
  expect exact totals, so a clean database is assumed (CI provisions one each run).
  For local `--keep-stack` iteration, reset between full runs with a
  `DROP DATABASE ... WITH (FORCE)` + re-init, or use a new `ODF_E2E_DB`.
- `ODF_E2E_ODOO_URL` — use an external Odoo; skips container management.
- `ODF_E2E_ODOO_VERSION` (default `18.0`), `ODF_E2E_ODOO_PORT` (default `8069`).
- `ODF_E2E_DB` (default `odf_e2e_target`), `ODF_E2E_ADMIN_PWD` (default `admin`),
  `ODF_E2E_PROTOCOL` (default `jsonrpc`).
- `ODF_E2E_SCALE` — rows per scenario (default 200 local; CI 5000; large 100000+).

## Layout

- `docker-compose.yml` — Postgres + Odoo stack (version via env).
- `_runtime.py` — runtime-agnostic compose driver (podman/docker auto-detect).
- `conftest.py` — fixtures: endpoint lifecycle, DB provisioning, connection, RPC, scale.
- `generators.py` — deterministic dataset builders (data is generated, never committed).
- `assertions.py` — the import drivers + the four integrity checks (reconciliation,
  DB truth, fail-file completeness, relational correctness).
- `test_integrity_*.py` — the scenarios.

## Scenario coverage

| # | Test | Guards against |
|---|------|----------------|
| 1 | relations: duplicate ids | silent overwrite/drop |
| 2 | relations: dangling ref | silent FK drop |
| 3 | relations: self-ref hierarchy | two-pass ordering |
| 4 | relations: cross-model xmlid | Pass-2 resolution (#179) |
| 5 | required_defer | deferring a required relation (fixed: importer safeguard) |
| 6 | failures: malformed rows | crash vs graceful fail + fallback |
| 7 | failures: mixed-type column | Polars inference crash |
| 8 | roundtrip: datetime export | type-cast null-out (odoo_lib map) |
| 10 | idempotent: skip-existing | duplicate creation on re-run |
| 11 | scale (large) | throughput/deadlock under workers+groupby |

Scenario 9 (load→create fallback) is exercised by #6 (binary-search fallback fires on
the malformed rows). Scenario 12 (checkpoint/crash resume) is a planned addition.
