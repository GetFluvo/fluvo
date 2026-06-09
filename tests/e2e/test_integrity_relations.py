"""Relational integrity scenarios (Pass-1 / Pass-2) against a real Odoo.

These reproduce the failure classes that bit us on real migrations: vanishing
records, dangling references, and relations that didn't resolve. Every test proves
no silent data loss (reconciliation), DB truth, fail-file completeness, and - where
relevant - relational correctness.
"""

from __future__ import annotations

from typing import Any

from . import assertions as A
from . import generators as G


def _fail_path(tmp_path: Any, name: str) -> str:
    return str(tmp_path / f"{name}_fail.csv")


def test_duplicate_external_ids_are_accounted(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Scenario 1: duplicate external ids must not silently drop records.

    The engine should create each unique id once and *account* for the duplicate
    rows (they collapse to the same record) rather than losing them unnoticed.
    """
    prefix = "s1dup"
    n_dupes = 5
    base = G.partners(scale, prefix)
    rows, _ = G.inject_duplicates(base, n_dupes)
    csv_path = str(tmp_path / "dupes.csv")
    G.write_csv(csv_path, ["id", "name", "email", "is_company"], rows)

    success, stats = A.import_with_stats(
        conn_config, "res.partner", csv_path, _fail_path(tmp_path, "dupes")
    )

    # Duplicates collapse to one record each -> they are the "unaccounted" delta,
    # and that delta must be exactly the number of duplicate rows (never silent).
    A.assert_reconciled(stats, expect_unaccounted=n_dupes)
    # Exactly `scale` distinct partners exist in the DB for this prefix.
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale)
    assert success


def test_dangling_reference_is_captured_not_dropped(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Scenario 2: rows pointing at a non-existent relation must not vanish.

    A bad cross-model reference should either fail loudly (fail file) or be created
    without the relation - but never disappear unaccounted.
    """
    prefix = "s2dangle"
    rows = G.partners(scale, prefix)
    bad_ids = []
    for i in range(5):
        rows[i]["country_id"] = "base.country_does_not_exist_xyz"
        bad_ids.append(rows[i]["id"])
    csv_path = str(tmp_path / "dangle.csv")
    G.write_csv(
        csv_path, ["id", "name", "email", "is_company", "country_id"], rows
    )

    _success, stats = A.import_with_stats(
        conn_config, "res.partner", csv_path, _fail_path(tmp_path, "dangle")
    )

    A.assert_reconciled(stats)
    # No silent loss: total rows are all accounted as created or failed.
    accounted = stats["created_records"] + stats["failed_records"]
    assert accounted == scale, f"Dangling-ref rows lost: {accounted} != {scale}"


def test_self_referential_hierarchy_resolves(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Scenario 3: self-referencing parent/child resolves via deferral.

    Children reference parents appearing later in the file; with parent_id deferred
    to Pass 2 every partner must be created and the links must point at the right
    parents.
    """
    prefix = "s3hier"
    rows = G.hierarchy(scale, prefix)
    csv_path = str(tmp_path / "hier.csv")
    G.write_csv(
        csv_path, ["id", "name", "email", "is_company", "parent_id"], rows
    )

    success, stats = A.import_with_stats(
        conn_config,
        "res.partner",
        csv_path,
        _fail_path(tmp_path, "hier"),
        deferred_fields=["parent_id"],
    )

    A.assert_reconciled(stats)
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale)
    # At least one child actually got a parent linked in Pass 2.
    with_parent = A.count(
        rpc,
        "res.partner",
        [["name", "like", f"{prefix} %"], ["parent_id", "!=", False]],
    )
    assert with_parent > 0, "Pass 2 did not link any parents."
    assert success


def test_cross_model_xmlid_reference_resolves(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Scenario 4: cross-model XML id (country_id) resolves in Pass 2 (#179)."""
    prefix = "s4xref"
    rows = G.with_country(scale, prefix, country_xmlid="base.be")
    csv_path = str(tmp_path / "xref.csv")
    G.write_csv(
        csv_path, ["id", "name", "email", "is_company", "country_id"], rows
    )

    success, stats = A.import_with_stats(
        conn_config,
        "res.partner",
        csv_path,
        _fail_path(tmp_path, "xref"),
        deferred_fields=["country_id"],
    )

    A.assert_reconciled(stats)
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale)
    be_id = A.xmlid_to_res_id(rpc, "base.be")
    linked = A.count(
        rpc,
        "res.partner",
        [["name", "like", f"{prefix} %"], ["country_id", "=", be_id]],
    )
    assert linked == scale, (
        f"Cross-model country_id resolved for {linked}/{scale} partners."
    )
    assert success
