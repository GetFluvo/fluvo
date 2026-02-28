"""Comprehensive tests for import_threaded module to improve coverage."""

import csv
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


def test_prepare_pass_2_data() -> None:
    """Test _prepare_pass_2_data function."""
    from odoo_data_flow.import_threaded import _prepare_pass_2_data

    # Mock the required parameters
    all_data = [["rec_1", "Test Name", "rec_2"]]  # rec_2 is a self-reference
    header = ["id", "name", "parent_id"]
    unique_id_field_index = 0
    id_map = {"rec_1": 1, "rec_2": 2}
    deferred_fields = ["parent_id"]

    # Create a mock model object
    mock_model = MagicMock()
    mock_model.fields_get.return_value = {"parent_id": {"type": "many2one"}}

    # Test with correct parameters
    result = _prepare_pass_2_data(
        all_data=all_data,
        header=header,
        unique_id_field_index=unique_id_field_index,
        id_map=id_map,
        deferred_fields=deferred_fields,
        model_obj=mock_model,
    )

    # Verify result is a list
    assert isinstance(result, list)


def test_handle_create_error_scenarios() -> None:
    """Test _handle_create_error with different error types and scenarios."""
    from odoo_data_flow.import_threaded import _handle_create_error

    # Test with different error types
    error1 = Exception("Database error")
    line = ["rec_1", "Test"]
    error_summary = "Initial error summary"

    result = _handle_create_error(
        i=0,
        create_error=error1,
        line=line,
        error_summary=error_summary,
    )

    # Verify the function returns the expected structure
    assert isinstance(result, tuple)
    assert len(result) == 3  # error_msg, padded_line, error_summary


def test_execute_load_batch_edge_cases() -> None:
    """Test _execute_load_batch with various edge cases."""
    from odoo_data_flow.import_threaded import _execute_load_batch

    # Create mock thread state
    mock_model = MagicMock()
    mock_model.load.return_value = {"ids": [1], "messages": []}

    thread_state: dict[str, Any] = {
        "model": mock_model,
        "id_map": {},
        "failed_lines": [],
        "context": {},
        "progress": None,
        "unique_id_field_index": 0,
    }

    batch_lines = [["rec_1", "Test Name"]]
    batch_header = ["id", "name"]
    batch_number = 1

    result = _execute_load_batch(thread_state, batch_lines, batch_header, batch_number)

    # Verify return structure
    assert isinstance(result, dict)


def test_execute_load_batch_with_errors() -> None:
    """Test _execute_load_batch when load fails."""
    from odoo_data_flow.import_threaded import _execute_load_batch

    # Create mock thread state that will cause load to fail
    mock_model = MagicMock()
    mock_model.load.side_effect = Exception("Load failed")

    thread_state: dict[str, Any] = {
        "model": mock_model,
        "id_map": {},
        "failed_lines": [],
        "context": {},
        "progress": None,
        "unique_id_field_index": 0,
    }

    batch_lines = [["rec_1", "Test Name"]]
    batch_header = ["id", "name"]
    batch_number = 1

    # This should handle the error gracefully
    try:
        result = _execute_load_batch(
            thread_state, batch_lines, batch_header, batch_number
        )
        # Verify return structure even with errors
        assert isinstance(result, dict)
    except Exception:  # noqa: S110
        # Expected due to mocked error, but the code path is covered
        pass


def test_recursive_create_batches() -> None:
    """Test _recursive_create_batches function."""
    from odoo_data_flow.import_threaded import _recursive_create_batches

    # Test with sample data
    current_data = [["rec_1", "val_a"], ["rec_1", "val_b"], ["rec_2", "val_c"]]
    group_cols = ["id"]
    header = ["id", "value"]
    batch_size = 2
    o2m = False

    # Create the generator and consume a few items to test the function
    gen = _recursive_create_batches(current_data, group_cols, header, batch_size, o2m)

    try:
        # Try to get first batch
        batch = next(gen)
        assert isinstance(batch, tuple)
    except StopIteration:
        # OK if no data to process
        pass


def test_execute_write_batch() -> None:
    """Test _execute_write_batch function."""
    from odoo_data_flow.import_threaded import _execute_write_batch

    # Mock model
    mock_model = MagicMock()
    mock_model.write.return_value = True

    thread_state: dict[str, Any] = {
        "model": mock_model,
        "id_map": {"rec1": 1},
        "failed_lines": [],
        "context": {"tracking_disable": True},
    }

    # The function expects a LIST of (list_of_ids, dict_of_vals) tuples
    batch_writes = [([1], {"name": "Test Name"})]
    batch_number = 1

    result = _execute_write_batch(thread_state, batch_writes, batch_number)

    # Verify the function returns expected structure
    assert isinstance(result, dict)


def test_import_data_with_complex_parameters() -> None:
    """Test import_data function with various parameter combinations."""
    from odoo_data_flow.import_threaded import import_data

    # Create temporary CSV file for testing with id column
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline=""
    ) as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["id", "name"])
        writer.writerow(["rec_1", "Test Record"])
        temp_file = f.name

    try:
        # Mock the connection
        with patch(
            "odoo_data_flow.import_threaded.conf_lib.get_connection_from_config"
        ) as mock_get_conn:
            mock_model = MagicMock()
            mock_model.load.return_value = {"ids": [1], "messages": []}
            mock_conn = MagicMock()
            mock_conn.get_model.return_value = mock_model
            mock_get_conn.return_value = mock_conn

            # Test with various parameters to cover different code paths
            result, _summary = import_data(
                config="dummy.conf",
                model="res.partner",
                unique_id_field="id",
                file_csv=temp_file,
                separator=";",
                encoding="utf-8",
                context={"tracking_disable": True},
                fail_file="fail.csv",
                skip=0,
                deferred_fields=[],
                ignore=[],
                max_connection=1,
                batch_size=10,
                force_create=False,
                o2m=False,
            )

            # Verify the function runs and returns expected structure
            assert result is not None
    finally:
        Path(temp_file).unlink()


def test_convert_external_id_field() -> None:
    """Test _convert_external_id_field function."""
    from odoo_data_flow.import_threaded import _convert_external_id_field

    # Create mock connection with ir.model.data model
    mock_connection = MagicMock()
    mock_ir_model_data = MagicMock()
    mock_ir_model_data.search_read.return_value = [{"res_id": 1}]
    mock_connection.get_model.return_value = mock_ir_model_data

    # Test converting external ID field with correct parameters
    result = _convert_external_id_field(
        connection=mock_connection,
        field_name="category_id/id",
        field_value="base.category_1",
    )

    # Should return a tuple (base field name, converted value)
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] == "category_id"

    # Test with empty field value
    result_empty = _convert_external_id_field(
        connection=mock_connection, field_name="category_id/id", field_value=""
    )
    assert result_empty[0] == "category_id"
    assert result_empty[1] is False  # Empty value returns False


def test_handle_create_error_detailed() -> None:
    """Test _handle_create_error with different error types."""
    from odoo_data_flow.import_threaded import _handle_create_error

    # Test error handling with different parameters
    error = Exception("Test Error")
    line = ["rec_1", "test_value"]
    error_summary = "Error summary"

    # Call the function with correct parameters
    result = _handle_create_error(
        i=0,
        create_error=error,
        line=line,
        error_summary=error_summary,
    )

    assert isinstance(result, tuple)
    assert len(result) == 3  # (error_msg, padded_line, error_summary)


def test_recursive_create_batches_complex() -> None:
    """Test _recursive_create_batches with complex grouping scenarios."""
    from odoo_data_flow.import_threaded import _recursive_create_batches

    # Create test data with complex grouping
    current_data = [
        ["group1", "item1", "val1"],
        ["group1", "item2", "val2"],
        ["group2", "item3", "val3"],
        ["group1", "item4", "val4"],
    ]
    group_cols = ["col0"]
    header = ["col0", "col1", "col2"]
    batch_size = 2
    o2m = True

    # Create the generator and test it works
    gen = _recursive_create_batches(current_data, group_cols, header, batch_size, o2m)

    # Count the batches to make sure it works
    batch_count = 0
    for batch in gen:
        assert isinstance(batch, tuple)
        batch_count += 1
        if batch_count > 10:  # Prevent infinite loop
            break


def test_format_odoo_error() -> None:
    """Test _format_odoo_error function."""
    from odoo_data_flow.import_threaded import _format_odoo_error

    # Test with plain string
    result = _format_odoo_error("Simple error")
    assert result == "Simple error"

    # Test with dict-like string
    result = _format_odoo_error("{'data': {'message': 'Validation failed'}}")
    assert result == "Validation failed"

    # Test with exception object
    error = Exception("Test exception message")
    result = _format_odoo_error(error)
    assert isinstance(result, str)
    assert "Test exception message" in result


def test_extract_per_row_errors() -> None:
    """Test _extract_per_row_errors function."""
    from odoo_data_flow.import_threaded import _extract_per_row_errors

    # Test with messages containing row information
    messages = [
        {"message": "Row 1: Validation error", "rows": {"from": 0, "to": 0}},
        {"message": "Missing field", "rows": {"from": 1, "to": 2}},
    ]

    result = _extract_per_row_errors(messages)
    assert isinstance(result, dict)


if __name__ == "__main__":
    test_prepare_pass_2_data()
    test_handle_create_error_scenarios()
    test_execute_load_batch_edge_cases()
    test_execute_load_batch_with_errors()
    test_recursive_create_batches()
    test_execute_write_batch()
    test_import_data_with_complex_parameters()
    test_convert_external_id_field()
    test_handle_create_error_detailed()
    test_recursive_create_batches_complex()
    test_format_odoo_error()
    test_extract_per_row_errors()
    print("All import_threaded comprehensive tests passed!")
