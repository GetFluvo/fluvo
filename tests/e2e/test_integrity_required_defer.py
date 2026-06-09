"""Scenario 5: required relational fields must never be deferred.

Deferring a *required* m2o means the Pass-1 create runs without it and fails with
'Missing required value'. This is the failure class the fork branch
``prevent-defer-required-fields`` targeted; here we prove the release branch's
behaviour against a real Odoo (res.country.state.country_id is required).
"""

from __future__ import annotations

from typing import Any

from . import assertions as A
from . import generators as G


def test_auto_defer_keeps_required_m2o_in_pass1(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """With --auto-defer, a required country_id is NOT deferred, so states import."""
    prefix = "s5req"
    n = min(scale, 40)
    rows = G.states(n, prefix)
    csv_path = str(tmp_path / "states.csv")
    G.write_csv(csv_path, ["id", "name", "code", "country_id/id"], rows)

    A.run_full_import(
        conn_config, "res.country.state", csv_path, auto_defer=True
    )

    A.assert_db_count(
        rpc, "res.country.state", [["name", "like", f"{prefix} %"]], n
    )


def test_explicit_defer_of_required_field_is_ignored(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Explicitly deferring a required field must be refused, not obeyed.

    This guards the importer-level safeguard from prevent-defer-required-fields:
    if the user passes ``--deferred-fields country_id`` for a required field, the
    tool should ignore it and still create the records, rather than failing every
    Pass-1 create. If this fails, the safeguard is the documented gap to close
    (see docs/RECONCILIATION.md).
    """
    prefix = "s5force"
    n = min(scale, 40)
    rows = G.states(n, prefix)
    csv_path = str(tmp_path / "states_force.csv")
    G.write_csv(csv_path, ["id", "name", "code", "country_id/id"], rows)

    A.run_full_import(
        conn_config,
        "res.country.state",
        csv_path,
        deferred_fields=["country_id"],
    )

    A.assert_db_count(
        rpc, "res.country.state", [["name", "like", f"{prefix} %"]], n
    )
