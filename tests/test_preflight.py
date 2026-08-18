"""Test the pre-flight checker functions."""

from collections.abc import Generator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from polars.exceptions import ColumnNotFoundError

from fluvo.enums import PreflightMode
from fluvo.lib import preflight


@pytest.fixture
def mock_polars_read_csv() -> Generator[MagicMock, None, None]:
    """Fixture to mock polars.read_csv."""
    with patch("fluvo.lib.preflight.pl.read_csv") as mock_read:
        yield mock_read


@pytest.fixture
def mock_conf_lib() -> Generator[MagicMock, None, None]:
    """Fixture to mock conf_lib.get_connection_from_config."""
    with patch("fluvo.lib.preflight.conf_lib.get_connection_from_config") as mock_conn:
        yield mock_conn


@pytest.fixture
def mock_show_error_panel() -> Generator[MagicMock, None, None]:
    """Fixture to mock _show_error_panel."""
    with patch("fluvo.lib.preflight._show_error_panel") as mock_panel:
        yield mock_panel


@pytest.fixture
def mock_cache() -> Generator[MagicMock, None, None]:
    """Fixture to mock the cache module."""
    with patch("fluvo.lib.preflight.cache") as mock_cache_module:
        yield mock_cache_module


@pytest.fixture
def mock_show_warning_panel() -> Generator[MagicMock, None, None]:
    """Fixture to mock _show_warning_panel."""
    with patch("fluvo.lib.preflight._show_warning_panel") as mock_panel:
        yield mock_panel


class TestSelfReferencingCheck:
    """Tests for the self_referencing_check."""

    @patch("fluvo.lib.preflight.sort.sort_for_self_referencing")
    def test_check_plans_strategy_when_hierarchy_detected(
        self, mock_sort: MagicMock, tmp_path: "Path"
    ) -> None:
        """Verify the import plan is updated when a hierarchy is found."""
        sorted_file = tmp_path / "sorted.csv"
        mock_sort.return_value = str(sorted_file)
        import_plan: dict[str, Any] = {}
        result = preflight.self_referencing_check(
            preflight_mode=PreflightMode.NORMAL,
            filename="file.csv",
            import_plan=import_plan,
        )
        assert result is True
        assert import_plan["strategy"] == "sort_and_one_pass_load"
        assert import_plan["id_column"] == "id"
        assert import_plan["parent_column"] == "parent_id"
        mock_sort.assert_called_once_with(
            "file.csv", id_column="id", parent_column="parent_id", separator=";"
        )

    @patch("fluvo.lib.preflight.sort.sort_for_self_referencing")
    def test_check_does_nothing_when_no_hierarchy(self, mock_sort: MagicMock) -> None:
        """Verify the import plan is unchanged when no hierarchy is found."""
        mock_sort.return_value = None
        import_plan: dict[str, Any] = {}
        result = preflight.self_referencing_check(
            preflight_mode=PreflightMode.NORMAL,
            filename="file.csv",
            import_plan=import_plan,
        )
        assert result is True
        assert "strategy" not in import_plan

    @patch("fluvo.lib.preflight.sort.sort_for_self_referencing")
    def test_check_is_skipped_for_o2m(self, mock_sort: MagicMock) -> None:
        """Verify the check is skipped when o2m flag is True."""
        import_plan: dict[str, Any] = {}
        result = preflight.self_referencing_check(
            preflight_mode=PreflightMode.NORMAL,
            filename="file.csv",
            import_plan=import_plan,
            o2m=True,
        )
        assert result is True
        assert "strategy" not in import_plan
        mock_sort.assert_not_called()


class TestInternalHelpers:
    """Tests for internal helper functions in the preflight module."""

    @patch("fluvo.lib.preflight._show_error_panel")
    def test_get_installed_languages_connection_fails(
        self, mock_show_error_panel: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Tests that _get_installed_languages handles a connection error."""
        mock_conf_lib.side_effect = Exception("Connection Error")
        result = preflight._get_installed_languages("dummy.conf")
        assert result is None
        mock_show_error_panel.assert_called_once()
        assert "Odoo Connection Error" in mock_show_error_panel.call_args[0][0]


class TestLanguageCheck:
    """Tests for the language_check pre-flight checker."""

    def test_language_check_skips_for_other_models(self) -> None:
        """Tests that the check is skipped for models other than partner/users."""
        result = preflight.language_check(
            preflight_mode=PreflightMode.NORMAL,
            model="product.product",
            filename="",
            config="",
            headless=False,
        )
        assert result is True

    def test_language_check_skips_if_lang_column_missing(
        self, mock_polars_read_csv: MagicMock
    ) -> None:
        """Tests that the check is skipped if the 'lang' column is not present."""
        mock_polars_read_csv.return_value.get_column.side_effect = ColumnNotFoundError
        result = preflight.language_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            headless=False,
        )
        assert result is True

    def test_language_check_handles_file_read_error(
        self, mock_polars_read_csv: MagicMock
    ) -> None:
        """Tests that the check handles an error when reading the CSV."""
        mock_polars_read_csv.side_effect = Exception("Read Error")
        result = preflight.language_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            headless=False,
        )
        assert result is True

    def test_language_check_no_required_languages(
        self, mock_polars_read_csv: MagicMock
    ) -> None:
        """Tests the case where the source file contains no languages."""
        mock_df = MagicMock()
        (
            mock_df.get_column.return_value.unique.return_value.drop_nulls.return_value.filter.return_value.to_list.return_value
        ) = []
        mock_polars_read_csv.return_value = mock_df
        result = preflight.language_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            headless=False,
        )
        assert result is True

    def test_all_languages_installed(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Tests the success case where all required languages are installed."""
        mock_df = MagicMock()
        (
            mock_df.get_column.return_value.unique.return_value.drop_nulls.return_value.filter.return_value.to_list.return_value
        ) = [
            "en_US",
            "fr_FR",
        ]
        mock_polars_read_csv.return_value = mock_df

        mock_conf_lib.return_value.get_model.return_value.search_read.return_value = [
            {"code": "en_US"},
            {"code": "fr_FR"},
            {"code": "de_DE"},
        ]
        result = preflight.language_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            headless=False,
        )
        assert result is True

    @patch("fluvo.lib.preflight.language_installer.run_language_installation")
    @patch("fluvo.lib.preflight.Confirm.ask", return_value=True)
    @patch(
        "fluvo.lib.preflight._get_installed_languages",
        return_value={"en_US"},
    )
    def test_missing_languages_user_confirms_install_success(
        self,
        mock_get_langs: MagicMock,
        mock_confirm: MagicMock,
        mock_installer: MagicMock,
        mock_polars_read_csv: MagicMock,
    ) -> None:
        """Tests missing languages where user confirms and install succeeds."""
        (
            mock_polars_read_csv.return_value.get_column.return_value.unique.return_value.drop_nulls.return_value.filter.return_value.to_list.return_value
        ) = ["fr_FR"]
        mock_installer.return_value = True

        result = preflight.language_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            headless=False,
        )
        assert result is True
        mock_confirm.assert_called_once()
        mock_installer.assert_called_once_with("", ["fr_FR"])

    @patch("fluvo.lib.preflight.Confirm.ask", return_value=True)
    @patch(
        "fluvo.lib.actions.language_installer.run_language_installation",
        return_value=False,
    )
    def test_missing_languages_user_confirms_install_fails(
        self,
        mock_install: MagicMock,
        mock_confirm: MagicMock,
        mock_polars_read_csv: MagicMock,
        mock_conf_lib: MagicMock,
    ) -> None:
        """Tests missing languages where user confirms but install fails."""
        (
            mock_polars_read_csv.return_value.get_column.return_value.unique.return_value.drop_nulls.return_value.filter.return_value.to_list.return_value
        ) = ["fr_FR"]
        mock_conf_lib.return_value.get_model.return_value.search_read.return_value = [
            {"code": "en_US"}
        ]
        result = preflight.language_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            headless=False,
        )
        assert result is False
        mock_confirm.assert_called_once()
        mock_install.assert_called_once_with("", ["fr_FR"])

    @patch("fluvo.lib.preflight.language_installer.run_language_installation")
    @patch("fluvo.lib.preflight.Confirm.ask", return_value=False)
    @patch(
        "fluvo.lib.preflight._get_installed_languages",
        return_value={"en_US"},
    )
    def test_missing_languages_user_cancels(
        self,
        mock_get_langs: MagicMock,
        mock_confirm: MagicMock,
        mock_installer: MagicMock,
        mock_polars_read_csv: MagicMock,
    ) -> None:
        """Tests that the check fails if the user cancels the installation."""
        (
            mock_polars_read_csv.return_value.get_column.return_value.unique.return_value.drop_nulls.return_value.filter.return_value.to_list.return_value
        ) = ["fr_FR"]

        result = preflight.language_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            headless=False,
        )
        assert result is False
        mock_confirm.assert_called_once()
        mock_installer.assert_not_called()

    @patch("fluvo.lib.preflight.language_installer.run_language_installation")
    @patch("fluvo.lib.preflight.Confirm.ask")
    @patch(
        "fluvo.lib.preflight._get_installed_languages",
        return_value={"en_US"},
    )
    def test_missing_languages_headless_mode(
        self,
        mock_get_langs: MagicMock,
        mock_confirm: MagicMock,
        mock_installer: MagicMock,
        mock_polars_read_csv: MagicMock,
    ) -> None:
        """Tests that languages are auto-installed in headless mode."""
        (
            mock_polars_read_csv.return_value.get_column.return_value.unique.return_value.drop_nulls.return_value.filter.return_value.to_list.return_value
        ) = ["fr_FR"]
        mock_installer.return_value = True

        result = preflight.language_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="dummy.conf",
            headless=True,
        )
        assert result is True
        mock_confirm.assert_not_called()
        mock_installer.assert_called_once_with("dummy.conf", ["fr_FR"])
        # In tests/test_preflight.py

    # Replace the old test_language_check_fail_mode_skips_install with this one.
    @patch("fluvo.lib.preflight.log.debug")  # Note: patching log.debug now
    @patch("fluvo.lib.preflight.Confirm.ask")
    @patch("fluvo.lib.actions.language_installer.run_language_installation")
    def test_language_check_fail_mode_skips_entire_check(
        self,
        mock_install: MagicMock,
        mock_confirm: MagicMock,
        mock_log_debug: MagicMock,  # Renamed from mock_log_warning
        mock_polars_read_csv: MagicMock,
        mock_conf_lib: MagicMock,
    ) -> None:
        """Test the skipped language check in fail mode.

        Tests that in FAIL_MODE, the language check is skipped entirely,
        preventing file reads or Odoo calls.
        """
        # ACT: Run the check in fail mode.
        result = preflight.language_check(
            preflight_mode=PreflightMode.FAIL_MODE,
            model="res.partner",
            filename="file.csv",
            config="",
            headless=False,
        )

        # ASSERT: Check for the new, correct behavior.
        assert result is True, "The check should return True in fail mode"

        # 1. Assert that the correct debug message was logged.
        mock_log_debug.assert_called_once_with("Skipping language pre-flight check.")

        # 2. Assert that the function exited before doing any real work.
        mock_polars_read_csv.assert_not_called()
        mock_conf_lib.assert_not_called()
        mock_install.assert_not_called()
        mock_confirm.assert_not_called()


class TestDeferralAndStrategyCheck:
    """Tests for the deferral_and_strategy_check pre-flight checker."""

    def test_write_tuple_strategy_for_large_volumes(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify 'write_tuple' is chosen for many m2m links too.

        The former 'direct_relational_import' path (>= 500 links) was broken and
        removed; all many2many now route through the working write_tuple path.
        """
        mock_df_header = MagicMock()
        mock_df_header.columns = ["id", "name", "category_id"]

        # Setup a more robust mock for the chained Polars calls
        mock_df_data = MagicMock()
        (
            mock_df_data.lazy.return_value.select.return_value.select.return_value.sum.return_value.collect.return_value.item.return_value
        ) = 500
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "category_id": {
                "type": "many2many",
                "relation": "res.partner.category",
                "relation_table": "res_partner_res_partner_category_rel",
                "relation_field": "partner_id",
            },
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=True,
        )
        assert result is True
        assert "category_id" in import_plan["deferred_fields"]
        assert import_plan["strategies"]["category_id"]["strategy"] == "write_tuple"

    def test_write_tuple_strategy_when_missing_relation_info(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify 'write_tuple' is chosen when relation info is missing."""
        mock_df_header = MagicMock()
        mock_df_header.columns = ["id", "name", "category_id"]

        # Setup a more robust mock for the chained Polars calls
        mock_df_data = MagicMock()
        (
            mock_df_data.lazy.return_value.select.return_value.select.return_value.sum.return_value.collect.return_value.item.return_value
        ) = 100
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "category_id": {
                "type": "many2many",
                "relation": "res.partner.category",
                # Missing relation_table and relation_field
            },
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=True,
        )
        assert result is True
        assert "category_id" in import_plan["deferred_fields"]
        assert import_plan["strategies"]["category_id"]["strategy"] == "write_tuple"
        # Should not have relation_table or relation_field in strategy
        assert "relation" in import_plan["strategies"]["category_id"]

    def test_write_tuple_strategy_for_small_volumes(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify 'write_tuple' is chosen for fewer m2m links."""
        mock_df_header = MagicMock()
        mock_df_header.columns = ["id", "name", "category_id"]

        # Setup a more robust mock for the chained Polars calls
        mock_df_data = MagicMock()
        (
            mock_df_data.lazy.return_value.select.return_value.select.return_value.sum.return_value.collect.return_value.item.return_value
        ) = 499
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "category_id": {
                "type": "many2many",
                "relation": "res.partner.category",
                "relation_table": "res_partner_res_partner_category_rel",
                "relation_field": "partner_id",
            },
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=True,
        )
        assert result is True
        assert "category_id" in import_plan["deferred_fields"]
        assert import_plan["strategies"]["category_id"]["strategy"] == "write_tuple"

    def test_self_referencing_m2o_is_deferred(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify self-referencing many2one fields are deferred."""
        mock_df_header = MagicMock()
        mock_df_header.columns = ["id", "name", "parent_id"]
        mock_df_data = MagicMock()
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "parent_id": {"type": "many2one", "relation": "res.partner"},
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=True,
        )
        assert result is True
        assert "parent_id" in import_plan["deferred_fields"]

    def test_required_relational_field_is_recorded(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """A required relational field is reported in required_relational_fields.

        The importer relies on this to refuse deferring required relations.
        """
        mock_df_header = MagicMock()
        mock_df_header.columns = ["id", "name", "country_id/id"]
        mock_df_data = MagicMock()
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "country_id": {
                "type": "many2one",
                "relation": "res.country",
                "required": True,
            },
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.country.state",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=True,
        )
        assert result is True
        assert import_plan["required_relational_fields"] == ["country_id"]
        # A required m2o must not be auto-deferred.
        assert "country_id" not in import_plan.get("deferred_fields", [])

    def test_auto_detects_unique_id_field(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify 'id' is automatically chosen as the unique id field."""
        mock_df_header = MagicMock()
        mock_df_header.columns = ["id", "name", "parent_id"]
        mock_df_data = MagicMock()
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "parent_id": {"type": "many2one", "relation": "res.partner"},
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=True,
        )
        assert result is True
        assert import_plan["unique_id_field"] == "id"

    def test_error_if_no_unique_id_field_for_deferrals(
        self,
        mock_polars_read_csv: MagicMock,
        mock_conf_lib: MagicMock,
        mock_show_error_panel: MagicMock,
    ) -> None:
        """Verify an error is shown if deferrals exist but no 'id' column."""
        mock_df_header = MagicMock()
        mock_df_header.columns = ["name", "parent_id"]
        mock_df_data = MagicMock()
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "name": {"type": "char"},
            "parent_id": {"type": "many2one", "relation": "res.partner"},
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=True,
        )
        assert result is False
        mock_show_error_panel.assert_called_once()
        assert "Action Required" in mock_show_error_panel.call_args[0][0]


class TestAutoDeferMode:
    """Tests for the auto-defer mode in preflight checks."""

    def test_auto_defer_defers_non_required_m2o_fields(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify auto_defer=True defers all non-required m2o fields."""
        mock_df_header = MagicMock()
        mock_df_header.columns = ["id", "name", "user_id", "country_id"]
        mock_df_data = MagicMock()
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "user_id": {
                "type": "many2one",
                "relation": "res.users",
                "required": False,
            },
            "country_id": {
                "type": "many2one",
                "relation": "res.country",
                "required": False,
            },
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=True,
        )
        assert result is True
        assert "user_id" in import_plan["deferred_fields"]
        assert "country_id" in import_plan["deferred_fields"]

    def test_auto_defer_skips_required_m2o_fields(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify auto_defer=True does NOT defer required m2o fields."""
        mock_df_header = MagicMock()
        mock_df_header.columns = ["id", "name", "company_id", "user_id"]
        mock_df_data = MagicMock()
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "company_id": {
                "type": "many2one",
                "relation": "res.company",
                "required": True,  # Required field - should NOT be deferred
            },
            "user_id": {
                "type": "many2one",
                "relation": "res.users",
                "required": False,  # Not required - should be deferred
            },
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=True,
        )
        assert result is True
        # Only user_id should be deferred, not company_id
        assert "user_id" in import_plan["deferred_fields"]
        assert "company_id" not in import_plan["deferred_fields"]

    def test_auto_defer_false_does_not_defer_m2o_fields(
        self, mock_polars_read_csv: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify auto_defer=False does NOT defer non-self-referencing m2o fields."""
        mock_df_header = MagicMock()
        mock_df_header.columns = ["id", "name", "user_id"]
        mock_df_data = MagicMock()
        mock_polars_read_csv.side_effect = [mock_df_header, mock_df_data]

        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "user_id": {
                "type": "many2one",
                "relation": "res.users",
                "required": False,
            },
        }
        import_plan: dict[str, Any] = {}
        result = preflight.deferral_and_strategy_check(
            preflight_mode=PreflightMode.NORMAL,
            model="res.partner",
            filename="file.csv",
            config="",
            import_plan=import_plan,
            auto_defer=False,
        )
        assert result is True
        # Without auto_defer, non-self-referencing m2o fields should NOT be deferred
        assert "deferred_fields" not in import_plan or "user_id" not in import_plan.get(
            "deferred_fields", []
        )


class TestGetOdooFields:
    """Tests for the _get_odoo_fields helper function."""

    def test_get_odoo_fields_cache_hit(
        self, mock_cache: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify fields are returned from cache and Odoo is not called."""
        mock_cache.load_fields_get_cache.return_value = {"name": {"type": "char"}}
        result = preflight._get_odoo_fields("dummy.conf", "res.partner")

        assert result == {"name": {"type": "char"}}
        mock_cache.load_fields_get_cache.assert_called_once_with(
            "dummy.conf", "res.partner"
        )
        mock_conf_lib.assert_not_called()

    def test_get_odoo_fields_cache_miss(
        self, mock_cache: MagicMock, mock_conf_lib: MagicMock
    ) -> None:
        """Verify fields are fetched from Odoo and cached on a cache miss."""
        mock_cache.load_fields_get_cache.return_value = None
        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.fields_get.return_value = {"name": {"type": "char"}}

        result = preflight._get_odoo_fields("dummy.conf", "res.partner")

        assert result == {"name": {"type": "char"}}
        mock_cache.load_fields_get_cache.assert_called_once_with(
            "dummy.conf", "res.partner"
        )
        mock_conf_lib.return_value.get_model.assert_called_once_with("res.partner")
        mock_model.fields_get.assert_called_once()
        mock_cache.save_fields_get_cache.assert_called_once_with(
            "dummy.conf", "res.partner", {"name": {"type": "char"}}
        )

    def test_get_odoo_fields_odoo_error(
        self,
        mock_cache: MagicMock,
        mock_conf_lib: MagicMock,
        mock_show_error_panel: MagicMock,
    ) -> None:
        """Verify None is returned and error is shown when Odoo call fails."""
        mock_cache.load_fields_get_cache.return_value = None
        mock_conf_lib.side_effect = Exception("Odoo Error")

        result = preflight._get_odoo_fields("dummy.conf", "res.partner")

        assert result is None
        mock_show_error_panel.assert_called_once()
        assert "Odoo Connection Error" in mock_show_error_panel.call_args[0][0]
        mock_cache.save_fields_get_cache.assert_not_called()


class TestValidateHeader:
    """Tests for the _validate_header function."""

    def test_validate_header_passes_with_valid_fields(self) -> None:
        """Verify _validate_header passes with all valid fields."""
        csv_header = ["id", "name", "email"]
        odoo_fields = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "email": {"type": "char"},
        }

        result = preflight._validate_header(csv_header, odoo_fields, "res.partner")
        assert result is True

    def test_validate_header_fails_with_invalid_fields(
        self, mock_show_error_panel: MagicMock
    ) -> None:
        """Verify _validate_header fails and shows error for invalid fields."""
        csv_header = ["id", "name", "invalid_field"]
        odoo_fields = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
        }

        result = preflight._validate_header(csv_header, odoo_fields, "res.partner")
        assert result is False
        mock_show_error_panel.assert_called_once()
        call_args = mock_show_error_panel.call_args
        assert call_args[0][0] == "Invalid Fields Found"
        assert "invalid_field" in call_args[0][1]

    def test_validate_header_passes_with_external_id_fields(self) -> None:
        """Verify _validate_header passes with external ID fields."""
        csv_header = ["id", "name", "parent_id/id", "category_id/id"]
        odoo_fields = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "parent_id": {"type": "many2one", "relation": "res.partner"},
            "category_id": {"type": "many2many", "relation": "res.partner.category"},
        }

        result = preflight._validate_header(csv_header, odoo_fields, "res.partner")
        assert result is True

    def test_validate_header_warns_about_readonly_fields(
        self, mock_show_warning_panel: MagicMock
    ) -> None:
        """Verify _validate_header warns about readonly fields."""
        csv_header = ["id", "name", "display_name"]
        odoo_fields = {
            "id": {"type": "integer", "readonly": True, "store": True},
            "name": {"type": "char", "readonly": False, "store": True},
            "display_name": {"type": "char", "readonly": True, "store": False},
        }

        result = preflight._validate_header(csv_header, odoo_fields, "res.partner")
        assert result is True
        mock_show_warning_panel.assert_called_once()
        call_args = mock_show_warning_panel.call_args
        assert call_args[0][0] == "ReadOnly Fields Detected"
        assert "display_name" in call_args[0][1]
        assert "non-stored" in call_args[0][1]
        # 'id' field should NOT be in the warning (it's mandatory for imports)
        assert "'id'" not in call_args[0][1]

    def test_validate_header_warns_about_multiple_readonly_fields(
        self, mock_show_warning_panel: MagicMock
    ) -> None:
        """Verify _validate_header warns about multiple readonly fields."""
        csv_header = ["id", "name", "display_name", "commercial_company_name"]
        odoo_fields = {
            "id": {"type": "integer", "readonly": True, "store": True},
            "name": {"type": "char", "readonly": False, "store": True},
            "display_name": {"type": "char", "readonly": True, "store": False},
            "commercial_company_name": {
                "type": "char",
                "readonly": True,
                "store": True,
            },
        }

        result = preflight._validate_header(csv_header, odoo_fields, "res.partner")
        assert result is True
        mock_show_warning_panel.assert_called_once()
        call_args = mock_show_warning_panel.call_args
        assert call_args[0][0] == "ReadOnly Fields Detected"
        assert "display_name" in call_args[0][1]
        assert "commercial_company_name" in call_args[0][1]
        assert "non-stored" in call_args[0][1]
        assert "1 non-stored readonly" in call_args[0][1]
        # 'id' field should NOT be in the warning (it's mandatory for imports)
        assert "'id'" not in call_args[0][1]


class TestXmlidCollisionCheck:
    """Tests for the xmlid_collision_check pre-flight check (#10)."""

    @staticmethod
    def _write(tmp_path: Path, content: str) -> str:
        f = tmp_path / "src.csv"
        f.write_text(content)
        return str(f)

    @patch("fluvo.lib.preflight._show_error_panel")
    def test_collision_aborts(self, mock_panel: MagicMock, tmp_path: Path) -> None:
        """Distinct ids sanitizing to one xmlid abort before any write."""
        # "a b" and "a,b" both sanitize to "a_b".
        path = self._write(tmp_path, "id;name\na b;one\na,b;two\n")
        result = preflight.xmlid_collision_check(
            preflight_mode=PreflightMode.NORMAL,
            filename=path,
            import_plan={},
            separator=";",
            unique_id_field="id",
        )
        assert result is False
        mock_panel.assert_called_once()

    @patch("fluvo.lib.preflight._show_error_panel")
    def test_collision_opt_out_proceeds(
        self, mock_panel: MagicMock, tmp_path: Path
    ) -> None:
        """--allow-xmlid-collisions downgrades the abort to a warning."""
        path = self._write(tmp_path, "id;name\na b;one\na,b;two\n")
        result = preflight.xmlid_collision_check(
            preflight_mode=PreflightMode.NORMAL,
            filename=path,
            import_plan={},
            separator=";",
            unique_id_field="id",
            allow_xmlid_collisions=True,
        )
        assert result is True
        mock_panel.assert_not_called()

    def test_no_collision_passes(self, tmp_path: Path) -> None:
        """Distinct, non-colliding ids pass."""
        path = self._write(tmp_path, "id;name\nrec1;one\nrec2;two\n")
        assert (
            preflight.xmlid_collision_check(
                preflight_mode=PreflightMode.NORMAL,
                filename=path,
                import_plan={},
                separator=";",
                unique_id_field="id",
            )
            is True
        )

    @patch("fluvo.lib.preflight.log.warning")
    def test_blank_id_with_relations_warns(
        self, mock_warning: MagicMock, tmp_path: Path
    ) -> None:
        """A blank id carrying relational data warns (Pass 2 can't link it)."""
        path = self._write(tmp_path, "id;tag_ids\n;t1\n")
        result = preflight.xmlid_collision_check(
            preflight_mode=PreflightMode.NORMAL,
            filename=path,
            import_plan={"strategies": {"tag_ids": {"strategy": "write_tuple"}}},
            separator=";",
            unique_id_field="id",
        )
        assert result is True
        assert any("blank" in str(c).lower() for c in mock_warning.call_args_list)

    @patch("fluvo.lib.preflight.log.warning")
    def test_blank_id_without_relations_is_ok(
        self, mock_warning: MagicMock, tmp_path: Path
    ) -> None:
        """A blank id with no relational data is fine (a plain create)."""
        path = self._write(tmp_path, "id;name\n;plain\n")
        result = preflight.xmlid_collision_check(
            preflight_mode=PreflightMode.NORMAL,
            filename=path,
            import_plan={},
            separator=";",
            unique_id_field="id",
        )
        assert result is True
        assert not any("blank" in str(c).lower() for c in mock_warning.call_args_list)


class TestCompanyContextCheck:
    """The #255 guard against silently importing into the wrong company."""

    def _run(
        self,
        fields: dict[str, Any],
        header: list[str],
        plan: dict[str, Any],
        company_count: Any = 3,
        **kwargs: Any,
    ) -> tuple[bool, MagicMock, MagicMock]:
        """Run company_context_check with the RPC/IO helpers stubbed out."""
        with (
            patch("fluvo.lib.preflight._get_csv_header", return_value=header),
            patch("fluvo.lib.preflight._get_odoo_fields", return_value=fields),
            patch("fluvo.lib.preflight._count_data_rows", return_value=42),
            patch("fluvo.lib.preflight._count_companies", return_value=company_count),
            patch(
                "fluvo.lib.preflight._resolve_default_company",
                return_value=(1, "YourCo NV"),
            ),
            patch(
                "fluvo.lib.preflight._resolve_company_names",
                return_value={3: "Acme BE"},
            ),
            patch("fluvo.lib.preflight._show_warning_panel") as warn,
            patch("fluvo.lib.preflight._show_error_panel") as err,
        ):
            result = preflight.company_context_check(
                preflight_mode=PreflightMode.NORMAL,
                model="res.partner",
                filename="x.csv",
                config="conn.conf",
                import_plan=plan,
                separator=";",
                encoding="utf-8",
                **kwargs,
            )
        return result, warn, err

    def test_not_company_aware_is_noop(self) -> None:
        """A model with no company_id/company-dependent fields is left alone."""
        plan: dict[str, Any] = {}
        result, warn, err = self._run({"name": {"type": "char"}}, ["id", "name"], plan)
        assert result is True
        assert "company_context" not in plan
        warn.assert_not_called()
        err.assert_not_called()

    def test_aborts_by_default_when_multi_company_and_unset(self) -> None:
        """Multi-company DB, no --company-id: abort with a clear error (default)."""
        plan: dict[str, Any] = {}
        result, warn, err = self._run(
            {"company_id": {"type": "many2one"}},
            ["id", "name"],
            plan,
            company_count=3,
            context={},
        )
        assert result is False
        err.assert_called_once()
        warn.assert_not_called()
        assert plan["company_context"]["explicit"] is False

    def test_unknown_company_count_treated_as_multi(self) -> None:
        """If the company count can't be determined, err on the safe side (abort)."""
        plan: dict[str, Any] = {}
        result, _, err = self._run(
            {"company_id": {"type": "many2one"}},
            ["id", "name"],
            plan,
            company_count=None,
            context={},
        )
        assert result is False
        err.assert_called_once()

    def test_single_company_db_proceeds_quietly(self) -> None:
        """A single-company DB has no ambiguity: proceed, no warn/abort."""
        plan: dict[str, Any] = {}
        result, warn, err = self._run(
            {"company_id": {"type": "many2one"}},
            ["id", "name"],
            plan,
            company_count=1,
            context={},
        )
        assert result is True
        warn.assert_not_called()
        err.assert_not_called()
        assert plan["company_context"]["explicit"] is False
        assert "only company" in plan["company_context"]["line"]

    def test_allow_default_company_warns_and_proceeds(self) -> None:
        """--allow-default-company: warn (naming the default) and proceed."""
        plan: dict[str, Any] = {}
        result, warn, err = self._run(
            {"company_id": {"type": "many2one"}},
            ["id", "name"],
            plan,
            company_count=3,
            context={},
            allow_default_company=True,
        )
        assert result is True
        err.assert_not_called()
        warn.assert_called_once()
        assert 'company 1 "YourCo NV"' in plan["company_context"]["line"]

    def test_company_dependent_field_triggers_guard(self) -> None:
        """A company-dependent column (not company_id) also triggers the guard."""
        plan: dict[str, Any] = {}
        result, _, err = self._run(
            {"standard_price": {"type": "float", "company_dependent": True}},
            ["id", "standard_price"],
            plan,
            company_count=2,
            context={},
        )
        assert result is False
        err.assert_called_once()

    def test_explicit_company_is_recorded_without_warning(self) -> None:
        """When a company is chosen, record it in the plan and don't warn/abort."""
        plan: dict[str, Any] = {}
        result, warn, err = self._run(
            {"company_id": {"type": "many2one"}},
            ["id", "name"],
            plan,
            context={"allowed_company_ids": [3]},
        )
        assert result is True
        warn.assert_not_called()
        err.assert_not_called()
        assert plan["company_context"]["explicit"] is True
        assert 'Company: 3 "Acme BE"' in plan["company_context"]["line"]


class TestExtractOdooErrorMessage:
    """Concise message extraction from Odoo RPC faults (#252)."""

    def test_dict_fault_message(self) -> None:
        """A fault dict in args[0] yields its 'message'."""
        err = Exception({"code": 200, "message": "Object x.y doesn't exist"})
        assert preflight._extract_odoo_error_message(err) == "Object x.y doesn't exist"

    def test_stringified_fault_message(self) -> None:
        """A stringified fault dict has its message pulled out."""
        err = Exception(
            "{'code': 200, 'message': \"Object product.attribute.line doesn't exist\"}"
        )
        assert (
            preflight._extract_odoo_error_message(err)
            == "Object product.attribute.line doesn't exist"
        )

    def test_plain_error_falls_back_to_first_line(self) -> None:
        """A plain error returns its first line."""
        err = Exception("Something broke\nsecond line\nthird")
        assert preflight._extract_odoo_error_message(err) == "Something broke"


class TestGetOdooFieldsModelNotFound:
    """A non-existent model shows a friendly 'Model Not Found' panel (#252)."""

    @patch("fluvo.lib.preflight._show_error_panel")
    @patch("fluvo.lib.preflight.conf_lib.get_connection_from_config")
    @patch("fluvo.lib.preflight.cache.load_fields_get_cache", return_value=None)
    def test_model_not_found_panel(
        self,
        _mock_cache: MagicMock,
        mock_conn: MagicMock,
        mock_panel: MagicMock,
    ) -> None:
        """A model-not-found fault shows the friendly panel, not a traceback."""
        conn = MagicMock()
        model_obj = MagicMock()
        model_obj.fields_get.side_effect = Exception(
            "{'message': \"Object product.attribute.line doesn't exist\"}"
        )
        conn.get_model.return_value = model_obj
        mock_conn.return_value = conn

        result = preflight._get_odoo_fields("conn.conf", "product.attribute.line")

        assert result is None
        title, body = mock_panel.call_args[0][0], mock_panel.call_args[0][1]
        assert title == "Model Not Found"
        assert "product.attribute.line" in body
        # The raw server traceback must NOT be dumped into the panel.
        assert "Traceback" not in body

    @patch("fluvo.lib.preflight._show_error_panel")
    @patch("fluvo.lib.preflight.conf_lib.get_connection_from_config")
    @patch("fluvo.lib.preflight.cache.load_fields_get_cache", return_value=None)
    def test_other_error_uses_connection_panel(
        self,
        _mock_cache: MagicMock,
        mock_conn: MagicMock,
        mock_panel: MagicMock,
    ) -> None:
        """A non-model error still uses the generic connection-error panel."""
        conn = MagicMock()
        model_obj = MagicMock()
        model_obj.fields_get.side_effect = Exception("connection reset by peer")
        conn.get_model.return_value = model_obj
        mock_conn.return_value = conn

        result = preflight._get_odoo_fields("conn.conf", "res.partner")

        assert result is None
        assert mock_panel.call_args[0][0] == "Odoo Connection Error"


class TestStructuredValueCheck:
    """Reject dict/list values written to plain-text fields (#274)."""

    def _run(
        self,
        tmp_path: "Path",
        fields: dict[str, Any],
        header: list[str],
        rows: list[list[str]],
        **kwargs: Any,
    ) -> tuple[bool, MagicMock]:
        """Write a CSV, stub fields_get, and run structured_value_check."""
        csv_path = tmp_path / "data.csv"
        lines = [";".join(header)] + [";".join(r) for r in rows]
        csv_path.write_text("\n".join(lines) + "\n")
        with (
            patch("fluvo.lib.preflight._get_odoo_fields", return_value=fields),
            patch("fluvo.lib.preflight._show_error_panel") as err,
        ):
            result = preflight.structured_value_check(
                preflight_mode=PreflightMode.NORMAL,
                model="product.template",
                filename=str(csv_path),
                config="conn.conf",
                separator=";",
                encoding="utf-8",
                **kwargs,
            )
        return result, err

    def test_dict_on_translated_field_is_rejected(self, tmp_path: "Path") -> None:
        """A dict on a translated field aborts and points at #254."""
        result, err = self._run(
            tmp_path,
            {"name": {"type": "char", "translate": True}},
            ["id", "name"],
            [["p1", "{'en_US': 'Chair', 'nl_NL': 'Stoel'}"]],
        )
        assert result is False
        err.assert_called_once()
        title, body = err.call_args[0][0], err.call_args[0][1]
        assert title == "Invalid structured value"
        assert "translated" in body
        assert "254" in body
        assert "name" in body

    def test_dict_on_plain_char_is_rejected(self, tmp_path: "Path") -> None:
        """A dict on a non-translated char is also rejected (plain message)."""
        result, err = self._run(
            tmp_path,
            {"name": {"type": "char", "translate": False}},
            ["id", "name"],
            [["p1", "{'a': 1}"]],
        )
        assert result is False
        err.assert_called_once()
        assert "plain text" in err.call_args[0][1]
        assert "254" not in err.call_args[0][1]

    def test_list_on_char_is_rejected(self, tmp_path: "Path") -> None:
        """A list value on a char field is rejected."""
        result, err = self._run(
            tmp_path,
            {"name": {"type": "char"}},
            ["id", "name"],
            [["p1", "['a', 'b']"]],
        )
        assert result is False
        err.assert_called_once()

    def test_json_object_string_is_rejected(self, tmp_path: "Path") -> None:
        """A raw JSON-object string in the column is rejected too."""
        result, err = self._run(
            tmp_path,
            {"description": {"type": "text", "translate": True}},
            ["id", "description"],
            [["p1", '{"en_US": "Chair"}']],
        )
        assert result is False
        err.assert_called_once()

    def test_plain_strings_pass(self, tmp_path: "Path") -> None:
        """Ordinary text values are not flagged (no false positives)."""
        result, err = self._run(
            tmp_path,
            {"name": {"type": "char", "translate": True}},
            ["id", "name"],
            [["p1", "Chair"], ["p2", "Bureaustoel, zwart"]],
        )
        assert result is True
        err.assert_not_called()

    def test_non_string_fields_are_ignored(self, tmp_path: "Path") -> None:
        """Columns that aren't char/text/html are not scanned."""
        result, err = self._run(
            tmp_path,
            {"list_price": {"type": "float"}},
            ["id", "list_price"],
            [["p1", "{'x': 1}"]],
        )
        assert result is True
        err.assert_not_called()

    def test_ignored_column_is_skipped(self, tmp_path: "Path") -> None:
        """A column in the ignore list is not scanned."""
        result, err = self._run(
            tmp_path,
            {"name": {"type": "char", "translate": True}},
            ["id", "name"],
            [["p1", "{'en_US': 'Chair'}"]],
            ignore=["name"],
        )
        assert result is True
        err.assert_not_called()


class TestTranslationColumnsCheck:
    """Tests for translation_columns_check (#254, field@lang detection)."""

    def _write(self, tmp_path: Path, header: str) -> str:
        src = tmp_path / "data.csv"
        src.write_text(f"{header}\nx1,val\n")
        return str(src)

    @patch("fluvo.lib.preflight._get_installed_languages")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_detects_and_records_plan(
        self,
        mock_fields: MagicMock,
        mock_langs: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A field@lang column on a translatable field is recorded in the plan."""
        mock_fields.return_value = {
            "id": {"type": "char"},
            "name": {"type": "char", "translate": True},
        }
        mock_langs.return_value = {"nl_NL", "en_US"}
        filename = self._write(tmp_path, "id,name@nl_NL")
        plan: dict[str, Any] = {}
        ok = preflight.translation_columns_check(
            PreflightMode.NORMAL, "res.partner", filename, "c.conf", plan, separator=","
        )
        assert ok is True
        assert plan["translations"] == {"nl_NL": ["name"]}
        assert plan["translation_columns"] == ["name@nl_NL"]

    @patch("fluvo.lib.preflight._get_installed_languages")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_no_at_columns_is_noop(
        self,
        mock_fields: MagicMock,
        mock_langs: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Without any @ column the check passes and records nothing."""
        filename = self._write(tmp_path, "id,name")
        plan: dict[str, Any] = {}
        ok = preflight.translation_columns_check(
            PreflightMode.NORMAL, "res.partner", filename, "c.conf", plan, separator=","
        )
        assert ok is True
        assert "translations" not in plan
        mock_fields.assert_not_called()
        mock_langs.assert_not_called()

    @patch("fluvo.lib.preflight._show_error_panel")
    @patch("fluvo.lib.preflight._get_installed_languages")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_uninstalled_language_aborts(
        self,
        mock_fields: MagicMock,
        mock_langs: MagicMock,
        mock_panel: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A field@lang for a language that isn't installed fails the check."""
        mock_fields.return_value = {
            "id": {"type": "char"},
            "name": {"type": "char", "translate": True},
        }
        mock_langs.return_value = {"en_US"}
        filename = self._write(tmp_path, "id,name@fr_FR")
        plan: dict[str, Any] = {}
        ok = preflight.translation_columns_check(
            PreflightMode.NORMAL, "res.partner", filename, "c.conf", plan, separator=","
        )
        assert ok is False
        mock_panel.assert_called_once()

    @patch("fluvo.lib.preflight._show_error_panel")
    @patch("fluvo.lib.preflight._get_installed_languages")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_non_translatable_at_column_aborts(
        self,
        mock_fields: MagicMock,
        mock_langs: MagicMock,
        mock_panel: MagicMock,
        tmp_path: Path,
    ) -> None:
        """An @ qualifier on a non-translatable, non-company field is rejected."""
        mock_fields.return_value = {
            "id": {"type": "char"},
            "ref": {"type": "char"},
        }
        filename = self._write(tmp_path, "id,ref@nl_NL")
        plan: dict[str, Any] = {}
        ok = preflight.translation_columns_check(
            PreflightMode.NORMAL, "res.partner", filename, "c.conf", plan, separator=","
        )
        assert ok is False
        mock_panel.assert_called_once()

    @patch("fluvo.lib.preflight._get_installed_languages")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_company_dependent_at_column_is_left_to_company_check(
        self,
        mock_fields: MagicMock,
        mock_langs: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A field@company on a company-dependent field is ignored here (#255 pt2).

        Ownership moved to company_columns_check: the translation check must skip
        it (not reject it) and record no translations.
        """
        mock_fields.return_value = {
            "id": {"type": "char"},
            "standard_price": {"type": "float", "company_dependent": True},
        }
        filename = self._write(tmp_path, "id,standard_price@2")
        plan: dict[str, Any] = {}
        ok = preflight.translation_columns_check(
            PreflightMode.NORMAL,
            "product.template",
            filename,
            "c.conf",
            plan,
            separator=",",
        )
        assert ok is True
        assert "translations" not in plan

    @patch("fluvo.lib.preflight._get_installed_languages")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_installed_langs_unverifiable_still_records(
        self,
        mock_fields: MagicMock,
        mock_langs: MagicMock,
        tmp_path: Path,
    ) -> None:
        """If the installed-language list can't be fetched, detection still proceeds."""
        mock_fields.return_value = {
            "id": {"type": "char"},
            "name": {"type": "char", "translate": True},
        }
        mock_langs.return_value = None  # connection couldn't fetch languages
        filename = self._write(tmp_path, "id,name@nl_NL")
        plan: dict[str, Any] = {}
        ok = preflight.translation_columns_check(
            PreflightMode.NORMAL, "res.partner", filename, "c.conf", plan, separator=","
        )
        assert ok is True
        assert plan["translations"] == {"nl_NL": ["name"]}


class TestCompanyColumnsCheck:
    """Tests for company_columns_check (#255 part 2, field@company detection)."""

    def _write(self, tmp_path: Path, header: str) -> str:
        src = tmp_path / "data.csv"
        src.write_text(f"{header}\nx1,val\n")
        return str(src)

    def _conn(self, *, company_ids: set[int], xmlids: dict[str, int]) -> MagicMock:
        """A fake connection resolving res.company reads + ir.model.data lookups."""
        conn = MagicMock()

        def get_model(name: str) -> MagicMock:
            m = MagicMock()
            if name == "res.company":

                def read(ids, fields=None, context=None):  # type: ignore[no-untyped-def]
                    cid = ids[0] if isinstance(ids, list) else ids
                    if cid in company_ids:
                        return [{"id": cid, "name": f"Company {cid}"}]
                    return []

                m.read.side_effect = read
            elif name == "ir.model.data":

                def search_read(domain, fields=None, context=None):  # type: ignore[no-untyped-def]
                    crit = {f: v for (f, _op, v) in domain}
                    key = f"{crit.get('module')}.{crit.get('name')}"
                    rid = xmlids.get(key)
                    return [{"res_id": rid}] if rid else []

                m.search_read.side_effect = search_read
            return m

        conn.get_model.side_effect = get_model
        return conn

    FIELDS: ClassVar[dict[str, dict[str, Any]]] = {
        "id": {"type": "char"},
        "name": {"type": "char", "translate": True},
        "standard_price": {"type": "float", "company_dependent": True},
        "ref": {"type": "char"},
    }

    @patch("fluvo.lib.preflight._preflight_connection")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_detects_company_columns_by_db_id(
        self, mock_fields: MagicMock, mock_conn: MagicMock, tmp_path: Path
    ) -> None:
        """A field@<db id> on a company-dependent field is recorded in the plan."""
        mock_fields.return_value = self.FIELDS
        mock_conn.return_value = self._conn(company_ids={1, 2}, xmlids={})
        filename = self._write(tmp_path, "id,standard_price@1,standard_price@2")
        plan: dict[str, Any] = {}
        ok = preflight.company_columns_check(
            PreflightMode.NORMAL,
            "product.template",
            filename,
            "c.conf",
            plan,
            separator=",",
        )
        assert ok is True
        assert plan["company_fields"] == {1: ["standard_price"], 2: ["standard_price"]}
        assert plan["company_column_map"] == {
            "standard_price@1": 1,
            "standard_price@2": 2,
        }

    @patch("fluvo.lib.preflight._preflight_connection")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_resolves_company_xmlid(
        self, mock_fields: MagicMock, mock_conn: MagicMock, tmp_path: Path
    ) -> None:
        """A field@<module.name> external id resolves to the company db id."""
        mock_fields.return_value = self.FIELDS
        mock_conn.return_value = self._conn(
            company_ids={5}, xmlids={"base.company_de": 5}
        )
        filename = self._write(tmp_path, "id,standard_price@base.company_de")
        plan: dict[str, Any] = {}
        ok = preflight.company_columns_check(
            PreflightMode.NORMAL,
            "product.template",
            filename,
            "c.conf",
            plan,
            separator=",",
        )
        assert ok is True
        assert plan["company_fields"] == {5: ["standard_price"]}

    @patch("fluvo.lib.preflight._show_error_panel")
    @patch("fluvo.lib.preflight._preflight_connection")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_unknown_company_aborts(
        self,
        mock_fields: MagicMock,
        mock_conn: MagicMock,
        mock_panel: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A company reference that resolves to nothing fails the check."""
        mock_fields.return_value = self.FIELDS
        mock_conn.return_value = self._conn(company_ids={1}, xmlids={})
        filename = self._write(tmp_path, "id,standard_price@99")
        plan: dict[str, Any] = {}
        ok = preflight.company_columns_check(
            PreflightMode.NORMAL,
            "product.template",
            filename,
            "c.conf",
            plan,
            separator=",",
        )
        assert ok is False
        mock_panel.assert_called_once()

    @patch("fluvo.lib.preflight._preflight_connection")
    @patch("fluvo.lib.preflight._get_odoo_fields")
    def test_translatable_at_column_is_left_to_translation_check(
        self, mock_fields: MagicMock, mock_conn: MagicMock, tmp_path: Path
    ) -> None:
        """A field@lang on a translatable field is ignored here (owned elsewhere)."""
        mock_fields.return_value = self.FIELDS
        mock_conn.return_value = self._conn(company_ids={1}, xmlids={})
        filename = self._write(tmp_path, "id,name@nl_NL")
        plan: dict[str, Any] = {}
        ok = preflight.company_columns_check(
            PreflightMode.NORMAL,
            "res.partner",
            filename,
            "c.conf",
            plan,
            separator=",",
        )
        assert ok is True
        assert "company_fields" not in plan
        mock_conn.assert_not_called()  # no company resolution needed
