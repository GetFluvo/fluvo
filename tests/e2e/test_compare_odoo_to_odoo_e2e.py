"""`fluvo compare --against-connection` between two real Odoo databases (#350).

The unit suite stubs both exports, so what is unproven there is the part that
matters on a migration: two **live** instances, read over RPC in the same run,
with every value coming back in whatever representation each instance chose.

The bar is the same as #298's: the only differences reported are the ones that
are really there. A second test closes one of them with a fix file, which is the
whole "compare, then fix" loop running source-Odoo to target-Odoo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from fluvo import load_dataframe
from fluvo.lib.compare import DUTCH, CompareOptions, run_compare

from . import assertions as A

MODEL = "res.partner"
PREFIX = "o2o"
_FIELDS = "name,active,color,partner_latitude,type"

# Four refs exist on both sides, one only on the source (not migrated), one only
# on the target (created after the cut-over).
_SHARED = ["0020", "0021", "0022", "0023"]
_SOURCE_ONLY = ["0024"]
_TARGET_ONLY = ["0025"]
#: A latitude whose dot a Dutch reading would take for a thousands separator,
#: turning 1.234 into one thousand two hundred and thirty-four. Both sides are
#: Odoo, so neither side may be read that way.
_TRAP_LATITUDE = 1.234
_COLORS = {"0020": 0, "0021": 3, "0022": 0, "0023": 7, "0024": 1, "0025": 2}
_ACTIVE = {"0020": True, "0021": False, "0022": True, "0023": True, "0024": False}
#: The one genuine difference: this partner was renamed in the target.
_RENAMED = "0022"


def _domain(refs: list[str]) -> list[Any]:
    """Scope a read to this test's partners, archived ones included."""
    return [["ref", "in", refs], ["active", "in", [True, False]]]


def _seed(
    config: dict[str, Any], refs: list[str], *, rename: str | None = None
) -> None:
    """Load one side's partners."""
    frame = pl.DataFrame(
        {
            "id": [f"{PREFIX}_{ref}" for ref in refs],
            "name": [
                f"{PREFIX} partner {ref}" + (" RENAMED" if ref == rename else "")
                for ref in refs
            ],
            "ref": refs,
            "active": [_ACTIVE.get(ref, True) for ref in refs],
            "type": ["contact"] * len(refs),
            "color": [_COLORS[ref] for ref in refs],
            "partner_latitude": [_TRAP_LATITUDE] * len(refs),
            "country_id/id": ["base.nl"] * len(refs),
        }
    )
    ok, stats = load_dataframe(frame, config, MODEL)
    assert ok, f"seeding failed: {stats}"


def _compare(
    source_config: dict[str, Any], target_config: dict[str, Any], **kwargs: Any
) -> Any:
    """Compare the two instances over this test's partners only."""
    all_refs = _SHARED + _SOURCE_ONLY + _TARGET_ONLY
    return run_compare(
        config=target_config,
        model=MODEL,
        file=None,
        key="ref",
        against_connection=source_config,
        fields=_FIELDS,
        domain=_domain(all_refs),
        # A caller may well pass the spreadsheet default; it must not be applied
        # to an Odoo source, or _TRAP_LATITUDE reads as 1234 on one side only.
        options=CompareOptions(locale=DUTCH, date_order="dmy"),
        fmt="table",
        **kwargs,
    )


def test_two_live_instances_report_only_the_real_drift(
    conn_config_source: dict[str, Any],
    conn_config: dict[str, Any],
    rpc_source: Any,
    rpc: Any,
) -> None:
    """The acceptance bar of #350, against two live Odoo databases."""
    _seed(conn_config_source, _SHARED + _SOURCE_ONLY)
    _seed(conn_config, _SHARED + _TARGET_ONLY, rename=_RENAMED)
    A.assert_db_count(
        rpc_source, MODEL, _domain(_SHARED + _SOURCE_ONLY), len(_SHARED) + 1
    )
    A.assert_db_count(rpc, MODEL, _domain(_SHARED + _TARGET_ONLY), len(_SHARED) + 1)

    result = _compare(conn_config_source, conn_config)

    # Every row of both sides lands in exactly one bucket.
    assert result.stats.matched_records == len(_SHARED)
    assert result.stats.only_in_source == len(_SOURCE_ONLY)
    assert result.stats.only_in_target == len(_TARGET_ONLY)
    assert result.stats.duplicate_source_keys == 0
    assert result.stats.duplicate_target_keys == 0
    assert result.stats.unkeyed_source_rows == 0
    assert result.stats.unkeyed_target_rows == 0

    # And of the matched records, exactly one cell really differs.
    assert result.stats.differing_records == 1
    assert result.stats.differing_cells == 1
    assert result.differences["field"].to_list() == ["name"]
    assert result.differences["ref"].to_list() == [_RENAMED]
    assert "RENAMED" in result.differences["target_value"].to_list()[0]
    assert result.stats.identical_records == len(_SHARED) - 1


def test_a_fix_file_from_the_source_instance_closes_the_difference(
    conn_config_source: dict[str, Any],
    conn_config: dict[str, Any],
) -> None:
    """The compare-review-import loop, with the source values coming from an Odoo."""
    fixes = Path(__file__).parent / "_o2o_fixes.csv"
    try:
        result = _compare(conn_config_source, conn_config, write_fixes=str(fixes))
        assert result.stats.differing_records == 1
        assert fixes.exists(), "no fix file was written"

        frame = pl.read_csv(fixes, separator=";", infer_schema_length=0)
        assert frame.height == 1
        ok, stats = load_dataframe(frame, conn_config, MODEL)
        assert ok, f"loading the fix file failed: {stats}"
        # An update, not a new partner.
        assert stats["created_records"] in (0, 1)

        after = _compare(conn_config_source, conn_config)
        assert after.stats.differing_cells == 0
        assert after.stats.identical_records == len(_SHARED)
    finally:
        fixes.unlink(missing_ok=True)
