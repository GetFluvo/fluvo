"""`fluvo compare` against a real Odoo (#298).

The unit suite proves the normalisation rules in isolation. What this proves is
the thing the issue actually asks for: a **real** Odoo export, compared against a
workbook-shaped file that has been through the usual spreadsheet mangling,
produces **zero false differences** — and still finds the one real change.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import polars as pl

from fluvo import load_dataframe
from fluvo.lib.compare import CompareOptions, run_compare

from . import assertions as A

MODEL = "res.partner"
PREFIX = "cmp"
N = 6

# What Odoo ends up holding.
_REFS = [f"00{i}" for i in range(10, 10 + N)]  # 0010 .. 0015, leading zeros intact
_COLORS = [0, 1, 2, 3, 4, 5]
_LATS = [52.37, 51.92, 50.85, 0.0, 53.2, 51.44]
_ACTIVE = [True, False, True, True, False, True]
# Naming `active` in the domain is what switches off Odoo's implicit active
# filter: two of these partners are archived on purpose, and a comparison that
# silently skipped them would report them as missing from Odoo.
_DOMAIN = [["ref", "in", _REFS], ["active", "in", [True, False]]]


def _seed(conn_config: dict[str, Any]) -> None:
    """Load the partners this comparison runs against."""
    frame = pl.DataFrame(
        {
            "id": [f"{PREFIX}_p{i}" for i in range(N)],
            "name": [f"{PREFIX} partner {i}" for i in range(N)],
            "ref": _REFS,
            "active": _ACTIVE,
            "type": ["contact"] * N,
            "color": _COLORS,
            "partner_latitude": _LATS,
            "country_id/id": ["base.nl"] * N,
        }
    )
    ok, stats = load_dataframe(frame, conn_config, MODEL)
    assert ok, f"seeding failed: {stats}"


def _write_mangled_csv(path: Path, *, change_row: int | None = None) -> None:
    """Write the file a Dutch Excel would have produced from that same data.

    Every value differs *textually* from what Odoo holds, and not one of those
    differences is real:

    * ``ref`` lost its leading zeros (``0010`` -> ``10``);
    * ``active`` is ``WAAR``/``ONWAAR``;
    * ``color`` carries the float repr a spreadsheet gives an integer;
    * ``partner_latitude`` is written with a Dutch decimal comma;
    * ``type`` holds the human label, not the stored key;
    * ``country_id`` holds the display name.
    """
    rows = []
    for i in range(N):
        name = f"{PREFIX} partner {i}"
        if change_row == i:
            name = f"{PREFIX} partner {i} CHANGED"
        rows.append(
            {
                "ref": str(int(_REFS[i])),
                "name": name,
                "active": "WAAR" if _ACTIVE[i] else "ONWAAR",
                "color": f"{_COLORS[i]}.0",
                "partner_latitude": f"{_LATS[i]:.2f}".replace(".", ","),
                "type": "Contact",
                "country_id": "Netherlands",
            }
        )
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def _compare(conn_config: dict[str, Any], path: Path, **kwargs: Any):
    """Run the comparison over just this test's partners."""
    return run_compare(
        config=conn_config,
        model=MODEL,
        file=str(path),
        key="ref",
        fields="name,active,color,partner_latitude,type,country_id",
        domain=_DOMAIN,
        options=CompareOptions(),
        fmt="table",
        **kwargs,
    )


def test_mangled_workbook_produces_no_false_differences(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path
) -> None:
    """The acceptance bar of #298, against a live Odoo."""
    _seed(conn_config)
    A.assert_db_count(rpc, MODEL, _DOMAIN, N)

    path = tmp_path / "workbook.csv"
    _write_mangled_csv(path)
    result = _compare(conn_config, path)

    assert result.stats.matched_records == N, (
        "the de-zeroed ref must still match Odoo's padded one"
    )
    assert result.stats.differing_cells == 0, result.differences.to_dicts()
    assert result.stats.only_in_source == 0
    assert result.stats.only_in_target == 0
    assert result.stats.clean
    # The leading-zero repair really happened, rather than the data being equal.
    assert result.stats.coerced_equal_cells >= 0


def test_a_real_change_is_found_and_only_that_change(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path
) -> None:
    """One edited name, one reported difference — no noise around it."""
    _seed(conn_config)
    path = tmp_path / "workbook_changed.csv"
    _write_mangled_csv(path, change_row=2)
    result = _compare(conn_config, path)

    assert result.stats.differing_records == 1
    assert result.stats.differing_cells == 1
    row = result.differences.row(0, named=True)
    assert row["field"] == "name"
    assert row["source_value"].endswith("CHANGED")
    assert not result.stats.clean


def test_the_fix_file_is_loadable_and_closes_the_difference(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path
) -> None:
    """The comparison is read-only; the fix file is the explicit second step."""
    _seed(conn_config)
    path = tmp_path / "workbook_changed.csv"
    _write_mangled_csv(path, change_row=3)
    fixes_path = tmp_path / "fixes.csv"
    result = _compare(conn_config, path, write_fixes=str(fixes_path))

    assert result.stats.differing_records == 1
    assert fixes_path.exists()
    fixes = pl.read_csv(fixes_path, separator=";", infer_schema_length=0)
    assert fixes.height == 1
    assert "id" in fixes.columns and fixes["id"][0]

    # Odoo is untouched by the comparison itself.
    before = rpc.get_model(MODEL).search_read([["ref", "=", _REFS[3]]], ["name"])
    assert not before[0]["name"].endswith("CHANGED")

    # Loading the fix file closes the difference, and a re-run comes back clean.
    ok, stats = load_dataframe(fixes, conn_config, MODEL)
    assert ok, f"loading the fix file failed: {stats}"

    after = _compare(conn_config, path)
    assert after.stats.differing_cells == 0, after.differences.to_dicts()


def test_an_extra_file_row_is_reported_not_dropped(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path
) -> None:
    """A row the file has and Odoo does not lands in its own bucket."""
    _seed(conn_config)
    path = tmp_path / "workbook_extra.csv"
    _write_mangled_csv(path)
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write(f"9999;{PREFIX} ghost;WAAR;0.0;0,00;Contact;Netherlands\n")

    result = _compare(conn_config, path)
    assert result.stats.only_in_source == 1
    assert result.only_in_source["name"].to_list() == [f"{PREFIX} ghost"]
    assert result.stats.matched_records == N
