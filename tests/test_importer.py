"""Test the high-level import orchestrator, including pre-flight checks."""

import unittest
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import polars as pl
import pytest

from odoo_data_flow.enums import PreflightMode
from odoo_data_flow.importer import (
    _run_preflight_checks,
    run_import,
    run_import_for_migration,
)


@pytest.fixture
def mock_conf_lib() -> Generator[MagicMock, None, None]:
    """Fixture to mock conf_lib.get_connection_from_config."""
    with patch(
        "odoo_data_flow.importer.conf_lib.get_connection_from_config"
    ) as mock_conn:
        mock_model_obj = MagicMock()
        mock_model_obj.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "is_company": {"type": "boolean"},
            "phone": {"type": "char"},
        }
        mock_connection = MagicMock()
        mock_connection.get_model.return_value = mock_model_obj
        mock_conn.return_value = mock_connection
        yield mock_conn


@patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
@patch("odoo_data_flow.importer.import_threaded.import_data")
def test_run_import_infers_model_from_filename(
    mock_import_data: MagicMock, mock_run_checks: MagicMock, tmp_path: Path
) -> None:
    """Test infer model name.

    Tests that the model name is correctly inferred from the filename
    when the --model argument is not provided.
    """
    # 1. Setup: Create a dummy file for the function to read.
    source_file = tmp_path / "res_partner.csv"
    source_file.write_text("id,name\n1,test")

    # 2. Action
    run_import(config="dummy.conf", filename=str(source_file), separator=",")

    # 3. Assertions
    mock_run_checks.assert_called_once()
    mock_import_data.assert_called_once()
    # The second positional argument passed to import_data should be the model name.
    called_model = mock_import_data.call_args.kwargs["model"]
    assert called_model == "res.partner"


@patch("odoo_data_flow.importer.import_threaded.import_data")
@patch("odoo_data_flow.importer._show_error_panel")
def test_run_import_no_model_fails(
    mock_show_error: MagicMock, mock_import_data: MagicMock, tmp_path: Path
) -> None:
    """Test import with no fails.

    Tests that the import fails if no model can be inferred from the filename.
    """
    # 1. Setup: A filename starting with a dot will result in an invalid model name.
    bad_file = tmp_path / ".badfilename"
    bad_file.touch()

    # 2. Action
    run_import(config="dummy.conf", filename=str(bad_file))
    mock_show_error.assert_called_once()
    assert "Model Not Found" in mock_show_error.call_args.args[0]
    mock_import_data.assert_not_called()


@patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
@patch("odoo_data_flow.importer.import_threaded.import_data")
def test_run_import_fail_mode(
    mock_import_data: MagicMock, mock_run_checks: MagicMock, tmp_path: Path
) -> None:
    """Test import in fail mode.

    Tests that when --fail is True, the correct parameters for a fail run
    are passed down to the core import function.
    """
    # 1. Setup
    source_file = tmp_path / "res_partner.csv"
    source_file.write_text("id,name\n1,test")  # Original file, can be minimal

    fail_file = tmp_path / "res_partner_fail.csv"
    fail_file.write_text("id,name\n2,failed_record")

    # 2. Action
    run_import(
        config="dummy.conf",
        filename=str(source_file),
        model="res.partner",
        fail=True,
    )

    # 3. Assertions
    mock_import_data.assert_called_once()

    # Verify that the correct file (the fail file) was passed to the importer
    call_kwargs = mock_import_data.call_args.kwargs
    assert call_kwargs["file_csv"] == str(fail_file)
    assert call_kwargs["is_fail_run"] is True


@patch("odoo_data_flow.importer.Console")
@patch("odoo_data_flow.importer.import_threaded.import_data")
def test_run_import_fail_mode_no_records_to_retry(
    mock_import_data: MagicMock, mock_console_class: MagicMock, tmp_path: Path
) -> None:
    """Test the no import running if no records in fail file.

    Tests that a panel is displayed and the import is skipped if the fail
    file is empty or missing.
    """
    # 1. Setup: Create a source file but an EMPTY fail file
    source_file = tmp_path / "res_partner.csv"
    source_file.write_text("id,name\n1,test")
    (tmp_path / "res_partner_fail.csv").write_text("id,name\n")  # Header only

    # Get a reference to the mock instance that will be created
    mock_console_instance = mock_console_class.return_value

    # 2. Action
    run_import(
        config="dummy.conf",
        filename=str(source_file),
        model="res.partner",
        fail=True,
        headless=False,  # Ensure headless is False for this test
    )

    # 3. Assertions
    # The main import function should NOT have been called
    mock_import_data.assert_not_called()

    # The print method on our mock console instance should have been called
    mock_console_instance.print.assert_called_once()

    # Check the content of the printed panel
    panel = mock_console_instance.print.call_args[0][0]
    assert "No Recovery Needed" in str(panel.title)
    assert "Nothing to retry" in str(panel.renderable)


@patch("odoo_data_flow.importer._run_preflight_checks")
@patch("odoo_data_flow.importer.import_threaded.import_data")
def test_preflight_checks_run_by_default(
    mock_import_data: MagicMock, mock_run_checks: MagicMock, tmp_path: Path
) -> None:
    """Tests that the pre-flight checks are run by default."""
    source_file = tmp_path / "data.csv"
    source_file.write_text("id,name\n1,test")
    mock_run_checks.return_value = True  # Simulate checks passing

    run_import(config="dummy.conf", filename=str(source_file), model="res.partner")

    mock_run_checks.assert_called_once()
    mock_import_data.assert_called_once()


@patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
@patch("odoo_data_flow.importer._show_error_panel")
def test_run_import_bad_context_string(
    mock_show_error: MagicMock, mock_run_checks: MagicMock, tmp_path: Path
) -> None:
    """Tests that a malformed context string is handled gracefully."""
    # Setup: Create a dummy file to get past the file-read stage
    source_file = tmp_path / "data.csv"
    source_file.write_text("id,name\n1,test")

    run_import(
        config="dummy.conf",
        filename=str(source_file),
        model="res.partner",
        context="this-is-not-a-dict",
        separator=",",
    )
    mock_show_error.assert_called_once()
    assert "Invalid Context" in mock_show_error.call_args.args[0]


@patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
@patch("odoo_data_flow.importer._show_error_panel")
def test_run_import_context_not_a_dict(
    mock_show_error: MagicMock, mock_run_checks: MagicMock, tmp_path: Path
) -> None:
    """Tests that an error is logged if the context string is not a dictionary."""
    # Setup: Create a dummy file to get past the file-read stage
    source_file = tmp_path / "data.csv"
    source_file.write_text("id,name\n1,test")

    # This context is a valid Python literal, but it's a list, not a dict.
    run_import(
        config="dummy.conf",
        filename=str(source_file),
        model="res.partner",
        context="['not', 'a', 'dict']",
        separator=",",
    )
    mock_show_error.assert_called_once()
    assert "Invalid Context" in mock_show_error.call_args[0]


@patch("odoo_data_flow.importer.import_threaded.import_data")
def test_run_import_for_migration(mock_import_data: MagicMock) -> None:
    """Tests the in-memory import runner used for migrations."""
    # 1. Action
    run_import_for_migration(
        config="dummy.conf",
        model="res.partner",
        header=["id", "name"],
        data=[["1", "Test"]],
        worker=2,
        batch_size=50,
    )

    # 2. Assertions
    mock_import_data.assert_called_once()
    call_kwargs = mock_import_data.call_args.kwargs
    assert call_kwargs["max_connection"] == 2
    assert call_kwargs["batch_size"] == 50
    assert "tracking_disable" in call_kwargs["context"]


@patch("odoo_data_flow.importer._run_preflight_checks")
@patch("odoo_data_flow.importer.import_threaded.import_data")
def test_preflight_checks_are_skipped(
    mock_import_data: MagicMock, mock_run_checks: MagicMock, tmp_path: Path
) -> None:
    """Tests that the pre-flight checks are skipped when the flag is passed."""
    source_file = tmp_path / "data.csv"
    source_file.write_text("id,name\n1,test")

    run_import(
        config="dummy.conf",
        filename=str(source_file),
        model="res.partner",
        no_preflight_checks=True,
    )
    mock_run_checks.assert_not_called()
    mock_import_data.assert_called_once()


@patch("odoo_data_flow.importer._run_preflight_checks")
@patch("odoo_data_flow.importer.import_threaded.import_data")
def test_import_aborts_on_preflight_failure(
    mock_import_data: MagicMock, mock_run_checks: MagicMock, tmp_path: Path
) -> None:
    """Tests that the import is aborted if a pre-flight check fails."""
    source_file = tmp_path / "data.csv"
    source_file.write_text("id,name\n1,test")
    mock_run_checks.return_value = False  # Simulate a check failing

    run_import(config="dummy.conf", filename=str(source_file), model="res.partner")

    mock_run_checks.assert_called_once()
    mock_import_data.assert_not_called()


@patch("odoo_data_flow.importer.import_threaded.import_data", return_value=False)
@patch("odoo_data_flow.importer._show_error_panel")
@patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
@patch("os.path.exists", return_value=True)
@patch("os.path.basename", return_value="dummy.csv")
@patch("os.path.splitext", return_value=("dummy", ".csv"))
def test_run_import_failure(
    mock_splitext: MagicMock,
    mock_basename: MagicMock,
    mock_exists: MagicMock,
    mock_run_preflight_checks: MagicMock,
    mock_show_error_panel: MagicMock,
    mock_import_data: MagicMock,
) -> None:
    """Test that run_import handles import failure."""
    run_import(
        config="dummy.conf",
        filename="dummy.csv",
        model="res.partner",
    )
    mock_show_error_panel.assert_called_once()
    assert "Import Aborted" in mock_show_error_panel.call_args.args[0]


@patch("odoo_data_flow.importer._show_error_panel")
@patch("os.path.basename", return_value="invalid_file")
@patch(
    "os.path.splitext", return_value=("", "")
)  # Modified to return empty string for inference failure
@patch(
    "os.path.exists", return_value=True
)  # Added to allow preflight checks to proceed
def test_run_import_model_inference_failure(
    mock_splitext: MagicMock,
    mock_basename: MagicMock,
    mock_exists: MagicMock,
    mock_show_error_panel: MagicMock,
) -> None:
    """Test that run_import handles model inference failure."""
    run_import(
        config="dummy.conf",
        filename="invalid_file",
    )
    mock_show_error_panel.assert_called_once()
    assert "Model Not Found" in mock_show_error_panel.call_args.args[0]


@patch("odoo_data_flow.importer._show_error_panel")
@patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
@patch("os.path.exists", return_value=True)  # Added
@patch(
    "odoo_data_flow.importer.preflight.pl.read_csv",
    return_value=pl.DataFrame({"id": [], "name": []}),
)  # Added
@patch("odoo_data_flow.importer.preflight.conf_lib.get_connection_from_config")  # Added
@patch("odoo_data_flow.importer.preflight.language_check", return_value=True)  # Added
@patch(
    "odoo_data_flow.importer.preflight.field_existence_check", return_value=True
)  # Added
def test_run_import_invalid_context(
    mock_run_preflight_checks: MagicMock,
    mock_exists: MagicMock,
    mock_read_csv: MagicMock,
    mock_get_connection: MagicMock,
    mock_language_check: MagicMock,
    mock_field_existence_check: MagicMock,
    mock_show_error_panel: MagicMock,
) -> None:
    """Test that run_import handles invalid context string."""
    run_import(
        config="dummy.conf",
        filename="dummy.csv",
        model="res.partner",
        context="this-is-not-a-dict",
    )
    mock_show_error_panel.assert_called_once()
    assert "Invalid Context" in mock_show_error_panel.call_args.args[0]


def mock_preflight_check_fail(**kwargs: Any) -> bool:
    """Mock a failling preflight check."""
    return False


def mock_preflight_check_pass(**kwargs: Any) -> bool:
    """Mock a passing preflight check."""
    return True


@patch(
    "odoo_data_flow.importer.preflight.PREFLIGHT_CHECKS",
    [mock_preflight_check_fail],
)
def test_run_preflight_checks_fail() -> None:
    """Test that _run_preflight_checks returns False if a check fails."""
    assert not _run_preflight_checks(
        PreflightMode.NORMAL,
        model="test",
        filename="test.csv",
        config="test.conf",
        headless=False,
        separator=";",
    )


@patch(
    "odoo_data_flow.importer.preflight.PREFLIGHT_CHECKS",
    [mock_preflight_check_pass],
)
def test_run_preflight_checks_pass() -> None:
    """Test that _run_preflight_checks returns True if all checks pass."""
    assert _run_preflight_checks(
        PreflightMode.NORMAL,
        model="test",
        filename="test.csv",
        config="test.conf",
        headless=False,
        separator=";",
    )


class TestRunImportSplitArgumentHandling(unittest.TestCase):
    """Test Split and spliy_by_cols handling.

    Tests the argument handling for `split` and `split_by_cols`
    in the `run_import` function.
    """

    def _get_common_kwargs(self) -> dict[str, Any]:
        """Returns a dictionary of common arguments required by run_import."""
        # We patch 'importer.preflight' to avoid running actual preflight checks
        # which require a live Odoo connection and valid files.
        patcher = patch(
            "odoo_data_flow.importer._run_preflight_checks", return_value=True
        )
        self.addCleanup(patcher.stop)
        patcher.start()

        return {
            "config": "dummy_config.cfg",
            "filename": "dummy_file.csv",
            "model": "res.partner",
            "no_preflight_checks": True,  # Skip checks to isolate the test
        }

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_split_by_cols_with_comma_separated_string(
        self, mock_import_data: Mock
    ) -> None:
        """Verify comma separated string parsing.

        Verify that a comma-separated string is correctly parsed into a list.
        """
        kwargs = self._get_common_kwargs()
        kwargs["split_by_cols"] = "partner_id/id, company_id/id"
        expected_list = ["partner_id/id", "company_id/id"]

        run_import(**kwargs)

        # Verify that import_data was called once, then check the specific argument
        mock_import_data.assert_called_once()
        self.assertEqual(
            mock_import_data.call_args.kwargs["split_by_cols"], expected_list
        )

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_split_by_cols_with_single_string(self, mock_import_data: Mock) -> None:
        """Test splitting columns with single string.

        Verify that a single string is correctly converted into a list.
        """
        kwargs = self._get_common_kwargs()
        kwargs["split_by_cols"] = "parent_id/id"
        expected_list = ["parent_id/id"]

        run_import(**kwargs)

        mock_import_data.assert_called_once()
        self.assertEqual(
            mock_import_data.call_args.kwargs["split_by_cols"], expected_list
        )

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_split_by_cols_with_tuple(self, mock_import_data: Mock) -> None:
        """Verify split by clumn tuple.

        Verify that a tuple of strings is correctly converted into a list.
        """
        kwargs = self._get_common_kwargs()
        kwargs["split_by_cols"] = ("partner_id/id", "company_id/id")
        expected_list = ["partner_id/id", "company_id/id"]

        run_import(**kwargs)

        mock_import_data.assert_called_once()
        self.assertEqual(
            mock_import_data.call_args.kwargs["split_by_cols"], expected_list
        )

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_split_by_cols_with_list(self, mock_import_data: Mock) -> None:
        """Test split by columns with list.

        Verify that a list of strings is passed through correctly.
        """
        kwargs = self._get_common_kwargs()
        kwargs["split_by_cols"] = ["partner_id/id", "company_id/id"]
        expected_list = ["partner_id/id", "company_id/id"]

        run_import(**kwargs)

        mock_import_data.assert_called_once()
        self.assertEqual(
            mock_import_data.call_args.kwargs["split_by_cols"], expected_list
        )

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_legacy_split_argument_is_handled(self, mock_import_data: Mock) -> None:
        """Test compatability split argument.

        Verify backward compatibility with the old `split` argument.
        """
        kwargs = self._get_common_kwargs()
        kwargs["split"] = "parent_id/id"
        expected_list = ["parent_id/id"]

        run_import(**kwargs)

        mock_import_data.assert_called_once()
        self.assertEqual(
            mock_import_data.call_args.kwargs["split_by_cols"], expected_list
        )

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_split_by_cols_takes_precedence(self, mock_import_data: Mock) -> None:
        """Test Split by clumn precedence.

        Verify that `split_by_cols` is used if both it and `split` are provided.
        """
        kwargs = self._get_common_kwargs()
        kwargs["split_by_cols"] = "new_arg"
        kwargs["split"] = "old_arg"  # This one should be ignored
        expected_list = ["new_arg"]

        run_import(**kwargs)

        mock_import_data.assert_called_once()
        self.assertEqual(
            mock_import_data.call_args.kwargs["split_by_cols"], expected_list
        )

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_no_split_argument_provided(self, mock_import_data: Mock) -> None:
        """Test no split argument.

        Verify that the argument is None if neither is provided.
        """
        kwargs = self._get_common_kwargs()
        # No split or split_by_cols key added

        run_import(**kwargs)

        mock_import_data.assert_called_once()
        self.assertIsNone(mock_import_data.call_args.kwargs["split_by_cols"])


class TestRunImportRouting:
    """Test the import routing.

    Tests the routing logic within the main run_import function to ensure
    it calls the correct import strategy (standard or deferred).
    """

    @patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
    @patch("odoo_data_flow.importer.import_threaded.import_data")
    @patch("odoo_data_flow.importer.run_import_deferred")
    def test_run_import_routes_to_deferred(
        self,
        mock_run_deferred: MagicMock,
        mock_import_standard: MagicMock,
        mock_preflight: MagicMock,
    ) -> None:
        """Test run deffered import routes.

        Test that providing deferred_fields and unique_id_field calls the
        deferred import function.
        """
        # --- Act ---
        run_import(
            config="dummy.conf",
            filename="dummy.csv",
            model="res.partner",
            deferred_fields="parent_id",
            unique_id_field="xml_id",
        )

        # --- Assert ---
        mock_run_deferred.assert_called_once()
        mock_import_standard.assert_not_called()

    @patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
    @patch("odoo_data_flow.importer.import_threaded.import_data")
    @patch("odoo_data_flow.importer.run_import_deferred")
    def test_run_import_routes_to_standard(
        self,
        mock_run_deferred: MagicMock,
        mock_import_standard: MagicMock,
        mock_preflight: MagicMock,
    ) -> None:
        """Test standard import route.

        Test that standard arguments call the original single-pass
        import function.
        """
        # --- Act ---
        run_import(
            config="dummy.conf",
            filename="dummy.csv",
            model="res.partner",
        )

        # --- Assert ---
        mock_run_deferred.assert_not_called()
        mock_import_standard.assert_called_once()

    @patch("odoo_data_flow.importer._show_error_panel")
    @patch("odoo_data_flow.importer.import_threaded.import_data")
    @patch("odoo_data_flow.importer.run_import_deferred")
    def test_run_import_handles_mutually_exclusive_flags(
        self,
        mock_run_deferred: MagicMock,
        mock_import_standard: MagicMock,
        mock_error_panel: MagicMock,
    ) -> None:
        """Test fail and deferred fields combo.

        Test that using --fail and --deferred-fields together shows an error
        and does not call any import function.
        """
        # --- Act ---
        run_import(
            config="dummy.conf",
            filename="dummy.csv",
            model="res.partner",
            fail=True,
            deferred_fields="parent_id",
            unique_id_field="xml_id",
        )

        # --- Assert ---
        mock_error_panel.assert_called_once()
        mock_run_deferred.assert_not_called()
        mock_import_standard.assert_not_called()
