"""Fixtures for the fluvo e2e integrity suite.

By default this manages a disposable Postgres+Odoo stack via compose
(``tests/e2e/docker-compose.yml``). Point ``FLUVO_E2E_ODOO_URL`` at an existing Odoo
(e.g. a doodba stack) to run the same scenarios against a realistic install instead.

Environment knobs:
    FLUVO_E2E_ODOO_URL      Use an external Odoo (``http://host:port``); skips containers.
    FLUVO_E2E_ODOO_VERSION  Odoo image tag for the managed stack (default ``18.0``).
    FLUVO_E2E_ODOO_PORT     Host port the managed Odoo is published on (default ``8069``).
    FLUVO_E2E_DB            Target database name (default ``fluvo_e2e_target``).
    FLUVO_E2E_ADMIN_PWD     Admin password / API key (default ``admin``).
    FLUVO_E2E_PROTOCOL      Connection protocol (default ``jsonrpc``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest

from fluvo.lib import conf_lib

from . import _runtime

DEFAULT_DB = os.environ.get("FLUVO_E2E_DB", "fluvo_e2e_target")
ADMIN_PWD = os.environ.get("FLUVO_E2E_ADMIN_PWD", "admin")
PROTOCOL = os.environ.get("FLUVO_E2E_PROTOCOL", "jsonrpc")


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register e2e CLI options."""
    parser.addoption(
        "--keep-stack",
        action="store_true",
        default=False,
        help="Do not tear down the Odoo container stack after the session "
        "(faster iteration / inspection).",
    )


@pytest.fixture(scope="session")
def odoo_endpoint(request: pytest.FixtureRequest) -> Iterator[dict[str, object]]:
    """Provide a ready Odoo endpoint, managing containers unless given one.

    Yields:
        Mapping with ``host`` and ``port`` of a ready Odoo server.
    """
    external = os.environ.get("FLUVO_E2E_ODOO_URL")
    if external:
        parsed = urlparse(external)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 8069)
        _runtime.wait_http_ready(host, port)
        yield {"host": host, "port": port, "managed": False}
        return

    host = "localhost"
    port = int(os.environ.get("FLUVO_E2E_ODOO_PORT", "8069"))
    _runtime.up()
    try:
        _runtime.wait_http_ready(host, port)
        yield {"host": host, "port": port, "managed": True}
    finally:
        if not request.config.getoption("--keep-stack"):
            _runtime.down()


@pytest.fixture(scope="session")
def target_db(odoo_endpoint: dict[str, object]) -> str:
    """Ensure a freshly-initialised target database exists; return its name.

    For a managed stack the database is created with ``base`` and no demo data.
    For an external (doodba) Odoo the database named by ``FLUVO_E2E_DB`` is assumed
    to already exist.
    """
    if not odoo_endpoint["managed"]:
        return DEFAULT_DB
    if not _runtime.database_exists(DEFAULT_DB):
        _runtime.create_database(DEFAULT_DB)
    return DEFAULT_DB


@pytest.fixture(scope="session")
def conn_config(
    odoo_endpoint: dict[str, object], target_db: str
) -> dict[str, object]:
    """Return a connection config dict accepted by ``run_import`` / ``conf_lib``."""
    return {
        "hostname": odoo_endpoint["host"],
        "port": odoo_endpoint["port"],
        "database": target_db,
        "login": "admin",
        "password": ADMIN_PWD,
        "protocol": PROTOCOL,
        "uid": 2,
    }


@pytest.fixture(scope="session")
def rpc(conn_config: dict[str, object]) -> object:
    """An odoo-client-lib connection for ground-truth verification queries."""
    return conf_lib.get_connection_from_dict(conn_config)


@pytest.fixture(scope="session")
def scale() -> int:
    """Row count for size-tiered scenarios.

    Default 200 keeps local iteration fast; CI sets ~5000; the ``large`` tier sets
    ~100000+ via ``FLUVO_E2E_SCALE`` for stress runs.
    """
    return int(os.environ.get("FLUVO_E2E_SCALE", "200"))
