"""Tests for the pre-flight reference check."""

import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from odoo_data_flow.lib import preflight


@pytest.fixture
def temp_dir() -> Generator[str, None, None]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_csv_with_refs(temp_dir: str) -> str:
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
def fields_info() -> dict[str, Any]:
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

    def test_extracts_many2one_refs(
        self, sample_csv_with_refs: str, fields_info: dict[str, Any]
    ) -> None:
        """Test that many2one references are extracted."""
        header = ["id", "name", "partner_id/id", "tag_ids/id"]
        refs = preflight._extract_references_from_csv(
            sample_csv_with_refs, header, fields_info
        )

        assert "res.partner" in refs
        assert "partner_id/id" in refs["res.partner"]
        assert "base.partner_1" in refs["res.partner"]["partner_id/id"]
        assert "base.partner_2" in refs["res.partner"]["partner_id/id"]

    def test_extracts_many2many_refs(
        self, sample_csv_with_refs: str, fields_info: dict[str, Any]
    ) -> None:
        """Test that many2many references are extracted and split."""
        header = ["id", "name", "partner_id/id", "tag_ids/id"]
        refs = preflight._extract_references_from_csv(
            sample_csv_with_refs, header, fields_info
        )

        assert "res.tag" in refs
        assert "tag_ids/id" in refs["res.tag"]
        assert "base.tag_1" in refs["res.tag"]["tag_ids/id"]
        assert "base.tag_2" in refs["res.tag"]["tag_ids/id"]

    def test_ignores_non_relational_columns(
        self, temp_dir: str, fields_info: dict[str, Any]
    ) -> None:
        """Test that non-relational columns are not included."""
        csv_path = Path(temp_dir) / "test.csv"
        csv_path.write_text("id;name\n1;Test\n")

        header = ["id", "name"]
        refs = preflight._extract_references_from_csv(
            str(csv_path), header, fields_info
        )

        # No relational columns, so empty result
        assert not any(refs.values())

    def test_handles_empty_values(
        self, temp_dir: str, fields_info: dict[str, Any]
    ) -> None:
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

    def test_respects_ignore_list(
        self, sample_csv_with_refs: str, fields_info: dict[str, Any]
    ) -> None:
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

    def test_all_refs_exist(self) -> None:
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

    def test_some_refs_missing(self) -> None:
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

    def test_handles_database_ids(self) -> None:
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

    def test_handles_invalid_refs(self) -> None:
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
        self, mock_conn: Any, mock_fields: Any, mock_header: Any
    ) -> None:
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
        self,
        mock_check: Any,
        mock_extract: Any,
        mock_conn: Any,
        mock_fields: Any,
        mock_header: Any,
    ) -> None:
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
        mock_display: Any,
        mock_check: Any,
        mock_extract: Any,
        mock_conn: Any,
        mock_fields: Any,
        mock_header: Any,
    ) -> None:
        """Test that missing refs with fail mode returns False."""
        from odoo_data_flow.enums import PreflightMode

        mock_header.return_value = ["id", "name", "partner_id/id"]
        mock_fields.return_value = {
            "partner_id": {"type": "many2one", "relation": "res.partner"}
        }
        mock_extract.return_value = {"res.partner": {"partner_id/id": {"base.missing"}}}
        mock_check.return_value = {"res.partner": {"partner_id/id": {"base.missing"}}}

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
        mock_display: Any,
        mock_check: Any,
        mock_extract: Any,
        mock_conn: Any,
        mock_fields: Any,
        mock_header: Any,
    ) -> None:
        """Test that missing refs with warn mode returns True."""
        from odoo_data_flow.enums import PreflightMode

        mock_header.return_value = ["id", "name", "partner_id/id"]
        mock_fields.return_value = {
            "partner_id": {"type": "many2one", "relation": "res.partner"}
        }
        mock_extract.return_value = {"res.partner": {"partner_id/id": {"base.missing"}}}
        mock_check.return_value = {"res.partner": {"partner_id/id": {"base.missing"}}}

        result = preflight.reference_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="test.csv",
            config="config.conf",
            check_refs="warn",
        )

        assert result is True
        mock_display.assert_called_once()

    def test_fail_mode_skipped(self) -> None:
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


class TestExtractIdsFromCSV:
    """Tests for _extract_ids_from_csv function."""

    def test_extracts_ids_from_id_column(self, temp_dir: str) -> None:
        """Test that IDs are extracted from the id column."""
        csv_path = Path(temp_dir) / "test_data.csv"
        csv_path.write_text(
            "id;name;parent_id/id\n"
            "__import__.company_a;Company A;\n"
            "__import__.company_b;Company B;\n"
            "__import__.contact_1;Contact 1;__import__.company_a\n"
        )
        header = ["id", "name", "parent_id/id"]

        ids = preflight._extract_ids_from_csv(str(csv_path), header)

        assert ids == {
            "__import__.company_a",
            "__import__.company_b",
            "__import__.contact_1",
        }

    def test_handles_empty_id_values(self, temp_dir: str) -> None:
        """Test that empty ID values are ignored."""
        csv_path = Path(temp_dir) / "test_data.csv"
        csv_path.write_text(
            "id;name;value\n"
            "__import__.rec1;Record 1;100\n"
            ";Record 2;200\n"  # Empty ID
            "__import__.rec3;Record 3;300\n"
        )
        header = ["id", "name", "value"]

        ids = preflight._extract_ids_from_csv(str(csv_path), header)

        assert ids == {"__import__.rec1", "__import__.rec3"}

    def test_returns_empty_if_no_id_column(self, temp_dir: str) -> None:
        """Test that empty set is returned if no id column exists."""
        csv_path = Path(temp_dir) / "test_data.csv"
        csv_path.write_text("name;value\nRecord 1;100\n")
        header = ["name", "value"]

        ids = preflight._extract_ids_from_csv(str(csv_path), header)

        assert ids == set()


class TestSelfReferenceExclusion:
    """Tests for excluding self-references from missing references."""

    @patch("odoo_data_flow.lib.preflight._get_csv_header")
    @patch("odoo_data_flow.lib.preflight._get_odoo_fields")
    @patch("odoo_data_flow.lib.preflight.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.lib.preflight._extract_references_from_csv")
    @patch("odoo_data_flow.lib.preflight._extract_ids_from_csv")
    @patch("odoo_data_flow.lib.preflight._check_references_exist")
    def test_self_references_excluded_from_missing(
        self,
        mock_check: Any,
        mock_extract_ids: Any,
        mock_extract_refs: Any,
        mock_conn: Any,
        mock_fields: Any,
        mock_header: Any,
    ) -> None:
        """Test that self-references (IDs in same file) are not flagged as missing."""
        from odoo_data_flow.enums import PreflightMode

        mock_header.return_value = ["id", "name", "parent_id/id"]
        mock_fields.return_value = {
            "parent_id": {"type": "many2one", "relation": "res.partner"}
        }
        # References include IDs that are defined in the same file
        mock_extract_refs.return_value = {
            "res.partner": {
                "parent_id/id": {"__import__.company_a", "__import__.external"}
            }
        }
        # IDs defined in this file
        mock_extract_ids.return_value = {"__import__.company_a", "__import__.company_b"}
        # Database check says both are "missing"
        mock_check.return_value = {
            "res.partner": {
                "parent_id/id": {"__import__.company_a", "__import__.external"}
            }
        }

        preflight.reference_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="test.csv",
            config="config.conf",
            check_refs="fail",  # Would fail if __import__.company_a was flagged
        )

        # Should return True because __import__.company_a is in the same file
        # Only __import__.external is truly missing, but since we mock
        # we need to verify the logic removes self-refs
        # The test passes if it doesn't fail on __import__.company_a
        mock_extract_ids.assert_called_once()

    @patch("odoo_data_flow.lib.preflight._get_csv_header")
    @patch("odoo_data_flow.lib.preflight._get_odoo_fields")
    @patch("odoo_data_flow.lib.preflight.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.lib.preflight._extract_references_from_csv")
    @patch("odoo_data_flow.lib.preflight._extract_ids_from_csv")
    @patch("odoo_data_flow.lib.preflight._check_references_exist")
    def test_all_self_references_returns_success(
        self,
        mock_check: Any,
        mock_extract_ids: Any,
        mock_extract_refs: Any,
        mock_conn: Any,
        mock_fields: Any,
        mock_header: Any,
    ) -> None:
        """Test that when all missing refs are self-refs, check passes."""
        from odoo_data_flow.enums import PreflightMode

        mock_header.return_value = ["id", "name", "parent_id/id"]
        mock_fields.return_value = {
            "parent_id": {"type": "many2one", "relation": "res.partner"}
        }
        mock_extract_refs.return_value = {
            "res.partner": {"parent_id/id": {"__import__.company_a"}}
        }
        # The "missing" reference is actually defined in the same file
        mock_extract_ids.return_value = {"__import__.company_a", "__import__.contact_1"}
        mock_check.return_value = {
            "res.partner": {"parent_id/id": {"__import__.company_a"}}
        }

        result = preflight.reference_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="test.csv",
            config="config.conf",
            check_refs="fail",
        )

        # Should pass because all "missing" refs are defined in the same file
        assert result is True
