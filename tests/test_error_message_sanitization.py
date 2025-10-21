"""Unit tests for error message sanitization fix."""

from odoo_data_flow.import_threaded import _sanitize_error_message


def test_sanitize_error_message_with_semicolons() -> None:
    """Test that semicolons in error messages are properly handled."""
    error_with_semicolons = "Error message with semicolon; and more text; and more"
    sanitized = _sanitize_error_message(error_with_semicolons)

    # Semicolons should be replaced with colons to prevent CSV column splitting
    assert ";" not in sanitized
    assert ":" in sanitized

    # Should still contain the important information
    assert "Error message with semicolon" in sanitized
    assert "and more text" in sanitized
    assert "and more" in sanitized


def test_sanitize_error_message_with_newlines() -> None:
    """Test that newlines in error messages are replaced."""
    error_with_newlines = "Error message\nwith newlines\rand carriage returns"
    sanitized = _sanitize_error_message(error_with_newlines)

    # Newlines should be replaced with " | "
    assert "\n" not in sanitized
    assert "\r" not in sanitized
    assert " | " in sanitized
    expected = "Error message | with newlines | and carriage returns"
    assert sanitized == expected


def test_sanitize_error_message_with_quotes() -> None:
    """Test that quotes in error messages are properly escaped."""
    error_with_quotes = 'Error "message" with "quotes" inside'
    sanitized = _sanitize_error_message(error_with_quotes)

    # Double quotes should be escaped by doubling them (CSV standard)
    assert '""' in sanitized
    expected = 'Error ""message"" with ""quotes"" inside'
    assert sanitized == expected


def test_sanitize_error_message_with_tabs() -> None:
    """Test that tabs in error messages are replaced with spaces."""
    error_with_tabs = "Error\tmessage\twith\ttabs"
    sanitized = _sanitize_error_message(error_with_tabs)

    # Tabs should be replaced with spaces
    assert "\t" not in sanitized
    assert " " in sanitized
    expected = "Error message with tabs"
    assert sanitized == expected


def test_sanitize_error_message_none() -> None:
    """Test that None input is handled properly."""
    sanitized = _sanitize_error_message(None)
    assert sanitized == ""


def test_sanitize_error_message_empty() -> None:
    """Test that empty string input is handled properly."""
    sanitized = _sanitize_error_message("")
    assert sanitized == ""


def test_sanitize_complex_error_message() -> None:
    """Test sanitization of a complex error message with multiple issues."""
    complex_error = 'Error: value {"key": "value; with semicolon",\n"other": "data\r\ntab\tcharacter"}'
    sanitized = _sanitize_error_message(complex_error)

    # Should contain no newlines, no carriage returns, properly escaped quotes
    assert "\n" not in sanitized
    assert "\r" not in sanitized
    assert "\t" not in sanitized
    assert " | " in sanitized  # newlines replaced
    assert '""' in sanitized  # quotes escaped
