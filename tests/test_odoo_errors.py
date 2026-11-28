"""Tests for the shared Odoo RPC error-message extractor."""

from __future__ import annotations

from fluvo.lib.odoo_errors import format_odoo_error


def test_plain_string_is_returned_trimmed() -> None:
    """A non-fault string comes back as itself, newlines flattened."""
    assert format_odoo_error("Some error message") == "Some error message"
    assert format_odoo_error("first\nsecond") == "first second"


def test_stringified_fault_uses_data_message() -> None:
    """data.message is preferred over the top-level message."""
    fault = {
        "code": 200,
        "message": "Odoo Server Error",
        "data": {"message": "Field 'name' is required", "debug": "Traceback ..."},
    }
    assert format_odoo_error(str(fault)) == "Field 'name' is required"


def test_data_arguments_fallback() -> None:
    """data.arguments[0] is used when data.message is absent."""
    fault = {"data": {"arguments": ["You cannot delete this record."]}}
    assert format_odoo_error(str(fault)) == "You cannot delete this record."


def test_top_level_message_fallback() -> None:
    """The top-level message is used when there is no data block."""
    assert format_odoo_error(str({"message": "Access Denied"})) == "Access Denied"


def test_exception_carrying_fault_dict_as_first_arg() -> None:
    """A fault dict passed as the exception's first arg is read directly."""
    err = Exception({"data": {"message": "Record does not exist."}})
    assert format_odoo_error(err) == "Record does not exist."


def test_exception_without_fault_falls_back_to_text() -> None:
    """A plain exception falls back to its own text."""
    assert format_odoo_error(Exception("boom")) == "boom"


def test_non_fault_dict_string_is_returned_as_text() -> None:
    """A dict string with no message fields is returned as its text."""
    assert format_odoo_error(str({"key": "value"})) == "{'key': 'value'}"
