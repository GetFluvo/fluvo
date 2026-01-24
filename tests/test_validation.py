"""Tests for the validation module."""

import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from odoo_data_flow.lib import validation as val


@pytest.fixture
def temp_dir() -> Generator[str, None, None]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_csv(temp_dir: str) -> str:
    """Create a sample CSV file for testing."""
    csv_path = Path(temp_dir) / "test_data.csv"
    csv_path.write_text("id;name;state;partner_id/id\n1;Test;draft;base.partner_1\n")
    return str(csv_path)


@pytest.fixture
def mock_connection() -> MagicMock:
    """Create a mock Odoo connection."""
    conn = MagicMock()

    # Mock ir.model.data for reference checking
    ir_model_data = MagicMock()
    ir_model_data.search_count.return_value = 1  # Reference exists

    # Mock model access
    conn.get_model.return_value = ir_model_data

    return conn


@pytest.fixture
def fields_info() -> dict[str, Any]:
    """Sample fields info from fields_get()."""
    return {
        "id": {"type": "integer", "required": False},
        "name": {"type": "char", "required": True},
        "state": {
            "type": "selection",
            "required": False,
            "selection": [
                ("draft", "Draft"),
                ("confirmed", "Confirmed"),
                ("done", "Done"),
            ],
        },
        "partner_id": {
            "type": "many2one",
            "required": False,
            "relation": "res.partner",
        },
        "active": {"type": "boolean", "required": False},
    }


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_validation_result_defaults(self) -> None:
        """Test that ValidationResult has sensible defaults."""
        result = val.ValidationResult()
        assert result.total_rows == 0
        assert result.valid_rows == 0
        assert result.errors == []
        assert result.warnings == []
        assert result.missing_references == {}
        assert result.invalid_selections == {}

    def test_is_valid_with_no_errors(self) -> None:
        """Test is_valid returns True when no errors."""
        result = val.ValidationResult(total_rows=10, valid_rows=10)
        assert result.is_valid is True

    def test_is_valid_with_errors(self) -> None:
        """Test is_valid returns False when errors exist."""
        result = val.ValidationResult(
            total_rows=10,
            valid_rows=9,
            errors=[
                val.ValidationError(
                    row_number=5,
                    column="name",
                    value="",
                    error_type="required_field",
                    message="Required field 'name' is empty",
                )
            ],
        )
        assert result.is_valid is False

    def test_error_count(self) -> None:
        """Test error_count property."""
        result = val.ValidationResult(
            errors=[
                val.ValidationError(1, "a", "", "err", "msg"),
                val.ValidationError(2, "b", "", "err", "msg"),
            ]
        )
        assert result.error_count == 2

    def test_warning_count(self) -> None:
        """Test warning_count property."""
        result = val.ValidationResult(
            warnings=[val.ValidationError(1, "a", "", "warn", "msg")]
        )
        assert result.warning_count == 1


class TestGetSelectionValues:
    """Tests for _get_selection_values helper."""

    def test_get_selection_values_returns_values(
        self, fields_info: dict[str, Any]
    ) -> None:
        """Test that selection values are extracted correctly."""
        values = val._get_selection_values(fields_info, "state")
        assert values == {"draft", "confirmed", "done"}

    def test_get_selection_values_non_selection_field(
        self, fields_info: dict[str, Any]
    ) -> None:
        """Test that non-selection fields return empty set."""
        values = val._get_selection_values(fields_info, "name")
        assert values == set()

    def test_get_selection_values_missing_field(
        self, fields_info: dict[str, Any]
    ) -> None:
        """Test that missing fields return empty set."""
        values = val._get_selection_values(fields_info, "nonexistent")
        assert values == set()


class TestGetRequiredFields:
    """Tests for _get_required_fields helper."""

    def test_get_required_fields(self, fields_info: dict[str, Any]) -> None:
        """Test that required fields are identified correctly."""
        required = val._get_required_fields(fields_info)
        assert "name" in required

    def test_readonly_required_fields_excluded(self) -> None:
        """Test that readonly required fields are excluded."""
        fields = {
            "name": {"required": True, "readonly": False},
            "create_date": {"required": True, "readonly": True},
        }
        required = val._get_required_fields(fields)
        assert "name" in required
        assert "create_date" not in required


class TestGetRelationalFields:
    """Tests for _get_relational_fields helper."""

    def test_get_relational_fields(self, fields_info: dict[str, Any]) -> None:
        """Test that relational fields are identified."""
        header = ["id", "name", "partner_id/id"]
        relational = val._get_relational_fields(fields_info, header)
        assert "partner_id/id" in relational
        assert relational["partner_id/id"]["type"] == "many2one"
        assert relational["partner_id/id"]["relation"] == "res.partner"

    def test_non_relational_fields_excluded(self, fields_info: dict[str, Any]) -> None:
        """Test that non-relational fields are excluded."""
        header = ["id", "name", "state"]
        relational = val._get_relational_fields(fields_info, header)
        assert "state" not in relational
        assert "name" not in relational


class TestValidateCsvData:
    """Tests for validate_csv_data function."""

    def test_validate_valid_data(
        self, temp_dir: str, mock_connection: MagicMock, fields_info: dict[str, Any]
    ) -> None:
        """Test validation of valid CSV data."""
        csv_path = Path(temp_dir) / "valid.csv"
        csv_path.write_text("id;name;state\n1;Product A;draft\n2;Product B;confirmed\n")

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_connection,
        )

        assert result.is_valid
        assert result.total_rows == 2
        assert result.valid_rows == 2
        assert result.error_count == 0

    def test_validate_missing_required_field(
        self, temp_dir: str, mock_connection: MagicMock, fields_info: dict[str, Any]
    ) -> None:
        """Test validation catches missing required fields."""
        csv_path = Path(temp_dir) / "missing_required.csv"
        csv_path.write_text("id;name;state\n1;;draft\n")

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_connection,
        )

        assert not result.is_valid
        assert result.error_count == 1
        assert result.errors[0].error_type == "required_field"
        assert result.errors[0].column == "name"

    def test_validate_invalid_selection(
        self, temp_dir: str, mock_connection: MagicMock, fields_info: dict[str, Any]
    ) -> None:
        """Test validation catches invalid selection values."""
        csv_path = Path(temp_dir) / "invalid_selection.csv"
        csv_path.write_text("id;name;state\n1;Product;invalid_state\n")

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_connection,
        )

        assert not result.is_valid
        assert result.error_count == 1
        assert result.errors[0].error_type == "invalid_selection"
        assert "invalid_state" in result.invalid_selections.get("state", set())

    def test_validate_missing_reference(
        self, temp_dir: str, fields_info: dict[str, Any]
    ) -> None:
        """Test validation catches missing references."""
        csv_path = Path(temp_dir) / "missing_ref.csv"
        csv_path.write_text("id;name;partner_id/id\n1;Product;base.nonexistent\n")

        # Mock connection that returns 0 for reference check
        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        ir_model_data.search_count.return_value = 0  # Reference doesn't exist
        mock_conn.get_model.return_value = ir_model_data

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_conn,
        )

        assert not result.is_valid
        assert result.error_count == 1
        assert result.errors[0].error_type == "missing_reference"
        missing = result.missing_references.get("partner_id/id", set())
        assert "base.nonexistent" in missing

    def test_validate_with_ignore_columns(
        self, temp_dir: str, mock_connection: MagicMock, fields_info: dict[str, Any]
    ) -> None:
        """Test validation ignores specified columns."""
        csv_path = Path(temp_dir) / "with_ignore.csv"
        csv_path.write_text("id;name;state;_INTERNAL\n1;Product;draft;ignore_me\n")

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_connection,
            ignore=["_INTERNAL"],
        )

        assert result.is_valid

    def test_validate_file_not_found(
        self, mock_connection: MagicMock, fields_info: dict[str, Any]
    ) -> None:
        """Test validation handles missing files."""
        result = val.validate_csv_data(
            file_path="/nonexistent/file.csv",
            model="test.model",
            fields_info=fields_info,
            connection=mock_connection,
        )

        assert not result.is_valid
        assert result.errors[0].error_type == "file_not_found"

    def test_validate_with_custom_separator(
        self, temp_dir: str, mock_connection: MagicMock, fields_info: dict[str, Any]
    ) -> None:
        """Test validation with custom CSV separator."""
        csv_path = Path(temp_dir) / "custom_sep.csv"
        csv_path.write_text("id,name,state\n1,Product,draft\n")

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_connection,
            separator=",",
        )

        assert result.is_valid

    def test_validate_empty_reference_value(
        self, temp_dir: str, mock_connection: MagicMock, fields_info: dict[str, Any]
    ) -> None:
        """Test that empty reference values don't cause errors."""
        csv_path = Path(temp_dir) / "empty_ref.csv"
        csv_path.write_text("id;name;partner_id/id\n1;Product;\n")

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_connection,
        )

        assert result.is_valid


class TestCheckReferenceExists:
    """Tests for _check_reference_exists helper."""

    def test_check_external_id_exists(self) -> None:
        """Test checking external ID reference."""
        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        ir_model_data.search_count.return_value = 1
        mock_conn.get_model.return_value = ir_model_data

        exists = val._check_reference_exists(mock_conn, "res.partner", "base.partner_1")

        assert exists is True
        mock_conn.get_model.assert_called_with("ir.model.data")

    def test_check_external_id_not_exists(self) -> None:
        """Test checking non-existent external ID."""
        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        ir_model_data.search_count.return_value = 0
        mock_conn.get_model.return_value = ir_model_data

        exists = val._check_reference_exists(
            mock_conn, "res.partner", "base.nonexistent"
        )

        assert exists is False

    def test_check_database_id_exists(self) -> None:
        """Test checking database ID reference."""
        mock_conn = MagicMock()
        model_obj = MagicMock()
        model_obj.search_count.return_value = 1
        mock_conn.get_model.return_value = model_obj

        exists = val._check_reference_exists(mock_conn, "res.partner", "123")

        assert exists is True
        mock_conn.get_model.assert_called_with("res.partner")

    def test_check_invalid_id_format(self) -> None:
        """Test checking invalid ID format returns False."""
        mock_conn = MagicMock()

        exists = val._check_reference_exists(mock_conn, "res.partner", "not_a_valid_id")

        assert exists is False

    def test_check_reference_handles_exception(self) -> None:
        """Test that exceptions are handled gracefully."""
        mock_conn = MagicMock()
        mock_conn.get_model.side_effect = Exception("Connection error")

        exists = val._check_reference_exists(mock_conn, "res.partner", "base.test")

        assert exists is False


class TestDisplayValidationResults:
    """Tests for display_validation_results function."""

    def test_display_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test displaying successful validation results."""
        result = val.ValidationResult(total_rows=100, valid_rows=100)

        val.display_validation_results(result, "res.partner")

        captured = capsys.readouterr()
        assert "Validation Passed" in captured.out
        assert "100" in captured.out

    def test_display_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test displaying validation errors."""
        result = val.ValidationResult(
            total_rows=100,
            valid_rows=90,
            errors=[
                val.ValidationError(
                    5, "name", "", "required_field", "Required field empty"
                ),
            ],
            missing_references={"partner_id": {"base.missing"}},
        )

        val.display_validation_results(result, "res.partner")

        captured = capsys.readouterr()
        assert "Validation Failed" in captured.out
        assert "1 errors" in captured.out


class TestDryRunCLI:
    """Tests for the --dry-run CLI option."""

    @patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
    def test_dry_run_validation(self, mock_get_conn: MagicMock, temp_dir: str) -> None:
        """Test dry-run validation via CLI."""
        from click.testing import CliRunner

        from odoo_data_flow.__main__ import cli

        # Create test CSV
        csv_path = Path(temp_dir) / "test.csv"
        csv_path.write_text("id;name\n1;Test\n")

        # Create mock connection file
        conn_file = Path(temp_dir) / "conn.conf"
        conn_file.write_text("[odoo]\nhost=localhost\n")

        # Mock connection
        mock_conn = MagicMock()
        mock_model = MagicMock()
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char", "required": True},
        }
        mock_conn.get_model.return_value = mock_model
        mock_get_conn.return_value = mock_conn

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "import",
                "--connection-file",
                str(conn_file),
                "--file",
                str(csv_path),
                "--model",
                "res.partner",
                "--dry-run",
            ],
        )

        # Should not fail
        assert result.exit_code == 0
        # Should show validation result
        assert "Validation" in result.output


class TestValidateCsvDataEdgeCases:
    """Additional edge case tests for validate_csv_data."""

    def test_validate_m2m_references(
        self, temp_dir: str, fields_info: dict[str, Any]
    ) -> None:
        """Test validation of many2many reference fields."""
        # Add m2m field to fields_info
        fields_info_m2m = dict(fields_info)
        fields_info_m2m["tag_ids"] = {
            "type": "many2many",
            "required": False,
            "relation": "res.partner.tag",
        }

        csv_path = Path(temp_dir) / "m2m.csv"
        csv_path.write_text("id;name;tag_ids/id\n1;Product;base.tag1,base.tag2\n")

        # Mock connection that returns 1 for all reference checks
        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        ir_model_data.search_count.return_value = 1  # References exist
        mock_conn.get_model.return_value = ir_model_data

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info_m2m,
            connection=mock_conn,
        )

        assert result.is_valid

    def test_validate_relational_field_no_relation_model(
        self, temp_dir: str
    ) -> None:
        """Test handling relational field with missing relation."""
        fields_info = {
            "partner_id": {
                "type": "many2one",
                "relation": "",  # Empty relation
            },
        }

        csv_path = Path(temp_dir) / "no_relation.csv"
        csv_path.write_text("id;partner_id/id\n1;base.test\n")

        mock_conn = MagicMock()

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_conn,
        )

        # Should not error - just skip validation for this field
        assert result.is_valid

    def test_validate_caches_reference_lookups(
        self, temp_dir: str, fields_info: dict[str, Any]
    ) -> None:
        """Test that reference lookups are cached."""
        csv_path = Path(temp_dir) / "cached_refs.csv"
        csv_path.write_text(
            "id;name;partner_id/id\n"
            "1;Product1;base.partner_1\n"
            "2;Product2;base.partner_1\n"  # Same reference
            "3;Product3;base.partner_1\n"  # Same reference again
        )

        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        ir_model_data.search_count.return_value = 1
        mock_conn.get_model.return_value = ir_model_data

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_conn,
        )

        # Reference should only be checked once due to caching
        assert ir_model_data.search_count.call_count == 1
        assert result.is_valid

    def test_validate_caches_missing_references(
        self, temp_dir: str, fields_info: dict[str, Any]
    ) -> None:
        """Test that missing references are tracked from cache."""
        csv_path = Path(temp_dir) / "cached_missing.csv"
        csv_path.write_text(
            "id;name;partner_id/id\n"
            "1;Product1;base.missing\n"
            "2;Product2;base.missing\n"  # Same missing reference
        )

        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        ir_model_data.search_count.return_value = 0  # Not found
        mock_conn.get_model.return_value = ir_model_data

        result = val.validate_csv_data(
            file_path=str(csv_path),
            model="test.model",
            fields_info=fields_info,
            connection=mock_conn,
        )

        # Both rows should have the missing reference error tracked
        assert "base.missing" in result.missing_references.get("partner_id/id", set())

    def test_validate_generic_exception(
        self, temp_dir: str, mock_connection: MagicMock, fields_info: dict[str, Any]
    ) -> None:
        """Test handling of generic exceptions during validation."""
        csv_path = Path(temp_dir) / "error.csv"
        csv_path.write_text("id;name;state\n1;Product;draft\n")

        # Make csv.reader raise an exception
        with patch("odoo_data_flow.lib.validation.csv.reader") as mock_reader:
            mock_reader.side_effect = Exception("Unexpected error")

            result = val.validate_csv_data(
                file_path=str(csv_path),
                model="test.model",
                fields_info=fields_info,
                connection=mock_connection,
            )

            assert not result.is_valid
            assert result.errors[0].error_type == "validation_error"


class TestGetSelectionValuesEdgeCases:
    """Additional edge case tests for _get_selection_values."""

    def test_get_selection_values_non_list_selection(self) -> None:
        """Test handling non-list selection definition."""
        fields_info = {
            "state": {
                "type": "selection",
                "selection": "get_states",  # Method name instead of list
            }
        }
        values = val._get_selection_values(fields_info, "state")
        assert values == set()


class TestDisplayValidationResultsEdgeCases:
    """Additional tests for display_validation_results."""

    def test_display_with_invalid_selections(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test displaying validation with invalid selection values."""
        result = val.ValidationResult(
            total_rows=10,
            valid_rows=8,
            errors=[
                val.ValidationError(
                    5, "state", "bad", "invalid_selection", "Invalid value"
                ),
            ],
            invalid_selections={"state": {"bad", "worse", "awful"}},
        )

        val.display_validation_results(result, "res.partner")

        captured = capsys.readouterr()
        assert "Invalid Selection Values" in captured.out

    def test_display_with_many_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test displaying more than 10 errors."""
        errors = [
            val.ValidationError(i, "field", "", "err", f"Error {i}")
            for i in range(15)
        ]
        result = val.ValidationResult(
            total_rows=15,
            valid_rows=0,
            errors=errors,
        )

        val.display_validation_results(result, "res.partner")

        captured = capsys.readouterr()
        assert "and 5 more errors" in captured.out

    def test_display_error_without_row_number(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test displaying error without row number (row_number=0)."""
        result = val.ValidationResult(
            total_rows=0,
            valid_rows=0,
            errors=[
                val.ValidationError(
                    0, "", "/path/to/file", "file_not_found", "File not found"
                ),
            ],
        )

        val.display_validation_results(result, "res.partner")

        captured = capsys.readouterr()
        assert "File not found" in captured.out
