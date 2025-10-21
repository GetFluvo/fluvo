"""Unit tests for the empty language code fix."""

import csv
import tempfile

from odoo_data_flow.lib.preflight import _get_required_languages


def test_get_required_languages_filters_empty_strings() -> None:
    """Test that _get_required_languages filters out empty strings.

    from the 'lang' column.
    """
    # Create a temporary CSV file with empty language codes
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "lang"])  # Header
        writer.writerow(["1", "Partner 1", "en_US"])  # Valid language
        writer.writerow(["2", "Partner 2", ""])  # Empty language code
        writer.writerow(["3", "Partner 3", "de_DE"])  # Valid language
        writer.writerow(["4", "Partner 4", ""])  # Empty language code
        writer.writerow(["5", "Partner 5", "fr_FR"])  # Valid language
        temp_file = f.name

    # Call the function
    result = _get_required_languages(temp_file, ",")

    # Verify that empty strings are filtered out
    assert result is not None
    assert len(result) == 3  # Should only have 3 non-empty language codes
    assert "en_US" in result
    assert "de_DE" in result
    assert "fr_FR" in result
    # Empty strings should not be in the result
    assert "" not in result


def test_get_required_languages_all_empty() -> None:
    """Test that _get_required_languages returns None when all.

    language codes are empty.
    """
    # Create a temporary CSV file with only empty language codes
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "lang"])  # Header
        writer.writerow(["1", "Partner 1", ""])  # Empty language code
        writer.writerow(["2", "Partner 2", ""])  # Empty language code
        writer.writerow(["3", "Partner 3", ""])  # Empty language code
        temp_file = f.name

    # Call the function
    result = _get_required_languages(temp_file, ",")

    # Verify that None is returned when all are empty
    assert result is None


def test_get_required_languages_with_whitespace() -> None:
    """Test that _get_required_languages filters out whitespace-only language codes."""
    # Create a temporary CSV file with whitespace-only language codes
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "lang"])  # Header
        writer.writerow(["1", "Partner 1", "en_US"])  # Valid language
        writer.writerow(["2", "Partner 2", "  "])  # Whitespace-only
        writer.writerow(["3", "Partner 3", "\t"])  # Tab-only
        writer.writerow(["4", "Partner 4", "\n"])  # Newline-only
        writer.writerow(["5", "Partner 5", "de_DE"])  # Valid language
        temp_file = f.name

    # Call the function
    result = _get_required_languages(temp_file, ",")

    # Verify that whitespace-only strings are filtered out
    assert result is not None
    assert (
        len(result) == 2
    )  # Should only have 2 non-empty, non-whitespace language codes
    assert "en_US" in result
    assert "de_DE" in result
    # Whitespace-only strings should not be in the result
    assert "  " not in result
    assert "\t" not in result
    assert "\n" not in result
