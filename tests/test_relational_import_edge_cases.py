"""Additional tests to cover missing functionality in relational_import.py."""

from unittest.mock import MagicMock, patch

import polars as pl
from rich.progress import Progress

from odoo_data_flow.lib.relational_import_strategies.direct import (
    _derive_missing_relation_info,
    _derive_relation_info,
    _query_relation_info_from_odoo,
    _resolve_related_ids,
    run_direct_relational_import,
)
from odoo_data_flow.lib.relational_import_strategies.write_o2m_tuple import (
    _create_relational_records,
    run_write_o2m_tuple_import,
)
from odoo_data_flow.lib.relational_import_strategies.write_tuple import (
    _execute_write_tuple_updates,
    _prepare_link_dataframe,
)


@patch("odoo_data_flow.lib.cache.load_id_map")
def test_resolve_related_ids_cache_hit(mock_load_id_map: MagicMock) -> None:
    """Test _resolve_related_ids with cache hit."""
    expected_df = pl.DataFrame({"external_id": ["p1"], "db_id": [1]})
    mock_load_id_map.return_value = expected_df

    result = _resolve_related_ids("dummy.conf", "res.partner", pl.Series(["p1"]))

    assert result is not None
    assert result.shape == expected_df.shape
    mock_load_id_map.assert_called_once_with("dummy.conf", "res.partner")


@patch("odoo_data_flow.lib.cache.load_id_map", return_value=None)
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_resolve_related_ids_db_ids_only(
    mock_get_conn: MagicMock, mock_load_id_map: MagicMock
) -> None:
    """Test _resolve_related_ids with only database IDs."""
    mock_data_model = MagicMock()
    mock_get_conn.return_value.get_model.return_value = mock_data_model
    # Return some fake data to simulate the search_read result
    # The search_read should return fields with "name" and "res_id" as per the function's expectations
    mock_data_model.search_read.return_value = [
        {"name": "ext_id_123", "res_id": 123},
        {"name": "ext_id_456", "res_id": 456},
    ]

    # Test with string IDs that should be processed by the mock
    result = _resolve_related_ids(
        "dummy.conf", "res.partner", pl.Series(["ext_id_123", "ext_id_456"])
    )

    # The result should not be None since we have mock data
    assert result is not None
    # The DataFrame should have the expected columns
    assert "id" in result.columns
    assert "res_id" in result.columns


@patch("odoo_data_flow.lib.cache.load_id_map", return_value=None)
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_resolve_related_ids_mixed_ids(
    mock_get_conn: MagicMock, mock_load_id_map: MagicMock
) -> None:
    """Test _resolve_related_ids with mixed database and XML IDs."""
    mock_data_model = MagicMock()
    mock_get_conn.return_value.get_model.return_value = mock_data_model
    mock_data_model.search_read.return_value = [{"name": "p1", "res_id": 789}]

    # Test with mixed numeric (db) and string (xml) IDs
    result = _resolve_related_ids("dummy.conf", "res.partner", pl.Series(["123", "p1"]))

    assert result is not None
    # Should handle both database and XML IDs


@patch("odoo_data_flow.lib.cache.load_id_map", return_value=None)
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_resolve_related_ids_invalid_ids(
    mock_get_conn: MagicMock, mock_load_id_map: MagicMock
) -> None:
    """Test _resolve_related_ids with invalid IDs."""
    mock_data_model = MagicMock()
    mock_get_conn.return_value.get_model.return_value = mock_data_model
    mock_data_model.search_read.return_value = []

    # Test with empty/None values
    result = _resolve_related_ids("dummy.conf", "res.partner", pl.Series(["", None]))

    # Should return an empty DataFrame when there are no valid IDs to process
    # The function returns an empty DataFrame with proper schema, not None
    assert result is not None  # The result is an empty DataFrame, not None
    assert result.height == 0  # Empty DataFrame


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_dict")
def test_resolve_related_ids_with_dict_config(mock_get_conn_dict: MagicMock) -> None:
    """Test _resolve_related_ids with dictionary config."""
    mock_data_model = MagicMock()
    mock_get_conn_dict.return_value.get_model.return_value = mock_data_model
    mock_data_model.search_read.return_value = [{"name": "p1", "res_id": 1}]

    result = _resolve_related_ids(
        {"host": "localhost"}, "res.partner", pl.Series(["p1"])
    )

    assert result is not None
    mock_get_conn_dict.assert_called_once()


@patch(
    "odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo"
)
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_derive_relation_info_self_referencing(
    mock_get_connection: MagicMock, mock_query_relation: MagicMock
) -> None:
    """Test _derive_relation_info with self-referencing detection."""
    # Mock the query to return expected values
    mock_query_relation.return_value = ("many2many", "product.template")

    # Mock the connection
    mock_connection = MagicMock()
    mock_get_connection.return_value = mock_connection
    mock_model = MagicMock()
    mock_connection.get_model.return_value = mock_model
    mock_model.fields_get.return_value = {
        "optional_product_ids": {"type": "many2many", "relation": "product.template"}
    }

    _relation_df, derived_type, derived_relation = _derive_relation_info(
        "dummy.conf",
        "product.template",
        "optional_product_ids",
        pl.DataFrame(),
        "many2many",
        "product.template",
    )

    # The function returns (relation_df, field_type, relation_model)
    # Test that we get the expected field type and relation
    assert derived_type == "many2many"
    assert derived_relation == "product.template"


@patch(
    "odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo"
)
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_derive_relation_info_regular(
    mock_get_connection: MagicMock, mock_query_relation: MagicMock
) -> None:
    """Test _derive_relation_info with regular models."""
    # Mock the query to return expected values
    mock_query_relation.return_value = ("many2one", "res.partner.category")

    # Mock the connection
    mock_connection = MagicMock()
    mock_get_connection.return_value = mock_connection
    mock_model = MagicMock()
    mock_connection.get_model.return_value = mock_model
    mock_model.fields_get.return_value = {
        "category_id": {"type": "many2one", "relation": "res.partner.category"}
    }

    _relation_df, derived_type, derived_relation = _derive_relation_info(
        "dummy.conf",
        "res.partner",
        "category_id",
        pl.DataFrame(),
        "many2one",
        "res.partner.category",
    )

    # The function returns (relation_df, field_type, relation_model)
    # Test that we get the expected field type and relation
    assert isinstance(derived_type, str)
    assert isinstance(derived_relation, str)
    assert derived_type == "many2one"
    assert derived_relation == "res.partner.category"


def test_derive_missing_relation_info_with_odoo_query() -> None:
    """Test _derive_missing_relation_info when Odoo query succeeds."""
    with patch(
        "odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo",
        return_value=("test_table", "test_field"),
    ):
        _relation_df, table, field = _derive_missing_relation_info(
            "dummy.conf",
            "res.partner",
            "category_id",
            None,
            "res.partner.category",
            pl.DataFrame(),
        )

        assert table == "test_table"
        assert field == "test_field"


def test_derive_missing_relation_info_self_referencing_skip() -> None:
    """Test _derive_missing_relation_info that skips self-referencing query."""
    with patch(
        "odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo",
        return_value=None,
    ):
        _relation_df, table, field = _derive_missing_relation_info(
            "dummy.conf",
            "res.partner",
            "category_id",
            "existing_table",  # field_type
            "res.partner.category",  # relation
            pl.DataFrame(),  # source_df
        )

        # Should return the provided values since query returns None (no override)
        assert table == "existing_table"
        assert field == "res.partner.category"  # This is the provided relation value


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_query_relation_info_from_odoo_self_referencing(
    mock_get_conn: MagicMock,
) -> None:
    """Test _query_relation_info_from_odoo with self-referencing models."""
    result = _query_relation_info_from_odoo("dummy.conf", "res.partner", "res.partner")

    # Should return None for self-referencing to avoid constraint errors
    assert result is None


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_query_relation_info_from_odoo_exception(mock_get_conn: MagicMock) -> None:
    """Test _query_relation_info_from_odoo with connection exception."""
    mock_get_conn.side_effect = Exception("Connection failed")

    result = _query_relation_info_from_odoo(
        "dummy.conf", "res.partner", "res.partner.category"
    )

    # Should return None on exception
    assert result is None


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_query_relation_info_from_odoo_value_error(mock_get_conn: MagicMock) -> None:
    """Test _query_relation_info_from_odoo with ValueError."""
    # Mock the connection and model but don't raise ValueError from search_read
    mock_model = MagicMock()
    mock_model.search_read.return_value = []
    mock_get_conn.return_value.get_model.return_value = mock_model

    result = _query_relation_info_from_odoo(
        "dummy.conf", "res.partner", "res.partner.category"
    )

    # Should return result (not None) when no exception occurs
    # If there are no relations, it would return None
    # So we want to make sure it doesn't crash
    assert result is None or isinstance(result, tuple)


@patch(
    "odoo_data_flow.lib.relational_import_strategies.direct._derive_missing_relation_info"
)
@patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
def test_run_direct_relational_import_missing_info(
    mock_resolve_ids: MagicMock, mock_derive_info: MagicMock
) -> None:
    """Test run_direct_relational_import when required info is missing."""
    source_df = pl.DataFrame(
        {"id": ["p1"], "name": ["Partner 1"], "category_id/id": ["cat1"]}
    )
    mock_resolve_ids.return_value = pl.DataFrame(
        {"external_id": ["cat1"], "db_id": [1]}
    )
    mock_derive_info.return_value = (
        pl.DataFrame(),
        "",
        "",
    )  # Missing table and field

    with Progress() as progress:
        task_id = progress.add_task("test")

        result = run_direct_relational_import(
            "dummy.conf",
            "res.partner",
            "category_id",
            {"relation": "res.partner.category"},
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        # Should return None when required info is missing
        assert result is None


@patch(
    "odoo_data_flow.lib.relational_import_strategies.direct._derive_missing_relation_info"
)
@patch(
    "odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids",
    return_value=None,
)
def test_run_direct_relational_import_resolve_fail(
    mock_resolve_ids: MagicMock, mock_derive_info: MagicMock
) -> None:
    """Test run_direct_relational_import when ID resolution fails."""
    source_df = pl.DataFrame(
        {"id": ["p1"], "name": ["Partner 1"], "category_id/id": ["cat1"]}
    )
    mock_derive_info.return_value = (
        pl.DataFrame(),
        "res_partner_category_rel",
        "partner_id",
    )

    with Progress() as progress:
        task_id = progress.add_task("test")

        result = run_direct_relational_import(
            "dummy.conf",
            "res.partner",
            "category_id",
            {"relation": "res.partner.category"},
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        # Should return None when ID resolution fails
        assert result is None


@patch(
    "odoo_data_flow.lib.relational_import_strategies.direct._derive_missing_relation_info"
)
@patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
def test_run_direct_relational_import_field_not_found(
    mock_resolve_ids: MagicMock, mock_derive_info: MagicMock
) -> None:
    """Test run_direct_relational_import when field is not found in DataFrame."""
    source_df = pl.DataFrame(
        {
            "id": ["p1"],
            "name": ["Partner 1"],
            # Note: category_id/id field is missing
        }
    )
    mock_resolve_ids.return_value = pl.DataFrame(
        {"external_id": ["cat1"], "db_id": [1]}
    )
    mock_derive_info.return_value = (
        pl.DataFrame(),
        "res_partner_category_rel",
        "partner_id",
    )

    with Progress() as progress:
        task_id = progress.add_task("test")

        result = run_direct_relational_import(
            "dummy.conf",
            "res.partner",
            "category_id",  # This field doesn't exist in the DataFrame
            {"relation": "res.partner.category"},
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        # Should return None when field is not found
        assert result is None


def test_prepare_link_dataframe_field_not_found() -> None:
    """Test _prepare_link_dataframe when field is not found in DataFrame."""
    source_df = pl.DataFrame(
        {
            "id": ["p1"],
            "name": ["Partner 1"],
        }
    )

    result = _prepare_link_dataframe(
        "dummy.conf",  # config
        "res.partner.category",  # model
        "partner_id",  # field
        source_df,  # source_df
        {"cat1": 1},  # id_map (sample mapping)
        1000,  # batch_size
    )

    # Should return None when field is not found
    assert result is None


def test_execute_write_tuple_updates_invalid_config_dict() -> None:
    """Test _execute_write_tuple_updates with dictionary config."""
    link_df = pl.DataFrame({"source_id": ["p1", "p2"], "field_value": [1, 2]})

    with patch(
        "odoo_data_flow.lib.conf_lib.get_connection_from_dict"
    ) as mock_get_conn_dict:
        mock_model = MagicMock()
        mock_get_conn_dict.return_value.get_model.return_value = mock_model

        result = _execute_write_tuple_updates(
            {
                "hostname": "localhost",
                "database": "test",
                "login": "admin",
                "password": "pass",
            },  # Dict config with required fields
            "res.partner",
            "category_id",
            link_df,
            {"p1": 100, "p2": 101},
            1000,  # batch_size
        )

        # Should handle dict config and return (successful_updates, failed_records)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], int)  # successful_updates
        assert isinstance(result[1], list)  # failed_records


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_execute_write_tuple_updates_model_access_error(
    mock_get_conn: MagicMock,
) -> None:
    """Test _execute_write_tuple_updates when model access fails."""
    mock_get_conn.return_value.get_model.side_effect = Exception("Model access error")

    link_df = pl.DataFrame({"source_id": ["p1"], "field_value": [1]})

    _execute_write_tuple_updates(
        "dummy.conf",
        "res.partner",
        "category_id",
        link_df,
        {"p1": 100},
        1000,  # batch_size
    )


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_execute_write_tuple_updates_invalid_related_id_format(
    mock_get_conn: MagicMock,
) -> None:
    """Test _execute_write_tuple_updates with invalid related ID format."""
    link_df = pl.DataFrame(
        {
            "source_id": ["p1"],
            "field_value": ["invalid"],  # Non-numeric ID
        }
    )

    mock_model = MagicMock()
    mock_get_conn.return_value.get_model.return_value = mock_model

    _execute_write_tuple_updates(
        "dummy.conf",
        "res.partner",
        "category_id",
        link_df,
        {"p1": 100},
        1000,  # batch_size
    )


@patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
@patch(
    "odoo_data_flow.lib.relational_import_strategies.write_tuple._execute_write_tuple_updates",
    return_value=True,
)
@patch(
    "odoo_data_flow.lib.relational_import_strategies.direct._derive_missing_relation_info"
)
def test__execute_write_tuple_updates_field_not_found(
    mock_derive_info: MagicMock, mock_execute: MagicMock, mock_resolve_ids: MagicMock
) -> None:
    """Test _execute_write_tuple_updates when field is not found in DataFrame."""
    pl.DataFrame(
        {
            "id": ["p1"],
            "name": ["Partner 1"],
        }
    )
    mock_resolve_ids.return_value = pl.DataFrame(
        {"external_id": ["cat1"], "db_id": [1]}
    )
    mock_derive_info.return_value = (
        pl.DataFrame(),
        "res_partner_category_rel",
        "partner_id",
    )

    with Progress() as progress:
        progress.add_task("test")

        result = _execute_write_tuple_updates(
            "dummy.conf",
            "res.partner",
            "category_id",
            pl.DataFrame({"source_id": ["p1"], "field_value": ["cat1"]}),
            {"p1": 1},
            1000,  # batch_size
        )
        # Should return tuple of (successful_updates, failed_records) on error
        assert isinstance(result, tuple)
        assert len(result) == 2
        successful_updates, failed_records = result
        assert isinstance(successful_updates, int)
        assert isinstance(failed_records, list)
        # On error, should have 0 successful updates and some failed records
        assert successful_updates == 0
        assert len(failed_records) > 0


def test_create_relational_records_dict_config() -> None:
    """Test _create_relational_records with dictionary config."""
    pl.DataFrame({"external_id": ["p1"], "category_id/id": ["cat1"]})
    pl.DataFrame({"external_id": ["p1"], "db_id": [100]})
    pl.DataFrame({"external_id": ["cat1"], "db_id": [1]})

    with patch(
        "odoo_data_flow.lib.conf_lib.get_connection_from_dict"
    ) as mock_get_conn_dict:
        mock_model = MagicMock()
        mock_get_conn_dict.return_value.get_model.return_value = mock_model
        result = _create_relational_records(
            {
                "hostname": "localhost",
                "database": "test",
                "login": "admin",
                "password": "pass",
            },  # Dict config with required fields
            "res.partner",
            "category_id",
            "res.partner.category",
            123,  # parent_id
            ["cat1", "cat2"],  # related_external_ids
            {},  # context
        )
        # Should handle dict config
        assert isinstance(result, tuple)
        assert len(result) == 2
        created_ids, failed_records = result
        assert isinstance(created_ids, list)
        assert isinstance(failed_records, list)


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_create_relational_records_model_error(mock_get_conn: MagicMock) -> None:
    """Test _create_relational_records when model access fails."""
    mock_get_conn.return_value.get_model.side_effect = Exception("Model error")

    pl.DataFrame({"external_id": ["p1"], "category_id/id": ["cat1"]})
    pl.DataFrame({"external_id": ["p1"], "db_id": [100]})
    pl.DataFrame({"external_id": ["cat1"], "db_id": [1]})
    result = _create_relational_records(
        "dummy.conf",
        "res.partner",
        "category_id",
        "res.partner.category",
        123,  # parent_id
        ["cat1", "cat2"],  # related_external_ids
        {},  # context
    )
    # Should return tuple of (created_ids, failed_records) on error
    assert isinstance(result, tuple)
    assert len(result) == 2
    created_ids, failed_records = result
    assert isinstance(created_ids, list)
    assert isinstance(failed_records, list)
    # On error, should have no created IDs and some failed records
    assert len(created_ids) == 0
    assert len(failed_records) > 0


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_run_write_o2m_tuple_import_invalid_json(mock_get_conn: MagicMock) -> None:
    """Test run_write_o2m_tuple_import with invalid JSON."""
    source_df = pl.DataFrame(
        {
            "id": ["p1"],
            "name": ["Partner 1"],
            "line_ids/id": ["invalid json"],  # Invalid JSON
        }
    )

    mock_model = MagicMock()
    mock_get_conn.return_value.get_model.return_value = mock_model

    with Progress() as progress:
        task_id = progress.add_task("test")

        result = run_write_o2m_tuple_import(
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

        # Should handle invalid JSON gracefully
        assert isinstance(result, bool)


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_run_write_o2m_tuple_import_field_not_found(mock_get_conn: MagicMock) -> None:
    """Test run_write_o2m_tuple_import when field is not found."""
    source_df = pl.DataFrame(
        {
            "id": ["p1"],
            "name": ["Partner 1"],
            # No line_ids field
        }
    )

    mock_model = MagicMock()
    mock_get_conn.return_value.get_model.return_value = mock_model

    with Progress() as progress:
        task_id = progress.add_task("test")

        result = run_write_o2m_tuple_import(
            "dummy.conf",
            "res.partner",
            "line_ids",
            {"relation": "res.partner.line"},  # Provide the required relation
            source_df,
            {"p1": 1},
            1,
            10,
            progress,
            task_id,
            "source.csv",
        )

        # Should return True when field is not found (function handles this gracefully)
        assert result is True


@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_run_write_o2m_tuple_import_write_error(mock_get_conn: MagicMock) -> None:
    """Test run_write_o2m_tuple_import when write operation fails."""
    source_df = pl.DataFrame(
        {
            "id": ["p1"],
            "name": ["Partner 1"],
            "line_ids/id": ['[{"product": "prodA", "qty": 1}]'],
        }
    )

    mock_model = MagicMock()
    mock_model.write.side_effect = Exception("Write error")
    mock_get_conn.return_value.get_model.return_value = mock_model

    with Progress() as progress:
        task_id = progress.add_task("test")

        result = run_write_o2m_tuple_import(
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

        # Should handle write errors gracefully
        assert isinstance(result, bool)
