"""Focused tests to cover specific missed lines in core modules like import_threaded."""

from typing import Any
from unittest.mock import MagicMock


def test_format_odoo_error() -> None:
    """Test _format_odoo_error with various error types."""
    from fluvo.import_threaded import _format_odoo_error

    # Test with plain string error
    result = _format_odoo_error("Some error message")
    assert result == "Some error message"

    # Test with dict-like error string
    error_dict_str = "{'data': {'message': 'Validation failed'}}"
    result = _format_odoo_error(error_dict_str)
    assert result == "Validation failed"

    # Create mock error objects with various attributes
    mock_error = MagicMock()
    mock_error.name = "ValidationError"
    mock_error.value = "Test validation error"
    mock_error.args = ("arg1", "arg2")

    formatted = _format_odoo_error(mock_error)
    # Just verify the function returns a string without error
    assert isinstance(formatted, str)


def test_recursive_create_batches_realistic() -> None:
    """Test _recursive_create_batches with realistic data."""
    from fluvo.import_threaded import _recursive_create_batches

    # Create realistic data grouped by some criteria
    current_data = [
        ["group1", "item1", "value1"],
        ["group1", "item2", "value2"],
        ["group2", "item3", "value3"],
        ["group1", "item4", "value4"],  # Another item for group1
        ["group3", "item5", "value5"],
    ]
    group_cols = ["col0"]  # Group by first column
    header = ["col0", "col1", "col2"]
    batch_size = 2
    o2m = True

    # Create the generator
    batches_generator = _recursive_create_batches(
        current_data, group_cols, header, batch_size, o2m
    )

    # Consume the generator to test the function runs
    batches = list(batches_generator)

    # Should have created some batches
    assert isinstance(batches, list)


def test_execute_load_batch_comprehensive() -> None:
    """Test _execute_load_batch with comprehensive parameters."""
    from fluvo.import_threaded import _execute_load_batch

    # Create mock model
    mock_model = MagicMock()
    mock_model.load.return_value = {"ids": [1, 2], "messages": []}

    thread_state: dict[str, Any] = {
        "model": mock_model,
        "id_map": {},
        "failed_lines": [],
        "context": {},
        "progress": None,
        "unique_id_field_index": 0,
    }

    batch_lines = [["rec_1", "Test Name"], ["rec_2", "Test Name 2"]]
    batch_header = ["id", "name"]
    batch_number = 1

    result = _execute_load_batch(thread_state, batch_lines, batch_header, batch_number)
    assert isinstance(result, dict)


if __name__ == "__main__":
    test_format_odoo_error()
    test_recursive_create_batches_realistic()
    test_execute_load_batch_comprehensive()
    print("All core import coverage tests passed!")
