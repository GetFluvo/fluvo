"""Test the Export Handling mechanism."""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from odoo_data_flow.export_threaded import (
    RPCThreadExport,
    _clean_batch,
    export_data,
)


@pytest.fixture
def mock_conf_lib() -> Generator[MagicMock, None, None]:
    """Fixture to mock conf_lib.get_connection_from_config."""
    with patch(
        "odoo_data_flow.export_threaded.conf_lib.get_connection_from_config"
    ) as mock_conn:
        # Setup a default mock connection object
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


class TestRPCThreadExport:
    """Tests for the RPCThreadExport class."""

    def test_execute_batch_technical_names(self) -> None:
        """Tests that model.read is called when technical_names is True."""
        mock_model = MagicMock()
        thread = RPCThreadExport(1, mock_model, ["id"], technical_names=True)
        thread._execute_batch([1], 1)
        mock_model.read.assert_called_once_with([1], ["id"])
        mock_model.export_data.assert_not_called()

    def test_execute_batch_export_data(self) -> None:
        """Tests that model.export_data is called when technical_names is False."""
        mock_model = MagicMock()
        mock_model.export_data.return_value = {"datas": [["Test"]]}
        thread = RPCThreadExport(1, mock_model, ["name"], technical_names=False)
        result = thread._execute_batch([1], 1)
        mock_model.export_data.assert_called_once()
        mock_model.read.assert_not_called()
        assert result == [{"name": "Test"}]


class TestCleanBatch:
    """Tests for the _clean_batch helper function."""

    def test_clean_batch_converts_false_correctly(self) -> None:
        """Tests that False is converted to None for non-booleans."""
        dirty_data = [
            {"id": 1, "name": "Test", "is_company": True, "phone": False},
            {"id": 2, "name": False, "is_company": False, "phone": "12345"},
        ]
        field_types = {
            "id": "integer",
            "name": "char",
            "is_company": "boolean",
            "phone": "char",
        }
        cleaned_df = _clean_batch(dirty_data, field_types)
        expected_df = pl.DataFrame(
            {
                "id": [1, 2],
                "name": ["Test", None],
                "is_company": [True, False],
                "phone": [None, "12345"],
            }
        )
        assert_frame_equal(cleaned_df, expected_df)

    def test_clean_batch_empty_input(self) -> None:
        """Tests that an empty list is handled correctly."""
        assert _clean_batch([], {}).is_empty()


class TestExportData:
    """Tests for the main export_data orchestrator function."""

    def test_export_to_file_success(
        self, mock_conf_lib: MagicMock, tmp_path: Path
    ) -> None:
        """Tests the success path where data is streamed to a CSV file."""
        output_file = tmp_path / "output.csv"
        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.search.return_value = [1, 2]
        mock_model.read.return_value = [
            {"id": 1, "name": "Test 1", "is_company": True, "phone": False},
            {"id": 2, "name": "Test 2", "is_company": False, "phone": "123"},
        ]

        result_df = export_data(
            config_file="dummy.conf",
            model="res.partner",
            domain=[],
            header=["id", "name", "is_company", "phone"],
            output=str(output_file),
            technical_names=True,
        )

        assert output_file.exists()
        assert isinstance(result_df, pl.DataFrame)
        # Check content of the written file
        read_df = pl.read_csv(output_file, separator=";")
        assert read_df["phone"][0] is None
        assert read_df["is_company"][1] is False

    def test_export_in_memory_success(self, mock_conf_lib: MagicMock) -> None:
        """Tests the success path for an in-memory export."""
        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.search.return_value = [1]
        mock_model.read.return_value = [
            {"id": 1, "name": "In-Memory", "is_company": False, "phone": False}
        ]

        result_df = export_data(
            config_file="dummy.conf",
            model="res.partner",
            domain=[],
            header=["id", "name", "is_company", "phone"],
            output=None,
            technical_names=True,
        )

        assert isinstance(result_df, pl.DataFrame)
        assert len(result_df) == 1
        assert result_df["phone"][0] is None
        assert result_df["is_company"][0] is False

    def test_export_handles_connection_failure(self) -> None:
        """Tests that None is returned if the initial connection fails."""
        with patch(
            "odoo_data_flow.export_threaded.conf_lib.get_connection_from_config",
            side_effect=Exception("Connection Error"),
        ):
            result = export_data(
                config_file="bad.conf",
                model="res.partner",
                domain=[],
                header=["id"],
                output="fail.csv",
            )
        assert result is None

    def test_export_handles_no_records_found(
        self, mock_conf_lib: MagicMock, tmp_path: Path
    ) -> None:
        """Tests behavior when no records match the domain."""
        output_file = tmp_path / "empty.csv"
        mock_model = mock_conf_lib.return_value.get_model.return_value
        mock_model.search.return_value = []  # No records found

        result_df = export_data(
            config_file="dummy.conf",
            model="res.partner",
            domain=[],
            header=["id", "name"],
            output=str(output_file),
        )

        assert output_file.exists()
        assert isinstance(result_df, pl.DataFrame)
        assert result_df.is_empty()
        # Ensure the file was created with only a header
        with open(output_file) as f:
            assert f.read().strip() == "id;name"
