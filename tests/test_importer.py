"""Test the main importer orchestrator."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fluvo.importer import (
    _count_lines,
    _get_env_from_config,
    _get_fail_filename,
    _infer_model_from_filename,
    run_import,
    run_import_for_migration,
)


class TestFilenameUtils:
    """Tests for filename and path utility functions."""

    def test_count_lines(self, tmp_path: Path) -> None:
        """Test that line counting works correctly."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("line1\nline2\nline3")
        assert _count_lines(str(file_path)) == 3

    def test_infer_model_from_filename(self) -> None:
        """Test model name inference from various filename formats."""
        assert _infer_model_from_filename("res_partner.csv") == "res.partner"
        assert _infer_model_from_filename("sale_order_line.csv") == "sale.order.line"
        assert _infer_model_from_filename("x_custom_model.csv") == "x.custom.model"
        assert _infer_model_from_filename("res_partner_fail.csv") == "res.partner"
        assert _infer_model_from_filename("res_users_123.csv") == "res.users"

    def test_get_fail_filename_recovery_mode(self) -> None:
        """Tests that _get_fail_filename returns same name regardless of mode (#182).

        The fail file is always the same name so it gets overwritten instead of
        accumulating timestamped copies.
        """
        filename = _get_fail_filename("res.partner", is_fail_run=True)
        assert filename == "res_partner_fail.csv"
        # Verify same result regardless of is_fail_run flag
        assert _get_fail_filename("res.partner", False) == filename


class TestEnvFromConfig:
    """Tests for environment name extraction from config files."""

    def test_get_env_from_config_with_connection_suffix(self) -> None:
        """Test extracting env name from config with _connection suffix."""
        assert _get_env_from_config("test_connection.conf") == "test"
        assert _get_env_from_config("uat_connection.conf") == "uat"
        assert _get_env_from_config("prod_connection.conf") == "prod"

    def test_get_env_from_config_without_suffix(self) -> None:
        """Test extracting env name from config without suffix."""
        assert _get_env_from_config("test.conf") == "test"
        assert _get_env_from_config("uat.conf") == "uat"

    def test_get_env_from_config_with_path(self) -> None:
        """Test extracting env name from full path."""
        assert _get_env_from_config("/path/to/test_connection.conf") == "test"
        assert _get_env_from_config("configs/uat.conf") == "uat"

    def test_get_env_from_config_dict(self) -> None:
        """Test extracting env name from config dict."""
        assert _get_env_from_config({"_config_file": "test_connection.conf"}) == "test"
        assert _get_env_from_config({"_config_file": "uat.conf"}) == "uat"

    def test_get_env_from_config_dict_without_file(self) -> None:
        """Test that config dict without _config_file returns None."""
        assert _get_env_from_config({"hostname": "localhost"}) is None

    def test_get_env_from_config_none(self) -> None:
        """Test that None config returns None."""
        assert _get_env_from_config(None) is None


class TestFailFilePath:
    """Tests for fail file path resolution with environment-specific folders."""

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_fail_file_uses_env_folder(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that fail file is placed in environment-specific folder."""
        # Create data directory with source file
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        source_file = data_dir / "res_partner.csv"
        source_file.write_text("id,name\n1,Test\n")

        mock_preflight.return_value = True
        mock_import_data.return_value = (True, {"total_records": 1})

        # Run import with uat_connection.conf - should place fail file in data/uat/
        run_import(
            config="uat_connection.conf",
            filename=str(source_file),
            model="res.partner",
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=False,
            headless=False,
            worker=1,
            batch_size=10,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            context="{}",
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        # Verify the fail_file path is in the uat subfolder
        call_args = mock_import_data.call_args
        fail_file_arg = call_args.kwargs.get("fail_file") or call_args[1].get(
            "fail_file"
        )
        assert fail_file_arg is not None
        fail_path = Path(fail_file_arg)
        assert fail_path.is_absolute()
        # Should be in data/uat/ folder
        assert fail_path.parent == data_dir / "uat"
        assert fail_path.name == "res_partner_fail.csv"

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_groupby_deferred_conflict_is_stripped(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A field in both --groupby and --deferred-fields is dropped from groupby.

        Regression for #185/#186: grouping Pass-1 batches by a deferred field is
        meaningless and silently breaks Pass-2 resolution.
        """
        source = tmp_path / "res_partner.csv"
        source.write_text("id;name;parent_id/id\n1;Test;\n")
        mock_preflight.return_value = True
        mock_import_data.return_value = (True, {"total_records": 1})

        run_import(
            config="test.conf",
            filename=str(source),
            model="res.partner",
            deferred_fields=["parent_id/id"],
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=False,
            headless=False,
            worker=1,
            batch_size=10,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            context="{}",
            encoding="utf-8",
            o2m=False,
            groupby=["parent_id/id"],
        )

        # The conflicting deferred field must have been removed from groupby.
        split = mock_import_data.call_args.kwargs.get("split_by_cols")
        assert split is None

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_fail_file_no_env_uses_same_dir(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that fail file stays in same dir when no env can be extracted."""
        # Create data directory with source file
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        source_file = data_dir / "res_partner.csv"
        source_file.write_text("id,name\n1,Test\n")

        mock_preflight.return_value = True
        mock_import_data.return_value = (True, {"total_records": 1})

        # Run import with config dict without _config_file
        run_import(
            config={
                "hostname": "localhost",
                "database": "db",
                "login": "a",
                "password": "b",
            },
            filename=str(source_file),
            model="res.partner",
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=False,
            headless=False,
            worker=1,
            batch_size=10,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            context="{}",
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        # Verify the fail_file path is in same directory as input file
        call_args = mock_import_data.call_args
        fail_file_arg = call_args.kwargs.get("fail_file") or call_args[1].get(
            "fail_file"
        )
        assert fail_file_arg is not None
        fail_path = Path(fail_file_arg)
        assert fail_path.parent == data_dir


class TestRunImport:
    """Tests for the main run_import orchestrator function."""

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_run_import_success_path(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test the successful execution path of run_import."""
        # Arrange
        source_file = tmp_path / "source.csv"
        source_file.touch()
        mock_preflight.return_value = True
        mock_import_data.return_value = (True, {"total_records": 1})

        # Act
        run_import(
            config="dummy.conf",
            filename=str(source_file),
            model="res.partner",
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=False,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        # Assert
        mock_preflight.assert_called_once()
        mock_import_data.assert_called_once()

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_run_import_refuses_to_defer_required_relational_field(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Required relational fields are stripped from explicit deferral.

        Deferring a required relation would make the Pass-1 create fail with
        'Missing required value', so run_import must drop it (and keep the rest).
        """
        source_file = tmp_path / "states.csv"
        source_file.write_text("id,name,country_id/id,user_id/id\n")

        def _populate_plan(*_args: object, **kwargs: object) -> bool:
            # Preflight reports country_id as a required relational field.
            plan = kwargs["import_plan"]
            plan["required_relational_fields"] = ["country_id"]  # type: ignore[index]
            return True

        mock_preflight.side_effect = _populate_plan
        mock_import_data.return_value = (True, {"total_records": 0})

        run_import(
            config="dummy.conf",
            filename=str(source_file),
            model="res.country.state",
            deferred_fields=["country_id", "user_id"],
            auto_defer=False,
            unique_id_field="id",
            no_preflight_checks=False,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=",",
            ignore=None,
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        passed_deferred = mock_import_data.call_args.kwargs["deferred_fields"]
        assert "country_id" not in passed_deferred, (
            "Required relational field was not stripped from deferral."
        )
        assert "user_id" in passed_deferred, (
            "Non-required deferred field was wrongly dropped."
        )

    @patch("fluvo.importer._infer_model_from_filename")
    @patch("fluvo.importer._show_error_panel")
    def test_run_import_fails_if_model_not_found(
        self,
        mock_show_error: MagicMock,
        mock_infer_model: MagicMock,
    ) -> None:
        """Test that the import aborts if no model can be determined."""
        # Arrange
        mock_infer_model.return_value = None

        # Act
        run_import(
            config="dummy.conf",
            filename="no_model.csv",
            model=None,  # No model provided
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=False,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        # Assert
        mock_show_error.assert_called_once()
        assert "Model Not Found" in mock_show_error.call_args[0]

    @patch("fluvo.importer.import_threaded.import_data")
    def test_import_data_simple_success(
        self,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Tests a simple, successful import with no failures."""
        source_file = tmp_path / "source.csv"
        source_file.touch()
        mock_import_data.return_value = (True, {"created_records": 2})

        run_import(
            config=str(source_file),
            filename=str(source_file),
            model="res.partner",
            deferred_fields=None,
            auto_defer=False,
            unique_id_field="id",
            no_preflight_checks=True,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=[],
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )
        mock_import_data.assert_called_once()

    @patch("fluvo.importer.import_threaded.import_data")
    def test_import_data_two_pass_success(
        self,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Tests a successful two-pass import with deferred fields."""
        source_file = tmp_path / "source.csv"
        source_file.touch()
        mock_import_data.return_value = (True, {"created_records": 2})

        run_import(
            config=str(source_file),
            filename=str(source_file),
            model="res.partner",
            deferred_fields=["parent_id"],
            auto_defer=False,
            unique_id_field="id",
            no_preflight_checks=True,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=[],
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )
        mock_import_data.assert_called_once()


@patch("fluvo.importer.import_threaded.import_data")
@patch("fluvo.importer._run_preflight_checks", return_value=False)
def test_run_import_preflight_fails(
    mock_preflight: MagicMock,
    mock_import_data: MagicMock,
    tmp_path: Path,
) -> None:
    """Test that the import aborts if preflight checks fail."""
    source_file = tmp_path / "source.csv"
    source_file.touch()
    run_import(
        config="dummy.conf",
        filename=str(source_file),
        model="res.partner",
        deferred_fields=None,
        auto_defer=False,
        unique_id_field=None,
        no_preflight_checks=False,
        headless=True,
        worker=1,
        batch_size=100,
        skip=0,
        fail=False,
        separator=";",
        ignore=None,
        context={},
        encoding="utf-8",
        o2m=False,
        groupby=None,
    )
    mock_import_data.assert_not_called()


@patch("fluvo.importer.import_threaded.import_data")
@patch("fluvo.importer._run_preflight_checks", return_value=True)
def test_run_import_fail_mode(
    mock_preflight: MagicMock,
    mock_import_data: MagicMock,
    tmp_path: Path,
) -> None:
    """Test the fail mode logic with environment-specific folders."""
    source_file = tmp_path / "source.csv"
    source_file.touch()
    # Create fail file in environment-specific folder (uat from uat_connection.conf)
    env_dir = tmp_path / "uat"
    env_dir.mkdir(parents=True)
    fail_file = env_dir / "res_partner_fail.csv"
    fail_file.write_text("id,name\n1,test")
    mock_import_data.return_value = (True, {"total_records": 1})

    run_import(
        config="uat_connection.conf",
        filename=str(source_file),
        model="res.partner",
        fail=True,
        deferred_fields=None,
        auto_defer=False,
        unique_id_field=None,
        no_preflight_checks=False,
        headless=True,
        worker=1,
        batch_size=100,
        skip=0,
        separator=";",
        ignore=None,
        context={},
        encoding="utf-8",
        o2m=False,
        groupby=None,
    )
    assert mock_import_data.call_args.kwargs["file_csv"] == str(fail_file)


@patch("fluvo.importer.sort.sort_for_self_referencing")
@patch("fluvo.importer.import_threaded.import_data")
@patch("fluvo.importer._run_preflight_checks")
def test_run_import_sort_strategy(
    mock_preflight: MagicMock,
    mock_import_data: MagicMock,
    mock_sort: MagicMock,
    tmp_path: Path,
) -> None:
    """Test the sort and one pass load strategy."""
    source_file = tmp_path / "source.csv"
    source_file.touch()
    sorted_file = tmp_path / "sorted.csv"
    mock_sort.return_value = str(sorted_file)

    def preflight_side_effect(*args: Any, **kwargs: Any) -> bool:
        kwargs["import_plan"]["strategy"] = "sort_and_one_pass_load"
        kwargs["import_plan"]["id_column"] = "id"
        kwargs["import_plan"]["parent_column"] = "parent_id"
        return True

    mock_preflight.side_effect = preflight_side_effect
    mock_import_data.return_value = (True, {"total_records": 1})

    run_import(
        config="dummy.conf",
        filename=str(source_file),
        model="res.partner",
        deferred_fields=None,
        auto_defer=False,
        unique_id_field=None,
        no_preflight_checks=False,
        headless=True,
        worker=1,
        batch_size=100,
        skip=0,
        fail=False,
        separator=";",
        ignore=None,
        context={},
        encoding="utf-8",
        o2m=False,
        groupby=None,
    )
    mock_sort.assert_called_once()
    assert mock_import_data.call_args.kwargs["file_csv"] == str(sorted_file)


@patch("fluvo.importer.import_threaded.import_data")
def test_run_import_for_migration(mock_import_data: MagicMock) -> None:
    """Test the run_import_for_migration function."""
    mock_import_data.return_value = (True, {})
    run_import_for_migration(
        config="dummy.conf",
        model="res.partner",
        header=["id", "name"],
        data=[[1, "test"]],
    )
    mock_import_data.assert_called_once()


@patch("fluvo.importer._show_error_panel")
def test_run_import_invalid_context(mock_show_error: MagicMock) -> None:
    """Test that run_import handles invalid context."""
    run_import(
        config="dummy.conf",
        filename="dummy.csv",
        model="res.partner",
        context="not a dict",
        deferred_fields=None,
        auto_defer=False,
        unique_id_field=None,
        no_preflight_checks=True,
        headless=True,
        worker=1,
        batch_size=100,
        skip=0,
        fail=False,
        separator=";",
        ignore=None,
        encoding="utf-8",
        o2m=False,
        groupby=None,
    )
    mock_show_error.assert_called_once()


@patch("fluvo.importer.relational_import.run_direct_relational_import")
@patch("fluvo.importer.import_threaded.import_data")
@patch("fluvo.importer._run_preflight_checks")
def test_run_import_fail_mode_with_strategies(
    mock_preflight: MagicMock,
    mock_import_data: MagicMock,
    mock_relational_import: MagicMock,
    tmp_path: Path,
) -> None:
    """Test that relational strategies are skipped in fail mode."""
    source_file = tmp_path / "source.csv"
    source_file.touch()
    # Create fail file in environment-specific folder (test from test_connection.conf)
    env_dir = tmp_path / "test"
    env_dir.mkdir(parents=True)
    fail_file = env_dir / "res_partner_fail.csv"
    fail_file.write_text("id,name\n1,test")

    def preflight_side_effect(*_args: Any, **kwargs: Any) -> bool:
        kwargs["import_plan"]["strategies"] = {
            "field": {"strategy": "direct_relational_import"}
        }
        return True

    mock_preflight.side_effect = preflight_side_effect
    mock_import_data.return_value = (True, {"total_records": 1, "id_map": {"1": 1}})

    run_import(
        config="test_connection.conf",
        filename=str(source_file),
        model="res.partner",
        fail=True,
        deferred_fields=None,
        auto_defer=False,
        unique_id_field=None,
        no_preflight_checks=False,
        headless=True,
        worker=1,
        batch_size=100,
        skip=0,
        separator=";",
        ignore=None,
        context={},
        encoding="utf-8",
        o2m=False,
        groupby=None,
    )
    mock_import_data.assert_called_once()
    mock_relational_import.assert_not_called()


class TestImporterEdgeCases:
    """Additional edge case tests for importer module."""

    def test_infer_model_from_filename_no_dot(self) -> None:
        """Test model inference from filename without dots."""
        # Single word filename without underscore - should return None
        assert _infer_model_from_filename("nomodel.csv") is None

    def test_count_lines_file_not_found(self) -> None:
        """Test line count returns 0 for non-existent file."""
        assert _count_lines("/nonexistent/file.csv") == 0

    @patch("fluvo.importer._show_error_panel")
    def test_run_import_context_type_error(self, mock_show_error: MagicMock) -> None:
        """Test run_import handles context that parses to non-dict."""
        result = run_import(
            config="dummy.conf",
            filename="dummy.csv",
            model="res.partner",
            context="[1, 2, 3]",  # Valid JSON but not a dict
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=True,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )
        assert result is None
        mock_show_error.assert_called_once()

    @patch("fluvo.importer._show_error_panel")
    def test_run_import_context_non_string_non_dict(
        self, mock_show_error: MagicMock
    ) -> None:
        """Test run_import handles context that is neither string nor dict."""
        result = run_import(
            config="dummy.conf",
            filename="dummy.csv",
            model="res.partner",
            context=12345,  # Neither string nor dict
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=True,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )
        assert result is None
        mock_show_error.assert_called_once()

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_run_import_fail_mode_no_records(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test fail mode when fail file has only header (no records)."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id;name\n1;test\n")

        # Create empty fail file (only header)
        env_dir = tmp_path / "test"
        env_dir.mkdir(parents=True)
        fail_file = env_dir / "res_partner_fail.csv"
        fail_file.write_text("id;name\n")  # Only header

        result = run_import(
            config="test_connection.conf",
            filename=str(source_file),
            model="res.partner",
            fail=True,
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=True,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            separator=";",
            ignore=None,
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        # Should return None without calling import_data
        assert result is None
        mock_import_data.assert_not_called()

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_run_import_fail_mode_adds_error_reason_ignore(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that _ERROR_REASON is added to ignore list in fail mode."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id;name\n1;test\n")

        env_dir = tmp_path / "test"
        env_dir.mkdir(parents=True)
        fail_file = env_dir / "res_partner_fail.csv"
        fail_file.write_text("id;name;_ERROR_REASON\n1;test;error\n")

        mock_preflight.return_value = True
        mock_import_data.return_value = (True, {"total_records": 1})

        run_import(
            config="test_connection.conf",
            filename=str(source_file),
            model="res.partner",
            fail=True,
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=False,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            separator=";",
            ignore=None,  # Start with None
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        # Verify _ERROR_REASON is in ignore list
        call_kwargs = mock_import_data.call_args.kwargs
        assert "_ERROR_REASON" in call_kwargs.get("ignore", [])

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_run_import_auto_defer_uses_detected_fields(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that auto_defer uses deferred fields from preflight."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id;name\n1;test\n")

        def preflight_side_effect(*_args: Any, **kwargs: Any) -> bool:
            kwargs["import_plan"]["deferred_fields"] = ["parent_id", "user_id"]
            return True

        mock_preflight.side_effect = preflight_side_effect
        mock_import_data.return_value = (True, {"total_records": 1})

        run_import(
            config="test.conf",
            filename=str(source_file),
            model="res.partner",
            deferred_fields=None,
            auto_defer=True,  # Enable auto_defer
            unique_id_field=None,
            no_preflight_checks=False,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        call_kwargs = mock_import_data.call_args.kwargs
        assert call_kwargs["deferred_fields"] == ["parent_id", "user_id"]

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_run_import_deferred_fields_logs_when_detected(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that detected deferred fields are logged when not applied."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id;name\n1;test\n")

        def preflight_side_effect(*_args: Any, **kwargs: Any) -> bool:
            kwargs["import_plan"]["deferred_fields"] = ["parent_id"]
            return True

        mock_preflight.side_effect = preflight_side_effect
        mock_import_data.return_value = (True, {"total_records": 1})

        run_import(
            config="test.conf",
            filename=str(source_file),
            model="res.partner",
            deferred_fields=None,
            auto_defer=False,  # Not using auto_defer
            unique_id_field=None,
            no_preflight_checks=False,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        # Should still work but with empty deferred_fields
        call_kwargs = mock_import_data.call_args.kwargs
        assert call_kwargs["deferred_fields"] == []

    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._show_error_panel")
    def test_run_import_returns_none_on_failure(
        self,
        mock_show_error: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that run_import returns None and shows error on import failure."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id;name\n1;test\n")

        mock_import_data.return_value = (False, {})  # Import failed

        result = run_import(
            config="test.conf",
            filename=str(source_file),
            model="res.partner",
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=True,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        assert result is None
        mock_show_error.assert_called_once()

    @patch("fluvo.importer.os.remove")
    @patch("fluvo.importer.os.path.exists", return_value=True)
    @patch("fluvo.importer.sort.sort_for_self_referencing")
    @patch("fluvo.importer.import_threaded.import_data")
    @patch("fluvo.importer._run_preflight_checks")
    def test_run_import_cleans_up_sorted_temp_file(
        self,
        mock_preflight: MagicMock,
        mock_import_data: MagicMock,
        mock_sort: MagicMock,
        mock_exists: MagicMock,
        mock_remove: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that sorted temp file is cleaned up after import."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id;name\n1;test\n")

        sorted_file = str(tmp_path / "sorted_temp.csv")
        mock_sort.return_value = sorted_file

        def preflight_side_effect(*_args: Any, **kwargs: Any) -> bool:
            kwargs["import_plan"]["strategy"] = "sort_and_one_pass_load"
            kwargs["import_plan"]["id_column"] = "id"
            kwargs["import_plan"]["parent_column"] = "parent_id"
            return True

        mock_preflight.side_effect = preflight_side_effect
        mock_import_data.return_value = (True, {"total_records": 1})

        run_import(
            config="test.conf",
            filename=str(source_file),
            model="res.partner",
            deferred_fields=None,
            auto_defer=False,
            unique_id_field=None,
            no_preflight_checks=False,
            headless=True,
            worker=1,
            batch_size=100,
            skip=0,
            fail=False,
            separator=";",
            ignore=None,
            context={},
            encoding="utf-8",
            o2m=False,
            groupby=None,
        )

        # Verify temp file was removed
        mock_remove.assert_called_once_with(sorted_file)
