"""Tests for the idempotent import module."""

from unittest.mock import MagicMock

from odoo_data_flow.lib import idempotent


class TestNormalizeValue:
    """Tests for normalize_value function."""

    def test_normalize_false(self):
        """Test that False becomes None."""
        assert idempotent.normalize_value(False) is None

    def test_normalize_none(self):
        """Test that None stays None."""
        assert idempotent.normalize_value(None) is None

    def test_normalize_empty_string(self):
        """Test that empty string becomes None."""
        assert idempotent.normalize_value("") is None
        assert idempotent.normalize_value("   ") is None

    def test_normalize_string(self):
        """Test that strings are stripped."""
        assert idempotent.normalize_value("  hello  ") == "hello"

    def test_normalize_m2o_tuple(self):
        """Test that many2one tuples return just the ID."""
        assert idempotent.normalize_value((5, "Partner Name")) == 5
        assert idempotent.normalize_value([5, "Partner Name"]) == 5

    def test_normalize_empty_list(self):
        """Test that empty list becomes None."""
        assert idempotent.normalize_value([]) is None

    def test_normalize_number(self):
        """Test that numbers are unchanged."""
        assert idempotent.normalize_value(42) == 42
        assert idempotent.normalize_value(3.14) == 3.14


class TestCompareValues:
    """Tests for compare_values function."""

    def test_compare_equal_strings(self):
        """Test that equal strings match."""
        assert idempotent.compare_values("hello", "hello") is True

    def test_compare_different_strings(self):
        """Test that different strings don't match."""
        assert idempotent.compare_values("hello", "world") is False

    def test_compare_both_empty(self):
        """Test that both empty values match."""
        assert idempotent.compare_values("", None) is True
        assert idempotent.compare_values(False, "") is True
        assert idempotent.compare_values(None, False) is True

    def test_compare_one_empty(self):
        """Test that one empty value doesn't match."""
        assert idempotent.compare_values("hello", None) is False
        assert idempotent.compare_values(None, "hello") is False

    def test_compare_m2o_with_id(self):
        """Test comparing many2one tuple with ID."""
        assert idempotent.compare_values("5", (5, "Partner")) is True
        assert idempotent.compare_values("6", (5, "Partner")) is False

    def test_compare_numbers_as_strings(self):
        """Test comparing numbers as strings."""
        assert idempotent.compare_values(42, "42") is True
        assert idempotent.compare_values("42", 42) is True


class TestGetExistingRecords:
    """Tests for get_existing_records function."""

    def test_empty_external_ids(self):
        """Test with no external IDs."""
        mock_conn = MagicMock()
        result = idempotent.get_existing_records(mock_conn, "res.partner", [], ["name"])
        assert result == {}

    def test_fetches_records(self):
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

    def test_handles_missing_records(self):
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

    def test_all_new_records(self):
        """Test when all records are new."""
        csv_data = [
            {"id": "base.new1", "name": "New 1"},
            {"id": "base.new2", "name": "New 2"},
        ]
        existing = {}

        changed, unchanged, stats = idempotent.find_unchanged_records(
            csv_data, existing
        )

        assert len(changed) == 2
        assert len(unchanged) == 0
        assert stats.new_records == 2

    def test_all_unchanged_records(self):
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

    def test_mixed_records(self):
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

    def test_no_existing_records(self):
        """Test when no existing records (all new)."""
        rows = [
            ["base.new1", "Name 1"],
            ["base.new2", "Name 2"],
        ]
        header = ["id", "name"]
        existing = {}

        filtered, stats = idempotent.filter_unchanged_rows(rows, header, existing)

        assert len(filtered) == 2
        assert stats.new_records == 2

    def test_filters_unchanged(self):
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

    def test_missing_id_field(self):
        """Test handling missing ID field in header."""
        rows = [["Name 1"], ["Name 2"]]
        header = ["name"]
        existing = {}

        filtered, _stats = idempotent.filter_unchanged_rows(
            rows, header, existing, id_field="id"
        )

        # Should return all rows when ID field not found
        assert len(filtered) == 2

    def test_with_compare_fields(self):
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

    def test_skip_rate_calculation(self):
        """Test skip rate calculation."""
        stats = idempotent.IdempotentStats(
            total_records=100,
            skipped_records=25,
        )
        assert stats.skip_rate == 25.0

    def test_skip_rate_zero_records(self):
        """Test skip rate with zero records."""
        stats = idempotent.IdempotentStats()
        assert stats.skip_rate == 0.0
