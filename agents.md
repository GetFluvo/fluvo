# Agent instructions for fluvo

Guidance for AI coding agents working in this repository.

## What fluvo is

fluvo is a **Python package** (a library plus a CLI) — **not** an Odoo module, and
it is not installed inside Odoo. It is a Polars-backed ETL engine that imports,
exports, and migrates data by talking to **Odoo 16–19 over RPC**
(xmlrpc / jsonrpc / json2). Because it connects over RPC, fluvo's own Python
version is decoupled from the target Odoo's — you can drive an old Odoo database
from a modern Python.

- Source lives in `src/fluvo/`. The CLI entry point is `fluvo = fluvo.__main__:cli`.
- Release wheels are **mypyc-compiled** (`FLUVO_COMPILE_MYPYC=1`); the code must
  stay mypyc-compatible.
- License is **LGPL-3.0**. fluvo is a derivative of Thibault Francois'
  `odoo_csv_import`; see `NOTICE`. Do not propose relicensing the core.
- There is **no Odoo-version-from-branch-name** convention. Ignore any such
  instruction — that scheme was dropped. Target Odoo versions are handled at
  runtime over RPC, and exercised by the e2e suite (Odoo 16–19), not by branches.

## Toolchain

Everything runs through **uv** and **nox**. Install once:

```sh
uv sync --all-groups --frozen   # exact locked deps (uv.lock is committed)
```

Then use nox sessions (each is isolated):

```sh
uv run nox                          # the full local gate (what CI runs)
uv run nox -s tests pre-commit mypy # fast iteration subset
uv run nox -s tests                 # unit tests (tests/)
uv run nox -s pre-commit            # ruff lint+format, pydoclint, file hygiene
uv run nox -s mypy                  # strict type check
uv run nox -s e2e                   # end-to-end integrity suite (see below)
```

Available sessions: `pre-commit`, `mypy`, `tests`, `typeguard`, `xdoctest`,
`docs-build`, `coverage`, `e2e`.

## Quality rules (enforced by CI — the `all-checks` gate)

1. **Tests.** Add or update tests for every change; coverage is gated. Unit tests
   are in `tests/`. Integrity behaviour (no silent data loss, relations, roundtrip
   types, idempotency) belongs in `tests/e2e/` — read `tests/e2e/README.md`.
2. **Types.** Fully type-hinted; `mypy --strict` must pass with zero errors. Use
   lowercase builtins (`list`, `dict`, `X | None`).
3. **Lint/format.** `ruff` (pinned to the version in `.pre-commit-config.yaml`);
   line length 88. Run `uv run nox -s pre-commit` before committing.
4. **Docstrings.** Google-style, checked by **pydoclint** against the committed
   `pydoclint-baseline.txt`. Write complete `Args:`/`Returns:`. If you deliberately
   change docstrings, refresh the baseline:
   `uv run pydoclint --generate-baseline=True $(git ls-files '*.py')`.

## The e2e suite

`tests/e2e` drives fluvo against a **real Odoo** in a disposable container to prove
the guarantees unit tests can't (reconciliation, relational correctness). It is
excluded from the default `tests` run and needs **podman or docker** on the host.
Run it with `uv run nox -s e2e`; see `tests/e2e/README.md` for scenarios and knobs.

## Working agreement

- Work on a feature branch off `master`; open a PR. `master` is protected and
  requires the `all-checks` gate to be green.
- Outside contributors sign the CLA on their first PR — see `CONTRIBUTING.md`.
- Keep changes focused; update docs when behaviour or the CLI changes.
