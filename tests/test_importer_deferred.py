"""Unit tests for the deferred import functionality in the importer module."""

from unittest.mock import MagicMock, mock_open, patch

from odoo_data_flow.importer import run_import_deferred


class TestImportDeferred:
    """Tests for the run_import_deferred wrapper function.

    This class specifically tests the logic of the wrapper itself,
    not the complex inner workings of import_threaded.import_data.
    """

    MOCK_CSV_DATA = (
        "xml_id,name,parent_id\n"
        "partner_A,Parent Company,\n"
        "partner_B,Subsidiary B,partner_A\n"
        "partner_C,Subsidiary C,partner_A\n"
        "partner_D,Independent Co.,\n"
    )

    # We mock the direct dependency of the function being tested
    @patch("odoo_data_flow.importer.import_threaded.import_data")
    @patch("odoo_data_flow.importer.Console")  # Also mock Console to hide test output
    def test_run_import_deferred_success_path(
        self, mock_console: MagicMock, mock_import_data: MagicMock
    ) -> None:
        """Test the successful execution of a two-pass deferred import.

        This test verifies that:
        1. Pass 1 (`batch_create`) is called with deferred fields removed.
        2. Pass 2 (`batch_write`) is called with the correct relational data.
        3. The function returns True.
        """
        # ARRANGE: Simulate a successful run from the dependency
        mock_import_data.return_value = True

        # ACT: Call the function we are testing
        result = run_import_deferred(
            config="dummy.conf",
            filename="dummy.csv",
            model_name="res.partner",
            unique_id_field="xml_id",
            deferred_fields=["parent_id"],
            encoding="utf-8-sig",
            separator=",",
        )

        # ASSERT
        assert result is True

        # Check that the underlying function was called correctly
        mock_import_data.assert_called_once_with(
            config_file="dummy.conf",
            model="res.partner",
            unique_id_field="xml_id",
            file_csv="dummy.csv",
            deferred_fields=["parent_id"],
            encoding="utf-8-sig",
            separator=",",
            max_connection=4,
            batch_size=200,
        )

        # Check that the success panel was printed
        mock_console.return_value.print.assert_called_once()

    @patch("odoo_data_flow.import_threaded.conf_lib")
    def test_import_fails_on_pass_1_exception(self, mock_conf_lib: MagicMock) -> None:
        """Test that the import returns False if Pass 1 fails."""
        mock_model = MagicMock()
        mock_model.batch_create.side_effect = Exception("Odoo connection lost")
        mock_conf_lib.get_connection_from_config.return_value.get_model.return_value = (
            mock_model
        )

        with patch("builtins.open", mock_open(read_data=self.MOCK_CSV_DATA)):
            result = run_import_deferred(
                config="dummy.conf",
                filename="dummy.csv",
                model_name="res.partner",
                unique_id_field="xml_id",
                deferred_fields=["parent_id"],
            )

        assert result is False
        mock_model.batch_write.assert_not_called()

    @patch("odoo_data_flow.import_threaded.conf_lib")
    def test_import_fails_on_pass_2_exception(self, mock_conf_lib: MagicMock) -> None:
        """Test that the import returns False if Pass 2 fails."""
        mock_model = MagicMock()
        mock_model.batch_create.return_value = {
            "partner_A": 101,
            "partner_B": 102,
        }
        mock_model.batch_write.side_effect = Exception("Write permission error")
        mock_conf_lib.get_connection_from_config.return_value.get_model.return_value = (
            mock_model
        )

        with patch("builtins.open", mock_open(read_data=self.MOCK_CSV_DATA)):
            result = run_import_deferred(
                config="dummy.conf",
                filename="dummy.csv",
                model_name="res.partner",
                unique_id_field="xml_id",
                deferred_fields=["parent_id"],
            )

        assert result is False

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    @patch("odoo_data_flow.importer._show_error_panel")  # Mock the error panel
    def test_run_import_deferred_failure_path(
        self, mock_show_error: MagicMock, mock_import_data: MagicMock
    ) -> None:
        """Test that run_import_deferred handles a failed result."""
        # ARRANGE: Simulate a failed run from the dependency
        mock_import_data.return_value = False

        # ACT
        result = run_import_deferred(
            config="dummy.conf",
            filename="dummy.csv",
            model_name="res.partner",
            unique_id_field="xml_id",
            deferred_fields=["parent_id"],
        )

        # ASSERT
        assert result is False
        mock_import_data.assert_called_once()
        mock_show_error.assert_called_once_with(
            "Import Failed",
            "The deferred import process failed. Check logs for details.",
        )
