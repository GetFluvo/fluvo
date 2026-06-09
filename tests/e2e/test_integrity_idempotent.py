"""Idempotency: re-running an import must not duplicate or lose records."""

from __future__ import annotations

from typing import Any

from . import assertions as A
from . import generators as G


def _fail_path(tmp_path: Any, name: str) -> str:
    return str(tmp_path / f"{name}_fail.csv")


def test_skip_existing_reimport_is_idempotent(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Scenario 10: importing the same file twice with --skip-existing is a no-op.

    After the second run the DB still holds exactly ``scale`` records (no dupes),
    and nothing was silently dropped on either pass.
    """
    prefix = "s10idem"
    rows = G.partners(scale, prefix)
    csv_path = str(tmp_path / "idem.csv")
    G.write_csv(csv_path, ["id", "name", "email", "is_company"], rows)

    success1, stats1 = A.import_with_stats(
        conn_config, "res.partner", csv_path, _fail_path(tmp_path, "idem1")
    )
    A.assert_reconciled(stats1)
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale)
    assert success1

    # Second pass: skip records whose external id already exists.
    success2, _stats2 = A.import_with_stats(
        conn_config,
        "res.partner",
        csv_path,
        _fail_path(tmp_path, "idem2"),
        skip_existing=True,
    )
    assert success2
    # No duplication: still exactly `scale` partners for this prefix.
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale)
