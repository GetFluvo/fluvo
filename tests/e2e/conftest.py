"""Fixtures for the fluvo e2e integrity suite.

By default this manages a disposable Postgres+Odoo stack via compose
(``tests/e2e/docker-compose.yml``). Point ``FLUVO_E2E_ODOO_URL`` at an existing Odoo
(e.g. a doodba stack) to run the same scenarios against a realistic install instead.

Environment knobs:
    FLUVO_E2E_ODOO_URL      External Odoo URL (``http://host:port``); skips startup.
    FLUVO_E2E_ODOO_VERSION  Odoo image tag for the managed stack (default ``18.0``).
    FLUVO_E2E_ODOO_PORT     Published host port for managed Odoo (default ``8069``).
    FLUVO_E2E_DB            Target database name (default ``fluvo_e2e_target``).
    FLUVO_E2E_ADMIN_PWD     Admin password / API key (default ``admin``).
    FLUVO_E2E_PROTOCOL      Connection protocol (default ``jsonrpc``).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest

from fluvo.lib import conf_lib

from . import _runtime

DEFAULT_DB = os.environ.get("FLUVO_E2E_DB", "fluvo_e2e_target")
SOURCE_DB = os.environ.get("FLUVO_E2E_SOURCE_DB", "fluvo_e2e_source")
ADMIN_PWD = os.environ.get("FLUVO_E2E_ADMIN_PWD", "admin")
PROTOCOL = os.environ.get("FLUVO_E2E_PROTOCOL", "jsonrpc")
TOXI_API_PORT = int(os.environ.get("FLUVO_E2E_TOXIPROXY_API_PORT", "8474"))
TOXI_PROXY_PORT = int(os.environ.get("FLUVO_E2E_TOXIPROXY_PORT", "18070"))


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
def conn_config(odoo_endpoint: dict[str, object], target_db: str) -> dict[str, object]:
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


# --- Source database (for server-to-server migration tests) ---
# A second freshly-initialised database on the same managed Odoo. Migrating
# source_db -> target_db (both real Odoo databases, real RPC round-trips)
# exercises the identical migrate code path as two separate Odoo hosts, at a
# fraction of the cost. Point FLUVO_E2E_SOURCE_DB at an existing DB to reuse one.
@pytest.fixture(scope="session")
def source_db(odoo_endpoint: dict[str, object]) -> str:
    """Ensure a freshly-initialised *source* database exists; return its name."""
    if not odoo_endpoint["managed"]:
        return SOURCE_DB
    if not _runtime.database_exists(SOURCE_DB):
        _runtime.create_database(SOURCE_DB)
    return SOURCE_DB


@pytest.fixture(scope="session")
def conn_config_source(
    odoo_endpoint: dict[str, object], source_db: str
) -> dict[str, object]:
    """Connection config dict for the migration *source* database."""
    return {
        "hostname": odoo_endpoint["host"],
        "port": odoo_endpoint["port"],
        "database": source_db,
        "login": "admin",
        "password": ADMIN_PWD,
        "protocol": PROTOCOL,
        "uid": 2,
    }


@pytest.fixture(scope="session")
def rpc_source(conn_config_source: dict[str, object]) -> object:
    """An odoo-client-lib connection to the source DB for verification queries."""
    return conf_lib.get_connection_from_dict(conn_config_source)


@pytest.fixture(scope="session")
def scale() -> int:
    """Row count for size-tiered scenarios.

    Default 200 keeps local iteration fast; CI sets ~5000; the ``large`` tier sets
    ~100000+ via ``FLUVO_E2E_SCALE`` for stress runs.
    """
    return int(os.environ.get("FLUVO_E2E_SCALE", "200"))


# --- Chaos / resilience (Toxiproxy) ---
@pytest.fixture
def toxiproxy(odoo_endpoint: dict[str, object]) -> Iterator[object]:
    """A Toxiproxy client with an 'odoo' proxy in front of the managed Odoo.

    Skipped unless the stack is managed (the external-Odoo path has no proxy).

    Args:
        odoo_endpoint: Managed Odoo endpoint details.

    Yields:
        object: A Toxiproxy control client; tests add toxics to the 'odoo' proxy.
    """
    if not odoo_endpoint["managed"]:
        pytest.skip("chaos tests require the managed Odoo stack (toxiproxy)")
    from ._toxiproxy import Toxiproxy

    host = str(odoo_endpoint["host"])
    tp = Toxiproxy(f"http://{host}:{TOXI_API_PORT}")
    try:
        tp.wait_ready()
        try:
            tp.create_proxy(
                "odoo", listen=f"0.0.0.0:{TOXI_PROXY_PORT}", upstream="odoo:8069"
            )
            yield tp
        finally:
            # Best-effort proxy cleanup, only after the server was reachable, so a
            # failed setup doesn't hang on unreachable-server timeouts.
            with contextlib.suppress(Exception):
                tp.reset()
            with contextlib.suppress(Exception):
                tp.delete_proxy("odoo")
    finally:
        tp.close()


@pytest.fixture
def conn_config_flaky(
    odoo_endpoint: dict[str, object], target_db: str, toxiproxy: object
) -> dict[str, object]:
    """Connection config routed through Toxiproxy, so injected toxics affect it."""
    return {
        "hostname": odoo_endpoint["host"],
        "port": TOXI_PROXY_PORT,
        "database": target_db,
        "login": "admin",
        "password": ADMIN_PWD,
        "protocol": PROTOCOL,
        "uid": 2,
    }
