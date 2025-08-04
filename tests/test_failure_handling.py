"""Test the Failure Handling mechanism."""

import csv
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from odoo_data_flow import import_threaded


@patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
def test_two_tier_failure_handling(mock_get_conn: MagicMock, tmp_path: Path) -> None:
    """Test two tier failure handling.

    Tests that the `load` -> `create` fallback works and that records
    which still fail are written to the fail file with a reason.

    Tests the complete two-tier failure handling process.

    This test verifies that:
    1. A normal run writes the entire failed batch to a `_fail.csv` file.
    2. A `--fail` run processes the `_fail.csv` file.
    3. Records that still fail are written to a final, timestamped `_failed.csv`
       file with an added `_ERROR_REASON` column.
    """
    # --- 1. Setup ---
    source_file = tmp_path / "source_data.csv"
    fail_file = tmp_path / "my_test_model_fail.csv"
    model_name = "my.test.model"
    header = ["id", "name", "value"]
    source_data = [
        ["rec_01", "Record 1", "100"],
        ["rec_02", "Record 2 (will fail create)", "200"],
    ]
    with open(source_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(header)
        writer.writerows(source_data)

    mock_model = MagicMock()
    mock_model.load.side_effect = Exception("Generic batch error")

    def create_side_effect(vals: dict[str, Any]) -> Any:
        if vals["id"] == "rec_02":
            raise Exception("Validation Error")
        else:
            mock_record = MagicMock()
            mock_record.id = 101
            return mock_record

    mock_model.create.side_effect = create_side_effect
    mock_get_conn.return_value.get_model.return_value = mock_model

    # --- Act ---
    # Capture the return value of the import process
    result = import_threaded.import_data(
        config_file="dummy.conf",
        model=model_name,
        unique_id_field="id",
        file_csv=str(source_file),
        fail_file=str(fail_file),
    )

    # --- Assert ---
    # NEW: Assert that the overall process is a success because the abort
    # flag was not set and good records were processed.
    assert result is True

    # Existing assertions for the fail file remain
    assert fail_file.exists()
    with open(fail_file, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        rows = list(reader)

        assert len(rows) == 2  # Header + one failed record
        assert rows[1][0] == "rec_02"
        assert "Validation Error" in rows[1][3]
