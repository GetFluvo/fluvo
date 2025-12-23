"""Tests for the pre-flight reference check."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from odoo_data_flow.lib import preflight


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_csv_with_refs(temp_dir):
    """Create a sample CSV file with relational references."""
    csv_path = Path(temp_dir) / "test_data.csv"
    csv_path.write_text(
        "id;name;partner_id/id;tag_ids/id\n"
        "1;Product A;base.partner_1;base.tag_1,base.tag_2\n"
        "2;Product B;base.partner_2;base.tag_1\n"
        "3;Product C;base.partner_1;\n"
    )
    return str(csv_path)


@pytest.fixture
def fields_info():
    """Sample fields info from fields_get()."""
    return {
        "id": {"type": "integer"},
        "name": {"type": "char", "required": True},
        "partner_id": {
            "type": "many2one",
            "relation": "res.partner",
        },
        "tag_ids": {
            "type": "many2many",
            "relation": "res.tag",
        },
    }


class TestExtractReferencesFromCSV:
    """Tests for _extract_references_from_csv function."""

    def test_extracts_many2one_refs(self, sample_csv_with_refs, fields_info):
        """Test that many2one references are extracted."""
        header = ["id", "name", "partner_id/id", "tag_ids/id"]
        refs = preflight._extract_references_from_csv(
            sample_csv_with_refs, header, fields_info
        )

        assert "res.partner" in refs
        assert "partner_id/id" in refs["res.partner"]
        assert "base.partner_1" in refs["res.partner"]["partner_id/id"]
        assert "base.partner_2" in refs["res.partner"]["partner_id/id"]

    def test_extracts_many2many_refs(self, sample_csv_with_refs, fields_info):
        """Test that many2many references are extracted and split."""
        header = ["id", "name", "partner_id/id", "tag_ids/id"]
        refs = preflight._extract_references_from_csv(
            sample_csv_with_refs, header, fields_info
        )

        assert "res.tag" in refs
        assert "tag_ids/id" in refs["res.tag"]
        assert "base.tag_1" in refs["res.tag"]["tag_ids/id"]
        assert "base.tag_2" in refs["res.tag"]["tag_ids/id"]

    def test_ignores_non_relational_columns(self, temp_dir, fields_info):
        """Test that non-relational columns are not included."""
        csv_path = Path(temp_dir) / "test.csv"
        csv_path.write_text("id;name\n1;Test\n")

        header = ["id", "name"]
        refs = preflight._extract_references_from_csv(
            str(csv_path), header, fields_info
        )

        # No relational columns, so empty result
        assert not any(refs.values())

    def test_handles_empty_values(self, temp_dir, fields_info):
        """Test that empty values are skipped."""
        csv_path = Path(temp_dir) / "test.csv"
        csv_path.write_text("id;name;partner_id/id\n1;Test;\n")

        header = ["id", "name", "partner_id/id"]
        refs = preflight._extract_references_from_csv(
            str(csv_path), header, fields_info
        )

        assert "res.partner" in refs
        # Empty values should not be added
        assert len(refs["res.partner"]["partner_id/id"]) == 0

    def test_respects_ignore_list(self, sample_csv_with_refs, fields_info):
        """Test that ignored columns are not processed."""
        header = ["id", "name", "partner_id/id", "tag_ids/id"]
        refs = preflight._extract_references_from_csv(
            sample_csv_with_refs, header, fields_info, ignore=["partner_id/id"]
        )

        # partner_id/id should be ignored
        assert "res.partner" not in refs or "partner_id/id" not in refs.get(
            "res.partner", {}
        )
        # tag_ids/id should still be included
        assert "res.tag" in refs


class TestCheckReferencesExist:
    """Tests for _check_references_exist function."""

    def test_all_refs_exist(self):
        """Test when all references exist."""
        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        ir_model_data.search_read.return_value = [
            {"module": "base", "name": "partner_1"},
            {"module": "base", "name": "partner_2"},
        ]
        mock_conn.get_model.return_value = ir_model_data

        refs = {
            "res.partner": {
                "partner_id/id": {"base.partner_1", "base.partner_2"},
            }
        }

        missing = preflight._check_references_exist(mock_conn, refs)
        assert not missing

    def test_some_refs_missing(self):
        """Test when some references are missing."""
        mock_conn = MagicMock()
        ir_model_data = MagicMock()
        # Only one reference exists
        ir_model_data.search_read.return_value = [
            {"module": "base", "name": "partner_1"},
        ]
        mock_conn.get_model.return_value = ir_model_data

        refs = {
            "res.partner": {
                "partner_id/id": {"base.partner_1", "base.missing"},
            }
        }

        missing = preflight._check_references_exist(mock_conn, refs)
        assert "res.partner" in missing
        assert "base.missing" in missing["res.partner"]["partner_id/id"]

    def test_handles_database_ids(self):
        """Test checking database IDs."""
        mock_conn = MagicMock()
        model_obj = MagicMock()
        model_obj.search.return_value = [1, 2]  # IDs that exist
        mock_conn.get_model.return_value = model_obj

        refs = {
            "res.partner": {
                "partner_id": {"1", "2", "999"},  # 999 doesn't exist
            }
        }

        missing = preflight._check_references_exist(mock_conn, refs)
        assert "res.partner" in missing
        assert "999" in missing["res.partner"]["partner_id"]

    def test_handles_invalid_refs(self):
        """Test that invalid reference formats are marked as missing."""
        mock_conn = MagicMock()
        mock_conn.get_model.return_value = MagicMock()

        refs = {
            "res.partner": {
                "partner_id": {"not_a_valid_id"},
            }
        }

        missing = preflight._check_references_exist(mock_conn, refs)
        assert "res.partner" in missing
        assert "not_a_valid_id" in missing["res.partner"]["partner_id"]


class TestReferenceCheck:
    """Tests for the reference_check preflight function."""

    @patch("odoo_data_flow.lib.preflight._get_csv_header")
    @patch("odoo_data_flow.lib.preflight._get_odoo_fields")
    @patch("odoo_data_flow.lib.preflight.conf_lib.get_connection_from_config")
    def test_skip_mode_returns_true(
        self, mock_conn, mock_fields, mock_header
    ):
        """Test that skip mode immediately returns True."""
        from odoo_data_flow.enums import PreflightMode

        result = preflight.reference_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="test.csv",
            config="config.conf",
            check_refs="skip",
        )

        assert result is True
        mock_header.assert_not_called()

    @patch("odoo_data_flow.lib.preflight._get_csv_header")
    @patch("odoo_data_flow.lib.preflight._get_odoo_fields")
    @patch("odoo_data_flow.lib.preflight.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.lib.preflight._extract_references_from_csv")
    @patch("odoo_data_flow.lib.preflight._check_references_exist")
    def test_all_refs_valid_returns_true(
        self, mock_check, mock_extract, mock_conn, mock_fields, mock_header
    ):
        """Test that valid references return True."""
        from odoo_data_flow.enums import PreflightMode

        mock_header.return_value = ["id", "name", "partner_id/id"]
        mock_fields.return_value = {
            "partner_id": {"type": "many2one", "relation": "res.partner"}
        }
        mock_extract.return_value = {
            "res.partner": {"partner_id/id": {"base.partner_1"}}
        }
        mock_check.return_value = {}  # No missing refs

        result = preflight.reference_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="test.csv",
            config="config.conf",
            check_refs="warn",
        )

        assert result is True

    @patch("odoo_data_flow.lib.preflight._get_csv_header")
    @patch("odoo_data_flow.lib.preflight._get_odoo_fields")
    @patch("odoo_data_flow.lib.preflight.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.lib.preflight._extract_references_from_csv")
    @patch("odoo_data_flow.lib.preflight._check_references_exist")
    @patch("odoo_data_flow.lib.preflight._display_missing_references")
    def test_missing_refs_fail_mode(
        self,
        mock_display,
        mock_check,
        mock_extract,
        mock_conn,
        mock_fields,
        mock_header,
    ):
        """Test that missing refs with fail mode returns False."""
        from odoo_data_flow.enums import PreflightMode

        mock_header.return_value = ["id", "name", "partner_id/id"]
        mock_fields.return_value = {
            "partner_id": {"type": "many2one", "relation": "res.partner"}
        }
        mock_extract.return_value = {
            "res.partner": {"partner_id/id": {"base.missing"}}
        }
        mock_check.return_value = {
            "res.partner": {"partner_id/id": {"base.missing"}}
        }

        result = preflight.reference_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="test.csv",
            config="config.conf",
            check_refs="fail",
        )

        assert result is False
        mock_display.assert_called_once()

    @patch("odoo_data_flow.lib.preflight._get_csv_header")
    @patch("odoo_data_flow.lib.preflight._get_odoo_fields")
    @patch("odoo_data_flow.lib.preflight.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.lib.preflight._extract_references_from_csv")
    @patch("odoo_data_flow.lib.preflight._check_references_exist")
    @patch("odoo_data_flow.lib.preflight._display_missing_references")
    def test_missing_refs_warn_mode(
        self,
        mock_display,
        mock_check,
        mock_extract,
        mock_conn,
        mock_fields,
        mock_header,
    ):
        """Test that missing refs with warn mode returns True."""
        from odoo_data_flow.enums import PreflightMode

        mock_header.return_value = ["id", "name", "partner_id/id"]
        mock_fields.return_value = {
            "partner_id": {"type": "many2one", "relation": "res.partner"}
        }
        mock_extract.return_value = {
            "res.partner": {"partner_id/id": {"base.missing"}}
        }
        mock_check.return_value = {
            "res.partner": {"partner_id/id": {"base.missing"}}
        }

        result = preflight.reference_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="test.csv",
            config="config.conf",
            check_refs="warn",
        )

        assert result is True
        mock_display.assert_called_once()

    def test_fail_mode_skipped(self):
        """Test that reference check is skipped in FAIL_MODE."""
        from odoo_data_flow.enums import PreflightMode

        result = preflight.reference_check(
            preflight_mode=PreflightMode.FAIL_MODE,
            model="res.partner",
            filename="test.csv",
            config="config.conf",
            check_refs="fail",
        )

        assert result is True
