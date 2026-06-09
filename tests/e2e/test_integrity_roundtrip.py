"""Scenario 8: date/datetime values must survive export, not silently null out.

The Odoo->Polars type map casts with ``strict=False`` (export_threaded.py). Mapping
date/datetime to a Polars temporal type makes that cast fail *quietly* on Odoo's
string format, producing nulls = silent data loss. The fix keeps them as strings.
This test exports real datetime values and asserts they come back intact.
"""

from __future__ import annotations

import csv
from typing import Any

from fluvo import exporter

from . import assertions as A
from . import generators as G


def test_datetime_export_preserves_values(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Export a datetime field and assert the values are preserved, not nulled."""
    prefix = "s8dt"
    rows = G.partners(min(scale, 50), prefix)
    csv_path = str(tmp_path / "dt_seed.csv")
    G.write_csv(csv_path, ["id", "name", "email", "is_company"], rows)

    # Seed records (each gets a server-side create_date / write_date datetime).
    success, _stats = A.import_with_stats(
        conn_config, "res.partner", csv_path, str(tmp_path / "dt_fail.csv")
    )
    assert success

    out = str(tmp_path / "dt_export.csv")
    exporter.run_export(
        config=conn_config,
        model="res.partner",
        fields="id,name,create_date,write_date",
        output=out,
        domain=f"[['name', 'like', '{prefix} %']]",
        separator=";",
        streaming=True,
    )

    with open(out, newline="", encoding="utf-8") as fh:
        exported = list(csv.DictReader(fh, delimiter=";"))

    assert exported, "Export produced no rows."
    # Every exported row must carry a non-empty datetime (no silent null-out).
    missing = [r for r in exported if not (r.get("create_date") or "").strip()]
    assert not missing, (
        f"{len(missing)}/{len(exported)} rows lost their create_date datetime on "
        f"export (Polars cast nulled them out)."
    )
    # Sanity: the value looks like an Odoo datetime, not a mangled cast.
    sample = exported[0]["create_date"]
    assert sample[:4].isdigit() and "-" in sample, (
        f"Exported datetime looks malformed: {sample!r}"
    )
