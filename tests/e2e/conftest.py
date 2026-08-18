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
from typing import Any
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


# --- product module (for the variant workflow test, #188) ---
@pytest.fixture(scope="session")
def product_module(odoo_endpoint: dict[str, object], target_db: str) -> str:
    """Ensure the 'product' module is installed in the target DB.

    Args:
        odoo_endpoint: Managed Odoo endpoint details.
        target_db: The target database name.

    Returns:
        str: The module name ('product').
    """
    if not odoo_endpoint["managed"]:
        pytest.skip("variant workflow test requires the managed Odoo stack")
    _runtime.install_module(target_db, "product")
    # The long-running server has already cached target_db's (base-only) registry
    # from earlier tests; a mid-session install does not reliably make it reload
    # (Object product.template doesn't exist on Odoo 16/17). Restart so the next
    # request rebuilds the registry with 'product', then wait for it to come back.
    _runtime.restart_service("odoo")
    _runtime.wait_http_ready(str(odoo_endpoint["host"]), int(odoo_endpoint["port"]))
    return "product"


# --- Installed languages (for the multi-language import tests, #254) ---
@pytest.fixture(scope="session")
def translated_languages(rpc: Any) -> list[str]:
    """Ensure a couple of extra languages are installed and active on the target DB.

    Uses fluvo's own language installer (so this doubles as coverage of it) and
    waits for the languages to become active. Skips the dependent test if they
    cannot be activated (e.g. a locked-down external Odoo).

    Args:
        rpc: A connection to the target database.

    Returns:
        list[str]: The language codes that are active and usable for translations.
    """
    from fluvo.lib import odoo_lib
    from fluvo.lib.actions import language_installer

    langs = ["nl_NL", "fr_FR"]
    try:
        version = odoo_lib.get_odoo_version(rpc)
        lang_model = rpc.get_model("res.lang")
        wizard = rpc.get_model("base.language.install")
        # Odoo 16+ takes the 'lang_ids' m2m (the whole e2e matrix is >= 16); older
        # versions used a single 'lang' Selection. Call lang_install with the wizard
        # id positionally (RPC style) — not via browse(), which the client omits.
        key = "lang_ids" if version >= 16 else "lang"
        for code in langs:
            ids = lang_model.search(
                [("code", "=", code)], context={"active_test": False}
            )
            if not ids:
                pytest.skip(f"Language {code} is not available in this Odoo image")
            vals = (
                {key: [(6, 0, ids)], "overwrite": False}
                if version >= 16
                else {key: code, "overwrite": False}
            )
            wizard_id = wizard.create(vals)
            wizard.lang_install([wizard_id])
        if not language_installer._wait_for_languages_to_be_active(
            rpc, langs, timeout=240
        ):
            pytest.skip(f"Could not activate languages {langs} on the target DB")
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Language installation unavailable: {exc}")
    return langs


# --- Second company (for the per-company import tests, #255 part 2) ---
@pytest.fixture(scope="session")
def second_company(rpc: Any, product_module: str) -> int:
    """Ensure a second res.company exists and the admin user may use it.

    Depends on ``product_module`` so 'product' (which defines the company-dependent
    ``standard_price``) is installed and the registry has been reloaded first.

    Args:
        rpc: A connection to the target database.
        product_module: The installed product module fixture.

    Returns:
        int: The database id of the second company.
    """
    companies = rpc.get_model("res.company")
    users = rpc.get_model("res.users")
    try:
        existing = companies.search([("name", "=", "Fluvo E2E Co2")])
        cid = (
            int(existing[0])
            if existing
            else int(companies.create({"name": "Fluvo E2E Co2"}))
        )
        # Grant the connecting user access to the new company.
        users.write([rpc.user_id], {"company_ids": [(4, cid)]})
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Could not set up a second company: {exc}")
    return cid
