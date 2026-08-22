"""Public DataFrame API against a real Odoo (load_dataframe / export_dataframe).

Proves the round-trip end-to-end: a natural Polars frame (with non-string types)
loads into Odoo with coercion applied, and reads back out as a DataFrame.
``res.partner.category`` is used — simple and not company-aware — with its integer
``color`` field to exercise type coercion.
"""

from __future__ import annotations

from typing import Any

import polars as pl

from fluvo import export_dataframe, load_dataframe

from . import assertions as A

MODEL = "res.partner.category"


def test_dataframe_roundtrip_with_coercion(
    conn_config: dict[str, Any], rpc: Any
) -> None:
    """A natural frame loads (with coercion) and reads back via export_dataframe."""
    prefix = "dfapi"
    n = 3
    df = pl.DataFrame(
        {
            "id": [f"{prefix}_c{i}" for i in range(n)],
            "name": [f"{prefix} {i}" for i in range(n)],
            "color": [1, 2, 3],  # real ints -> coerced to import strings
        }
    )

    ok, stats = load_dataframe(df, conn_config, MODEL)
    assert ok, f"load failed: {stats}"
    assert stats.get("created_records", 0) == n
    A.assert_db_count(rpc, MODEL, [["name", "like", f"{prefix} %"]], n)

    # The integer color landed correctly (coercion round-tripped through Odoo).
    recs = rpc.get_model(MODEL).search_read(
        [["name", "like", f"{prefix} %"]], ["name", "color"]
    )
    by_name = {r["name"]: r["color"] for r in recs}
    assert by_name == {f"{prefix} {i}": i + 1 for i in range(n)}

    # export_dataframe reads them back into a Polars frame.
    out = export_dataframe(
        conn_config,
        MODEL,
        ["id", "name", "color"],
        domain=[["name", "like", f"{prefix} %"]],
    )
    assert out.height == n
    assert set(out.get_column("name").to_list()) == {f"{prefix} {i}" for i in range(n)}
    assert "color" in out.columns
