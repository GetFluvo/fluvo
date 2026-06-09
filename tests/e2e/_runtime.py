"""Container-runtime helpers for the e2e suite.

Runtime-agnostic: prefers ``docker compose`` and falls back to ``podman compose``
so the same suite runs locally (podman, the dev default) and in CI (docker).

Nothing here is imported by the unit-test suite; ``tests/e2e`` is excluded from the
default pytest run and only the ``e2e`` nox session pulls it in.
"""

from __future__ import annotations

import socket
import subprocess
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

COMPOSE_FILE = str(Path(__file__).with_name("docker-compose.yml"))
PROJECT = "fluvo_e2e"


@lru_cache(maxsize=1)
def compose_base() -> list[str]:
    """Return the working ``<runtime> compose`` invocation, or raise.

    Returns:
        The base command list, e.g. ``["docker", "compose"]``.

    Raises:
        RuntimeError: If neither docker nor podman compose is usable.
    """
    for base in (["docker", "compose"], ["podman", "compose"]):
        try:
            subprocess.run(
                [*base, "version"],
                capture_output=True,
                check=True,
                timeout=30,
            )
            return base
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            continue
    raise RuntimeError(
        "No usable container compose runtime found (tried 'docker compose' and "
        "'podman compose'). Install one, or set FLUVO_E2E_ODOO_URL to use an "
        "external Odoo (e.g. a doodba stack)."
    )


def _compose(
    *args: str, check: bool = True, timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    """Run a compose subcommand against the e2e project."""
    cmd = [*compose_base(), "-p", PROJECT, "-f", COMPOSE_FILE, *args]
    return subprocess.run(
        cmd, check=check, timeout=timeout, capture_output=True, text=True
    )


def up() -> None:
    """Bring the stack up (idempotent)."""
    _compose("up", "-d", timeout=900)


def down() -> None:
    """Tear the stack down, removing volumes."""
    _compose("down", "-v", check=False, timeout=300)


def exec_in(
    service: str, command: list[str], timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    """Run a command inside a running service container (no TTY)."""
    return _compose("exec", "-T", service, *command, timeout=timeout)


def create_database(db_name: str, timeout: int = 600) -> None:
    """Initialise a fresh Odoo database with the ``base`` module, no demo data.

    Idempotent-ish: Odoo errors if the DB already exists, which the caller may
    choose to ignore.

    Args:
        db_name: Database to create.
        timeout: Seconds to allow for the (slow) base install.
    """
    exec_in(
        "odoo",
        [
            "odoo",
            "-d",
            db_name,
            "-i",
            "base",
            "--without-demo=True",
            "--db_host=db",
            "--db_user=odoo",
            "--db_password=odoo",
            "--stop-after-init",
            "--log-level=warn",
        ],
        timeout=timeout,
    )


def database_exists(db_name: str) -> bool:
    """Return True if the Postgres database already exists."""
    result = exec_in(
        "db",
        [
            "psql",
            "-U",
            "odoo",
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname='{db_name}'",
        ],
        timeout=60,
    )
    return result.stdout.strip() == "1"


def wait_http_ready(host: str, port: int, timeout: int = 300) -> None:
    """Block until the Odoo web endpoint answers, or raise on timeout.

    Args:
        host: Hostname Odoo is reachable on.
        port: Mapped HTTP port.
        timeout: Maximum seconds to wait.

    Raises:
        TimeoutError: If Odoo never became ready.
    """
    deadline = time.monotonic() + timeout
    url = f"http://{host}:{port}/web/login"
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
                if resp.status < 500:
                    return
        except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
            last_err = exc
        time.sleep(3)
    raise TimeoutError(
        f"Odoo did not become ready at {url} within {timeout}s (last error: {last_err})"
    )
