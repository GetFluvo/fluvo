"""Scenario 11: large-dataset throughput with concurrency and grouping.

This is the opt-in stress tier (``-m large``). It pushes a large batch through
multiple workers with ``--groupby`` (the deadlock-avoidance path) and asserts the
same integrity guarantee holds at scale: everything is accounted for, the row count
is exact, and self-referential relations still resolve in Pass 2.

Run with, e.g.::

    FLUVO_E2E_SCALE=100000 pytest tests/e2e/test_integrity_scale.py -m large
"""

from __future__ import annotations

from typing import Any

import pytest

from . import assertions as A
from . import generators as G


@pytest.mark.large
def test_large_import_is_fully_accounted(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """A large, multi-worker, grouped import loses nothing."""
    prefix = "s11big"
    # Default scale (200) keeps a -m large smoke run quick; CI/stress overrides it.
    n = max(scale, 2000)
    rows = G.partners(n, prefix)
    csv_path = str(tmp_path / "big.csv")
    G.write_csv(csv_path, ["id", "name", "email", "is_company"], rows)

    success, stats = A.import_with_stats(
        conn_config,
        "res.partner",
        csv_path,
        str(tmp_path / "big_fail.csv"),
        worker=4,
        batch_size=500,
        groupby=["is_company"],
    )

    A.assert_reconciled(stats)
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), n)
    assert success


@pytest.mark.large
def test_large_two_pass_hierarchy_resolves(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """A large self-referential import resolves every parent link in Pass 2."""
    prefix = "s11hier"
    n = max(scale, 2000)
    rows = G.hierarchy(n, prefix)
    csv_path = str(tmp_path / "bighier.csv")
    G.write_csv(
        csv_path, ["id", "name", "email", "is_company", "parent_id"], rows
    )

    success, stats = A.import_with_stats(
        conn_config,
        "res.partner",
        csv_path,
        str(tmp_path / "bighier_fail.csv"),
        worker=4,
        batch_size=500,
        deferred_fields=["parent_id"],
    )

    A.assert_reconciled(stats)
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), n)
    assert success
