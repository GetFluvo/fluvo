"""Weekly-delta export (``--since``) against a real Odoo (PLAN 2.4).

Proves the ``--since`` sugar actually filters by the change timestamp: an export
with a past ``--since`` returns every record, and one with a future ``--since``
returns none — without the caller writing any domain.
"""

from __future__ import annotations

from typing import Any

import polars as pl

from fluvo import exporter

from . import assertions as A
from . import generators as G

MODEL = "res.partner.category"


def _rowcount(path: str) -> int:
    """Data-row count of an exported CSV (0 if header-only/empty)."""
    try:
        return pl.read_csv(path, separator=",").height
    except Exception:
        return 0


def test_since_filters_by_change_timestamp(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any
) -> None:
    """A past --since exports all rows; a future --since exports none."""
    prefix = "delta"
    n = 4
    rows = [{"id": f"{prefix}_c{i}", "name": f"{prefix} {i}"} for i in range(n)]
    csv_path = G.write_csv(str(tmp_path / "seed.csv"), ["id", "name"], rows)
    id_map = A.run_full_import(conn_config, MODEL, csv_path)
    assert id_map is not None and len(id_map) == n

    domain = str([["name", "like", f"{prefix} %"]])

    # Past --since: everything changed since then -> all rows.
    past = str(tmp_path / "past.csv")
    exporter.run_export(
        config=conn_config,
        model=MODEL,
        fields="id,name",
        output=past,
        domain=domain,
        since="2000-01-01",
        separator=",",
        context={},
    )
    assert _rowcount(past) == n

    # Future --since: nothing changed since then -> no rows.
    future = str(tmp_path / "future.csv")
    exporter.run_export(
        config=conn_config,
        model=MODEL,
        fields="id,name",
        output=future,
        domain=domain,
        since="2999-01-01",
        separator=",",
        context={},
    )
    assert _rowcount(future) == 0
