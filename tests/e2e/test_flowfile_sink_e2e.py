"""Fluvo-as-a-sink for a Polars frame, against a real Odoo (PLAN 4.5, issue #288).

Proves the "Fluvo as an in-flow sink" mode of the Flowfile integration: a Polars
DataFrame (the shape a Flowfile Polars/Python node hands off) loads into Odoo
through Fluvo's in-memory importer, with real reconciliation and DB truth. This
exercises the exact mechanism in ``examples/flowfile/fluvo_sink.py`` without
importing the example (examples/ is not a package).
"""

from __future__ import annotations

from typing import Any

import polars as pl

from fluvo import importer

from . import assertions as A

MODEL = "res.partner.category"


def test_polars_frame_loads_to_odoo(
    conn_config: dict[str, Any], rpc: Any
) -> None:
    """A Polars frame's columns/rows load into Odoo via run_import_for_migration."""
    prefix = "ffsink"
    n = 3
    df = pl.DataFrame(
        {
            "id": [f"{prefix}_c{i}" for i in range(n)],
            "name": [f"{prefix} {i}" for i in range(n)],
        }
    )
    # The exact two lines from examples/flowfile/fluvo_sink.load_dataframe.
    header = df.columns
    data = [list(row) for row in df.iter_rows()]

    ok, stats = importer.run_import_for_migration(
        config=conn_config, model=MODEL, header=header, data=data
    )

    assert ok, f"load failed: {stats}"
    assert stats.get("created_records", 0) == n
    A.assert_db_count(rpc, MODEL, [["name", "like", f"{prefix} %"]], n)
