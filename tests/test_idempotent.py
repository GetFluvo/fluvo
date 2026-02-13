"""Tests for the idempotent import module."""

from typing import Any
from unittest.mock import MagicMock

from odoo_data_flow.lib import idempotent


class TestNormalizeValue:
    """Tests for normalize_value function."""

    def test_normalize_false(self) -> None:
        """Test that False becomes None."""
        assert idempotent.normalize_value(False) is None

    def test_normalize_none(self) -> None:
        """Test that None stays None."""
        assert idempotent.normalize_value(None) is None

    def test_normalize_empty_string(self) -> None:
        """Test that empty string becomes None."""
        assert idempotent.normalize_value("") is None
        assert idempotent.normalize_value("   ") is None

    def test_normalize_string(self) -> None:
        """Test that strings are stripped."""
        assert idempotent.normalize_value("  hello  ") == "hello"

    def test_normalize_m2o_tuple(self) -> None:
        """Test that many2one tuples return just the ID."""
        assert idempotent.normalize_value((5, "Partner Name")) == 5
        assert idempotent.normalize_value([5, "Partner Name"]) == 5

    def test_normalize_empty_list(self) -> None:
        """Test that empty list becomes None."""
        assert idempotent.normalize_value([]) is None

    def test_normalize_number(self) -> None:
        """Test that numbers are unchanged."""
        assert idempotent.normalize_value(42) == 42
        assert idempotent.normalize_value(3.14) == 3.14


class TestCompareValues:
    """Tests for compare_values function."""

    def test_compare_equal_strings(self) -> None:
        """Test that equal strings match."""
        assert idempotent.compare_values("hello", "hello") is True

    def test_compare_different_strings(self) -> None:
        """Test that different strings don't match."""
        assert idempotent.compare_values("hello", "world") is False

    def test_compare_both_empty(self) -> None:
        """Test that both empty values match."""
        assert idempotent.compare_values("", None) is True
        assert idempotent.compare_values(False, "") is True
        assert idempotent.compare_values(None, False) is True

    def test_compare_one_empty(self) -> None:
        """Test that one empty value doesn't match."""
        assert idempotent.compare_values("hello", None) is False
        assert idempotent.compare_values(None, "hello") is False

    def test_compare_m2o_with_id(self) -> None:
        """Test comparing many2one tuple with ID."""
        assert idempotent.compare_values("5", (5, "Partner")) is True
        assert idempotent.compare_values("6", (5, "Partner")) is False

    def test_compare_numbers_as_strings(self) -> None:
        """Test comparing numbers as strings."""
        assert idempotent.compare_values(42, "42") is True
        assert idempotent.compare_values("42", 42) is True


class TestGetExistingRecords:
    """Tests for get_existing_records function."""

    def test_empty_external_ids(self) -> None:
        """Test with no external IDs."""
        mock_conn = MagicMock()
        result = idempotent.get_existing_records(mock_conn, "res.partner", [], ["name"])
        assert result == {}

    def test_fetches_records(self) -> None:
        """Test fetching existing records."""
        mock_conn = MagicMock()

        ir_model_data = MagicMock()
        ir_model_data.search_read.return_value = [{"res_id": 1}]

        model_obj = MagicMock()
        model_obj.search_read.return_value = [{"id": 1, "name": "Test"}]

        mock_conn.get_model.side_effect = lambda m: (
            ir_model_data if m == "ir.model.data" else model_obj
        )

        result = idempotent.get_existing_records(
            mock_conn, "res.partner", ["base.test"], ["name"]
        )

        assert "base.test" in result
        assert result["base.test"]["name"] == "Test"

    def test_handles_missing_records(self) -> None:
        """Test handling records not found in Odoo."""
        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        ir_model_data.search_read.return_value = []  # Not found
        mock_conn.get_model.return_value = ir_model_data

        result = idempotent.get_existing_records(
            mock_conn, "res.partner", ["base.nonexistent"], ["name"]
        )

        assert result == {}


class TestFindUnchangedRecords:
    """Tests for find_unchanged_records function."""

    def test_all_new_records(self) -> None:
        """Test when all records are new."""
        csv_data = [
            {"id": "base.new1", "name": "New 1"},
            {"id": "base.new2", "name": "New 2"},
        ]
        existing: dict[str, Any] = {}

        changed, unchanged, stats = idempotent.find_unchanged_records(
            csv_data, existing
        )

        assert len(changed) == 2
        assert len(unchanged) == 0
        assert stats.new_records == 2

    def test_all_unchanged_records(self) -> None:
        """Test when all records are unchanged."""
        csv_data = [
            {"id": "base.test1", "name": "Test 1"},
            {"id": "base.test2", "name": "Test 2"},
        ]
        existing = {
            "base.test1": {"id": 1, "name": "Test 1"},
            "base.test2": {"id": 2, "name": "Test 2"},
        }

        changed, unchanged, stats = idempotent.find_unchanged_records(
            csv_data, existing
        )

        assert len(changed) == 0
        assert len(unchanged) == 2
        assert stats.unchanged_records == 2
        assert stats.skipped_records == 2

    def test_mixed_records(self) -> None:
        """Test with mix of new, changed, and unchanged records."""
        csv_data = [
            {"id": "base.new", "name": "New"},
            {"id": "base.unchanged", "name": "Unchanged"},
            {"id": "base.changed", "name": "Changed Name"},
        ]
        existing = {
            "base.unchanged": {"id": 1, "name": "Unchanged"},
            "base.changed": {"id": 2, "name": "Original Name"},
        }

        changed, unchanged, stats = idempotent.find_unchanged_records(
            csv_data, existing
        )

        assert len(changed) == 2  # new + changed
        assert len(unchanged) == 1
        assert stats.new_records == 1
        assert stats.changed_records == 1
        assert stats.unchanged_records == 1


class TestFilterUnchangedRows:
    """Tests for filter_unchanged_rows function."""

    def test_no_existing_records(self) -> None:
        """Test when no existing records (all new)."""
        rows = [
            ["base.new1", "Name 1"],
            ["base.new2", "Name 2"],
        ]
        header = ["id", "name"]
        existing: dict[str, Any] = {}

        filtered, stats = idempotent.filter_unchanged_rows(rows, header, existing)

        assert len(filtered) == 2
        assert stats.new_records == 2

    def test_filters_unchanged(self) -> None:
        """Test that unchanged rows are filtered out."""
        rows = [
            ["base.unchanged", "Same Name"],
            ["base.changed", "New Name"],
        ]
        header = ["id", "name"]
        existing = {
            "base.unchanged": {"id": 1, "name": "Same Name"},
            "base.changed": {"id": 2, "name": "Old Name"},
        }

        filtered, stats = idempotent.filter_unchanged_rows(rows, header, existing)

        assert len(filtered) == 1
        assert filtered[0][0] == "base.changed"
        assert stats.skipped_records == 1
        assert stats.changed_records == 1

    def test_missing_id_field(self) -> None:
        """Test handling missing ID field in header."""
        rows = [["Name 1"], ["Name 2"]]
        header = ["name"]
        existing: dict[str, Any] = {}

        filtered, _stats = idempotent.filter_unchanged_rows(
            rows, header, existing, id_field="id"
        )

        # Should return all rows when ID field not found
        assert len(filtered) == 2

    def test_with_compare_fields(self) -> None:
        """Test comparing only specific fields."""
        rows = [
            ["base.test", "Same Name", "Different Desc"],
        ]
        header = ["id", "name", "description"]
        existing = {
            "base.test": {"id": 1, "name": "Same Name", "description": "Original"},
        }

        # Only compare name field
        filtered, stats = idempotent.filter_unchanged_rows(
            rows, header, existing, compare_fields=["name"]
        )

        # Should be unchanged because we only compare name
        assert len(filtered) == 0
        assert stats.skipped_records == 1


class TestIdempotentStats:
    """Tests for IdempotentStats dataclass."""

    def test_skip_rate_calculation(self) -> None:
        """Test skip rate calculation."""
        stats = idempotent.IdempotentStats(
            total_records=100,
            skipped_records=25,
        )
        assert stats.skip_rate == 25.0

    def test_skip_rate_zero_records(self) -> None:
        """Test skip rate with zero records."""
        stats = idempotent.IdempotentStats()
        assert stats.skip_rate == 0.0


class TestNormalizeValueEdgeCases:
    """Additional edge case tests for normalize_value."""

    def test_normalize_list_more_than_two_elements(self) -> None:
        """Test that lists with more than 2 elements are returned as-is."""
        result = idempotent.normalize_value([1, 2, 3])
        assert result == [1, 2, 3]

    def test_normalize_list_with_non_int_first_element(self) -> None:
        """Test list with non-int first element is returned as-is."""
        result = idempotent.normalize_value(["a", "b"])
        assert result == ["a", "b"]


class TestGetExistingRecordsEdgeCases:
    """Additional edge case tests for get_existing_records."""

    def test_external_id_without_dot(self) -> None:
        """Test that external IDs without dots are skipped."""
        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        mock_conn.get_model.return_value = ir_model_data

        result = idempotent.get_existing_records(
            mock_conn, "res.partner", ["no_dot_id", "also_no_dot"], ["name"]
        )

        assert result == {}
        # ir.model.data.search_read should never be called since no valid IDs
        ir_model_data.search_read.assert_not_called()

    def test_error_handling(self) -> None:
        """Test that errors are handled gracefully."""
        mock_conn = MagicMock()
        mock_conn.get_model.side_effect = Exception("Connection error")

        result = idempotent.get_existing_records(
            mock_conn, "res.partner", ["base.test"], ["name"]
        )

        assert result == {}


class TestFindUnchangedRecordsEdgeCases:
    """Additional edge case tests for find_unchanged_records."""

    def test_field_not_in_record(self) -> None:
        """Test when compare field is not in the record."""
        csv_data = [{"id": "base.test", "name": "Test"}]
        existing = {"base.test": {"id": 1, "name": "Test", "extra": "field"}}

        _changed, unchanged, _stats = idempotent.find_unchanged_records(
            csv_data, existing, compare_fields=["name", "description"]
        )

        # Should be unchanged because name matches and description not in record
        assert len(unchanged) == 1

    def test_base_field_not_in_existing(self) -> None:
        """Test when base field is not in existing record."""
        csv_data = [{"id": "base.test", "name": "Test", "extra": "value"}]
        existing = {"base.test": {"id": 1, "name": "Test"}}  # No "extra" field

        _changed, unchanged, _stats = idempotent.find_unchanged_records(
            csv_data, existing, compare_fields=["name", "extra"]
        )

        # Should be unchanged because name matches and extra skipped
        assert len(unchanged) == 1

    def test_comparison_error(self) -> None:
        """Test handling of comparison errors."""

        # Create a value that will raise an exception during comparison
        class BadValue:
            def __str__(self) -> str:
                raise ValueError("Cannot convert to string")

        csv_data = [{"id": "base.test", "name": BadValue()}]
        existing = {"base.test": {"id": 1, "name": "Test"}}

        changed, _unchanged, stats = idempotent.find_unchanged_records(
            csv_data, existing
        )

        # Should be marked as changed due to comparison error
        assert len(changed) == 1
        assert stats.comparison_errors == 1

    def test_empty_external_id(self) -> None:
        """Test record with empty external ID."""
        csv_data = [{"id": "", "name": "Test"}]
        existing = {"base.test": {"id": 1, "name": "Test"}}

        changed, _unchanged, stats = idempotent.find_unchanged_records(
            csv_data, existing
        )

        # Should be treated as new
        assert len(changed) == 1
        assert stats.new_records == 1


class TestFilterUnchangedRowsEdgeCases:
    """Additional edge case tests for filter_unchanged_rows."""

    def test_row_shorter_than_id_index(self) -> None:
        """Test handling rows shorter than the id field index."""
        rows = [
            [],  # Empty row
        ]
        header = ["id", "name"]
        existing = {"base.test": {"id": 1, "name": "Test"}}

        filtered, _stats = idempotent.filter_unchanged_rows(rows, header, existing)

        # Should include the row despite being short
        assert len(filtered) == 1

    def test_row_shorter_than_field_index(self) -> None:
        """Test handling rows shorter than a compare field index."""
        rows = [
            ["base.test"],  # Only has id, no name
        ]
        header = ["id", "name"]
        existing = {"base.test": {"id": 1, "name": "Test"}}

        filtered, _stats = idempotent.filter_unchanged_rows(rows, header, existing)

        # Should be unchanged because field comparison is skipped
        assert len(filtered) == 0

    def test_subfield_notation(self) -> None:
        """Test handling of subfield notation like 'partner_id/id'."""
        rows = [
            ["base.test", "5"],  # partner_id/id = 5
        ]
        header = ["id", "partner_id/id"]
        existing = {
            "base.test": {"id": 1, "partner_id": (5, "Partner Name")},
        }

        filtered, _stats = idempotent.filter_unchanged_rows(rows, header, existing)

        # Should be unchanged because partner_id matches
        assert len(filtered) == 0

    def test_comparison_error_in_filter(self) -> None:
        """Test handling comparison error in filter_unchanged_rows."""

        # Create a value that will raise an exception during comparison
        class BadValue:
            def __str__(self) -> str:
                raise ValueError("Cannot convert")

        rows = [
            ["base.test", BadValue()],
        ]
        header = ["id", "name"]
        existing = {"base.test": {"id": 1, "name": "Test"}}

        filtered, stats = idempotent.filter_unchanged_rows(rows, header, existing)

        # Should be marked as changed due to error
        assert len(filtered) == 1
        assert stats.comparison_errors == 1


class TestDisplayIdempotentStats:
    """Tests for display_idempotent_stats function."""

    def test_display_stats(self) -> None:
        """Test that display_idempotent_stats runs without error."""
        from io import StringIO
        from unittest.mock import patch

        stats = idempotent.IdempotentStats(
            total_records=100,
            new_records=20,
            changed_records=30,
            unchanged_records=50,
            skipped_records=50,
            fields_compared=200,
            comparison_errors=0,
        )

        # Capture console output
        with patch("sys.stdout", new_callable=StringIO):
            idempotent.display_idempotent_stats(stats, "res.partner")
        # If no exception, test passes

    def test_display_stats_with_errors(self) -> None:
        """Test display with comparison errors."""
        from io import StringIO
        from unittest.mock import patch

        stats = idempotent.IdempotentStats(
            total_records=100,
            comparison_errors=5,  # Has errors
        )

        with patch("sys.stdout", new_callable=StringIO):
            idempotent.display_idempotent_stats(stats, "res.partner")
        # If no exception, test passes
