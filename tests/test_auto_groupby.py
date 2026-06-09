"""Tests for auto-groupby detection (deadlock-avoidance column selection)."""

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


def test_auto_groupby_wiring_sets_import_plan(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """auto_groupby=True flows through preflight and sets import_plan['groupby']."""
    from fluvo.lib.preflight import _plan_deferrals_and_strategies

    csv = tmp_path / "p.csv"
    csv.write_text("id,name,country_id/id\na,A,be\nb,B,be\nc,C,be\nd,D,fr\n")
    odoo_fields = {
        "id": {"type": "char"},
        "name": {"type": "char"},
        "country_id": {"type": "many2one", "relation": "res.country"},
    }
    import_plan: dict = {}  # type: ignore[type-arg]
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


def test_auto_groupby_respects_explicit_groupby(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """auto_groupby does nothing when the user already passed --groupby."""
    from fluvo.lib.preflight import _plan_deferrals_and_strategies

    csv = tmp_path / "p.csv"
    csv.write_text("id,name,country_id/id\na,A,be\nb,B,be\nc,C,be\nd,D,fr\n")
    odoo_fields = {
        "id": {"type": "char"},
        "name": {"type": "char"},
        "country_id": {"type": "many2one", "relation": "res.country"},
    }
    import_plan: dict = {}  # type: ignore[type-arg]
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
