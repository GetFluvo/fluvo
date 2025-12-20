import csv
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from odoo_data_flow import import_threaded


def debug_fallback_handles_malformed_rows():
    """Debug test that the fallback handles malformed rows."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. ARRANGE
        source_file = tmp_path / "source.csv"
        fail_file = tmp_path / "source_fail.csv"
        model_name = "res.partner"
        header = ["id", "name", "value"]  # Expects 3 columns
        source_data = [
            ["rec_ok", "Good Record", "100"],
            ["rec_bad", "Bad Record"],  # This row is malformed (only 2 columns)
        ]
        with open(source_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(source_data)

        mock_model = MagicMock()
        mock_model.with_context.return_value = mock_model
        mock_model.load.side_effect = Exception("Load fails, trigger fallback")
        mock_model.browse.return_value.env.ref.return_value = (
            None  # Ensure create is attempted
        )

        # 2. ACT
        with patch(
            "odoo_data_flow.import_threaded.conf_lib.get_connection_from_config"
        ) as mock_get_conn:
            mock_get_conn.return_value.get_model.return_value = mock_model

            def mock_create(vals: dict[str, Any], context=None) -> Any:
                record = MagicMock()
                record.id = 1
                return record

            mock_model.create.side_effect = mock_create
            result, _ = import_threaded.import_data(
                config="dummy.conf",
                model=model_name,
                unique_id_field="id",
                file_csv=str(source_file),
                fail_file=str(fail_file),
                separator=",",
            )

        # Debug output
        print(f"Result: {result}")
        print(f"Fail file exists: {fail_file.exists()}")
        if fail_file.exists():
            with open(fail_file) as f:
                reader = csv.reader(f, delimiter=",")
                fail_content = list(reader)

            print(f"Fail content rows: {len(fail_content)}")
            for i, row in enumerate(fail_content):
                print(f"  Row {i}: {row}")


def debug_fallback_with_dirty_csv():
    """Debug test with dirty CSV."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. ARRANGE
        source_file = tmp_path / "dirty.csv"
        fail_file = tmp_path / "dirty_fail.csv"
        model_name = "res.partner"
        header = ["id", "name", "email"]
        # CSV content with various issues
        dirty_data = [
            ["ok_1", "Normal Record", "ok1@test.com"],
            ["bad_cols"],  # Malformed row, too few columns
            ["ok_2", "Another Good One", "ok2@test.com"],
            [],  # Empty row
        ]
        with open(source_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(dirty_data)

        mock_model = MagicMock()
        mock_model.load.side_effect = Exception("Load fails, forcing fallback")
        mock_model.browse.return_value.env.ref.return_value = None  # Force create
        mock_model.with_context.return_value = (
            mock_model  # Mock with_context to return self
        )

        # Mock the create method to return a simple mock record
        def mock_create(vals: dict[str, Any], context=None) -> Any:
            record = MagicMock()
            record.id = 1
            return record

        mock_model.create.side_effect = mock_create

        with patch(
            "odoo_data_flow.import_threaded.conf_lib.get_connection_from_config"
        ) as mock_get_conn:
            mock_get_conn.return_value.get_model.return_value = mock_model

            # 2. ACT
            result, _ = import_threaded.import_data(
                config="dummy.conf",
                model=model_name,
                unique_id_field="id",
                file_csv=str(source_file),
                fail_file=str(fail_file),
                separator=",",
            )

        # Debug output
        print(f"Result: {result}")
        print(f"Fail file exists: {fail_file.exists()}")
        if fail_file.exists():
            with open(fail_file, encoding="utf-8") as f:
                reader = csv.reader(f)
                failed_rows = list(reader)

            print(f"Failed rows: {len(failed_rows)}")
            for i, row in enumerate(failed_rows):
                print(f"  Row {i}: {row}")


if __name__ == "__main__":
    print("=== Debug malformed rows test ===")
    debug_fallback_handles_malformed_rows()

    print("\n=== Debug dirty CSV test ===")
    debug_fallback_with_dirty_csv()
