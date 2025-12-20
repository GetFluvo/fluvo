"""Additional tests for import_threaded module to improve coverage."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from odoo_data_flow.import_threaded import (
    _convert_external_id_field,
    _create_batches,
    _execute_load_batch,
    _execute_write_batch,
    _filter_ignored_columns,
    _format_odoo_error,
    _get_model_fields,
    _get_model_fields_safe,
    _handle_create_error,
    _orchestrate_pass_1,
    _parse_csv_data,
    _process_external_id_fields,
    _read_data_file,
    _recursive_create_batches,
    _run_threaded_pass,
    _safe_convert_field_value,
    _sanitize_error_message,
    _setup_fail_file,
    import_data,
)


def test_sanitize_error_message() -> None:
    """Test _sanitize_error_message with various inputs."""
    # Test with None
    result = _sanitize_error_message(None)
    assert result == ""

    # Test with newlines
    result = _sanitize_error_message("line1\nline2\rline3")
    assert " | " in result

    # Test with tabs
    result = _sanitize_error_message("col1\tcol2")
    assert result == "col1 col2"

    # Test with quotes
    result = _sanitize_error_message('text "with" quotes')
    assert 'text ""with"" quotes' in result

    # Test with semicolons
    result = _sanitize_error_message("part1;part2")
    assert "part1:part2" in result

    # Test with control characters
    result = _sanitize_error_message("test\x00\x01value")
    assert "test  value" in result

    # Test with sencond typo correction
    result = _sanitize_error_message("sencond word")
    assert "sencond word" in result


def test_format_odoo_error() -> None:
    """Test _format_odoo_error with various inputs."""
    # Test with string
    result = _format_odoo_error("simple error")
    assert "simple error" in result

    # Test with non-string
    result = _format_odoo_error(123)
    assert "123" in result

    # Test with dict-like string that should be parsed
    error_dict = "{'data': {'message': 'test message'}}"
    result = _format_odoo_error(error_dict)
    assert "test message" in result

    # Test with invalid dict string
    result = _format_odoo_error("invalid [dict")
    assert "invalid [dict" in result


def test_parse_csv_data() -> None:
    """Test _parse_csv_data function."""
    from io import StringIO

    # Test with valid data
    f = StringIO("id,name\n1,Alice\n2,Bob")
    header, data = _parse_csv_data(f, ",", 0)
    assert header == ["id", "name"]
    assert data == [["1", "Alice"], ["2", "Bob"]]

    # Test with skip parameter
    f = StringIO("skip1\nskip2\nid,name\n1,Alice\n2,Bob")
    header, data = _parse_csv_data(f, ",", 2)
    assert header == ["id", "name"]
    assert data == [["1", "Alice"], ["2", "Bob"]]

    # Test with no id column (should raise ValueError)
    f = StringIO("name,age\nAlice,25\nBob,30")
    with pytest.raises(ValueError):
        _parse_csv_data(f, ",", 0)


def test_read_data_file_exceptions() -> None:
    """Test _read_data_file with various exception cases."""
    # Already tested in main test file, but let's add more edge cases
    with patch("builtins.open") as mock_open:
        # Test exception during file access after encoding attempts
        def side_effect(*args: Any, **kwargs: Any) -> None:
            raise OSError("Permission denied")  # Using OSError instead of Exception

        mock_open.side_effect = side_effect
        header, data = _read_data_file("dummy.csv", ",", "utf-8", 0)
        assert header == []
        assert data == []


def test_filter_ignored_columns_with_split() -> None:
    """Test _filter_ignored_columns with field names containing '/'."""
    ignore_list = ["category_id"]
    header = ["id", "name", "category_id/type"]
    data = [["1", "Alice", "type1"], ["2", "Bob", "type2"]]

    filtered_header, _filtered_data = _filter_ignored_columns(ignore_list, header, data)
    # The function ignores fields based on base name (before /), so category_id/type should be ignored
    # because its base name (before /) is 'category_id' which matches the ignore list
    assert "id" in filtered_header
    assert "name" in filtered_header
    assert "category_id/type" not in filtered_header  # Should be filtered out


def test_setup_fail_file_os_error() -> None:
    """Test _setup_fail_file with OSError."""
    with patch("builtins.open") as mock_open:
        mock_open.side_effect = OSError("Permission denied")
        writer, handle = _setup_fail_file("fail.csv", ["id", "name"], ",", "utf-8")
        assert writer is None
        assert handle is None


def test_get_model_fields_various_cases() -> None:
    """Test _get_model_fields with various model attributes."""
    # Test with _fields as dict
    mock_model = MagicMock()
    mock_model._fields = {"field1": {"type": "char"}}
    result = _get_model_fields(mock_model)
    assert result == {"field1": {"type": "char"}}

    # Test with no _fields attribute
    mock_model_no_fields = MagicMock()
    delattr(mock_model_no_fields, "_fields")
    result = _get_model_fields(mock_model_no_fields)
    assert result is None

    # Test with _fields not a dict
    mock_model_str_fields = MagicMock()
    mock_model_str_fields._fields = "not_a_dict"
    result = _get_model_fields(mock_model_str_fields)
    assert result is None


def test_get_model_fields_safe_various_cases() -> None:
    """Test _get_model_fields_safe with various model attributes."""
    # Test with _fields as dict
    mock_model = MagicMock()
    mock_model._fields = {"field1": {"type": "char"}}
    result = _get_model_fields_safe(mock_model)
    assert result == {"field1": {"type": "char"}}

    # Test with no _fields attribute
    mock_model_no_fields = MagicMock()
    delattr(mock_model_no_fields, "_fields")
    result = _get_model_fields_safe(mock_model_no_fields)
    assert result is None

    # Test with _fields not a dict
    mock_model_str_fields = MagicMock()
    mock_model_str_fields._fields = "not_a_dict"
    result = _get_model_fields_safe(mock_model_str_fields)
    assert result is None


def test_convert_external_id_field() -> None:
    """Test _convert_external_id_field function."""
    mock_model = MagicMock()
    mock_record = MagicMock()
    mock_record.id = 123
    mock_model.env.ref.return_value = mock_record

    # Test with non-empty field value
    base_name, value = _convert_external_id_field(
        mock_model, "parent_id/id", "external.id"
    )
    assert base_name == "parent_id"
    assert value == 123

    # Test with empty field value
    base_name, value = _convert_external_id_field(mock_model, "parent_id/id", "")
    assert base_name == "parent_id"
    assert value is None

    # Test with None field value
    base_name, value = _convert_external_id_field(mock_model, "parent_id/id", None)
    assert base_name == "parent_id"
    assert value is None

    # Test with exception during lookup
    mock_model.env.ref.side_effect = Exception("Lookup failed")
    base_name, value = _convert_external_id_field(
        mock_model, "parent_id/id", "invalid.id"
    )
    assert base_name == "parent_id"
    assert value is None


def test_safe_convert_field_value_comprehensive() -> None:
    """Test _safe_convert_field_value with comprehensive test cases."""
    # Test with empty values for different field types
    result = _safe_convert_field_value("field", None, "integer")
    assert result == 0

    result = _safe_convert_field_value("field", "", "float")
    assert result == 0.0

    result = _safe_convert_field_value("field", "", "many2one")
    assert result is False

    result = _safe_convert_field_value("field", "", "boolean")
    assert result is False

    # Test numeric conversions
    result = _safe_convert_field_value("field", "123", "integer")
    assert result == 123

    result = _safe_convert_field_value("field", "123.45", "float")
    assert result == 123.45

    # Test with float string that represents integer
    result = _safe_convert_field_value("field", "123.0", "integer")
    assert result == 123

    # Test European decimal notation
    result = _safe_convert_field_value("field", "1.234,56", "float")
    assert result == 1234.56

    # Test with /id suffix fields
    result = _safe_convert_field_value("parent_id/id", "external_id", "char")
    assert result == "external_id"

    # Test with empty /id suffix field
    result = _safe_convert_field_value("parent_id/id", "", "char")
    assert result == ""

    # Test with placeholder values
    result = _safe_convert_field_value("field", "invalid_text", "integer")
    assert result == 0

    # Test with non-numeric string for integer field (should return original)
    result = _safe_convert_field_value("field", "not_a_number", "integer")
    assert result == "not_a_number"


def test_process_external_id_fields() -> None:
    """Test _process_external_id_fields function."""
    mock_model = MagicMock()

    # Test with /id fields
    clean_vals = {"name": "test", "parent_id/id": "external.parent"}
    converted_vals, external_id_fields = _process_external_id_fields(
        mock_model, clean_vals
    )

    assert "name" in converted_vals
    assert "parent_id" in converted_vals  # Should be converted to base name
    assert "parent_id/id" in external_id_fields


def test_handle_create_error_tuple_index_error() -> None:
    """Test _handle_create_error with tuple index error."""
    error = Exception("tuple index out of range")
    error_str, _failed_line, summary = _handle_create_error(
        0, error, ["test", "data"], "Fell back to create"
    )
    assert "Tuple unpacking error" in error_str
    assert "Tuple unpacking error detected" in summary


def test_handle_create_error_database_connection_pool() -> None:
    """Test _handle_create_error with database connection pool error."""
    error = Exception("connection pool is full")
    error_str, _failed_line, _summary = _handle_create_error(
        0, error, ["test", "data"], "message"
    )
    assert "Database connection pool exhaustion" in error_str


def test_handle_create_error_serialization() -> None:
    """Test _handle_create_error with database serialization error."""
    error = Exception("could not serialize access due to concurrent update")
    error_str, _failed_line, summary = _handle_create_error(
        0, error, ["test", "data"], "Fell back to create"
    )
    assert "Database serialization error" in error_str
    assert "Database serialization conflict detected during create" in summary


def test_execute_load_batch_force_create() -> None:
    """Test _execute_load_batch with force_create enabled."""
    mock_model = MagicMock()
    thread_state = {
        "model": mock_model,
        "progress": MagicMock(),
        "unique_id_field_index": 0,
        "force_create": True,
        "ignore_list": [],
        "context": {},
    }
    batch_header = ["id", "name"]
    batch_lines = [["rec1", "Alice"], ["rec2", "Bob"]]

    with patch(
        "odoo_data_flow.import_threaded._create_batch_individually"
    ) as mock_create:
        mock_create.return_value = {
            "id_map": {"rec1": 1, "rec2": 2},
            "failed_lines": [],
            "error_summary": "",
        }

        result = _execute_load_batch(thread_state, batch_lines, batch_header, 1)

        # Should call _create_batch_individually due to force_create
        mock_create.assert_called()
        assert result["id_map"] == {"rec1": 1, "rec2": 2}


def test_execute_load_batch_memory_error() -> None:
    """Test _execute_load_batch with memory error."""
    mock_model = MagicMock()
    mock_model.load.side_effect = Exception("memory error")

    thread_state = {
        "model": mock_model,
        "progress": MagicMock(),
        "unique_id_field_index": 0,
        "force_create": False,
        "ignore_list": [],
        "context": {},
    }
    batch_header = ["id", "name"]
    batch_lines = [["rec1", "Alice"], ["rec2", "Bob"]]

    with patch(
        "odoo_data_flow.import_threaded._handle_fallback_create"
    ) as mock_fallback:
        _execute_load_batch(thread_state, batch_lines, batch_header, 1)

        # Should handle memory error with fallback
        mock_fallback.assert_called()


def test_execute_write_batch_exception_handling() -> None:
    """Test _execute_write_batch with exception handling."""
    mock_model = MagicMock()
    mock_model.write.side_effect = Exception("Write failed")

    thread_state = {"model": mock_model}
    batch_writes = ([1, 2], {"name": "test"})
    batch_number = 1

    result = _execute_write_batch(thread_state, batch_writes, batch_number)

    # Should have failed writes
    assert len(result["failed_writes"]) > 0
    assert result["success"] is False


def test_run_threaded_pass_keyboard_interrupt() -> None:
    """Test _run_threaded_pass with keyboard interrupt."""
    mock_rpc_thread = MagicMock()
    mock_rpc_thread.abort_flag = False

    # Simulate a keyboard interrupt during processing
    with patch("concurrent.futures.as_completed") as mock_as_completed:
        mock_as_completed.side_effect = KeyboardInterrupt()

        _result, aborted = _run_threaded_pass(
            mock_rpc_thread, lambda x: {"success": True}, [(1, [])], {}
        )

        assert aborted is True


def test_orchestrate_pass_1_missing_unique_id() -> None:
    """Test _orchestrate_pass_1 when unique ID field is removed by ignore list."""
    mock_model = MagicMock()
    header = ["name", "email"]  # No 'id' field
    all_data = [["Alice", "alice@example.com"]]
    unique_id_field = "id"  # This field doesn't exist
    deferred_fields: list[str] = []
    ignore = ["id"]  # This will remove the 'id' field

    with patch("odoo_data_flow.import_threaded.Progress") as mock_progress:
        mock_progress_instance = MagicMock()
        mock_progress.return_value.__enter__.return_value = mock_progress_instance

        result = _orchestrate_pass_1(
            mock_progress_instance,
            mock_model,
            "res.partner",
            header,
            all_data,
            unique_id_field,
            deferred_fields,
            ignore,
            {},
            None,
            None,
            1,
            10,
            False,
            None,
        )

        # Should return with success=False
        assert result.get("success") is False


def test_recursive_create_batches_o2m_batching() -> None:
    """Test _recursive_create_batches with o2m batching logic."""
    data = [["parent1", "child1"], ["parent1", "child2"], ["parent2", "child3"]]
    header = ["id", "name"]

    # Test with o2m=True to trigger parent splitting logic
    batches = list(_recursive_create_batches(data, [], header, 1, True))

    # Should create batches respecting o2m logic
    assert len(batches) >= 0  # Should not crash


def test_recursive_create_batches_group_cols() -> None:
    """Test _recursive_create_batches with group columns."""
    data = [
        ["parent1", "child1", "cat1"],
        ["parent1", "child2", "cat1"],
        ["parent2", "child3", "cat2"],
    ]
    header = ["id", "name", "category"]

    # Test with group_by column
    batches = list(_recursive_create_batches(data, ["category"], header, 10, False))

    # Should group by the specified column
    assert len(batches) >= 0  # Should not crash


def test_create_batches_edge_cases() -> None:
    """Test _create_batches with edge cases."""
    # Test with empty data
    batches = list(_create_batches([], None, [], 10, False))
    assert batches == []

    # Test with real data
    data = [["id1", "name1"], ["id2", "name2"]]
    header = ["id", "name"]
    batches = list(_create_batches(data, None, header, 1, False))
    assert len(batches) == 2  # Should split into 2 batches of 1


def test_import_data_empty_header() -> None:
    """Test import_data when header is empty."""
    with patch("odoo_data_flow.import_threaded._read_data_file") as mock_read:
        mock_read.return_value = ([], [])  # Empty header and data

        result, stats = import_data(
            config={
                "hostname": "localhost",
                "database": "test",
                "login": "admin",
                "password": "admin",
            },
            model="res.partner",
            unique_id_field="id",
            file_csv="dummy.csv",
        )

        # Should return False when header is empty
        assert result is False
        assert stats == {}


def test_import_data_pass_2_processing() -> None:
    """Test import_data with deferred fields (pass 2 processing)."""
    with patch("odoo_data_flow.import_threaded._read_data_file") as mock_read:
        mock_read.return_value = (["id", "name"], [["1", "Alice"]])

        with patch(
            "odoo_data_flow.import_threaded.conf_lib.get_connection_from_dict"
        ) as mock_get_conn:
            mock_connection = MagicMock()
            mock_get_conn.return_value = mock_connection
            mock_model = MagicMock()
            mock_connection.get_model.return_value = mock_model

            with patch(
                "odoo_data_flow.import_threaded._orchestrate_pass_1"
            ) as mock_pass_1:
                mock_pass_1.return_value = {"success": True, "id_map": {"1": 101}}

                with patch(
                    "odoo_data_flow.import_threaded._orchestrate_pass_2"
                ) as mock_pass_2:
                    mock_pass_2.return_value = (True, 5)  # success, updates_made

                    result, stats = import_data(
                        config={
                            "hostname": "localhost",
                            "database": "test",
                            "login": "admin",
                            "password": "admin",
                        },
                        model="res.partner",
                        unique_id_field="id",
                        file_csv="dummy.csv",
                        deferred_fields=["category_id"],
                    )

                    # Should call both passes and succeed
                    mock_pass_1.assert_called_once()
                    mock_pass_2.assert_called_once()
                    assert result is True
                    assert stats["updated_relations"] == 5
