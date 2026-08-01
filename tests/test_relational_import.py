"""Tests for the direct relational import strategy."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import polars as pl
import pytest
from polars.testing import assert_frame_equal
from rich.progress import Progress

from fluvo.lib import relational_import


@patch("fluvo.lib.relational_import.cache.load_id_map")
def test_run_direct_relational_import(
    mock_load_id_map: MagicMock,
    tmp_path: Path,
) -> None:
    """Verify the direct relational import workflow."""
    # Arrange
    source_df = pl.DataFrame(
        {
            "id": ["p1", "p2"],
            "name": ["Partner 1", "Partner 2"],
            "category_id": ["cat1,cat2", "cat2,cat3"],
        }
    )
    mock_load_id_map.return_value = pl.DataFrame(
        {"external_id": ["cat1", "cat2", "cat3"], "db_id": [11, 12, 13]}
    )

    strategy_details = {
        "relation_table": "res.partner.category.rel",
        "relation_field": "partner_id",
        "relation": "category_id",
    }
    id_map = {"p1": 1, "p2": 2}
    progress = Progress()
    task_id = progress.add_task("test")

    # Act
    result = relational_import.run_direct_relational_import(
        "dummy.conf",
        "res.partner",
        "category_id",
        strategy_details,
        source_df,
        id_map,
        1,
        10,
        progress,
        task_id,
        "source.csv",
    )

    # Assert
    assert isinstance(result, dict)
    assert "file_csv" in result
    assert "model" in result
    assert "unique_id_field" in result
    assert result["model"] == "res.partner.category.rel"
    assert result["unique_id_field"] == "partner_id"

    # Verify the content of the temporary CSV and cleanup
    temp_csv_path = result["file_csv"]
    try:
        df = pl.read_csv(temp_csv_path, truncate_ragged_lines=True)
        expected_df = pl.DataFrame(
            {
                "partner_id": [1, 1, 2, 2],
                "category_id/id": [11, 12, 12, 13],
            }
        )
        assert_frame_equal(df, expected_df, check_row_order=False)
    finally:
        Path(temp_csv_path).unlink(missing_ok=True)


@patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
@patch("fluvo.lib.relational_import._resolve_related_ids")
def test_run_write_tuple_import(
    mock_resolve_ids: MagicMock,
    mock_get_conn: MagicMock,
) -> None:
    """Verify the write tuple import workflow."""
    # Arrange
    source_df = pl.DataFrame(
        {
            "id": ["p1", "p2"],
            "name": ["Partner 1", "Partner 2"],
            "category_id": ["cat1,cat2", "cat2,cat3"],
        }
    )
    mock_resolve_ids.return_value = pl.DataFrame(
        {"external_id": ["cat1", "cat2", "cat3"], "db_id": [11, 12, 13]}
    )
    mock_owning_model = MagicMock()
    mock_get_conn.return_value.get_model.return_value = mock_owning_model

    strategy_details = {
        "relation_table": "res.partner.category.rel",
        "relation_field": "partner_id",
        "relation": "category_id",
    }
    id_map = {"p1": 1, "p2": 2}
    progress = Progress()
    task_id = progress.add_task("test")

    # Act
    result = relational_import.run_write_tuple_import(
        "dummy.conf",
        "res.partner",
        "category_id",
        strategy_details,
        source_df,
        id_map,
        1,
        10,
        progress,
        task_id,
        "source.csv",
    )

    # Assert
    assert result is True
    # Should have called write on the owning model, not create on the relation model
    assert mock_owning_model.write.call_count >= 1


@patch("fluvo.lib.relational_import.cache.load_id_map", return_value=None)
@patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
def test_resolve_related_ids_failure(
    mock_get_conn: MagicMock,
    mock_load_id_map: MagicMock,
) -> None:
    """Test that _resolve_related_ids returns None on failure."""
    mock_get_conn.return_value.get_model.return_value.search_read.return_value = []
    result = relational_import._resolve_related_ids(
        "dummy.conf", "res.partner", pl.Series(["p1"])
    )
    assert result is None


@patch("fluvo.lib.relational_import.conf_lib.get_connection_from_dict")
def test_resolve_related_ids_with_dict(mock_get_conn_dict: MagicMock) -> None:
    """Test _resolve_related_ids with a dictionary config."""
    mock_get_conn_dict.return_value.get_model.return_value.search_read.return_value = []
    result = relational_import._resolve_related_ids(
        {"host": "localhost"}, "res.partner", pl.Series(["p1.p1"])
    )
    assert result is None


@patch("fluvo.lib.relational_import.cache.load_id_map", return_value=None)
@patch(
    "fluvo.lib.relational_import.conf_lib.get_connection_from_config",
    side_effect=Exception("Connection failed"),
)
def test_resolve_related_ids_connection_error(
    mock_get_conn: MagicMock,
    mock_load_id_map: MagicMock,
) -> None:
    """Test that _resolve_related_ids returns None on connection error."""
    with pytest.raises(Exception, match="Connection failed"):
        relational_import._resolve_related_ids(
            "dummy.conf", "res.partner", pl.Series(["p1.p1"])
        )


@patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
def test_run_write_o2m_tuple_import(mock_get_conn: MagicMock) -> None:
    """Verify the o2m tuple import workflow."""
    # Arrange
    source_df = pl.DataFrame(
        {
            "id": ["p1"],
            "name": ["Partner 1"],
            "line_ids": ['[{"product": "prodA", "qty": 1}]'],
        }
    )
    mock_parent_model = MagicMock()
    mock_get_conn.return_value.get_model.return_value = mock_parent_model

    strategy_details: dict[str, str] = {}
    id_map = {"p1": 1}
    progress = Progress()
    task_id = progress.add_task("test")

    # Act
    result = relational_import.run_write_o2m_tuple_import(
        "dummy.conf",
        "res.partner",
        "line_ids",
        strategy_details,
        source_df,
        id_map,
        1,
        10,
        progress,
        task_id,
        "source.csv",
    )

    # Assert
    assert result is True
    mock_parent_model.write.assert_called_once_with(
        [1], {"line_ids": [(0, 0, {"product": "prodA", "qty": 1})]}
    )


class TestDeriveRelationInfo:
    """Tests for the _derive_relation_info function."""

    def test_derive_relation_info_known_self_referencing(self) -> None:
        """Test derivation for known self-referencing fields."""
        result = relational_import._derive_relation_info(
            "product.template", "optional_product_ids", "product.template"
        )
        assert result == ("product_optional_rel", "product_template_id")

    def test_derive_relation_info_standard_case(self) -> None:
        """Test derivation for standard cases."""
        result = relational_import._derive_relation_info(
            "res.partner", "category_ids", "res.partner.category"
        )
        # Models sorted: res_partner, res_partner_category
        assert result[0] == "res_partner_res_partner_category_rel"
        assert result[1] == "res_partner_id"


class TestQueryRelationInfoFromOdoo:
    """Tests for the _query_relation_info_from_odoo function."""

    def test_query_relation_info_self_referencing_skipped(self) -> None:
        """Test that self-referencing fields skip the Odoo query."""
        result = relational_import._query_relation_info_from_odoo(
            "dummy.conf", "res.partner", "res.partner"
        )
        assert result is None

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_query_relation_info_found(self, mock_get_conn: MagicMock) -> None:
        """Test successful query from ir.model.relation."""
        mock_relation_model = MagicMock()
        mock_relation_model.search_read.return_value = [
            {
                "name": "partner_category_rel",
                "model": "res.partner",
                "comodel": "res.partner.category",
            }
        ]
        mock_get_conn.return_value.get_model.return_value = mock_relation_model

        result = relational_import._query_relation_info_from_odoo(
            "dummy.conf", "res.partner", "res.partner.category"
        )

        assert result is not None
        assert result[0] == "partner_category_rel"
        assert result[1] == "res_partner_id"

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_query_relation_info_not_found(self, mock_get_conn: MagicMock) -> None:
        """Test when no relation is found in ir.model.relation."""
        mock_relation_model = MagicMock()
        mock_relation_model.search_read.return_value = []
        mock_get_conn.return_value.get_model.return_value = mock_relation_model

        result = relational_import._query_relation_info_from_odoo(
            "dummy.conf", "res.partner", "res.partner.category"
        )

        assert result is None

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_query_relation_info_invalid_field_error(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test handling of Invalid field ValueError."""
        mock_relation_model = MagicMock()
        mock_relation_model.search_read.side_effect = ValueError(
            "Invalid field ir.model.relation.comodel"
        )
        mock_get_conn.return_value.get_model.return_value = mock_relation_model

        result = relational_import._query_relation_info_from_odoo(
            "dummy.conf", "res.partner", "res.partner.category"
        )

        assert result is None

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_query_relation_info_other_value_error(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test that other ValueErrors are re-raised."""
        mock_relation_model = MagicMock()
        mock_relation_model.search_read.side_effect = ValueError("Some other error")
        mock_get_conn.return_value.get_model.return_value = mock_relation_model

        with pytest.raises(ValueError, match="Some other error"):
            relational_import._query_relation_info_from_odoo(
                "dummy.conf", "res.partner", "res.partner.category"
            )

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_dict")
    def test_query_relation_info_with_dict_config(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test query with dict config."""
        mock_relation_model = MagicMock()
        mock_relation_model.search_read.return_value = []
        mock_get_conn.return_value.get_model.return_value = mock_relation_model

        result = relational_import._query_relation_info_from_odoo(
            {"host": "localhost"}, "res.partner", "res.partner.category"
        )

        assert result is None
        mock_get_conn.assert_called_once()

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_query_relation_info_connection_error(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test handling of connection errors."""
        mock_get_conn.side_effect = Exception("Connection failed")

        result = relational_import._query_relation_info_from_odoo(
            "dummy.conf", "res.partner", "res.partner.category"
        )

        assert result is None


class TestResolveRelatedIds:
    """Additional tests for _resolve_related_ids."""

    @patch("fluvo.lib.relational_import.cache.load_id_map")
    def test_resolve_related_ids_cache_hit(self, mock_load_id_map: MagicMock) -> None:
        """Test successful cache hit."""
        expected_df = pl.DataFrame({"external_id": ["cat1"], "db_id": [11]})
        mock_load_id_map.return_value = expected_df

        result = relational_import._resolve_related_ids(
            "dummy.conf", "res.partner.category", pl.Series(["cat1"])
        )

        assert result is not None
        assert result.shape == expected_df.shape

    @patch("fluvo.lib.relational_import.cache.load_id_map", return_value=None)
    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_resolve_related_ids_no_valid_ids(
        self, mock_get_conn: MagicMock, mock_load_id_map: MagicMock
    ) -> None:
        """Test when all IDs are invalid (no module.identifier format)."""
        result = relational_import._resolve_related_ids(
            "dummy.conf", "res.partner.category", pl.Series(["invalid_id_no_dot"])
        )
        assert result is None

    @patch("fluvo.lib.relational_import.cache.load_id_map", return_value=None)
    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_resolve_related_ids_mixed_valid_invalid(
        self, mock_get_conn: MagicMock, mock_load_id_map: MagicMock
    ) -> None:
        """Test when some IDs are valid and some are invalid (covers branch 50->52)."""
        mock_data_model = MagicMock()
        mock_data_model.search_read.return_value = [
            {"module": "mod", "name": "cat1", "res_id": 11}
        ]
        mock_get_conn.return_value.get_model.return_value = mock_data_model

        # Mix of valid and invalid IDs - should log warning but continue
        result = relational_import._resolve_related_ids(
            "dummy.conf",
            "res.partner.category",
            pl.Series(["mod.cat1", "invalid_no_dot"]),
        )

        # Should return result because there's at least one valid ID
        assert result is not None
        assert len(result) == 1

    @patch("fluvo.lib.relational_import.cache.load_id_map", return_value=None)
    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_resolve_related_ids_bulk_success(
        self, mock_get_conn: MagicMock, mock_load_id_map: MagicMock
    ) -> None:
        """Test successful bulk XML-ID resolution."""
        mock_data_model = MagicMock()
        mock_data_model.search_read.return_value = [
            {"module": "mod", "name": "cat1", "res_id": 11},
            {"module": "mod", "name": "cat2", "res_id": 12},
        ]
        mock_get_conn.return_value.get_model.return_value = mock_data_model

        result = relational_import._resolve_related_ids(
            "dummy.conf", "res.partner.category", pl.Series(["mod.cat1", "mod.cat2"])
        )

        assert result is not None
        assert len(result) == 2

    @patch("fluvo.lib.relational_import.cache.load_id_map", return_value=None)
    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_resolve_related_ids_exception_handling(
        self, mock_get_conn: MagicMock, mock_load_id_map: MagicMock
    ) -> None:
        """Test exception handling during bulk resolution."""
        mock_data_model = MagicMock()
        mock_data_model.search_read.side_effect = Exception("Database error")
        mock_get_conn.return_value.get_model.return_value = mock_data_model

        result = relational_import._resolve_related_ids(
            "dummy.conf", "res.partner.category", pl.Series(["mod.cat1"])
        )

        assert result is None


class TestDeriveMissingRelationInfo:
    """Tests for _derive_missing_relation_info."""

    @patch("fluvo.lib.relational_import._query_relation_info_from_odoo")
    def test_derive_missing_uses_odoo_query_result(self, mock_query: MagicMock) -> None:
        """Test that Odoo query result is used when available."""
        mock_query.return_value = ("odoo_relation_table", "odoo_relation_field")

        result = relational_import._derive_missing_relation_info(
            "dummy.conf",
            "res.partner",
            "category_ids",
            None,  # No relation_table
            None,  # No owning_model_fk
            "res.partner.category",
        )

        assert result == ("odoo_relation_table", "odoo_relation_field")

    @patch("fluvo.lib.relational_import._query_relation_info_from_odoo")
    @patch("fluvo.lib.relational_import._derive_relation_info")
    def test_derive_missing_falls_back_to_derivation(
        self, mock_derive: MagicMock, mock_query: MagicMock
    ) -> None:
        """Test fallback to derivation when Odoo query fails."""
        mock_query.return_value = None
        mock_derive.return_value = ("derived_table", "derived_field")

        result = relational_import._derive_missing_relation_info(
            "dummy.conf",
            "res.partner",
            "category_ids",
            None,
            None,
            "res.partner.category",
        )

        assert result == ("derived_table", "derived_field")


class TestRunDirectRelationalImportEdgeCases:
    """Edge case tests for run_direct_relational_import."""

    @patch("fluvo.lib.relational_import.cache.load_id_map")
    def test_run_direct_relational_import_missing_relation_table(
        self, mock_load_id_map: MagicMock
    ) -> None:
        """Test handling when relation_table cannot be derived."""
        source_df = pl.DataFrame({"id": ["p1"], "category_id": ["cat1"]})
        # No relation in strategy_details means we can't derive
        strategy_details: dict[str, str] = {}
        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_direct_relational_import(
            "dummy.conf",
            "res.partner",
            "category_id",
            strategy_details,
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is None

    @patch("fluvo.lib.relational_import._resolve_related_ids", return_value=None)
    @patch("fluvo.lib.relational_import.cache.load_id_map")
    def test_run_direct_relational_import_resolve_fails(
        self, mock_load_id_map: MagicMock, mock_resolve: MagicMock
    ) -> None:
        """Test handling when related ID resolution fails."""
        source_df = pl.DataFrame({"id": ["p1"], "category_id": ["cat1"]})
        strategy_details = {
            "relation_table": "partner_category_rel",
            "relation_field": "partner_id",
            "relation": "res.partner.category",
        }
        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_direct_relational_import(
            "dummy.conf",
            "res.partner",
            "category_id",
            strategy_details,
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is None


class TestRunWriteTupleImportEdgeCases:
    """Edge case tests for run_write_tuple_import."""

    def test_run_write_tuple_import_missing_relation_info(self) -> None:
        """Test handling when relation info cannot be derived."""
        source_df = pl.DataFrame({"id": ["p1"], "category_id": ["cat1"]})
        strategy_details: dict[str, str] = {}
        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_tuple_import(
            "dummy.conf",
            "res.partner",
            "category_id",
            strategy_details,
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is False

    @patch("fluvo.lib.relational_import._resolve_related_ids", return_value=None)
    def test_run_write_tuple_import_resolve_fails(
        self, mock_resolve: MagicMock
    ) -> None:
        """Test handling when related ID resolution fails."""
        source_df = pl.DataFrame({"id": ["p1"], "category_id": ["cat1"]})
        strategy_details = {
            "relation_table": "partner_category_rel",
            "relation_field": "partner_id",
            "relation": "res.partner.category",
        }
        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_tuple_import(
            "dummy.conf",
            "res.partner",
            "category_id",
            strategy_details,
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is False

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    @patch("fluvo.lib.relational_import._resolve_related_ids")
    def test_run_write_tuple_import_field_not_found(
        self, mock_resolve: MagicMock, mock_get_conn: MagicMock
    ) -> None:
        """Test handling when field is not found in source DataFrame."""
        source_df = pl.DataFrame({"id": ["p1"], "name": ["Partner 1"]})
        mock_resolve.return_value = pl.DataFrame(
            {"external_id": ["cat1"], "db_id": [11]}
        )
        strategy_details = {
            "relation_table": "partner_category_rel",
            "relation_field": "partner_id",
            "relation": "res.partner.category",
        }
        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_tuple_import(
            "dummy.conf",
            "res.partner",
            "category_id",  # This field doesn't exist in source_df
            strategy_details,
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is False


class TestFieldIdSuffix:
    """Tests for field/id suffix handling."""

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    @patch("fluvo.lib.relational_import._resolve_related_ids")
    def test_run_direct_relational_import_with_id_suffix(
        self, mock_resolve: MagicMock, mock_get_conn: MagicMock
    ) -> None:
        """Test handling when field has /id suffix in column name."""
        # Source DataFrame has category_id/id column (with /id suffix)
        source_df = pl.DataFrame({"id": ["p1"], "category_id/id": ["cat1"]})
        mock_resolve.return_value = pl.DataFrame(
            {"external_id": ["cat1"], "db_id": [11]}
        )
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        strategy_details = {
            "relation_table": "partner_category_rel",
            "relation_field": "partner_id",
            "relation": "res.partner.category",
        }
        progress = Progress()
        task_id = progress.add_task("test")

        # Field name without /id - function should find category_id/id column
        relational_import.run_direct_relational_import(
            "dummy.conf",
            "res.partner",
            "category_id",
            strategy_details,
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        # Should successfully use the /id suffix column
        mock_resolve.assert_called_once()


class TestRunWriteO2MTupleImportEdgeCases:
    """Edge case tests for run_write_o2m_tuple_import."""

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_dict")
    def test_run_write_o2m_tuple_import_with_dict_config(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test O2M import with dict config."""
        source_df = pl.DataFrame(
            {
                "id": ["p1"],
                "line_ids": ['[{"product": "prodA"}]'],
            }
        )
        mock_parent_model = MagicMock()
        mock_get_conn.return_value.get_model.return_value = mock_parent_model

        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_o2m_tuple_import(
            {"host": "localhost"},
            "res.partner",
            "line_ids",
            {},
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is True

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_run_write_o2m_tuple_import_field_not_found(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test handling when O2M field is not found."""
        source_df = pl.DataFrame({"id": ["p1"], "name": ["Partner 1"]})
        mock_get_conn.return_value.get_model.return_value = MagicMock()
        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_o2m_tuple_import(
            "dummy.conf",
            "res.partner",
            "line_ids",  # Doesn't exist
            {},
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is False

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_run_write_o2m_tuple_import_with_id_suffix_field(
        self, mock_get_conn: MagicMock
    ) -> None:
        """When only the '<field>/id' column exists, read the o2m data from it (#14).

        The code detects the '<field>/id' fallback column for filtering; it must
        also read the row data from that column. Previously it read record[field],
        which KeyError'd on exactly this fallback path (field absent as a column).
        """
        # Only the '/id' column exists (the bare field name is absent).
        source_df = pl.DataFrame(
            {
                "id": ["p1"],
                "line_ids/id": ['[{"product": "prodA"}]'],
            }
        )
        mock_parent_model = MagicMock()
        mock_get_conn.return_value.get_model.return_value = mock_parent_model

        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_o2m_tuple_import(
            "dummy.conf",
            "res.partner",
            "line_ids",
            {},
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is True
        # The child records were written from the '/id' column's JSON, keyed by
        # the real Odoo field name.
        mock_parent_model.write.assert_called_once_with(
            [1], {"line_ids": [(0, 0, {"product": "prodA"})]}
        )

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_run_write_o2m_tuple_import_json_decode_error(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test handling of JSON decode errors."""
        source_df = pl.DataFrame(
            {
                "id": ["p1"],
                "line_ids": ["not valid json"],
            }
        )
        mock_parent_model = MagicMock()
        mock_get_conn.return_value.get_model.return_value = mock_parent_model

        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_o2m_tuple_import(
            "dummy.conf",
            "res.partner",
            "line_ids",
            {},
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is False

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_run_write_o2m_tuple_import_not_a_list_error(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test handling when JSON is not a list."""
        source_df = pl.DataFrame(
            {
                "id": ["p1"],
                "line_ids": ['{"product": "prodA"}'],  # Not a list
            }
        )
        mock_parent_model = MagicMock()
        mock_get_conn.return_value.get_model.return_value = mock_parent_model

        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_o2m_tuple_import(
            "dummy.conf",
            "res.partner",
            "line_ids",
            {},
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is False

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_run_write_o2m_tuple_import_parent_not_in_id_map(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test handling when parent ID is not in id_map."""
        source_df = pl.DataFrame(
            {
                "id": ["p1", "p2"],
                "line_ids": ['[{"product": "A"}]', '[{"product": "B"}]'],
            }
        )
        mock_parent_model = MagicMock()
        mock_get_conn.return_value.get_model.return_value = mock_parent_model

        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_o2m_tuple_import(
            "dummy.conf",
            "res.partner",
            "line_ids",
            {},
            source_df,
            {"p1": 1},  # p2 is not in id_map
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is True
        # Only p1 should be processed
        mock_parent_model.write.assert_called_once()

    @patch("fluvo.lib.relational_import.writer.write_relational_failures_to_csv")
    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_run_write_o2m_tuple_import_write_exception(
        self, mock_get_conn: MagicMock, mock_write_failures: MagicMock
    ) -> None:
        """Test handling when write() raises an exception."""
        source_df = pl.DataFrame(
            {
                "id": ["p1"],
                "line_ids": ['[{"product": "prodA"}]'],
            }
        )
        mock_parent_model = MagicMock()
        mock_parent_model.write.side_effect = Exception("Write failed")
        mock_get_conn.return_value.get_model.return_value = mock_parent_model

        progress = Progress()
        task_id = progress.add_task("test")

        result = relational_import.run_write_o2m_tuple_import(
            "dummy.conf",
            "res.partner",
            "line_ids",
            {},
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        assert result is False
        mock_write_failures.assert_called_once()


class TestCreateRelationalRecords:
    """Tests for _create_relational_records."""

    @patch("fluvo.lib.relational_import.conf_lib.get_connection_from_config")
    def test_create_relational_records_model_access_error(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test handling when model access fails."""
        mock_get_conn.return_value.get_model.side_effect = Exception("Access denied")

        link_df = pl.DataFrame(
            {
                "external_id": ["p1"],
                "category_id": ["cat1"],
                "partner_id": [1],
                "res.partner.category/id": [11],
            }
        )
        owning_df = pl.DataFrame({"external_id": ["p1"], "db_id": [1]})
        related_df = pl.DataFrame({"external_id": ["cat1"], "db_id": [11]})

        result = relational_import._create_relational_records(
            "dummy.conf",
            "res.partner",
            "category_ids",
            "category_id",
            "partner_category_rel",
            "partner_id",
            "res.partner.category",
            link_df,
            owning_df,
            related_df,
            "source.csv",
            10,
        )

        assert result is False
