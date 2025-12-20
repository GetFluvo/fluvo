"""Tests for the direct relational import strategy."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import polars as pl
from rich.progress import Progress

from odoo_data_flow.lib import relational_import
from odoo_data_flow.lib.relational_import_strategies import direct as direct_strategy


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
@patch("odoo_data_flow.lib.cache.load_id_map")
@patch("odoo_data_flow.lib.relational_import_strategies.direct._derive_relation_info")
def test_run_direct_relational_import(
    mock_derive_relation_info: MagicMock,
    mock_load_id_map: MagicMock,
    mock_get_connection_from_config: MagicMock,
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

    # Mock the connection setup to prevent configuration errors
    mock_connection = MagicMock()
    mock_get_connection_from_config.return_value = mock_connection
    mock_model = MagicMock()
    mock_connection.get_model.return_value = mock_model

    # Mock _derive_relation_info to return valid data instead of letting it fail
    mock_derive_relation_info.return_value = (
        pl.DataFrame({"id": ["p1"], "res_id": [101]}),  # relation_df
        "many2one",  # derived_type
        "res.partner.category",  # derived_relation
    )

    strategy_details = {
        "type": "many2one",
        "relation": "res.partner.category",
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
        "test.csv",
    )

    # Assert
    assert result is not None
    assert isinstance(result, dict)
    assert "model" in result
    assert "field" in result
    assert "updates" in result


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
@patch("odoo_data_flow.lib.cache.load_id_map")
@patch("odoo_data_flow.lib.relational_import_strategies.write_tuple.pl.read_csv")
def test_run_write_tuple_import(
    mock_polars_read_csv: MagicMock,
    mock_load_id_map: MagicMock,
    mock_get_connection_from_config: MagicMock,
    tmp_path: Path,
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
    # Mock pl.read_csv to return the source_df when called with "test.csv"
    mock_polars_read_csv.return_value = source_df

    mock_load_id_map.return_value = pl.DataFrame(
        {"external_id": ["cat1", "cat2", "cat3"], "db_id": [11, 12, 13]}
    )

    mock_connection = MagicMock()
    mock_get_connection_from_config.return_value = mock_connection
    mock_model = MagicMock()
    mock_connection.get_model.return_value = mock_model
    mock_model.export_data.return_value = {"datas": [["Test"]]}

    strategy_details = {
        "type": "many2one",
        "relation": "res.partner.category",
    }
    id_map = {"p1": 1, "p2": 2}
    progress = Progress()
    task_id = progress.add_task("test")

    # Act
    print("DEBUG: About to call run_write_tuple_import")
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
        "test.csv",
    )
    print(f"DEBUG: run_write_tuple_import returned: {result}")
    print(f"DEBUG: mock_load_id_map.call_count: {mock_load_id_map.call_count}")

    # Assert
    assert result is True


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_resolve_related_ids_failure(
    mock_get_connection_from_config: MagicMock,
) -> None:
    """Test that _resolve_related_ids returns None on failure."""
    mock_connection = MagicMock()
    mock_get_connection_from_config.return_value = mock_connection
    mock_model = MagicMock()
    mock_connection.get_model.return_value = mock_model
    mock_model.search_read.side_effect = Exception("Test error")

    result = direct_strategy._resolve_related_ids(
        "dummy.conf", "res.partner.category", pl.Series(["cat1", "cat2"])
    )

    assert result is None


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_dict")
def test_resolve_related_ids_with_dict(mock_get_conn_dict: MagicMock) -> None:
    """Test _resolve_related_ids with a dictionary config."""
    mock_connection = MagicMock()
    mock_get_conn_dict.return_value = mock_connection
    mock_model = MagicMock()
    mock_connection.get_model.return_value = mock_model
    mock_model.search_read.return_value = [
        {"module": "base", "name": "partner_category_1", "res_id": 11},
        {"module": "base", "name": "partner_category_2", "res_id": 12},
    ]

    result = direct_strategy._resolve_related_ids(
        {"hostname": "localhost"},
        "res.partner.category",
        pl.Series(["cat1", "cat2"]),
    )

    assert result is not None
    # The function returns a DataFrame with external_id and db_id columns
    assert result.height == 2
    # Check that the DataFrame contains the expected data
    assert "id" in result.columns
    assert "res_id" in result.columns
    # Check the values in the DataFrame
    ids = result["id"].to_list()
    res_ids = result["res_id"].to_list()
    assert "partner_category_1" in ids
    assert "partner_category_2" in ids
    assert 11 in res_ids
    assert 12 in res_ids


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_resolve_related_ids_connection_error(
    mock_get_connection_from_config: MagicMock,
) -> None:
    """Test that _resolve_related_ids returns None on connection error."""
    mock_get_connection_from_config.side_effect = Exception("Connection error")

    result = direct_strategy._resolve_related_ids(
        "dummy.conf", "res.partner.category", pl.Series(["cat1", "cat2"])
    )

    assert result is None


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
@patch("odoo_data_flow.lib.cache.load_id_map")
def test_run_write_o2m_tuple_import(
    mock_load_id_map: MagicMock,
    mock_get_connection_from_config: MagicMock,
) -> None:
    """Test write O2M tuple import."""
    # Arrange
    source_df = pl.DataFrame(
        {
            "id": ["p1", "p2"],
            "name": ["Partner 1", "Partner 2"],
            "child_ids": [
                '[{"name": "Child 1"}, {"name": "Child 2"}]',
                '[{"name": "Child 3"}]',
            ],
        }
    )
    mock_load_id_map.return_value = pl.DataFrame(
        {"external_id": ["p1", "p2"], "db_id": [1, 2]}
    )

    mock_connection = MagicMock()
    mock_get_connection_from_config.return_value = mock_connection
    mock_model = MagicMock()
    mock_connection.get_model.return_value = mock_model
    mock_model.export_data.return_value = {"datas": [["Test"]]}

    strategy_details = {
        "relation": "res.partner",
    }
    id_map = {"p1": 1, "p2": 2}
    progress = Progress()
    task_id = progress.add_task("test")

    # Act
    result = relational_import.run_write_o2m_tuple_import(
        "dummy.conf",
        "res.partner",
        "child_ids",
        strategy_details,
        source_df,
        id_map,
        1,
        10,
        progress,
        task_id,
        "test.csv",
    )

    # Assert
    assert result is True


class TestQueryRelationInfoFromOdoo:
    """Tests for the _query_relation_info_from_odoo function."""

    @patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
    def test_query_relation_info_from_odoo_success(
        self, mock_get_connection: MagicMock
    ) -> None:
        """Test successful query of relation info from Odoo."""
        # Arrange
        mock_connection = MagicMock()
        mock_get_connection.return_value = mock_connection
        mock_model = MagicMock()
        mock_connection.get_model.return_value = mock_model
        mock_model.fields_get.return_value = {
            "product.attribute.value": {
                "type": "many2one",
                "relation": "product_template_attribute_line_rel",
            }
        }

        # Act
        result = direct_strategy._query_relation_info_from_odoo(
            "dummy.conf", "product.template", "product.attribute.value"
        )

        # Assert
        assert result is not None
        assert result[0] == "many2one"  # field type from mocked fields_get
        assert (
            result[1] == "product_template_attribute_line_rel"
        )  # relation from mocked fields_get
        mock_get_connection.assert_called_once_with(config_file="dummy.conf")
        mock_model.fields_get.assert_called_once_with(["product.attribute.value"])

    @patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
    def test_query_relation_info_from_odoo_no_results(
        self, mock_get_connection: MagicMock
    ) -> None:
        """Test query of relation info from Odoo when no relations are found."""
        # Arrange
        mock_connection = MagicMock()
        mock_get_connection.return_value = mock_connection
        mock_model = MagicMock()
        mock_connection.get_model.return_value = mock_model
        mock_model.fields_get.return_value = {}

        # Act
        result = direct_strategy._query_relation_info_from_odoo(
            "dummy.conf", "product.template", "product.attribute.value"
        )

        # Assert
        assert result is None
        mock_get_connection.assert_called_once_with(config_file="dummy.conf")
        mock_model.fields_get.assert_called_once_with(["product.attribute.value"])

    @patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
    def test_query_relation_info_from_odoo_value_error_handling(
        self, mock_get_connection: MagicMock
    ) -> None:
        """Test query of relation info from Odoo with ValueError handling."""
        # Arrange
        mock_connection = MagicMock()
        mock_get_connection.return_value = mock_connection
        mock_model = MagicMock()
        mock_connection.get_model.return_value = mock_model
        # Simulate Odoo raising a ValueError with a field validation error
        # that includes ir.model.relation
        mock_model.fields_get.side_effect = ValueError(
            "Invalid field 'comodel' in domain [('model', '=', 'product.template')]"
            " for model ir.model.relation"
        )

        # Act
        result = direct_strategy._query_relation_info_from_odoo(
            "dummy.conf", "product.template", "product.attribute.value"
        )

        # Assert
        assert result is None
        mock_get_connection.assert_called_once_with(config_file="dummy.conf")
        mock_model.fields_get.assert_called_once_with(["product.attribute.value"])

    @patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
    def test_query_relation_info_from_odoo_general_exception(
        self, mock_get_connection: MagicMock
    ) -> None:
        """Test query of relation info from Odoo with general exception handling."""
        # Arrange
        mock_get_connection.side_effect = Exception("Connection failed")

        # Act
        result = direct_strategy._query_relation_info_from_odoo(
            "dummy.conf", "product.template", "product.attribute.value"
        )

        # Assert
        assert result is None

    @patch("odoo_data_flow.lib.conf_lib.get_connection_from_dict")
    def test_query_relation_info_from_odoo_with_dict_config(
        self, mock_get_connection: MagicMock
    ) -> None:
        """Test query of relation info from Odoo with dictionary configuration."""
        # Arrange
        mock_connection = MagicMock()
        mock_get_connection.return_value = mock_connection
        mock_model = MagicMock()
        mock_connection.get_model.return_value = mock_model
        mock_model.fields_get.return_value = {
            "product.attribute.value": {
                "type": "many2one",
                "relation": "product_template_attribute_line_rel",
            }
        }

        config_dict = {"hostname": "localhost", "database": "test_db"}

        # Act
        result = direct_strategy._query_relation_info_from_odoo(
            config_dict, "product.template", "product.attribute.value"
        )

        # Assert
        assert result is not None
        assert result[0] == "many2one"  # field type from mocked fields_get
        assert (
            result[1] == "product_template_attribute_line_rel"
        )  # relation from mocked fields_get
        mock_get_connection.assert_called_once_with(config_dict)
        mock_model.fields_get.assert_called_once_with(["product.attribute.value"])


class TestDeriveMissingRelationInfo:
    """Tests for the _derive_missing_relation_info function."""

    def test_derive_missing_relation_info_with_all_info(self) -> None:
        """Test derive missing relation info when all info is already present."""
        import polars as pl

        # Arrange - Create a mock DataFrame as the source_df parameter
        mock_df = pl.DataFrame({"attribute_line_ids": ["test_val"]})

        # Act - Call with proper parameters: config, model, field, field_type, relation, source_df
        result = direct_strategy._derive_missing_relation_info(
            "dummy.conf",
            "product.template",
            "attribute_line_ids",
            "product_template_attribute_line_rel",  # field_type
            "product_template_id",  # relation
            mock_df,  # source_df - the 6th parameter
        )

        # Assert - Function returns (DataFrame, str, str), so check the second and third values
        # The function should return the field_type and relation as provided or derived
        _, returned_field_type, returned_relation = result
        assert returned_field_type == "product_template_attribute_line_rel"
        assert returned_relation == "product_template_id"

    @patch(
        "odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo"
    )
    def test_derive_missing_relation_info_without_table(
        self, mock_query: MagicMock
    ) -> None:
        """Test derive missing relation info when table is missing."""
        # Arrange
        mock_query.return_value = ("derived_table", "derived_field")

        # Act
        result = direct_strategy._derive_missing_relation_info(
            "dummy.conf",
            "product.template",
            "attribute_line_ids",
            None,  # Missing table
            "product_template_id",
            pl.DataFrame(),  # source_df - should be DataFrame, not string
        )

        # Assert
        assert result[0].height == 0  # Empty DataFrame
        assert result[1] == "derived_table"  # derived_type from mock
        assert result[2] == "derived_field"  # original relation parameter
        mock_query.assert_called_once()

    @patch(
        "odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo"
    )
    def test_derive_missing_relation_info_without_field(
        self, mock_query: MagicMock
    ) -> None:
        """Test derive missing relation info when field is missing."""
        import polars as pl

        # Arrange
        mock_query.return_value = (
            "product_template_attribute_line_rel",
            "derived_field",
        )
        # Create a mock DataFrame as the source_df parameter
        mock_df = pl.DataFrame({"attribute_line_ids": ["test_val"]})

        # Act
        result = direct_strategy._derive_missing_relation_info(
            "dummy.conf",
            "product.template",
            "attribute_line_ids",
            "product_template_attribute_line_rel",
            None,  # Missing relation
            mock_df,  # source_df - the 6th parameter
        )

        # Assert
        mock_query.assert_called_once()
        # The result is (DataFrame, derived_type, derived_relation)
        _, returned_type, returned_relation = result
        assert (
            returned_type == "product_template_attribute_line_rel"
        )  # from original field_type param
        assert returned_relation == "derived_field"  # from mock query result

    @patch(
        "odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo"
    )
    def test_derive_missing_relation_info_without_both(
        self, mock_query: MagicMock
    ) -> None:
        """Test derive missing relation info when both table and field are missing."""
        import polars as pl

        # Arrange
        mock_query.return_value = ("derived_table", "derived_field")
        # Create a mock DataFrame as the source_df parameter
        mock_df = pl.DataFrame({"attribute_line_ids": ["test_val"]})

        # Act
        result = direct_strategy._derive_missing_relation_info(
            "dummy.conf",
            "product.template",
            "attribute_line_ids",
            None,  # Missing field_type
            None,  # Missing relation
            mock_df,  # source_df - the 6th parameter
        )

        # Assert
        mock_query.assert_called_once()
        # The result is (DataFrame, derived_type, derived_relation)
        _, returned_type, returned_relation = result
        assert returned_type == "derived_table"  # from mock query result
        assert returned_relation == "derived_field"  # from mock query result

    @patch(
        "odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo"
    )
    def test_derive_missing_relation_info_query_returns_none(
        self, mock_query: MagicMock
    ) -> None:
        """Test derive missing relation info when query returns None."""
        # Arrange
        mock_query.return_value = None

        # Act
        result = direct_strategy._derive_missing_relation_info(
            "dummy.conf",
            "product.template",
            "attribute_line_ids",
            None,  # Missing table
            None,  # Missing field
            pl.DataFrame(),
        )

        # Assert
        # Should fall back to derivation logic
        assert result[0] is not None
        assert result[1] is not None
        mock_query.assert_called_once()


class TestDeriveRelationInfo:
    """Tests for the _derive_relation_info function."""

    def test_derive_relation_info_known_mapping(self) -> None:
        """Test derive relation info with a known self-referencing field mapping."""
        # Act
        result = direct_strategy._derive_relation_info(
            "dummy.conf", "product.template", "optional_product_ids", pl.DataFrame()
        )

        # Assert
        assert isinstance(result[0], pl.DataFrame)  # First element is DataFrame
        assert (
            result[1] == ""
        )  # Second element is field type (empty when connection fails)
        assert (
            result[2] == ""
        )  # Third element is relation model (empty when connection fails)

    def test_derive_relation_info_derived_mapping(self) -> None:
        """Test derive relation info with derived mapping."""
        # Act
        result = direct_strategy._derive_relation_info(
            "dummy.conf", "product.template", "attribute_line_ids", pl.DataFrame()
        )

        # Assert
        assert isinstance(result[0], pl.DataFrame)  # First element is DataFrame
        assert (
            result[1] == ""
        )  # Second element is field type (empty when connection fails)
        assert (
            result[2] == ""
        )  # Third element is relation model (empty when connection fails)

    def test_derive_relation_info_reverse_order(self) -> None:
        """Test derive relation info with reversed model order."""
        # Act
        result = direct_strategy._derive_relation_info(
            "dummy.conf",
            "product.attribute.value",
            "attribute_line_ids",
            pl.DataFrame(),
        )

        # Assert
        assert isinstance(result[0], pl.DataFrame)  # First element is DataFrame
        assert (
            result[1] == ""
        )  # Second element is field type (empty when connection fails)
        assert (
            result[2] == ""
        )  # Third element is relation model (empty when connection fails)
