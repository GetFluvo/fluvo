"""The `id` column must never silently hold a database id (#339).

`id` is Fluvo's match key on import, so a file whose `id` column holds database
ids does not update those records when it is loaded back — Odoo reads `10` as a
new external id and **creates duplicates**. The export succeeds, the import
succeeds, and the damage only shows up as doubled records.

The read path returns database ids, and it can be forced by something the caller
never asked for: a `selection` or `binary` field anywhere in the header. So these
tests pin two things — *when* external-id enrichment runs, and that it produces
the same frame shape whichever column the database ids came from.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from fluvo.export_threaded import (
    _determine_export_strategy,
    _enrich_main_df_with_xml_ids,
)

_SELECTION_MODEL = {
    "id": {"type": "char"},
    "ref": {"type": "char"},
    "name": {"type": "char"},
    "type": {"type": "selection"},
}
_PLAIN_MODEL = {
    "id": {"type": "char"},
    "ref": {"type": "char"},
    "name": {"type": "char"},
}


def _strategy(
    header: list[str],
    fields_info: dict[str, dict[str, Any]],
    *,
    technical_names: bool = False,
) -> tuple[bool, bool, bool]:
    """Run the strategy decision with the metadata lookup stubbed.

    Args:
        header: The export header being decided for.
        fields_info: The ``fields_get`` metadata the stub returns.
        technical_names: Whether raw database values were explicitly asked for.

    Returns:
        tuple[bool, bool, bool]: force_read_method, is_hybrid, enrich_main_xml_id.
    """
    with patch("fluvo.export_threaded._initialize_export") as initialize:
        initialize.return_value = (MagicMock(), MagicMock(), fields_info)
        *_, force_read, is_hybrid, enrich = _determine_export_strategy(
            "dummy.conf", "res.partner", header, technical_names
        )
    return force_read, is_hybrid, enrich


# ---------------------------------------------------------------------------
# When enrichment runs
# ---------------------------------------------------------------------------


def test_a_selection_field_alone_still_enriches_the_id_column() -> None:
    """The regression. `--fields id,ref,name,type` used to emit database ids.

    `type` is a selection field, which forces the read path on its own. Nothing
    in that header asks for raw database values, so `id` must still come back as
    an external id.
    """
    force_read, _, enrich = _strategy(["id", "ref", "name", "type"], _SELECTION_MODEL)
    assert force_read is True, "a selection field forces the read path"
    assert enrich is True


def test_a_read_specifier_on_a_relation_also_enriches() -> None:
    """`country_id/.id` forces the read path without a literal '.id' in the header."""
    fields = dict(_PLAIN_MODEL, **{"country_id/.id": {"type": "integer"}})
    _, _, enrich = _strategy(["id", "name", "country_id/.id"], fields)
    assert enrich is True


def test_the_export_data_path_is_left_alone() -> None:
    """With nothing forcing `read`, `export_data` already returns external ids."""
    force_read, _, enrich = _strategy(["id", "ref", "name"], _PLAIN_MODEL)
    assert force_read is False
    assert enrich is False, "no enrichment needed, and none should be attempted"


def test_technical_names_still_means_raw_database_values() -> None:
    """The one case where a database id in `id` is the explicit request."""
    _, _, enrich = _strategy(["id", "ref", "name"], _PLAIN_MODEL, technical_names=True)
    assert enrich is False


def test_technical_names_with_both_columns_keeps_enriching() -> None:
    """Asking for '.id' alongside 'id' has always meant 'give me both'."""
    fields = dict(_PLAIN_MODEL, **{".id": {"type": "integer"}})
    _, _, enrich = _strategy(["id", ".id", "ref"], fields, technical_names=True)
    assert enrich is True


def test_the_raw_id_column_on_its_own_is_not_enriched() -> None:
    """Without 'id' in the header there is nothing to populate."""
    fields = {".id": {"type": "integer"}, "name": {"type": "char"}}
    _, _, enrich = _strategy([".id", "name"], fields)
    assert enrich is False


# ---------------------------------------------------------------------------
# The enrichment itself
# ---------------------------------------------------------------------------


def _connection(*pairs: tuple[int, str]) -> MagicMock:
    """A connection whose ir.model.data holds exactly these res_id -> xmlid pairs."""
    connection = MagicMock()
    connection.get_model.return_value.search_read.return_value = [
        {"res_id": res_id, "module": xmlid.split(".")[0], "name": xmlid.split(".")[1]}
        for res_id, xmlid in pairs
    ]
    return connection


def test_the_database_id_is_read_from_the_id_column_when_that_is_all_there_is() -> None:
    """The #339 shape: no '.id' column, so 'id' is both source and target."""
    df = pl.DataFrame({"id": ["10", "11"], "name": ["Acme", "Beta"]})
    out = _enrich_main_df_with_xml_ids(
        df, _connection((10, "base.acme"), (11, "base.beta")), "res.partner"
    )
    assert out.columns == ["id", "name"], "no column may be added or reordered"
    assert out["id"].to_list() == ["base.acme", "base.beta"]


def test_an_integer_id_column_joins_just_as_well() -> None:
    """Whether 'id' came back typed or as text depends on the header; both work."""
    df = pl.DataFrame({"id": [10], "name": ["Acme"]})
    out = _enrich_main_df_with_xml_ids(
        df, _connection((10, "base.acme")), "res.partner"
    )
    assert out["id"].to_list() == ["base.acme"]


def test_the_raw_id_column_is_preferred_and_preserved() -> None:
    """When the caller asked for '.id' too, it stays in the output."""
    df = pl.DataFrame({"id": ["10"], ".id": [10], "name": ["Acme"]})
    out = _enrich_main_df_with_xml_ids(
        df, _connection((10, "base.acme")), "res.partner"
    )
    assert out.columns == ["id", ".id", "name"]
    assert out[".id"].to_list() == [10]
    assert out["id"].to_list() == ["base.acme"]


def test_a_record_without_an_external_id_is_emptied_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty id creates a record on re-import; a database id corrupts one.

    So the honest output is null, and it is said out loud rather than left for
    the user to notice as duplicates.
    """
    df = pl.DataFrame({"id": ["10", "11"], "name": ["Acme", "Beta"]})
    with caplog.at_level("WARNING"):
        out = _enrich_main_df_with_xml_ids(
            df, _connection((10, "base.acme")), "res.partner"
        )
    assert out["id"].to_list() == ["base.acme", None]
    assert "no external id" in caplog.text


def test_no_external_ids_at_all_empties_the_column(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rather than leaving database ids behind."""
    df = pl.DataFrame({"id": ["10"], "name": ["Acme"]})
    with caplog.at_level("WARNING"):
        out = _enrich_main_df_with_xml_ids(df, _connection(), "res.partner")
    assert out["id"].to_list() == [None]
    assert "No XML IDs found" in caplog.text


def test_an_empty_batch_is_not_enriched() -> None:
    """No ids to look up, and no RPC call to make."""
    df = pl.DataFrame(
        {"id": [None], "name": ["Acme"]}, schema_overrides={"id": pl.String}
    )
    connection = _connection()
    out = _enrich_main_df_with_xml_ids(df, connection, "res.partner")
    assert out["id"].to_list() == [None]
    connection.get_model.assert_not_called()
