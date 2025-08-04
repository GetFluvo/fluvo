"""Unit tests for the deferred import functionality in the importer module."""

from unittest.mock import MagicMock, mock_open, patch

from odoo_data_flow.importer import run_import_deferred


class TestImportDeferred:
    """Tests for the run_import_deferred function and its helpers."""

    # --- Test Data ---
    MOCK_CSV_DATA = (
        "xml_id,name,parent_id\n"
        "partner_A,Parent Company,\n"
        "partner_B,Subsidiary B,partner_A\n"
        "partner_C,Subsidiary C,partner_A\n"
        "partner_D,Independent Co.,\n"
    )

    @patch("odoo_data_flow.importer.csv.DictReader")
    @patch("odoo_data_flow.importer.open", new_callable=mock_open)
    @patch("odoo_data_flow.importer.conf_lib.get_connection_from_config")
    def test_run_import_deferred_success_path(
        self,
        mock_get_conn: MagicMock,
        mock_file: MagicMock,
        mock_csv_reader: MagicMock,
    ) -> None:
        """Test the successful execution of a two-pass deferred import.

        This test verifies that:
        1. Pass 1 (`batch_create`) is called with deferred fields removed.
        2. Pass 2 (`batch_write`) is called with the correct relational data.
        3. The function returns True.
        """
        # --- Arrange ---
        mock_file.return_value.read.return_value = self.MOCK_CSV_DATA
        mock_csv_reader.return_value = [
            {"xml_id": "partner_A", "name": "Parent Company", "parent_id": ""},
            {
                "xml_id": "partner_B",
                "name": "Subsidiary B",
                "parent_id": "partner_A",
            },
            {
                "xml_id": "partner_C",
                "name": "Subsidiary C",
                "parent_id": "partner_A",
            },
            {"xml_id": "partner_D", "name": "Independent Co.", "parent_id": ""},
        ]

        mock_model = MagicMock()
        mock_model.batch_create.return_value = {
            "partner_A": 101,
            "partner_B": 102,
            "partner_C": 103,
            "partner_D": 104,
        }
        mock_model.batch_write.return_value = {"success": 2, "failed": 0}
        mock_get_conn.return_value.get_model.return_value = mock_model

        # --- Act ---
        result = run_import_deferred(
            config="dummy.conf",
            filename="dummy.csv",
            model_name="res.partner",
            unique_id_field="xml_id",
            deferred_fields=["parent_id"],
        )

        # --- Assert ---
        assert result is True

        # Assert Pass 1: `batch_create` was called with deferred fields stripped.
        pass_1_call_args = mock_model.batch_create.call_args[0][0]
        assert "parent_id" not in pass_1_call_args[1]  # Check record B
        assert len(pass_1_call_args) == 4

        # Assert Pass 2: `batch_write` was called with resolved DB IDs.
        mock_model.batch_write.assert_called_once()
        pass_2_call_args = mock_model.batch_write.call_args[0][0]
        assert len(pass_2_call_args) == 2  # Only two records have parents
        # Check that ('partner_B', 102) is updated with parent ('partner_A', 101)
        assert (102, {"parent_id": 101}) in pass_2_call_args
        assert (103, {"parent_id": 101}) in pass_2_call_args

    @patch("odoo_data_flow.importer.conf_lib.get_connection_from_config")
    def test_import_fails_on_pass_1_exception(self, mock_get_conn: MagicMock) -> None:
        """Test that the import returns False if Pass 1 fails."""
        # --- Arrange ---
        mock_model = MagicMock()
        mock_model.batch_create.side_effect = Exception("Odoo connection lost")
        mock_get_conn.return_value.get_model.return_value = mock_model

        with patch("builtins.open", mock_open(read_data=self.MOCK_CSV_DATA)):
            # --- Act ---
            result = run_import_deferred(
                config="dummy.conf",
                filename="dummy.csv",
                model_name="res.partner",
                unique_id_field="xml_id",
                deferred_fields=["parent_id"],
            )

        # --- Assert ---
        assert result is False
        mock_model.batch_write.assert_not_called()

    @patch("odoo_data_flow.importer.csv.DictReader")
    @patch("odoo_data_flow.importer.conf_lib.get_connection_from_config")
    def test_import_fails_on_pass_2_exception(
        self, mock_get_conn: MagicMock, mock_csv_reader: MagicMock
    ) -> None:
        """Test that the import returns False if Pass 2 fails."""
        # --- Arrange ---
        # Directly provide the parsed data, bypassing mock_open issues.
        mock_csv_reader.return_value = [
            {
                "xml_id": "partner_A",
                "name": "Parent Company",
                "parent_id": "",
            },
            {
                "xml_id": "partner_B",
                "name": "Subsidiary B",
                "parent_id": "partner_A",
            },
        ]

        mock_model = MagicMock()
        mock_model.batch_create.return_value = {
            "partner_A": 101,
            "partner_B": 102,
        }
        mock_model.batch_write.side_effect = Exception("Write permission error")
        mock_get_conn.return_value.get_model.return_value = mock_model

        # The open call still happens, so we need a basic mock for it.
        with patch("builtins.open", mock_open()):
            # --- Act ---
            result = run_import_deferred(
                config="dummy.conf",
                filename="dummy.csv",
                model_name="res.partner",
                unique_id_field="xml_id",
                deferred_fields=["parent_id"],
            )

        # --- Assert ---
        assert result is False
        mock_model.batch_write.assert_called_once()  # Ensure it was actually called

    @patch("odoo_data_flow.importer.csv.DictReader")
    @patch("odoo_data_flow.importer.conf_lib.get_connection_from_config")
    def test_handles_records_not_created_in_pass_1(
        self, mock_get_conn: MagicMock, mock_csv_reader: MagicMock
    ) -> None:
        """Test that records not in the id_map from Pass 1 are skipped in Pass 2."""
        # --- Arrange ---
        mock_csv_reader.return_value = [
            {"xml_id": "partner_A", "name": "Company A"},
            {
                "xml_id": "partner_B",
                "name": "Company B",
                "parent_id": "partner_A",
            },
        ]

        mock_model = MagicMock()
        # Simulate that 'partner_B' failed to be created in Pass 1
        mock_model.batch_create.return_value = {"partner_A": 101}
        mock_get_conn.return_value.get_model.return_value = mock_model

        with patch("builtins.open", mock_open()):
            # --- Act ---
            result = run_import_deferred(
                config="dummy.conf",
                filename="dummy.csv",
                model_name="res.partner",
                unique_id_field="xml_id",
                deferred_fields=["parent_id"],
            )

        # --- Assert ---
        # The process should still be successful, as this is a warning, not an error.
        assert result is True
        # Crucially, batch_write should not have been called because the record
        # to be updated ('partner_B') was never created.
        mock_model.batch_write.assert_not_called()

    @patch("odoo_data_flow.importer.conf_lib.get_connection_from_config")
    def test_handles_unresolved_relations_gracefully(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test that missing relations in the ID map are logged but don't crash."""
        # --- Arrange ---
        # Simulate 'partner_A' failing to be created in Pass 1
        mock_model = MagicMock()
        mock_model.batch_create.return_value = {
            "partner_B": 102,
            "partner_C": 103,
        }
        mock_get_conn.return_value.get_model.return_value = mock_model

        with patch("builtins.open", mock_open(read_data=self.MOCK_CSV_DATA)):
            # --- Act ---
            # This should succeed, as the missing relation is just a warning
            result = run_import_deferred(
                config="dummy.conf",
                filename="dummy.csv",
                model_name="res.partner",
                unique_id_field="xml_id",
                deferred_fields=["parent_id"],
            )

        # --- Assert ---
        assert result is True
        # Pass 2 should not be called as no valid relations could be resolved
        mock_model.batch_write.assert_not_called()

    def test_returns_false_on_file_not_found(self) -> None:
        """Test that the function returns False if the source file doesn't exist."""
        # --- Act ---
        result = run_import_deferred(
            config="dummy.conf",
            filename="non_existent_file.csv",
            model_name="res.partner",
            unique_id_field="xml_id",
            deferred_fields=["parent_id"],
        )
        # --- Assert ---
        assert result is False
