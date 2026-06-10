"""Tests for auto-groupby detection (deadlock-avoidance column selection)."""

from pathlib import Path
from typing import Any

import polars as pl

from fluvo.lib.preflight import _detect_groupby_column

_FIELDS = {
    "country_id": {"type": "many2one", "relation": "res.country"},
    "user_id": {"type": "many2one", "relation": "res.users"},
    "parent_id": {"type": "many2one", "relation": "res.partner"},
    "name": {"type": "char"},
}


def test_picks_most_duplicated_many2one() -> None:
    """The non-self m2o with the most shared targets is chosen."""
    df = pl.DataFrame(
        {
            "id": ["a", "b", "c", "d"],
            "country_id/id": ["be", "be", "fr", "be"],  # 4 rows / 2 unique = 2.0
            "user_id/id": ["u1", "u2", "u3", "u4"],  # all unique -> skipped
        }
    )
    assert (
        _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner")
        == "country_id/id"
    )


def test_all_unique_returns_none() -> None:
    """No contention (all targets unique) -> no groupby."""
    df = pl.DataFrame({"id": ["a", "b"], "country_id/id": ["be", "fr"]})
    assert _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner") is None


def test_self_reference_is_skipped() -> None:
    """Self-referencing m2o is left to two-pass deferral, not groupby."""
    df = pl.DataFrame({"id": ["a", "b", "c"], "parent_id/id": ["p", "p", "p"]})
    assert _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner") is None


def test_no_relational_column_returns_none() -> None:
    """A flat dataset has nothing to group by."""
    df = pl.DataFrame({"id": ["a", "b"], "name": ["x", "y"]})
    assert _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner") is None


def test_auto_groupby_wiring_sets_import_plan(tmp_path: Path) -> None:
    """auto_groupby=True flows through preflight and sets import_plan['groupby']."""
    from fluvo.lib.preflight import _plan_deferrals_and_strategies

    csv = tmp_path / "p.csv"
    csv.write_text("id,name,country_id/id\na,A,be\nb,B,be\nc,C,be\nd,D,fr\n")
    odoo_fields = {
        "id": {"type": "char"},
        "name": {"type": "char"},
        "country_id": {"type": "many2one", "relation": "res.country"},
    }
    import_plan: dict[str, Any] = {}
    _plan_deferrals_and_strategies(
        ["id", "name", "country_id/id"],
        odoo_fields,
        "res.partner",
        str(csv),
        ",",
        import_plan,
        auto_groupby=True,
        groupby=None,
    )
    assert import_plan.get("groupby") == ["country_id/id"]


def test_auto_groupby_respects_explicit_groupby(tmp_path: Path) -> None:
    """auto_groupby does nothing when the user already passed --groupby."""
    from fluvo.lib.preflight import _plan_deferrals_and_strategies

    csv = tmp_path / "p.csv"
    csv.write_text("id,name,country_id/id\na,A,be\nb,B,be\nc,C,be\nd,D,fr\n")
    odoo_fields = {
        "id": {"type": "char"},
        "name": {"type": "char"},
        "country_id": {"type": "many2one", "relation": "res.country"},
    }
    import_plan: dict[str, Any] = {}
    _plan_deferrals_and_strategies(
        ["id", "name", "country_id/id"],
        odoo_fields,
        "res.partner",
        str(csv),
        ",",
        import_plan,
        auto_groupby=True,
        groupby=["name"],  # explicit -> auto must not override
    )
    assert "groupby" not in import_plan


def test_picks_dbid_suffix_column() -> None:
    """A /.id (database-id) relational column is recognised (review fix)."""
    df = pl.DataFrame(
        {
            "id": ["a", "b", "c", "d"],
            "country_id/.id": ["10", "10", "20", "10"],  # 4 rows / 2 unique = 2.0
        }
    )
    assert (
        _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner")
        == "country_id/.id"
    )


def test_returns_none_for_tiny_df() -> None:
    """A df with fewer than 2 rows can't benefit from grouping (early return)."""
    df = pl.DataFrame({"id": ["a"], "country_id/id": ["be"]})
    assert _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner") is None


def test_skips_field_absent_from_df() -> None:
    """A many2one named in the header but missing from the df is skipped."""
    df = pl.DataFrame({"id": ["a", "b"], "name": ["A", "B"]})
    header = ["id", "name", "country_id/id"]  # claims a column the df lacks
    assert _detect_groupby_column(df, header, _FIELDS, "res.partner") is None


def test_skips_sparse_column() -> None:
    """A many2one column with fewer than 2 non-empty values is skipped."""
    df = pl.DataFrame({"id": ["a", "b"], "country_id/id": ["be", ""]})
    assert _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner") is None


def test_auto_groupby_wiring_no_detectable_column(tmp_path: Path) -> None:
    """auto_groupby with no groupable column leaves the plan unset (no-op + log)."""
    from fluvo.lib.preflight import _plan_deferrals_and_strategies

    csv = tmp_path / "p.csv"
    csv.write_text("id,name\na,A\nb,B\n")  # no many2one column at all
    odoo_fields = {"id": {"type": "char"}, "name": {"type": "char"}}
    import_plan: dict[str, Any] = {}
    _plan_deferrals_and_strategies(
        ["id", "name"],
        odoo_fields,
        "res.partner",
        str(csv),
        ",",
        import_plan,
        auto_groupby=True,
        groupby=None,
    )
    assert "groupby" not in import_plan


def test_prefers_higher_cardinality_among_candidates() -> None:
    """Among duplicated columns, the higher-cardinality one wins (#191 review).

    A low-cardinality column has a higher duplication ratio but yields fewer
    partitions, serializing the import and worsening lock contention
    (performance_tuning.md). The detector must pick the higher-cardinality column to
    preserve parallelism.
    """
    df = pl.DataFrame(
        {
            "id": [str(i) for i in range(8)],
            # 2 unique -> dup 4.0, but only 2 partitions
            "country_id/id": ["be", "be", "be", "be", "fr", "fr", "fr", "fr"],
            # 4 unique -> dup 2.0, 4 partitions -> preferred
            "user_id/id": ["u1", "u1", "u2", "u2", "u3", "u3", "u4", "u4"],
        }
    )
    assert (
        _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner")
        == "user_id/id"
    )


def test_handles_non_string_column_without_error() -> None:
    """A non-string relational column is analyzed without a Polars error (#191)."""
    df = pl.DataFrame({"id": ["a", "b", "c", "d"], "country_id/id": [1, 1, 2, 1]})
    assert (
        _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner")
        == "country_id/id"
    )


def test_selects_modestly_duplicated_high_cardinality() -> None:
    """A high-cardinality column with modest duplication (dup<2) is still chosen.

    The old dup>=2 gate disqualified exactly the high-cardinality columns the
    cardinality-preference selection is meant to favour (#191 review).
    """
    # 6 rows, 4 unique -> dup 1.5: real but modest duplication, lots of partitions.
    df = pl.DataFrame(
        {
            "id": [str(i) for i in range(6)],
            "country_id/id": ["a", "a", "b", "c", "d", "d"],
        }
    )
    assert (
        _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner")
        == "country_id/id"
    )
