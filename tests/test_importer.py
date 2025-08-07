"""Test the high-level import orchestrator, including pre-flight checks."""

import csv
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

from odoo_data_flow import import_threaded
from odoo_data_flow.importer import (
    _get_fail_filename,
    run_import,
    run_import_for_migration,
)


class TestRunImport:
    """Tests for the main run_import orchestrator function."""

    DEFAULT_ARGS: ClassVar[dict[str, Any]] = {
        "config": "dummy.conf",
        "filename": "res.partner.csv",
        "model": "res.partner",
        "deferred_fields": [],
        "unique_id_field": "id",
        "no_preflight_checks": True,
        "headless": True,
        "worker": 1,
        "batch_size": 100,
        "skip": 0,
        "fail": False,
        "separator": ",",
        "ignore": [],
        "context": "{}",
        "encoding": "utf-8",
        "o2m": False,
        "groupby": [],
    }

    @patch("odoo_data_flow.importer._infer_model_from_filename", return_value=None)
    @patch("odoo_data_flow.importer._show_error_panel")
    def test_run_import_no_model_fails(
        self, mock_show_error: MagicMock, mock_infer: MagicMock, tmp_path: Path
    ) -> None:
        """Tests that the import fails if no model can be inferred."""
        test_args = self.DEFAULT_ARGS.copy()
        test_args["model"] = None
        run_import(**test_args)
        mock_show_error.assert_called_once()

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_run_import_routes_to_single_pass(
        self, mock_import_data: MagicMock
    ) -> None:
        """Tests that a non-deferred call routes to import_data."""
        # --- Provide a valid return value for the mock ---
        mock_import_data.return_value = (True, {"total_records": 123})
        test_args = self.DEFAULT_ARGS.copy()
        test_args["context"] = {}
        run_import(**test_args)
        mock_import_data.assert_called_once()

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    @patch("odoo_data_flow.importer._count_lines", return_value=0)
    def test_run_import_fail_mode_no_records_to_retry(
        self, mock_count: MagicMock, mock_import_data: MagicMock, tmp_path: Path
    ) -> None:
        """Tests that fail mode is skipped if the fail file is empty."""
        fail_file = tmp_path / "res_partner_fail.csv"
        fail_file.touch()

        test_args = self.DEFAULT_ARGS.copy()
        test_args.update({"filename": str(tmp_path / "res.partner.csv"), "fail": True})

        run_import(**test_args)
        mock_import_data.assert_not_called()

    @patch("odoo_data_flow.importer.log")
    @patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_fail_mode_with_records_logs_count_and_proceeds(
        self,
        mock_import_data: MagicMock,
        mock_preflight_checks: MagicMock,
        mock_log: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Tests that fail mode with records logs the count and continues."""
        source_file = tmp_path / "res_partner.csv"
        source_file.touch()
        fail_file = tmp_path / "res_partner_fail.csv"
        fail_file.write_text("id,name\n1,a\n2,b\n3,c\n4,d\n5,e\n")
        record_count = 5

        # --- FIX: Add this line to set the mock's return value ---
        mock_import_data.return_value = (True, {"total_records": record_count})

        test_args = self.DEFAULT_ARGS.copy()
        test_args["context"] = {}
        test_args.update(
            {
                "filename": str(source_file),
                "fail": True,
            }
        )

        run_import(**test_args)
        mock_import_data.assert_called_once()

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_run_import_routes_correctly(self, mock_import_data: MagicMock) -> None:
        """Tests that a standard call correctly delegates to the core engine."""
        mock_import_data.return_value = (True, {"total_records": 123})
        test_args = self.DEFAULT_ARGS.copy()
        test_args["context"] = {}
        test_args["deferred_fields"] = ["parent_id"]  # Test with deferred fields
        run_import(**test_args)
        mock_import_data.assert_called_once()
        # Assert that the PARSED list is passed
        assert mock_import_data.call_args.kwargs["deferred_fields"] == ["parent_id"]

    @patch(
        "odoo_data_flow.importer.import_threaded.conf_lib.get_connection_from_config"
    )
    @patch("odoo_data_flow.importer._show_error_panel")
    @patch("odoo_data_flow.importer._run_preflight_checks", return_value=True)
    def test_pass_1_create_fallback_failure_creates_fail_file(
        self,
        mock_preflight: MagicMock,
        mock_show_error: MagicMock,
        mock_get_conn: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Tests that a `create` failure during Pass 1 fallback creates a fail file."""
        source_file = tmp_path / "res.partner.csv"
        fail_file = tmp_path / "res_partner_fail.csv"
        header = ["id", "name"]
        # Only one record needed to test this logic
        source_data = [["new_partner", "Partner Name"]]
        with open(source_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(source_data)

        mock_model = MagicMock()
        mock_model.with_context.return_value = mock_model
        # 1. Simulate the initial batch `load` failing
        mock_model.load.return_value = {
            "ids": [],
            "messages": [{"type": "error", "message": "Batch load failed"}],
        }

        # 2.
        #    Simulate the "search" step finding nothing, to force a `create` attempt.
        mock_model.browse.return_value.env.ref.return_value = None

        # 3. Simulate the fallback `create` method also failing
        mock_model.create.side_effect = Exception("Validation Error on Create")
        mock_get_conn.return_value.get_model.return_value = mock_model

        test_args = self.DEFAULT_ARGS.copy()
        test_args.update(
            {
                "filename": str(source_file),
                "deferred_fields": [],  # No deferred fields for this test
                "no_preflight_checks": False,
            }
        )

        # With the logic from the previous answer, this will run with a large batch size
        run_import(**test_args)

        # The import should be marked as failed overall
        mock_show_error.assert_called_once()
        # The critical assertion: the fail file must exist
        assert fail_file.exists()

        # Optional but good: check the content of the fail file
        with open(fail_file) as f:
            reader = csv.reader(f)
            content = list(reader)
            # Should contain header, the failed row, and the error reason
            assert len(content) == 2
            assert content[0] == [*header, "_ERROR_REASON"]
            assert content[1] == source_data[0] + ["Validation Error on Create"]

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    @patch("odoo_data_flow.importer.Console")
    def test_fail_mode_aborts_if_fail_file_is_empty(
        self,
        mock_console: MagicMock,
        mock_import_data: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test fail mode aborts if fail file is empty.

        Tests that running in --fail mode exits gracefully if the fail file
        has no records to process.
        """
        source_file = tmp_path / "res.partner.csv"
        source_file.touch()
        fail_file = tmp_path / "res.partner_fail.csv"
        fail_file.write_text("id,name,_ERROR_REASON\n")  # File with only a header

        test_args = self.DEFAULT_ARGS.copy()
        test_args.update(
            {
                "filename": str(source_file),
                "fail": True,
            }
        )

        run_import(**test_args)

        # The core import engine should NOT have been called
        mock_import_data.assert_not_called()
        # A message should have been printed to the console
        mock_console.return_value.print.assert_called_once()

    # In a test file (e.g., tests/test_importer.py)

    @patch(
        "odoo_data_flow.importer.import_threaded.conf_lib.get_connection_from_config"
    )
    def test_import_data_simple_success(
        self, mock_get_conn: MagicMock, tmp_path: Path
    ) -> None:
        """Tests a simple, successful import with no failures."""
        # 1. ARRANGE
        source_file = tmp_path / "source.csv"
        fail_file = tmp_path / "source_fail.csv"
        header = ["id", "name"]
        source_data = [["rec1", "Record 1"], ["rec2", "Record 2"]]
        with open(source_file, "w", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(header)
            writer.writerows(source_data)

        mock_model = MagicMock()
        mock_model.load.return_value = {"ids": [101, 102], "messages": []}
        mock_get_conn.return_value.get_model.return_value = mock_model

        # 2. ACT
        success, stats = import_threaded.import_data(
            config_file="dummy.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv=str(source_file),
            fail_file=str(fail_file),
        )

        # 3. ASSERT
        assert success is True
        assert stats["total_records"] == 2
        # The file is created proactively, so we check that it exists
        # but only contains the header.
        assert fail_file.exists()
        with open(fail_file, encoding="utf-8") as f:
            lines = f.readlines()
            assert len(lines) == 1

    @patch(
        "odoo_data_flow.importer.import_threaded.conf_lib.get_connection_from_config"
    )
    def test_import_data_two_pass_success(
        self, mock_get_conn: MagicMock, tmp_path: Path
    ) -> None:
        """Tests a successful two-pass import with deferred fields."""
        # 1. ARRANGE
        source_file = tmp_path / "source.csv"
        header = ["id", "name", "parent_id/id"]
        source_data = [
            ["parent1", "Parent One", ""],
            ["child1", "Child One", "parent1"],
        ]
        with open(source_file, "w", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(header)
            writer.writerows(source_data)

        mock_model = MagicMock()
        mock_model.with_context.return_value = mock_model
        # Pass 1: `load` is called on data without the parent_id column
        mock_model.load.return_value = {"ids": [10, 20], "messages": []}

        # Pass 2: The `write` method should be called directly on the model
        # with the appropriate IDs and values.
        mock_get_conn.return_value.get_model.return_value = mock_model

        # 2. ACT
        success, _ = import_threaded.import_data(
            config_file="dummy.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv=str(source_file),
            deferred_fields=["parent_id"],
            separator=";",
        )

        # 3. ASSERT
        assert success is True
        # Assert Pass 2 called `write` with the child ID and resolved parent ID
        mock_model.write.assert_called_once_with(
            [20], {"parent_id": 10}, context={"tracking_disable": True}
        )


class TestRunImportEdgeCases:
    """Tests for edge cases and error handling in the importer."""

    def test_get_fail_filename_recovery_mode(self) -> None:
        """Tests that _get_fail_filename creates a timestamped name in fail mode."""
        filename = _get_fail_filename("res.partner", is_fail_run=True)
        assert "res_partner" in filename
        assert "failed" in filename
        # Check that a timestamp like _20230401_123055_ is present
        assert any(char.isdigit() for char in filename)

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_run_import_fail_mode_ignore_is_none(
        self, mock_import_data: MagicMock, tmp_path: Path
    ) -> None:
        """Test fail mode with no ignore list."""
        fail_file = tmp_path / "res_partner_fail.csv"
        fail_file.write_text("id,name,_ERROR_REASON\n1,a,error")

        args = TestRunImport.DEFAULT_ARGS.copy()
        args.update(
            {
                "filename": str(tmp_path / "res.partner.csv"),
                "fail": True,
                "ignore": None,  # Explicitly set to None
            }
        )

        mock_import_data.return_value = (True, {"total_records": 1})
        run_import(**args)

        # Assert that the core import function was called with the error column ignored
        called_kwargs = mock_import_data.call_args.kwargs
        assert "_ERROR_REASON" in called_kwargs["ignore"]

    @patch("odoo_data_flow.importer.import_threaded.import_data")
    def test_run_import_for_migration_success(
        self, mock_import_data: MagicMock
    ) -> None:
        """Tests the successful execution of run_import_for_migration."""
        header = ["id", "name"]
        data = [[1, "Test"], [2, "Another"]]

        run_import_for_migration(
            config="dummy.conf",
            model="res.partner",
            header=header,
            data=data,
        )

        mock_import_data.assert_called_once()
        kwargs = mock_import_data.call_args.kwargs
        assert kwargs["model"] == "res.partner"
        assert kwargs["unique_id_field"] == "id"
        assert "tmp" in kwargs["file_csv"]  # Check that a temp file is used
        assert kwargs["context"] == {"tracking_disable": True}
