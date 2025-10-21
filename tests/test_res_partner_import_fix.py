"""Unit tests for res_partner specific import fixes."""

import pytest

from odoo_data_flow.import_threaded import _safe_convert_field_value


def test_safe_convert_field_value_res_partner_fields() -> None:
    """Test that res_partner specific fields are properly handled to prevent.

    tuple index errors.
    """
    # Test parent_id field with text value
    # (should convert to 0 to prevent errors)
    result = _safe_convert_field_value("parent_id", "RES_PARTNER.invalid", "many2one")
    assert (
        result == 0
    )  # Should convert invalid external ID to 0 to prevent tuple index errors

    # Test parent_id field with empty value
    # (should convert to False for relational fields)
    result = _safe_convert_field_value("parent_id", "", "many2one")
    assert result is False
    # Should convert empty value to False for relational fields

    # Test company_id field with text value
    # (should convert to 0 to prevent errors)
    result = _safe_convert_field_value("company_id", "invalid_text", "many2one")
    assert result == 0
    # Should convert invalid text to 0 to prevent tuple index errors

    # Test country_id field with text value
    # (should convert to 0 to prevent errors)
    result = _safe_convert_field_value("country_id", "invalid_country", "many2one")
    assert result == 0
    # Should convert invalid text to 0 to prevent tuple index errors

    # Test state_id field with text value
    # (should convert to 0 to prevent errors)
    result = _safe_convert_field_value("state_id", "invalid_state", "many2one")
    assert result == 0  # Should convert invalid text to 0 to prevent tuple index errors


def test_safe_convert_field_value_numeric_fields_with_text() -> None:
    """Test that numeric fields with text values are properly handled."""
    # Test integer field with text value
    result = _safe_convert_field_value("credit_limit", "invalid_text", "integer")
    assert result == 0  # Should convert invalid text to 0 for integer field

    # Test float field with text value
    result = _safe_convert_field_value("vat_check_date", "not_a_number", "float")
    assert (
        result == "not_a_number"
    )  # Should preserve original value for server validation

    # Test positive field with text value
    result = _safe_convert_field_value("positive_field", "bad_value", "positive")
    assert result == 0  # Should convert invalid text to 0 for positive field

    # Test negative field with text value
    result = _safe_convert_field_value("negative_field", "invalid_input", "negative")
    assert result == 0  # Should convert invalid text to 0 for negative field


def test_safe_convert_field_value_empty_values() -> None:
    """Test that empty values are properly handled for different field types."""
    # Test integer field with empty string
    result = _safe_convert_field_value("credit_limit", "", "integer")
    assert result == 0  # Should convert empty string to 0 for integer field

    # Test float field with empty string
    result = _safe_convert_field_value("vat_check_date", "", "float")
    assert result == 0.0  # Should convert empty string to 0.0 for float field

    # Test positive field with empty string
    result = _safe_convert_field_value("positive_field", "", "positive")
    assert result == 0  # Should convert empty string to 0 for positive field

    # Test negative field with empty string
    result = _safe_convert_field_value("negative_field", "", "negative")
    assert result == 0  # Should convert empty string to 0 for negative field

    # Test boolean field with empty string
    result = _safe_convert_field_value("active", "", "boolean")
    assert result is False  # Should convert empty string to False for boolean field

    # Test many2one field with empty string (relational fields)
    result = _safe_convert_field_value("parent_id", "", "many2one")
    assert result is False  # Should convert empty string to False for many2one field

    # Test many2many field with empty string (relational fields)
    result = _safe_convert_field_value("category_id", "", "many2many")
    assert result is False  # Should convert empty string to False for many2many field

    # Test one2many field with empty string (relational fields)
    result = _safe_convert_field_value("child_ids", "", "one2many")
    assert result is False  # Should convert empty string to False for one2many field


def test_safe_convert_field_value_valid_values() -> None:
    """Test that valid values are properly converted."""
    # Test integer field with valid string
    result = _safe_convert_field_value("credit_limit", "123", "integer")
    assert result == 123
    assert isinstance(result, int)

    # Test float field with valid string
    result = _safe_convert_field_value("vat_check_date", "123.45", "float")
    assert result == 123.45
    assert isinstance(result, float)

    # Test integer field with float string that's actually an integer
    result = _safe_convert_field_value("credit_limit", "123.0", "integer")
    assert result == 123
    assert isinstance(result, int)

    # Test negative integer
    result = _safe_convert_field_value("discount_limit", "-456", "integer")
    assert result == -456
    assert isinstance(result, int)


def test_safe_convert_field_value_external_id_fields() -> None:
    """Test that external ID fields remain as strings."""
    # External ID fields should remain as strings regardless of content
    result = _safe_convert_field_value("parent_id/id", "RES_PARTNER.12345", "many2one")
    assert result == "RES_PARTNER.12345"
    assert isinstance(result, str)

    # Even with numeric values, external ID fields should remain as strings
    result = _safe_convert_field_value("category_id/id", "12345", "many2many")
    assert result == "12345"
    assert isinstance(result, str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
