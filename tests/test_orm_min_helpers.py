"""Unit tests for the ORM-minimizing load-path helpers (no Odoo needed)."""

from unittest.mock import MagicMock, patch

import polars as pl

from fluvo.lib import auto_clean, cache, relational_import

# --- auto_clean ----------------------------------------------------------------


def test_auto_clean_strips_and_normalizes_nulls() -> None:
    """Whitespace is stripped and null tokens become empty."""
    df = pl.DataFrame({"id": ["a"], "name": ["  Foo  "], "note": ["NULL"]})
    out = auto_clean.auto_clean_dataframe(df, {"name": "char", "note": "text"})
    assert out["name"][0] == "Foo"
    assert out["note"][0] == ""


def test_auto_clean_canonicalizes_booleans() -> None:
    """Boolean-typed fields are canonicalized to True/False."""
    df = pl.DataFrame({"flag": ["yes", "no", "TRUE", "0", "maybe", ""]})
    out = auto_clean.auto_clean_dataframe(df, {"flag": "boolean"})
    assert out["flag"].to_list() == ["True", "False", "True", "False", "maybe", ""]


def test_auto_clean_numeric_only_when_separator_given() -> None:
    """Numeric reformat only with an explicit decimal separator."""
    df = pl.DataFrame({"amount": ["1.234,56"]})
    # Without separator: left as-is (no locale assumption).
    assert auto_clean.auto_clean_dataframe(df, {"amount": "float"})["amount"][0] == (
        "1.234,56"
    )
    # With European separators: reformatted.
    out = auto_clean.auto_clean_dataframe(
        df, {"amount": "float"}, decimal_separator=","
    )
    assert out["amount"][0] == "1234.56"


# --- relation pre-resolution ---------------------------------------------------


_MAP = pl.DataFrame(
    {
        "key": ["Belgium", "France"],
        "xmlid": ["base.be", "base.fr"],
        "db_id": [21, 75],
    }
)


def test_resolve_relation_m2o_to_xmlid() -> None:
    """m2o natural key resolves to a field/id xmlid column."""
    df = pl.DataFrame({"id": ["p1", "p2"], "country": ["Belgium", "France"]})
    spec = {
        "source_column": "country",
        "model": "res.country",
        "key_field": "name",
        "relation_field": "country_id",
        "to": "xmlid",
    }
    with patch.object(cache, "export_id_map", return_value=_MAP):
        out = relational_import.resolve_relations_in_df(df, [spec], {"h": 1})
    assert out["country_id/id"].to_list() == ["base.be", "base.fr"]


def test_resolve_relation_m2o_to_dbid() -> None:
    """m2o natural key resolves to a field/.id db-id column."""
    df = pl.DataFrame({"id": ["p1"], "country": ["Belgium"]})
    spec = {
        "source_column": "country",
        "model": "res.country",
        "key_field": "name",
        "relation_field": "country_id",
        "to": "dbid",
    }
    with patch.object(cache, "export_id_map", return_value=_MAP):
        out = relational_import.resolve_relations_in_df(df, [spec], {"h": 1})
    assert out["country_id/.id"].to_list() == ["21"]


def test_resolve_relation_m2m_splits_and_rejoins() -> None:
    """m2m values split, resolve, and rejoin by separator."""
    df = pl.DataFrame({"id": ["p1"], "countries": ["Belgium,France"]})
    spec = {
        "source_column": "countries",
        "model": "res.country",
        "key_field": "name",
        "relation_field": "country_ids",
        "multi": True,
        "sep": ",",
    }
    with patch.object(cache, "export_id_map", return_value=_MAP):
        out = relational_import.resolve_relations_in_df(df, [spec], {"h": 1})
    assert out["country_ids/id"].to_list() == ["base.be,base.fr"]


def test_resolve_relation_on_missing_error() -> None:
    """on_missing='error' raises for unresolved values."""
    df = pl.DataFrame({"id": ["p1"], "country": ["Atlantis"]})
    spec = {
        "source_column": "country",
        "model": "res.country",
        "key_field": "name",
        "relation_field": "country_id",
        "on_missing": "error",
    }
    with patch.object(cache, "export_id_map", return_value=_MAP):
        try:
            relational_import.resolve_relations_in_df(df, [spec], {"h": 1})
        except ValueError as exc:
            assert "Atlantis" in str(exc)
        else:
            raise AssertionError("Expected ValueError for unresolved value.")


# --- export_id_map -------------------------------------------------------------


def test_export_id_map_builds_key_xmlid_dbid(tmp_path: object) -> None:
    """export_id_map yields key/xmlid/db_id rows."""
    conn = MagicMock()
    country = MagicMock()
    country.search_read.return_value = [
        {"id": 21, "name": "Belgium"},
        {"id": 75, "name": "France"},
    ]
    imd = MagicMock()
    imd.search_read.return_value = [
        {"res_id": 21, "module": "base", "name": "be"},
        {"res_id": 75, "module": "base", "name": "fr"},
    ]
    conn.get_model.side_effect = lambda m: country if m == "res.country" else imd

    cfg = {"hostname": "h", "port": 1, "database": "d", "login": "a", "password": "b"}
    with patch.object(cache, "resolve_cache_dir", return_value=None), patch(
        "fluvo.lib.conf_lib.get_connection_from_dict", return_value=conn
    ):
        df = cache.export_id_map(cfg, "res.country", "name")

    assert df is not None
    assert set(df.columns) == {"key", "xmlid", "db_id"}
    rows = {r["key"]: (r["xmlid"], r["db_id"]) for r in df.to_dicts()}
    assert rows["Belgium"] == ("base.be", 21)
    assert rows["France"] == ("base.fr", 75)
