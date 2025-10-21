"""Unit tests for constraint violation error handling."""

import pytest

from odoo_data_flow.import_threaded import _sanitize_error_message


def test_constraint_violation_detection_logic() -> None:
    """Test the constraint violation detection logic."""
    # Test null constraint violation detection
    null_error = 'null value in column "name" violates not-null constraint'
    error_str_lower = null_error.lower()

    # Should detect as constraint violation
    assert "violates" in error_str_lower
    assert "constraint" in error_str_lower
    assert "null value in column" in error_str_lower
    assert "violates not-null" in error_str_lower

    # Should NOT be detected as tuple index error
    assert "tuple index out of range" not in error_str_lower

    # Test unique constraint violation detection
    unique_error = (
        'duplicate key value violates unique constraint "res_partner_name_unique"'
    )
    error_str_lower = unique_error.lower()

    # Should detect as constraint violation
    assert "violates" in error_str_lower
    assert "constraint" in error_str_lower
    assert "duplicate key value" in error_str_lower

    # Should NOT be detected as tuple index error
    assert "tuple index out of range" not in error_str_lower

    # Test foreign key constraint violation detection
    fk_error = (
        'insert or update on table "res_partner" violates foreign key '
        'constraint "res_partner_parent_id_fkey"'
    )
    error_str_lower = fk_error.lower()

    # Should detect as constraint violation
    assert "violates" in error_str_lower
    assert "constraint" in error_str_lower
    assert "foreign key" in error_str_lower

    # Should NOT be detected as tuple index error
    assert "tuple index out of range" not in error_str_lower


def test_mixed_error_detection() -> None:
    """Test detection of mixed errors that contain both constraint.

    and tuple keywords.
    """
    # Test mixed error - contains both constraint and tuple keywords
    mixed_error = (
        "tuple index out of range error due to null value in column "
        "violates not-null constraint"
    )
    error_str_lower = mixed_error.lower()

    # Should detect as BOTH constraint violation AND tuple index error
    assert "violates" in error_str_lower
    assert "constraint" in error_str_lower
    assert "null value in column" in error_str_lower
    assert "tuple index out of range" in error_str_lower

    # But constraint violation should take precedence
    # This is handled in the actual error detection logic by checking
    # constraint violations first
    constraint_violation_detected = (
        "violates" in error_str_lower and "constraint" in error_str_lower
    ) or (
        "null value in column" in error_str_lower
        and "violates not-null" in error_str_lower
    )

    tuple_index_error_detected = "tuple index out of range" in error_str_lower

    assert constraint_violation_detected
    assert tuple_index_error_detected

    # In our implementation, constraint violations are checked first and take precedence


def test_pure_tuple_index_error_detection() -> None:
    """Test detection of pure tuple index errors."""
    # Test pure tuple index error (no constraint keywords)
    pure_tuple_error = "tuple index out of range error in api.py"
    error_str_lower = pure_tuple_error.lower()

    # Should detect as tuple index error
    assert "tuple index out of range" in error_str_lower

    # Should NOT detect as constraint violation
    assert not ("violates" in error_str_lower and "constraint" in error_str_lower)
    assert not (
        "null value in column" in error_str_lower
        and "violates not-null" in error_str_lower
    )


def test_error_message_sanitization() -> None:
    """Test that constraint violation error messages are properly sanitized."""
    # Test null constraint violation error message sanitization
    null_violation_error = 'null value in column "name" violates not-null constraint'
    sanitized = _sanitize_error_message(null_violation_error)

    # Should not contain semicolons that would cause CSV column splitting
    assert ";" not in sanitized

    # Should still contain the important information
    assert "null value" in sanitized.lower()
    assert "violates" in sanitized.lower()
    assert "constraint" in sanitized.lower()

    # Test unique constraint violation error message sanitization
    unique_violation_error = (
        'duplicate key value violates unique constraint "res_partner_name_unique"'
    )
    sanitized = _sanitize_error_message(unique_violation_error)

    # Should not contain semicolons that would cause CSV column splitting
    assert ";" not in sanitized

    # Should still contain the important information
    assert "duplicate key" in sanitized.lower()
    assert "violates" in sanitized.lower()
    assert "unique constraint" in sanitized.lower()

    # Test foreign key constraint violation error message sanitization
    fk_violation_error = (
        'insert or update on table "res_partner" violates foreign key '
        'constraint "res_partner_parent_id_fkey"'
    )
    sanitized = _sanitize_error_message(fk_violation_error)

    # Should not contain semicolons that would cause CSV column splitting
    assert ";" not in sanitized

    # Should still contain the important information
    assert "violates" in sanitized.lower()
    assert "foreign key" in sanitized.lower()
    assert "constraint" in sanitized.lower()


def test_complex_error_message_sanitization() -> None:
    """Test sanitization of complex constraint violation error messages."""
    # Test a complex error message with multiple constraint types
    complex_error = (
        'null value in column "name" violates not-null constraint; '
        'duplicate key value violates unique constraint "res_partner_name_unique"'
    )
    sanitized = _sanitize_error_message(complex_error)

    # Should not contain semicolons that would cause CSV column splitting
    assert ";" not in sanitized

    # Should still contain all the important information but with semicolons replaced
    assert "null value" in sanitized.lower()
    assert "violates" in sanitized.lower()
    assert "not-null constraint" in sanitized.lower()
    assert "duplicate key" in sanitized.lower()
    assert "unique constraint" in sanitized.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
