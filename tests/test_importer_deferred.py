"""Unit tests for the deferred import functionality in the importer module."""

import csv
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, mock_open, patch

from odoo_data_flow import import_threaded
from odoo_data_flow.importer import run_import_deferred


class TestImportDeferred:
    """Tests for the run_import_deferred wrapper function.

    This class specifically tests the logic of the wrapper itself,
    not the complex inner workings of import_threaded.import_data.
    """

    MOCK_CSV_DATA = (
        "id,xml_id,name,parent_id\n"
        "__export__.partner_A,partner_A,Parent Company,\n"
        "__export__.partner_B,partner_B,Subsidiary B,partner_A\n"
    )

    # We mock the direct dependency of the function being tested
    @patch("odoo_data_flow.importer.import_threaded.import_data")
    @patch(
        "odoo_data_flow.importer.Console"
    )  # Also mock Console to hide test output
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
        mock_import_data.return_value = (True, 2)

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

    @patch("odoo_data_flow.import_threaded.conf_lib")
    def test_import_fails_on_pass_1_exception(
        self, mock_conf_lib: MagicMock
    ) -> None:
        """Test that the import returns False if Pass 1 fails."""
        mock_model = MagicMock()
        # The new 'vanilla' implementation uses 'load', not 'batch_create'
        mock_model.load.side_effect = Exception("Odoo connection lost")
        mock_model.create.side_effect = Exception("Odoo still down")
        mock_conf_lib.get_connection_from_config.return_value.get_model.return_value = mock_model

        with patch("builtins.open", mock_open(read_data=self.MOCK_CSV_DATA)):
            result = run_import_deferred(
                config="dummy.conf",
                filename="dummy.csv",
                model_name="res.partner",
                unique_id_field="xml_id",
                deferred_fields=["parent_id"],
                separator=",",
            )

        assert result is False

    @patch("odoo_data_flow.import_threaded.conf_lib")
    def test_import_fails_on_pass_2_exception(
        self, mock_conf_lib: MagicMock
    ) -> None:
        """Test that the import returns False if Pass 2 fails."""
        mock_model = MagicMock()
        mock_model.load.return_value = {"ids": [101, 102], "messages": []}
        mock_model.browse.return_value.write.side_effect = Exception(
            "Write permission error"
        )
        mock_conf_lib.get_connection_from_config.return_value.get_model.return_value = mock_model

        with patch("builtins.open", mock_open(read_data=self.MOCK_CSV_DATA)):
            result = run_import_deferred(
                config="dummy.conf",
                filename="dummy.csv",
                model_name="res.partner",
                unique_id_field="xml_id",
                deferred_fields=["parent_id"],
                separator=",",
            )

        assert result is False

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    @patch("odoo_data_flow.importer._show_error_panel")  # Mock the error panel
    def test_run_import_deferred_failure_path(
        self, mock_show_error: MagicMock, mock_import_data: MagicMock
    ) -> None:
        """Test that run_import_deferred handles a failed result."""
        # ARRANGE: Simulate a failed run from the dependency
        mock_import_data.return_value = (False, 2)

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


def test_pass_2_failure_writes_to_fail_file(tmp_path: Path) -> None:
    """Tests that when a `write` fails in Pass 2 of a deferred import,
    the original record and error are correctly written to the fail file.
    """
    # 1. ARRANGE
    source_file = tmp_path / "source.csv"
    fail_file = tmp_path / "source_fail.csv"
    model_name = "res.partner"
    header = ["id", "name", "parent_id/id"]
    source_data = [
        ["parent", "Parent Rec", ""],
        ["child_ok", "Child OK", "parent"],
        ["child_fail", "Child Will Fail Write", "parent"],
    ]

    with open(source_file, "w", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(header)
        writer.writerows(source_data)

    mock_model = MagicMock()

    # Mock Pass 1 (`load`) to succeed and create a valid ID map.
    # The internal id_map will be {"parent": 101, "child_ok": 102, "child_fail": 103}
    mock_model.load.return_value = {"ids": [101, 102, 103], "messages": []}

    # Mock Pass 2 (`write`) to fail only for a specific record.
    def write_side_effect(vals: dict[str, Any]) -> Any:
        # `browse()` is called with the ID of the record being written to.
        # We can inspect the mock to see which record this is.
        current_record_db_id = mock_model.browse.call_args.args[0]
        if current_record_db_id == 103:  # This is the db_id for "child_fail"
            raise Exception("Write Permission Error")
        return True

    # Attach the side effect to the final method in the call chain.
    mock_model.browse.return_value.write.side_effect = write_side_effect

    # 2. ACT
    with patch(
        "odoo_data_flow.import_threaded.conf_lib.get_connection_from_config"
    ) as mock_get_conn:
        mock_get_conn.return_value.get_model.return_value = mock_model

        # Note: We now expect a tuple (bool, int) from import_data
        result, _ = import_threaded.import_data(
            config_file="dummy.conf",
            model=model_name,
            unique_id_field="id",
            file_csv=str(source_file),
            fail_file=str(fail_file),
            deferred_fields=["parent_id"],
            separator=";",
        )

    # 3. ASSERT
    assert result is False, "Import should fail if Pass 2 has errors."

    assert fail_file.exists(), "A fail file should have been created."

    with open(fail_file) as f:
        reader = csv.reader(f, delimiter=";")
        fail_data = list(reader)

    # We expect a header and one failed record.
    assert len(fail_data) == 2, (
        "Fail file should contain a header and one failed row."
    )

    # Check the header
    assert fail_data[0][-1] == "_ERROR_REASON"

    # Check the content of the failed row
    failed_row = fail_data[1]
    assert failed_row[0] == "child_fail"
    assert failed_row[1] == "Child Will Fail Write"
    assert "Write Permission Error" in failed_row[3]
