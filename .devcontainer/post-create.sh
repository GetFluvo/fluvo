#!/usr/bin/env bash
# One-time setup for the fluvo dev container. Installs uv, restores the exact
# locked dependency set, and installs the pre-commit git hook. Idempotent: safe to
# re-run.
set -euo pipefail

echo "==> Installing uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

echo "==> Syncing dependencies from the frozen lockfile (uv.lock)"
uv sync --all-groups --frozen

echo "==> Installing the pre-commit git hook"
uv run pre-commit install

echo "==> Verifying nox is available"
uv run nox --version

cat <<'EOF'

==================================================================
 fluvo dev container ready.

   uv run fluvo --help        # the CLI
   uv run nox                 # the full local gate (what CI runs)
   uv run nox -s tests        # just the unit tests
   uv run nox -s pre-commit   # lint / format / pydoclint
   uv run pytest              # fast unit-test loop

 The end-to-end suite (uv run nox -s e2e) needs podman or docker on
 the host for the Odoo containers — see tests/e2e/README.md.
==================================================================
EOF
