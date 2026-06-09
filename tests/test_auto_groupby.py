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
    df = pl.DataFrame(
        {"id": ["a", "b", "c"], "parent_id/id": ["p", "p", "p"]}
    )
    assert _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner") is None


def test_no_relational_column_returns_none() -> None:
    """A flat dataset has nothing to group by."""
    df = pl.DataFrame({"id": ["a", "b"], "name": ["x", "y"]})
    assert _detect_groupby_column(df, list(df.columns), _FIELDS, "res.partner") is None
