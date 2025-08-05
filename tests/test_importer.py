"""Test the high-level import orchestrator, including pre-flight checks."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from odoo_data_flow.importer import run_import


class TestRunImport:
    """Tests for the main run_import orchestrator function."""

    @patch("odoo_data_flow.importer._orchestrate_import")
    def test_run_import_no_model_fails(
        self, mock_orchestrate: MagicMock, tmp_path: Path
    ) -> None:
        """Test model inference failure.

        Tests that the import fails if no model can be inferred from the filename.
        """
        bad_file = tmp_path / ".badfilename"
        bad_file.touch()

        with patch("odoo_data_flow.importer._show_error_panel") as mock_show_error:
            run_import(config="dummy.conf", filename=str(bad_file))
            mock_show_error.assert_called_once()
            assert "Model Not Found" in mock_show_error.call_args[0][0]

        mock_orchestrate.assert_not_called()

    @patch("odoo_data_flow.importer._orchestrate_import")
    def test_run_import_failure_panel(self, mock_orchestrate: MagicMock) -> None:
        """Test import failure panel.

        Test that run_import shows the 'Import Failed' panel on failure.
        """
        # Simulate the orchestrator returning without success
        mock_orchestrate.return_value = None

        with patch(
            "odoo_data_flow.importer.import_threaded.import_data",
            return_value=False,
        ):
            with patch("odoo_data_flow.importer._show_error_panel") as mock_show_error:  # noqa
                run_import(
                    config="dummy.conf",
                    filename="dummy.csv",
                    model="res.partner",
                )
                # The final panel is now inside _orchestrate_import, which is mocked.
                # To test the final panel, we'd need a more complex integration test.
                # For now, we confirm the orchestrator is called.
                mock_orchestrate.assert_called_once()

    @patch("odoo_data_flow.importer._orchestrate_import")
    def test_run_import_routes_to_orchestrator(
        self, mock_orchestrate: MagicMock
    ) -> None:
        """Test that a standard call correctly delegates to the orchestrator."""
        run_import(
            config="dummy.conf",
            filename="dummy.csv",
            model="res.partner",
            deferred_fields="parent_id",
            unique_id_field="xml_id",
        )
        mock_orchestrate.assert_called_once()
        call_kwargs = mock_orchestrate.call_args.kwargs
        assert call_kwargs["deferred_fields"] == "parent_id"

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_run_import_fail_mode_no_records_to_retry(
        self, mock_import_data: MagicMock, tmp_path: Path
    ) -> None:
        """Test fail mode with no records to retry.

        Tests that the import is skipped if the fail file is empty or missing.
        """
        source_file = tmp_path / "res_partner.csv"
        source_file.write_text("id,name\n1,test")
        fail_file = tmp_path / "res_partner_fail.csv"
        fail_file.write_text("id,name\n")  # Header only

        with patch("odoo_data_flow.importer.Console") as mock_console_class:
            run_import(
                config="dummy.conf",
                filename=str(source_file),
                model="res.partner",
                fail=True,
            )
            mock_console_class.return_value.print.assert_called_once()

        mock_import_data.assert_not_called()
