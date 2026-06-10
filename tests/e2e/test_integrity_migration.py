"""E2E: server-to-server migration (source DB -> target DB) parity.

The ``migrate`` command exports a model from one Odoo connection, transforms it in
memory (Polars), and imports it into another — the headline server-to-server
feature. Two real databases on the managed Odoo (real RPC round-trips in both
directions) exercise the identical code path as two separate Odoo hosts, at a
fraction of the cost. This asserts the target ends up with the same records
(count + a migrated cross-model relation).

Heavy (a second Odoo database is initialised), so it lives in the opt-in e2e suite
(``nox -s e2e``), never the unit CI matrix.
"""

from __future__ import annotations

from typing import Any

from fluvo import migrator

from . import assertions as A
from . import generators as G


def _fail_path(tmp_path: Any, name: str) -> str:
    return str(tmp_path / f"{name}_fail.csv")


def test_server_to_server_migration_parity(
    conn_config_source: dict[str, Any],
    conn_config: dict[str, Any],
    rpc: Any,
    rpc_source: Any,
    tmp_path: Any,
    scale: int,
) -> None:
    """Seed the source DB, migrate to the target DB, assert parity on the target."""
    prefix = "e2emig"

    # 1. Seed the SOURCE database with partners carrying a cross-model country_id.
    rows = G.with_country(scale, prefix, country_xmlid="base.be")
    csv_path = str(tmp_path / "seed.csv")
    G.write_csv(csv_path, ["id", "name", "email", "is_company", "country_id"], rows)
    success, _stats = A.import_with_stats(
        conn_config_source,
        "res.partner",
        csv_path,
        _fail_path(tmp_path, "seed"),
        deferred_fields=["country_id"],
    )
    assert success, "seeding the source DB failed"
    A.assert_db_count(rpc_source, "res.partner", G.name_domain(prefix), scale)

    # 2. Migrate res.partner from source -> target (1-to-1 mapping), scoped to our
    #    seeded records via the export domain.
    migrator.run_migration(
        config_export=conn_config_source,  # type: ignore[arg-type]
        config_import=conn_config,  # type: ignore[arg-type]
        model="res.partner",
        domain=f"[('name', 'like', '{prefix} %')]",
        # Migration export runs in read mode -> technical field names only
        # (no 'field/id' export specifiers). country_id migrates as its relation.
        fields=["id", "name", "email", "country_id"],
    )

    # 3. Verify parity on the TARGET database (ground truth via RPC).
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale)
    be_id = A.xmlid_to_res_id(rpc, "base.be")
    linked = A.count(
        rpc,
        "res.partner",
        [["name", "like", f"{prefix} %"], ["country_id", "=", be_id]],
    )
    assert linked == scale, f"country_id did not migrate: {linked}/{scale}"
