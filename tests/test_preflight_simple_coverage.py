"""Simple tests to improve coverage for the preflight module."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from odoo_data_flow.enums import PreflightMode
from odoo_data_flow.lib import preflight


class TestPreflightSimpleCoverage:
    """Simple tests to improve coverage for the preflight module."""

    def test_connection_check_with_string_config(self) -> None:
        """Test connection_check with string config to cover elif branch."""
        with patch(
            "odoo_data_flow.lib.preflight.conf_lib.get_connection_from_config"
        ) as mock_get_conn:
            mock_get_conn.return_value = MagicMock()

            result = preflight.connection_check(
                preflight_mode=PreflightMode.NORMAL,
                config="dummy.conf",
                model="res.partner",
                filename="file.csv",
                headless=False,
                import_plan={},
            )

            assert result is True
            mock_get_conn.assert_called_once_with(config_file="dummy.conf")

    def test_self_referencing_check_sort_function_error(self) -> None:
        """Test self_referencing_check when sort function raises an error."""
        with patch(
            "odoo_data_flow.lib.preflight.sort.sort_for_self_referencing"
        ) as mock_sort:
            # Make the sort function raise an exception
            mock_sort.side_effect = Exception("Sort error")

            result = preflight.self_referencing_check(
                preflight_mode=PreflightMode.NORMAL,
                filename="file.csv",
                import_plan={},
                o2m=False,
                separator=";",
            )

            # Should return True (graceful degradation when sort fails)
            assert result is True

    def test_self_referencing_check_sort_performed(self) -> None:
        """Test self_referencing_check when sort is performed."""
        with patch(
            "odoo_data_flow.lib.preflight.sort.sort_for_self_referencing"
        ) as mock_sort:
            # Make the sort function return a file path (truthy result)
            mock_sort.return_value = "sorted_file.csv"

            import_plan: dict[str, Any] = {}
            result = preflight.self_referencing_check(
                preflight_mode=PreflightMode.NORMAL,
                filename="file.csv",
                import_plan=import_plan,
                o2m=False,
                separator=";",
            )

            # Should return True and update import_plan
            assert result is True
            assert import_plan["strategy"] == "sort_and_one_pass_load"
            assert import_plan["id_column"] == "id"
            assert import_plan["parent_column"] == "parent_id"

    def test_handle_m2m_field_missing_relation_info(self) -> None:
        """Test _handle_m2m_field with missing relation information."""
        with patch("odoo_data_flow.lib.preflight.log") as mock_log:
            import polars as pl

            # Create a simple DataFrame
            df = pl.DataFrame({"field_name": ["value1,value2", "value3"]})

            # Call with missing relation info
            field_info = {
                "relation_table": None,  # Missing
                "relation_field": None,  # Missing
                "relation": "res.partner",
            }

            success, strategy_details = preflight._handle_m2m_field(
                field_name="field_name",
                clean_field_name="field_name",
                field_info=field_info,
                df=df,
            )

            # Should still succeed with fallback strategy
            assert success is True
            assert strategy_details["strategy"] == "write_tuple"
            assert strategy_details["relation_table"] is None
            assert strategy_details["relation_field"] is None
            assert strategy_details["relation"] == "res.partner"

            # Should log a warning
            mock_log.warning.assert_called_once()

    def test_get_installed_languages_with_string_config(self) -> None:
        """Test _get_installed_languages with string config to cover elif branch."""
        with patch(
            "odoo_data_flow.lib.preflight.conf_lib.get_connection_from_config"
        ) as mock_get_conn:
            mock_connection = MagicMock()
            mock_lang_obj = MagicMock()
            mock_get_conn.return_value = mock_connection
            mock_connection.get_model.return_value = mock_lang_obj
            mock_lang_obj.search_read.return_value = [
                {"code": "en_US"},
                {"code": "fr_FR"},
            ]

            result = preflight._get_installed_languages("dummy.conf")

            assert result == {"en_US", "fr_FR"}
            mock_get_conn.assert_called_once_with("dummy.conf")
            mock_get_conn.assert_called_once_with("dummy.conf")
            mock_connection.get_model.assert_called_once_with("res.lang")
            mock_lang_obj.search_read.assert_called_once_with(
                [("active", "=", True)], ["code"]
            )

    def test_get_installed_languages_with_exception(self) -> None:
        """Test _get_installed_languages when it raises an exception."""
        with patch(
            "odoo_data_flow.lib.preflight.conf_lib.get_connection_from_config"
        ) as mock_get_conn:
            # Make the connection raise an exception
            mock_get_conn.side_effect = Exception("Connection failed")

            result = preflight._get_installed_languages("dummy.conf")

            # Should return None when an exception occurs
            assert result is None

    def test_get_required_languages_column_not_found_error(self) -> None:
        """Test _get_required_languages when ColumnNotFoundError is raised."""
        with patch("odoo_data_flow.lib.preflight.pl.read_csv") as mock_read_csv:
            # Make read_csv raise ColumnNotFoundError
            from polars.exceptions import ColumnNotFoundError

            mock_read_csv.side_effect = ColumnNotFoundError("Column 'lang' not found")

            result = preflight._get_required_languages("dummy.csv", ";")

            # Should return None when ColumnNotFoundError occurs
            assert result is None

    def test_get_required_languages_general_exception(self) -> None:
        """Test _get_required_languages when a general exception is raised."""
        with patch("odoo_data_flow.lib.preflight.pl.read_csv") as mock_read_csv:
            # Make read_csv raise a general exception
            mock_read_csv.side_effect = Exception("General error")

            result = preflight._get_required_languages("dummy.csv", ";")

            # Should return None when a general exception occurs
            assert result is None

    def test_language_check_handles_get_required_languages_exception(self) -> None:
        """Test language_check when _get_required_languages raises an exception."""
        with (
            patch(
                "odoo_data_flow.lib.preflight._get_required_languages"
            ) as mock_get_req_langs,
            patch(
                "odoo_data_flow.lib.preflight._get_csv_header",
                return_value=["id", "name", "lang"],
            ),
        ):
            # Make _get_required_languages raise an exception
            mock_get_req_langs.side_effect = Exception("File read error")

            result = preflight.language_check(
                preflight_mode=PreflightMode.NORMAL,
                model="res.partner",
                filename="dummy.csv",
                config="dummy.conf",
                headless=False,
                separator=";",
            )

            # Should return True (graceful degradation when _get_required_languages fails)
            assert result is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
