"""Tests for the refactored, low-level, multi-threaded import logic."""

from unittest.mock import MagicMock, patch

from odoo_data_flow.import_threaded import _create_batches, import_data


class TestImportDataRefactored:
    """Tests for the main `import_data` orchestrator."""

    @patch("odoo_data_flow.import_threaded._read_data_file")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_import_data_success_path_no_defer(
        self,
        mock_run_pass: MagicMock,
        mock_get_conn: MagicMock,
        mock_read_file: MagicMock,
    ) -> None:
        """Test a successful single-pass import (no deferred fields)."""
        # Arrange
        mock_read_file.return_value = (["id", "name"], [["xml_a", "A"]])
        mock_run_pass.return_value = (
            {"id_map": {"xml_a": 101}, "failed_lines": []},  # results dict
            False,  # aborted = False
        )

        mock_get_conn.return_value.get_model.return_value = MagicMock()

        # Act
        result, _ = import_data(
            config_file="dummy.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv="dummy.csv",
        )

        # Assert
        assert result is True
        mock_run_pass.assert_called_once()  # Only Pass 1 should run

    @patch("odoo_data_flow.import_threaded._read_data_file")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_import_data_success_path_with_defer(
        self,
        mock_run_pass: MagicMock,
        mock_get_conn: MagicMock,
        mock_read_file: MagicMock,
    ) -> None:
        """Test a successful two-pass import (with deferred fields)."""
        # Arrange
        mock_read_file.return_value = (
            ["id", "name", "parent_id"],
            [["xml_a", "A", ""], ["xml_b", "B", "xml_a"]],
        )
        # Simulate results for Pass 1 and Pass 2
        mock_run_pass.side_effect = [
            (
                {"id_map": {"xml_a": 101, "xml_b": 102}, "failed_lines": []},
                False,
            ),  # Pass 1 (results, aborted)
            (
                {"failed_writes": []},
                False,
            ),  # Pass 2 (results, aborted)
        ]
        mock_get_conn.return_value.get_model.return_value = MagicMock()

        # Act
        result = import_data(
            config_file="dummy.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv="dummy.csv",
            deferred_fields=["parent_id"],
        )

        # Assert
        assert result[0] is True
        assert mock_run_pass.call_count == 2  # Both passes should run

    @patch("odoo_data_flow.import_threaded._read_data_file")
    def test_import_data_fails_if_unique_id_not_in_header(
        self, mock_read_file: MagicMock
    ) -> None:
        """Test that the import fails if the unique_id_field is missing."""
        # Arrange
        mock_read_file.return_value = (["name"], [["A"]])  # No 'id' column

        # Act
        result, _ = import_data(
            config_file="dummy.conf",
            model="res.partner",
            unique_id_field="id",  # We expect 'id' but it's not there
            file_csv="dummy.csv",
        )

        # Assert
        assert result is False


class TestBatchingHelpers:
    """Tests for the batch creation helper functions."""

    def test_create_batches_handles_o2m_format(self) -> None:
        """Test _create_batches with the o2m flag enabled.

        Verifies that records with empty key fields are correctly grouped with
        their preceding parent record into a single batch.
        """
        # --- Arrange ---
        header = ["id", "name", "line_item"]
        data = [
            ["order1", "Order One", "item_A"],
            ["", "", "item_B"],  # Child of order1
            ["order2", "Order Two", "item_C"],
            ["", "", "item_D"],  # Child of order2
            ["", "", "item_E"],  # Child of order2
            ["order3", "Order Three", "item_F"],
        ]

        # --- Act ---
        batches = list(
            _create_batches(
                data=data,
                split_by_cols=None,  # Not grouping by column value
                header=header,
                batch_size=10,  # Batch size is large enough to not interfere
                o2m=True,
            )
        )

        # --- Assert ---
        assert len(batches) == 3
        assert batches[0][1] == [
            ["order1", "Order One", "item_A"],
            ["", "", "item_B"],
        ]
        assert batches[1][1] == [
            ["order2", "Order Two", "item_C"],
            ["", "", "item_D"],
            ["", "", "item_E"],
        ]
        assert batches[2][1] == [
            ["order3", "Order Three", "item_F"],
        ]
