"""Parquet as the export/import intermediate format, against a real Odoo (PLAN 4.8).

Proves both directions end-to-end: `fluvo import --file x.parquet` loads a Parquet
source (converted + coerced through the full pipeline), and `fluvo export --output
x.parquet` writes a Parquet file — a transform tool's typed handoff to/from Odoo.
``res.partner.category`` is used (simple, not company-aware).
"""

from __future__ import annotations

from typing import Any

import polars as pl

from fluvo import exporter

from . import assertions as A

MODEL = "res.partner.category"


def test_parquet_import_and_export_round_trip(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any
) -> None:
    """Import from a Parquet source, then export the records back to Parquet."""
    prefix = "pq"
    n = 3

    # --- Import from a Parquet file ---
    src = tmp_path / "in.parquet"
    pl.DataFrame(
        {
            "id": [f"{prefix}_c{i}" for i in range(n)],
            "name": [f"{prefix} {i}" for i in range(n)],
            "color": [1, 2, 3],  # typed ints, coerced on the way in
        }
    ).write_parquet(src)

    id_map = A.run_full_import(conn_config, MODEL, str(src))
    assert id_map is not None and len(id_map) == n, "parquet import failed"
    A.assert_db_count(rpc, MODEL, [["name", "like", f"{prefix} %"]], n)

    # --- Export back to a Parquet file ---
    out = str(tmp_path / "out.parquet")
    exporter.run_export(
        config=conn_config,
        model=MODEL,
        fields="id,name,color",
        output=out,
        domain=str([["name", "like", f"{prefix} %"]]),
        separator=",",
        context={},
    )
    frame = pl.read_parquet(out)
    assert frame.height == n
    by_name = dict(
        zip(frame.get_column("name"), frame.get_column("color"), strict=True)
    )
    assert by_name == {f"{prefix} {i}": i + 1 for i in range(n)}
