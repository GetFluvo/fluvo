"""Tests for the refactored, low-level, multi-threaded import logic."""

from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from rich.progress import Progress

from odoo_data_flow.import_threaded import (
    _count_csv_rows,
    _create_batch_individually,
    _create_batches,
    _execute_load_batch,
    _execute_write_batch,
    _extract_per_row_errors,
    _filter_ignored_columns,
    _format_odoo_error,
    _load_batch_with_binary_fallback,
    _orchestrate_pass_1,
    _orchestrate_pass_2,
    _prepare_pass_2_data,
    _read_data_file,
    _setup_fail_file,
    _stream_csv_batches,
    _warn_empty_ids,
    import_data,
)


class TestImportData:
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
            config="dummy.conf",
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
            config="dummy.conf",
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
            config="dummy.conf",
            model="res.partner",
            unique_id_field="id",  # We expect 'id' but it's not there
            file_csv="dummy.csv",
        )

        # Assert
        assert result is False

    @patch("odoo_data_flow.import_threaded._create_batches")
    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_orchestrate_pass_1_does_not_sort_for_o2m(
        self, mock_run_pass: MagicMock, mock_create_batches: MagicMock
    ) -> None:
        """Verify Pass 1 does NOT sort data when o2m is True."""
        mock_run_pass.return_value = ({}, False)
        header = ["id", "name", "parent_id"]
        data = [
            ["child1", "C1", "parent1"],
            ["parent1", "P1", ""],
        ]

        with Progress() as progress:
            _orchestrate_pass_1(
                progress,
                MagicMock(),
                "res.partner",
                MagicMock(),  # connection
                header,
                data,
                "id",
                [],
                [],
                {},
                None,
                None,
                1,
                10,
                batch_delay=0.0,
                o2m=True,
                split_by_cols=None,
            )

        # Check that the data passed to _create_batches was NOT sorted
        call_args = mock_create_batches.call_args[0]
        unsorted_data = call_args[0]
        assert unsorted_data[0][0] == "child1"
        assert unsorted_data[1][0] == "parent1"


class TestExecuteLoadBatch:
    """Tests for the _execute_load_batch function's resilience features."""

    @patch("odoo_data_flow.import_threaded._create_batch_individually")
    def test_batch_scales_down_on_memory_error(
        self, mock_create_individually: MagicMock
    ) -> None:
        """Verify batch size is reduced on memory errors and eventually succeeds."""
        mock_model = MagicMock()
        # Fail on batches of 4, then 2, then succeed on 1
        mock_model.load.side_effect = [
            Exception("out of memory"),
            Exception("memory error"),
            {"ids": [1]},
            {"ids": [2]},
            {"ids": [3]},
            {"ids": [4]},
        ]
        mock_progress = MagicMock()
        thread_state = {
            "model": mock_model,
            "progress": mock_progress,
            "unique_id_field_index": 0,
            "ignore_list": [],
        }
        batch_header = ["id", "name"]
        batch_lines = [
            ["rec1", "A"],
            ["rec2", "B"],
            ["rec3", "C"],
            ["rec4", "D"],
        ]

        result = _execute_load_batch(thread_state, batch_lines, batch_header, 1)

        assert result["success"] is True
        assert len(result["id_map"]) == 4
        assert result["id_map"] == {"rec1": 1, "rec2": 2, "rec3": 3, "rec4": 4}
        assert mock_model.load.call_count == 6
        mock_create_individually.assert_not_called()
        mock_progress.console.print.assert_any_call(
            "[yellow]WARN:[/] Batch 1 hit transient error (out of memory). "
            "Reducing chunk size to 2."
        )
        mock_progress.console.print.assert_any_call(
            "[yellow]WARN:[/] Batch 1 hit transient error (memory). "
            "Reducing chunk size to 1."
        )

    @patch("odoo_data_flow.import_threaded.time.sleep")
    @patch("odoo_data_flow.import_threaded._create_batch_individually")
    def test_batch_scales_down_on_gateway_error(
        self, mock_create_individually: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Verify batch size is reduced on 502 gateway errors."""
        mock_model = MagicMock()
        mock_model.load.side_effect = [
            Exception("502 Bad Gateway"),
            {"ids": [1, 2]},
            {"ids": [3, 4]},
        ]
        mock_progress = MagicMock()
        thread_state = {
            "model": mock_model,
            "progress": mock_progress,
            "unique_id_field_index": 0,
            "ignore_list": [],
        }
        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"], ["rec2", "B"], ["rec3", "C"], ["rec4", "D"]]

        result = _execute_load_batch(thread_state, batch_lines, batch_header, 1)

        assert result["success"] is True
        assert len(result["id_map"]) == 4
        assert mock_model.load.call_count == 3
        mock_create_individually.assert_not_called()
        # Verify both adaptive throttle and batch reduction messages were shown
        # Note: the server overload message has jitter in the delay, so check prefix
        calls = [str(c) for c in mock_progress.console.print.call_args_list]
        assert any(
            "Server overload detected (502). Backing off for" in c
            and "(attempt 1)" in c
            for c in calls
        ), f"Server overload message not found in: {calls}"
        mock_progress.console.print.assert_any_call(
            "[yellow]WARN:[/] Batch 1 hit transient error (502). "
            "Reducing chunk size to 2."
        )

    @patch("odoo_data_flow.import_threaded._load_batch_with_binary_fallback")
    def test_batch_falls_back_for_non_scalable_error(
        self, mock_binary_fallback: MagicMock
    ) -> None:
        """Verify fallback to binary search for regular errors."""
        mock_model = MagicMock()
        mock_model.load.side_effect = [ValueError("Invalid field value")]
        mock_binary_fallback.return_value = {
            "id_map": {"rec1": 1},
            "failed_lines": [["rec2", "B", "Error"]],
            "success": False,
        }
        mock_progress = MagicMock()
        thread_state = {
            "model": mock_model,
            "progress": mock_progress,
            "unique_id_field_index": 0,
            "ignore_list": [],
        }
        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"], ["rec2", "B"]]

        result = _execute_load_batch(thread_state, batch_lines, batch_header, 1)

        assert result["success"] is True
        assert result["id_map"] == {"rec1": 1}
        assert len(result["failed_lines"]) == 1
        mock_model.load.assert_called_once()
        mock_binary_fallback.assert_called_once()


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

    def test_create_batches_no_data(self) -> None:
        """Test that _create_batches handles empty data."""
        header = ["id", "name"]
        data: list[list[str]] = []
        batches = list(_create_batches(data, None, header, 10, False))
        assert len(batches) == 0


class TestPass2Batching:
    """Tests for the Pass 2 batching and writing logic."""

    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_pass_2_groups_writes_correctly(self, mock_run_pass: MagicMock) -> None:
        """Verify that Pass 2 groups records by identical write values."""
        # Arrange
        mock_run_pass.return_value = ({}, False)  # Simulate a successful run
        mock_model = MagicMock()
        header = ["id", "name", "parent_id", "user_id"]
        all_data = [
            ["c1", "C1", "p1", "u1"],
            ["c2", "C2", "p1", "u1"],
            ["c3", "C3", "p2", "u1"],
            ["c4", "C4", "p2", "u2"],
        ]
        id_map = {
            "c1": 1,
            "c2": 2,
            "c3": 3,
            "c4": 4,
            "p1": 101,
            "p2": 102,
            "u1": 201,
            "u2": 202,
        }
        deferred_fields = ["parent_id", "user_id"]

        # Act
        with Progress() as progress:
            _orchestrate_pass_2(
                progress,
                mock_model,
                "res.partner",
                header,
                all_data,
                "id",
                id_map,
                deferred_fields,
                {},
                MagicMock(),
                MagicMock(),
                max_connection=1,
                batch_size=10,
            )

        # Assert
        assert mock_run_pass.call_count == 1

        # Get the super-batches that were passed to the runner
        call_args = mock_run_pass.call_args[0]
        super_batches = list(call_args[2])  # The batches iterable

        # With batch_size=10 and only 4 records, all 3 write operations
        # should be aggregated into 1 super-batch
        assert len(super_batches) == 1

        # Extract all write operations from the super-batch
        # Format: (batch_number, [list of (ids, vals) tuples])
        _batch_number, write_ops = super_batches[0]
        assert len(write_ops) == 3  # Three unique sets of values

        # Convert to a dict for easier checking
        batch_dict = {frozenset(vals.items()): ids for (ids, vals) in write_ops}

        # Check group 1: parent=p1, user=u1
        group1_key = frozenset({"parent_id": 101, "user_id": 201}.items())
        assert group1_key in batch_dict
        assert sorted(batch_dict[group1_key]) == [1, 2]

        # Check group 2: parent=p2, user=u1
        group2_key = frozenset({"parent_id": 102, "user_id": 201}.items())
        assert group2_key in batch_dict
        assert batch_dict[group2_key] == [3]

        # Check group 3: parent=p2, user=u2
        group3_key = frozenset({"parent_id": 102, "user_id": 202}.items())
        assert group3_key in batch_dict
        assert batch_dict[group3_key] == [4]

    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_pass_2_handles_failed_batch(self, mock_run_pass: MagicMock) -> None:
        """Verify that a failed batch write in Pass 2 is handled correctly."""
        # Arrange
        mock_fail_writer = MagicMock()
        mock_model = MagicMock()

        header = ["id", "name", "parent_id"]
        all_data = [["c1", "C1", "p1"], ["c2", "C2", "p1"]]
        id_map = {"c1": 1, "c2": 2, "p1": 101}
        deferred_fields = ["parent_id"]

        # Simulate a failure from the threaded runner for this batch
        failed_write_result = {
            "failed_writes": [
                (1, {"parent_id": 101}, "Access Error"),
                (2, {"parent_id": 101}, "Access Error"),
            ],
        }
        mock_run_pass.return_value = (failed_write_result, False)  # result, aborted

        # Act
        with Progress() as progress:
            result = _orchestrate_pass_2(
                progress,
                mock_model,
                "res.partner",
                header,
                all_data,
                "id",
                id_map,
                deferred_fields,
                {},
                mock_fail_writer,
                MagicMock(),  # fail_handle
                max_connection=1,
                batch_size=10,
            )

        # Assert
        assert result[0] is False  # The orchestration should report failure
        mock_fail_writer.writerows.assert_called_once()

        # Check that the rows written to the fail file are correct
        failed_rows = mock_fail_writer.writerows.call_args[0][0]
        assert len(failed_rows) == 2
        assert failed_rows[0] == ["c1", "C1", "p1", "Access Error"]
        assert failed_rows[1] == ["c2", "C2", "p1", "Access Error"]

    def test_orchestrate_pass_2_no_relations(self) -> None:
        """Test that Pass 2 handles no relations to update."""
        mock_model = MagicMock()
        header = ["id", "name"]
        all_data = [["c1", "C1"], ["c2", "C2"]]
        id_map = {"c1": 1, "c2": 2}
        deferred_fields: list[str] = []
        with Progress() as progress:
            result, updates = _orchestrate_pass_2(
                progress,
                mock_model,
                "res.partner",
                header,
                all_data,
                "id",
                id_map,
                deferred_fields,
                {},
                None,
                None,
                1,
                10,
            )
        assert result is True
        assert updates == 0


class TestImportThreadedEdgeCases:
    """Tests for edge cases and error handling in import_threaded.py."""

    def test_format_odoo_error_not_a_string(self) -> None:
        """Test that _format_odoo_error handles non-string errors."""
        error_obj = {"key": "value"}
        formatted = _format_odoo_error(error_obj)
        assert formatted == "{'key': 'value'}"

    def test_format_odoo_error_fallback(self) -> None:
        """Test that _format_odoo_error handles non-dictionary strings."""
        error_string = "A simple error message"
        formatted = _format_odoo_error(error_string)
        assert formatted == "A simple error message"

    def test_read_data_file_not_found(self) -> None:
        """Test that _read_data_file handles a FileNotFoundError."""
        header, data = _read_data_file("non_existent_file.csv", ",", "utf-8", 0)
        assert header == []
        assert data == []

    @patch("builtins.open", side_effect=ValueError("bad file"))
    def test_read_data_file_general_exception(self, mock_open: MagicMock) -> None:
        """Test that _read_data_file handles a general exception."""
        with pytest.raises(ValueError):
            _read_data_file("any.csv", ",", "utf-8", 0)

    def test_read_data_file_no_id_column(self, tmp_path: Path) -> None:
        """Test that a ValueError is raised if the 'id' column is missing."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("name,age\nAlice,30")
        with pytest.raises(
            ValueError, match=r"Source file must contain an 'id' column."
        ):
            _read_data_file(str(source_file), ",", "utf-8", 0)

    def test_read_data_file_unicode_and_multiline(self, tmp_path: Path) -> None:
        """Test that Unicode characters and multiline values are preserved."""
        import csv

        source_file = tmp_path / "unicode_test.csv"
        # Write test data with Unicode and multiline content
        test_rows = [
            ["id", "name", "note"],
            ["test_1", "日本語テスト", "Line 1\nLine 2\nLine 3"],
            ["test_2", "中文测试", "Tabs\there\tand\nnewlines"],
            ["test_3", "한국어 테스트", "Special: äöü ñ é"],
            ["test_4", "Emoji 🎉🚀", 'Contains "quotes"'],
        ]
        with open(source_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_ALL)
            writer.writerows(test_rows)

        # Read back using _read_data_file
        header, data = _read_data_file(str(source_file), ";", "utf-8", 0)

        assert header == ["id", "name", "note"]
        assert len(data) == 4

        # Verify Unicode preserved
        assert data[0][1] == "日本語テスト"
        assert data[1][1] == "中文测试"
        assert data[2][1] == "한국어 테스트"
        assert data[3][1] == "Emoji 🎉🚀"

        # Verify multiline preserved
        assert data[0][2] == "Line 1\nLine 2\nLine 3"
        assert "\n" in data[1][2]
        assert "\t" in data[1][2]

        # Verify quotes preserved
        assert '"quotes"' in data[3][2]

    @patch("builtins.open", side_effect=OSError("Permission denied"))
    def test_setup_fail_file_os_error(self, mock_open: MagicMock) -> None:
        """Test that _setup_fail_file handles an OSError."""
        writer, handle = _setup_fail_file("fail.csv", ["id"], ",", "utf-8")
        assert writer is None
        assert handle is None

    def test_create_batch_individually_malformed_row(self) -> None:
        """Test handling of malformed rows."""
        mock_model = MagicMock()
        mock_connection = MagicMock()
        # Configure ir.model.data mock to return empty search results
        mock_connection.get_model.return_value.search.return_value = []
        batch_header = ["id", "name"]
        # This row has only one column, but the header has two
        batch_lines = [["record1"]]

        result = _create_batch_individually(
            mock_model, mock_connection, batch_lines, batch_header, 0, {}, []
        )

        assert len(result["failed_lines"]) == 1
        assert "Row has 1 columns, but header has 2" in result["failed_lines"][0][-1]
        assert result["error_summary"] == "Malformed CSV row detected"

    @patch(
        "odoo_data_flow.import_threaded.concurrent.futures.as_completed",
        side_effect=KeyboardInterrupt,
    )
    def test_run_threaded_pass_keyboard_interrupt(
        self, mock_as_completed: MagicMock
    ) -> None:
        """Test that a KeyboardInterrupt is handled gracefully."""
        from odoo_data_flow.import_threaded import RPCThreadImport, _run_threaded_pass

        rpc_thread = RPCThreadImport(1, Progress(), MagicMock())
        rpc_thread.task_id = rpc_thread.progress.add_task("test")
        target_func = MagicMock()
        target_func.__name__ = "mock_func"
        with patch.object(rpc_thread, "spawn_thread", return_value=MagicMock()):
            _, aborted = _run_threaded_pass(rpc_thread, target_func, [(1, {})], {})
            assert aborted is True

    @patch(
        "odoo_data_flow.import_threaded.conf_lib.get_connection_from_config",
        side_effect=Exception("Conn fail"),
    )
    def test_import_data_connection_failure(self, mock_get_conn: MagicMock) -> None:
        """Test that import_data handles a connection failure gracefully."""
        # Arrange
        with patch(
            "odoo_data_flow.import_threaded._read_data_file",
            return_value=(["id"], [["a"]]),
        ):
            # Act
            success, count = import_data("dummy.conf", "res.partner", "id", "dummy.csv")

            # Assert
            assert success is False
            assert count == {}

    @patch("odoo_data_flow.import_threaded._read_data_file", return_value=([], []))
    def test_import_data_no_header(self, mock_read_file: MagicMock) -> None:
        """Test that import_data handles a CSV with no header."""
        success, stats = import_data("dummy.conf", "res.partner", "id", "dummy.csv")
        assert success is False
        assert stats == {}

    @patch("odoo_data_flow.lib.internal.ui._show_error_panel")
    @patch(
        "odoo_data_flow.import_threaded.conf_lib.get_connection_from_config",
        side_effect=Exception("Conn fail"),
    )
    def test_import_data_connection_failure_shows_panel(
        self, mock_get_conn: MagicMock, mock_show_error: MagicMock
    ) -> None:
        """Test that import_data shows the error panel on connection failure."""
        # Arrange
        with patch(
            "odoo_data_flow.import_threaded._read_data_file",
            return_value=(["id"], [["a"]]),
        ):
            # Act
            import_data("dummy.conf", "res.partner", "id", "dummy.csv")

            # Assert
            mock_show_error.assert_called_once()
            call_args, _ = mock_show_error.call_args
            assert call_args[0] == "Odoo Connection Error"
            assert "Could not connect to Odoo" in call_args[1]

    def test_filter_ignored_columns(self) -> None:
        """Test that ignored columns are correctly filtered."""
        from odoo_data_flow.import_threaded import _filter_ignored_columns

        header = ["id", "name", "age", "city"]
        data = [
            ["1", "Alice", "30", "New York"],
            ["2", "Bob", "25", "London"],
        ]
        ignore = ["age", "city"]
        new_header, new_data = _filter_ignored_columns(ignore, header, data)
        assert new_header == ["id", "name"]
        assert new_data == [["1", "Alice"], ["2", "Bob"]]


class TestXmlIdCreation:
    """Tests for XML ID creation when using create() method."""

    def test_create_xmlid_entry_with_module_prefix(self) -> None:
        """Test XML ID creation with module prefix (e.g., 'my_module.identifier')."""
        from odoo_data_flow.import_threaded import _create_xmlid_entry

        mock_connection = MagicMock()
        mock_ir_model_data = MagicMock()
        mock_ir_model_data.search.return_value = []  # No existing entry
        mock_connection.get_model.return_value = mock_ir_model_data

        result = _create_xmlid_entry(
            mock_connection, "my_module.partner_001", 42, "res.partner"
        )

        assert result is True
        mock_ir_model_data.create.assert_called_once_with(
            {
                "module": "my_module",
                "name": "partner_001",
                "model": "res.partner",
                "res_id": 42,
            }
        )

    def test_create_xmlid_entry_without_module_prefix(self) -> None:
        """Test XML ID creation without module prefix (uses __import__)."""
        from odoo_data_flow.import_threaded import _create_xmlid_entry

        mock_connection = MagicMock()
        mock_ir_model_data = MagicMock()
        mock_ir_model_data.search.return_value = []  # No existing entry
        mock_connection.get_model.return_value = mock_ir_model_data

        result = _create_xmlid_entry(mock_connection, "PARTNER_001", 42, "res.partner")

        assert result is True
        mock_ir_model_data.create.assert_called_once_with(
            {
                "module": "__import__",
                "name": "PARTNER_001",
                "model": "res.partner",
                "res_id": 42,
            }
        )

    def test_create_xmlid_entry_existing_entry_same_res_id(self) -> None:
        """Test that existing entries with same res_id are not updated."""
        from odoo_data_flow.import_threaded import _create_xmlid_entry

        mock_connection = MagicMock()
        mock_ir_model_data = MagicMock()
        mock_ir_model_data.search.return_value = [1]  # Existing entry ID
        mock_ir_model_data.read.return_value = {"res_id": 42, "model": "res.partner"}
        mock_connection.get_model.return_value = mock_ir_model_data

        result = _create_xmlid_entry(
            mock_connection, "my_module.partner_001", 42, "res.partner"
        )

        assert result is True
        mock_ir_model_data.create.assert_not_called()
        mock_ir_model_data.write.assert_not_called()

    def test_create_xmlid_entry_existing_entry_different_res_id(self) -> None:
        """Test that existing entries with different res_id are updated."""
        from odoo_data_flow.import_threaded import _create_xmlid_entry

        mock_connection = MagicMock()
        mock_ir_model_data = MagicMock()
        mock_ir_model_data.search.return_value = [1]  # Existing entry ID
        mock_ir_model_data.read.return_value = {"res_id": 99, "model": "res.partner"}
        mock_connection.get_model.return_value = mock_ir_model_data

        result = _create_xmlid_entry(
            mock_connection, "my_module.partner_001", 42, "res.partner"
        )

        assert result is True
        mock_ir_model_data.create.assert_not_called()
        mock_ir_model_data.write.assert_called_once_with(
            1, {"res_id": 42, "model": "res.partner"}
        )

    def test_create_xmlid_entry_handles_exception(self) -> None:
        """Test that exceptions during XML ID creation are handled gracefully."""
        from odoo_data_flow.import_threaded import _create_xmlid_entry

        mock_connection = MagicMock()
        mock_connection.get_model.side_effect = Exception("Connection error")

        result = _create_xmlid_entry(
            mock_connection, "my_module.partner_001", 42, "res.partner"
        )

        assert result is False


class TestAccessErrorHandling:
    """Tests for access error message extraction and handling."""

    def test_extract_access_error_from_private_method(self) -> None:
        """Test extracting error message from 'cannot be called remotely' error."""
        from odoo_data_flow.import_threaded import _extract_access_error_message

        error = (
            "{'code': 0, 'message': 'Odoo Server Error', 'data': {'name': "
            "'odoo.exceptions.AccessError', 'message': \"Private methods "
            "(such as 'fleet.vehicle.model.browse') cannot be called remotely.\"}}"
        )
        result = _extract_access_error_message(error)
        assert "fleet.vehicle.model.browse" in result
        assert "Access denied" in result

    def test_extract_access_error_from_message_field(self) -> None:
        """Test extracting error message from 'message' field."""
        from odoo_data_flow.import_threaded import _extract_access_error_message

        error = "{'message': 'Access denied for model res.partner'}"
        result = _extract_access_error_message(error)
        assert result == "Access denied for model res.partner"

    def test_handle_create_error_access_denied(self) -> None:
        """Test that access errors produce clean messages in fail file."""
        from odoo_data_flow.import_threaded import _handle_create_error

        error = Exception(
            "Private methods (such as 'res.partner.browse') cannot be called remotely."
        )
        line = ["id_001", "Test Partner", "test@example.com"]

        error_message, failed_line, summary = _handle_create_error(
            0, error, line, "Fell back to create"
        )

        assert "Access denied" in error_message
        assert "res.partner.browse" in error_message
        assert summary == "Access denied - check user permissions"
        assert len(failed_line) == 4  # Original 3 fields + error message

    def test_handle_create_error_truncates_long_errors(self) -> None:
        """Test that very long error messages are truncated."""
        from odoo_data_flow.import_threaded import _extract_access_error_message

        long_error = "AccessError: " + "x" * 500
        result = _extract_access_error_message(long_error)
        assert len(result) <= 203  # 200 chars + "..."


class TestRecursiveBatching:
    """Tests for the recursive batch creation logic."""

    def test_recursive_batching_single_column(self) -> None:
        """Test recursive batching with a single grouping column."""
        from odoo_data_flow.import_threaded import _recursive_create_batches

        header = ["id", "name", "country"]
        data = [
            ["1", "A", "USA"],
            ["2", "B", "USA"],
            ["3", "C", "Canada"],
            ["4", "D", "USA"],
        ]
        batches = list(_recursive_create_batches(data, ["country"], header, 10, False))
        assert len(batches) == 2
        assert batches[0][1][0][2] == "Canada"
        assert batches[1][1][0][2] == "USA"

    def test_recursive_batching_multiple_columns(self) -> None:
        """Test recursive batching with multiple grouping columns."""
        from odoo_data_flow.import_threaded import _recursive_create_batches

        header = ["id", "name", "country", "state"]
        data = [
            ["1", "A", "USA", "CA"],
            ["2", "B", "USA", "NY"],
            ["3", "C", "Canada", "QC"],
            ["4", "D", "USA", "CA"],
        ]
        batches = list(
            _recursive_create_batches(data, ["country", "state"], header, 10, False)
        )
        assert len(batches) == 3
        # Note: The order of batches is not guaranteed, so we check the content
        # of each batch.
        batch_contents = [tuple(row) for _, batch_data in batches for row in batch_data]
        assert ("1", "A", "USA", "CA") in batch_contents
        assert ("4", "D", "USA", "CA") in batch_contents
        assert ("2", "B", "USA", "NY") in batch_contents
        assert ("3", "C", "Canada", "QC") in batch_contents

    def test_recursive_batching_group_col_not_found(self) -> None:
        """Test that an error is logged if a grouping column is not found."""
        from odoo_data_flow.import_threaded import _recursive_create_batches

        header = ["id", "name"]
        data = [["1", "A"]]
        with patch("odoo_data_flow.import_threaded.log") as mock_log:
            list(_recursive_create_batches(data, ["non_existent"], header, 10, False))
            mock_log.error.assert_called_once_with(
                "Grouping column 'non_existent' not found. Cannot use --groupby."
            )

    def test_recursive_batching_with_special_chars_in_col_name(self) -> None:
        """Test batching with special characters in column names."""
        from odoo_data_flow.import_threaded import _recursive_create_batches

        header = ["id", "name", "partner_id/id"]
        data = [
            ["1", "A", "p1"],
            ["2", "B", "p1"],
            ["3", "C", "p2"],
        ]
        batches = list(
            _recursive_create_batches(data, ["partner_id/id"], header, 10, False)
        )
        assert len(batches) == 2
        assert batches[0][1][0][2] == "p1"
        assert batches[1][1][0][2] == "p2"

    def test_recursive_batching_multiple_cols_with_special_chars(self) -> None:
        """Test batching with multiple columns, one with special characters."""
        from odoo_data_flow.import_threaded import _recursive_create_batches

        header = ["id", "name", "partner_id/id", "company_id"]
        data = [
            ["1", "A", "p1", "c1"],
            ["2", "B", "p1", "c2"],
            ["3", "C", "p2", "c1"],
            ["4", "D", "p1", "c1"],
        ]
        batches = list(
            _recursive_create_batches(
                data, ["partner_id/id", "company_id"], header, 10, False
            )
        )
        assert len(batches) == 3


class TestExtractPerRowErrors:
    """Tests for the _extract_per_row_errors function."""

    def test_extract_per_row_errors_with_rows_dict(self) -> None:
        """Test extraction when Odoo provides row info in 'rows' dict."""
        messages = [
            {
                "type": "error",
                "message": "Validation error on field name",
                "rows": {"from": 2, "to": 2},
            }
        ]
        result = _extract_per_row_errors(messages)
        assert 2 in result
        assert result[2] == "Validation error on field name"

    def test_extract_per_row_errors_with_rows_range(self) -> None:
        """Test extraction when Odoo provides a range of rows."""
        messages = [
            {
                "type": "error",
                "message": "Multiple records affected",
                "rows": {"from": 5, "to": 7},
            }
        ]
        result = _extract_per_row_errors(messages)
        assert 5 in result
        assert 6 in result
        assert 7 in result
        assert result[5] == "Multiple records affected"

    def test_extract_per_row_errors_row_pattern(self) -> None:
        """Test extraction from 'Row X:' pattern in message."""
        messages = [{"type": "error", "message": "Row 5: Missing required field"}]
        result = _extract_per_row_errors(messages)
        # Row 5 in 1-based becomes index 4 in 0-based
        assert 4 in result
        assert "Missing required field" in result[4]

    def test_extract_per_row_errors_line_pattern(self) -> None:
        """Test extraction from 'Line X:' pattern in message."""
        messages = [{"type": "error", "message": "Line 3: Invalid value"}]
        result = _extract_per_row_errors(messages)
        # Line 3 in 1-based becomes index 2 in 0-based
        assert 2 in result

    def test_extract_per_row_errors_at_row_pattern(self) -> None:
        """Test extraction from 'at row X' pattern in message."""
        messages = [{"type": "error", "message": "Error occurred at row 10"}]
        result = _extract_per_row_errors(messages)
        assert 9 in result  # 0-based index

    def test_extract_per_row_errors_in_row_pattern(self) -> None:
        """Test extraction from 'in row X' pattern in message."""
        messages = [{"type": "error", "message": "Duplicate found in row 4"}]
        result = _extract_per_row_errors(messages)
        assert 3 in result  # 0-based index

    def test_extract_per_row_errors_empty_messages(self) -> None:
        """Test with empty messages list."""
        result = _extract_per_row_errors([])
        assert result == {}

    def test_extract_per_row_errors_no_row_info(self) -> None:
        """Test with message that has no row information."""
        messages = [{"type": "error", "message": "Generic error without row info"}]
        result = _extract_per_row_errors(messages)
        assert result == {}


class TestFormatOdooError:
    """Additional tests for _format_odoo_error."""

    def test_format_odoo_error_extracts_data_message(self) -> None:
        """Test that error dict with data.message is properly extracted."""
        error_dict = {"data": {"message": "Field 'name' is required"}}
        result = _format_odoo_error(str(error_dict))
        assert result == "Field 'name' is required"

    def test_format_odoo_error_strips_newlines(self) -> None:
        """Test that newlines are stripped from error messages."""
        error_with_newlines = "First line\nSecond line\nThird line"
        result = _format_odoo_error(error_with_newlines)
        assert "\n" not in result
        assert "First line Second line Third line" == result


class TestFilterIgnoredColumns:
    """Tests for _filter_ignored_columns edge cases."""

    def test_filter_ignored_columns_empty_ignore(self) -> None:
        """Test that empty ignore list returns original data."""
        header = ["id", "name", "age"]
        data = [["1", "Alice", "30"]]
        new_header, new_data = _filter_ignored_columns([], header, data)
        assert new_header == header
        assert new_data == data

    def test_filter_ignored_columns_all_columns_ignored(self) -> None:
        """Test when all non-id columns are ignored."""
        header = ["id", "name"]
        data = [["1", "Alice"]]
        new_header, new_data = _filter_ignored_columns(["id", "name"], header, data)
        assert new_header == []
        assert new_data == [[]]

    def test_filter_ignored_columns_malformed_row(self) -> None:
        """Test handling of rows with fewer columns than header."""
        header = ["id", "name", "age", "city"]
        data = [
            ["1", "Alice", "30", "NYC"],  # Valid
            ["2", "Bob"],  # Malformed - too few columns
            ["3", "Charlie", "25", "LA"],  # Valid
        ]
        _new_header, new_data = _filter_ignored_columns(["age"], header, data)
        # Malformed row should be skipped
        assert len(new_data) == 2
        assert new_data[0][0] == "1"
        assert new_data[1][0] == "3"

    def test_filter_ignored_columns_with_subfield_notation(self) -> None:
        """Test that parent_id/id is filtered when parent_id is ignored."""
        header = ["id", "name", "parent_id/id"]
        data = [["1", "A", "p1"]]
        new_header, _new_data = _filter_ignored_columns(["parent_id"], header, data)
        assert "parent_id/id" not in new_header
        assert new_header == ["id", "name"]


class TestExecuteWriteBatch:
    """Tests for the _execute_write_batch function."""

    def test_execute_write_batch_success(self) -> None:
        """Test successful batch write operation with super-batch format."""
        mock_model = MagicMock()
        thread_state = {"model": mock_model, "context": {"tracking_disable": True}}
        # Super-batch format: list of (ids, vals) tuples
        batch_writes = [([1, 2, 3], {"name": "Updated"})]

        result = _execute_write_batch(thread_state, batch_writes, 1)

        assert result["success"] is True
        assert result["successful_writes"] == 3
        assert result["failed_writes"] == []
        mock_model.write.assert_called_once_with(
            [1, 2, 3], {"name": "Updated"}, context={"tracking_disable": True}
        )

    def test_execute_write_batch_multiple_ops(self) -> None:
        """Test successful super-batch with multiple write operations."""
        mock_model = MagicMock()
        thread_state = {"model": mock_model, "context": {"tracking_disable": True}}
        # Super-batch with multiple operations (different parent_ids)
        batch_writes = [
            ([1, 2], {"parent_id": 10}),
            ([3, 4, 5], {"parent_id": 20}),
        ]

        result = _execute_write_batch(thread_state, batch_writes, 1)

        assert result["success"] is True
        assert result["successful_writes"] == 5
        assert result["failed_writes"] == []
        assert mock_model.write.call_count == 2

    def test_execute_write_batch_failure(self) -> None:
        """Test batch write operation that fails."""
        mock_model = MagicMock()
        mock_model.write.side_effect = Exception("Access denied")
        thread_state = {"model": mock_model, "context": {}}
        # Super-batch format: list of (ids, vals) tuples
        batch_writes = [([1, 2], {"parent_id": 10})]

        result = _execute_write_batch(thread_state, batch_writes, 1)

        assert result["success"] is False
        assert result["successful_writes"] == 0
        assert len(result["failed_writes"]) == 2
        assert result["failed_writes"][0][0] == 1
        assert result["failed_writes"][1][0] == 2

    def test_execute_write_batch_partial_failure(self) -> None:
        """Test super-batch where one operation fails."""
        mock_model = MagicMock()
        # First call succeeds, second fails
        mock_model.write.side_effect = [None, Exception("Timeout")]
        thread_state = {"model": mock_model, "context": {}}
        batch_writes = [
            ([1, 2], {"parent_id": 10}),
            ([3], {"parent_id": 20}),
        ]

        result = _execute_write_batch(thread_state, batch_writes, 1)

        assert result["success"] is False
        assert result["successful_writes"] == 2  # First op succeeded
        assert len(result["failed_writes"]) == 1  # Second op failed


class TestExecuteLoadBatchEdgeCases:
    """Additional edge case tests for _execute_load_batch."""

    def test_execute_load_batch_force_create_mode(self) -> None:
        """Test that force_create bypasses batch load and uses single-record load."""
        mock_model = MagicMock()
        # Single-record load returns success
        mock_model.load.return_value = {"ids": [42], "messages": []}
        mock_connection = MagicMock()

        mock_progress = MagicMock()
        thread_state = {
            "model": mock_model,
            "connection": mock_connection,
            "progress": mock_progress,
            "unique_id_field_index": 0,
            "ignore_list": [],
            "force_create": True,
            "model_name": "res.partner",
            "context": {},
        }
        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _execute_load_batch(thread_state, batch_lines, batch_header, 1)

        # In force_create mode, load IS called but only for single records
        # (via _load_records_individually)
        mock_model.load.assert_called_once()
        # Verify it was called with single record data
        call_args = mock_model.load.call_args
        assert len(call_args[0][1]) == 1  # Single record in data list
        assert result["success"] is True

    @patch("odoo_data_flow.import_threaded._create_batch_individually")
    def test_execute_load_batch_timeout_ignored(
        self, mock_create_individually: MagicMock
    ) -> None:
        """Test that client-side timeouts are ignored to allow server processing."""
        mock_model = MagicMock()
        mock_model.load.side_effect = [
            Exception("timed out"),
            {"ids": [1, 2]},
        ]
        mock_progress = MagicMock()
        thread_state = {
            "model": mock_model,
            "progress": mock_progress,
            "unique_id_field_index": 0,
            "ignore_list": [],
        }
        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"], ["rec2", "B"]]

        result = _execute_load_batch(thread_state, batch_lines, batch_header, 1)

        # Timeout should be ignored and processing should continue
        assert result["success"] is True
        mock_create_individually.assert_not_called()

    @patch("odoo_data_flow.import_threaded._create_batch_individually")
    @patch("odoo_data_flow.import_threaded.time.sleep")
    def test_execute_load_batch_connection_pool_error(
        self, mock_sleep: MagicMock, mock_create_individually: MagicMock
    ) -> None:
        """Test that connection pool errors trigger batch size reduction."""
        mock_model = MagicMock()
        mock_model.load.side_effect = [
            Exception("connection pool is full"),
            {"ids": [1]},
            {"ids": [2]},
        ]
        mock_progress = MagicMock()
        thread_state = {
            "model": mock_model,
            "progress": mock_progress,
            "unique_id_field_index": 0,
            "ignore_list": [],
        }
        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"], ["rec2", "B"]]

        result = _execute_load_batch(thread_state, batch_lines, batch_header, 1)

        assert result["success"] is True
        # Should reduce batch size on pool error
        mock_progress.console.print.assert_any_call(
            "[yellow]WARN:[/] Batch 1 hit transient error (connection pool). "
            "Reducing chunk size to 1."
        )

    @patch("odoo_data_flow.import_threaded._create_batch_individually")
    def test_execute_load_batch_empty_load_lines(
        self, mock_create_individually: MagicMock
    ) -> None:
        """Test handling when filtering results in empty load_lines."""
        mock_model = MagicMock()
        mock_model.load.return_value = {"ids": []}
        mock_progress = MagicMock()
        thread_state = {
            "model": mock_model,
            "progress": mock_progress,
            "unique_id_field_index": 0,
            "ignore_list": ["name"],  # Ignore the only non-id column
        }
        batch_header = ["id", "name"]
        # Row has fewer columns than needed after filtering
        batch_lines = [["rec1"]]

        result = _execute_load_batch(thread_state, batch_lines, batch_header, 1)

        # Should handle gracefully
        assert result is not None


class TestReadDataFileEdgeCases:
    """Additional tests for _read_data_file edge cases."""

    def test_read_data_file_with_skip(self, tmp_path: Path) -> None:
        """Test that skip parameter correctly skips rows."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name\nskip1,A\nskip2,B\nkeep1,C\nkeep2,D")

        header, data = _read_data_file(str(source_file), ",", "utf-8", skip=2)

        assert header == ["id", "name"]
        assert len(data) == 2
        assert data[0][0] == "keep1"
        assert data[1][0] == "keep2"


class TestLoadRecordsIndividuallyEdgeCases:
    """Tests for _load_records_individually edge cases."""

    def test_load_records_individually_serialization_error(self) -> None:
        """Test handling of database serialization errors."""
        mock_model = MagicMock()
        mock_model.load.side_effect = Exception("could not serialize access")
        mock_connection = MagicMock()

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _create_batch_individually(
            mock_model, mock_connection, batch_lines, batch_header, 0, {}, []
        )

        # Serialization errors should not add to failed_lines (retryable)
        assert len(result["failed_lines"]) == 0

    def test_load_records_individually_connection_pool_error(self) -> None:
        """Test handling of connection pool exhaustion errors."""
        mock_model = MagicMock()
        mock_model.load.side_effect = Exception("connection pool is full")
        mock_connection = MagicMock()

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _create_batch_individually(
            mock_model, mock_connection, batch_lines, batch_header, 0, {}, []
        )

        # Pool errors should add to failed_lines for retry
        assert len(result["failed_lines"]) == 1
        assert "connection pool exhaustion" in result["failed_lines"][0][-1]

    def test_load_records_individually_odoo_server_error(self) -> None:
        """Test handling of Odoo server internal errors."""
        mock_model = MagicMock()
        mock_model.load.side_effect = Exception(
            "Odoo Server Error: tuple index out of range"
        )
        mock_connection = MagicMock()

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _create_batch_individually(
            mock_model, mock_connection, batch_lines, batch_header, 0, {}, []
        )

        # Server internal errors should be recorded
        assert len(result["failed_lines"]) == 1
        assert "Odoo server internal error" in result["failed_lines"][0][-1]

    def test_load_records_individually_constraint_violation(self) -> None:
        """Test handling of database constraint violations."""
        mock_model = MagicMock()
        mock_model.load.side_effect = Exception("check constraint 'nospaces' violated")
        mock_connection = MagicMock()

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _create_batch_individually(
            mock_model, mock_connection, batch_lines, batch_header, 0, {}, []
        )

        assert len(result["failed_lines"]) == 1
        assert "constraint" in result["error_summary"].lower()

    def test_load_records_individually_load_returns_error(self) -> None:
        """Test handling when load() returns error in messages."""
        mock_model = MagicMock()
        mock_model.load.return_value = {
            "ids": [],
            "messages": [{"message": "Validation failed: name is required"}],
        }
        mock_connection = MagicMock()

        batch_header = ["id", "name"]
        batch_lines = [["rec1", ""]]

        result = _create_batch_individually(
            mock_model, mock_connection, batch_lines, batch_header, 0, {}, []
        )

        assert len(result["failed_lines"]) == 1
        assert "Validation failed" in result["failed_lines"][0][-1]

    def test_load_records_individually_success(self) -> None:
        """Test successful single-record load."""
        mock_model = MagicMock()
        mock_model.load.return_value = {"ids": [42], "messages": []}
        mock_connection = MagicMock()

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "Record A"]]

        result = _create_batch_individually(
            mock_model, mock_connection, batch_lines, batch_header, 0, {}, []
        )

        assert len(result["failed_lines"]) == 0
        assert result["id_map"]["rec1"] == 42


class TestLoadBatchWithBinaryFallback:
    """Tests for _load_batch_with_binary_fallback binary search optimization."""

    def test_all_records_succeed(self) -> None:
        """Test when all records load successfully - no binary search needed."""
        mock_model = MagicMock()
        mock_model.load.return_value = {"ids": [1, 2, 3, 4], "messages": []}
        mock_connection = MagicMock()

        batch_header = ["id", "name"]
        batch_lines = [
            ["rec1", "A"],
            ["rec2", "B"],
            ["rec3", "C"],
            ["rec4", "D"],
        ]

        result = _load_batch_with_binary_fallback(
            mock_model,
            mock_connection,
            batch_lines,
            batch_header,
            0,
            {},
            [],
            "res.partner",
        )

        assert result["success"] is True
        assert len(result["failed_lines"]) == 0
        assert len(result["id_map"]) == 4
        # Should only call load once since all succeeded
        assert mock_model.load.call_count == 1

    def test_single_bad_record_found_via_binary_search(self) -> None:
        """Test binary search efficiently finds single bad record in batch of 8."""
        mock_model = MagicMock()
        mock_connection = MagicMock()

        # Track which records are being loaded to simulate targeted failures
        def mock_load(
            header: list[str],
            lines: list[list[Any]],
            context: Optional[dict[str, Any]] = None,
        ) -> dict[str, Any]:
            # Check if the bad record (rec5) is in the batch
            has_bad = any("rec5" in str(line) for line in lines)
            if has_bad and len(lines) == 1:
                # Single bad record - return failure
                return {
                    "ids": [],
                    "messages": [{"message": "Validation error for rec5"}],
                }
            elif has_bad:
                # Batch contains bad record - raise exception to trigger split
                raise ValueError("Batch contains invalid data")
            else:
                # All good records - return success
                return {"ids": list(range(1, len(lines) + 1)), "messages": []}

        mock_model.load.side_effect = mock_load

        batch_header = ["id", "name"]
        batch_lines = [
            ["rec1", "A"],
            ["rec2", "B"],
            ["rec3", "C"],
            ["rec4", "D"],
            ["rec5", "BAD"],  # This one will fail
            ["rec6", "F"],
            ["rec7", "G"],
            ["rec8", "H"],
        ]

        result = _load_batch_with_binary_fallback(
            mock_model,
            mock_connection,
            batch_lines,
            batch_header,
            0,
            {},
            [],
            "res.partner",
        )

        # 7 records should succeed, 1 should fail
        assert len(result["id_map"]) == 7
        assert len(result["failed_lines"]) == 1
        assert "rec5" in str(result["failed_lines"][0])
        # Binary search should be more efficient than 8 individual calls
        # Expected: ~log2(8) splits + successful batches < 8 calls
        assert mock_model.load.call_count < 8

    def test_multiple_bad_records_scattered(self) -> None:
        """Test binary search handles multiple scattered bad records."""
        mock_model = MagicMock()
        mock_connection = MagicMock()

        bad_records = {"rec2", "rec6"}

        def mock_load(
            header: list[str],
            lines: list[list[Any]],
            context: Optional[dict[str, Any]] = None,
        ) -> dict[str, Any]:
            has_bad = any(line[0] in bad_records for line in lines)
            if has_bad and len(lines) == 1:
                return {"ids": [], "messages": [{"message": "Validation error"}]}
            elif has_bad:
                raise ValueError("Batch contains invalid data")
            else:
                return {"ids": list(range(1, len(lines) + 1)), "messages": []}

        mock_model.load.side_effect = mock_load

        batch_header = ["id", "name"]
        batch_lines = [
            ["rec1", "A"],
            ["rec2", "BAD1"],
            ["rec3", "C"],
            ["rec4", "D"],
            ["rec5", "E"],
            ["rec6", "BAD2"],
            ["rec7", "G"],
            ["rec8", "H"],
        ]

        result = _load_batch_with_binary_fallback(
            mock_model,
            mock_connection,
            batch_lines,
            batch_header,
            0,
            {},
            [],
            "res.partner",
        )

        # 6 records should succeed, 2 should fail
        assert len(result["id_map"]) == 6
        assert len(result["failed_lines"]) == 2

    def test_all_records_fail(self) -> None:
        """Test worst case - all records fail (same efficiency as individual load)."""
        mock_model = MagicMock()
        mock_model.load.side_effect = ValueError("All records invalid")
        mock_connection = MagicMock()

        batch_header = ["id", "name"]
        batch_lines = [
            ["rec1", "BAD1"],
            ["rec2", "BAD2"],
            ["rec3", "BAD3"],
            ["rec4", "BAD4"],
        ]

        result = _load_batch_with_binary_fallback(
            mock_model,
            mock_connection,
            batch_lines,
            batch_header,
            0,
            {},
            [],
            "res.partner",
        )

        # All records should fail
        assert len(result["id_map"]) == 0
        assert len(result["failed_lines"]) == 4

    def test_partial_success_from_load_response(self) -> None:
        """Test handling partial success where load() returns mixed ids (some None)."""
        mock_model = MagicMock()
        mock_connection = MagicMock()

        # First call returns partial success, subsequent calls succeed
        call_count = [0]

        def mock_load(
            header: list[str],
            lines: list[list[Any]],
            context: Optional[dict[str, Any]] = None,
        ) -> dict[str, Any]:
            call_count[0] += 1
            if call_count[0] == 1 and len(lines) == 4:
                # First batch: partial success - rec2 fails
                return {"ids": [1, None, 3, 4], "messages": []}
            elif len(lines) == 1 and lines[0][0] == "rec2":
                # Individual load of bad record
                return {"ids": [], "messages": [{"message": "rec2 validation failed"}]}
            else:
                return {"ids": list(range(1, len(lines) + 1)), "messages": []}

        mock_model.load.side_effect = mock_load

        batch_header = ["id", "name"]
        batch_lines = [
            ["rec1", "A"],
            ["rec2", "BAD"],
            ["rec3", "C"],
            ["rec4", "D"],
        ]

        result = _load_batch_with_binary_fallback(
            mock_model,
            mock_connection,
            batch_lines,
            batch_header,
            0,
            {},
            [],
            "res.partner",
        )

        # 3 succeed from first batch, 1 fails on retry
        assert len(result["id_map"]) == 3
        assert len(result["failed_lines"]) == 1

    def test_single_record_base_case(self) -> None:
        """Test base case with single record uses _load_records_individually."""
        mock_model = MagicMock()
        mock_model.load.return_value = {"ids": [42], "messages": []}
        mock_connection = MagicMock()

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _load_batch_with_binary_fallback(
            mock_model,
            mock_connection,
            batch_lines,
            batch_header,
            0,
            {},
            [],
            "res.partner",
        )

        assert result["id_map"].get("rec1") == 42
        assert len(result["failed_lines"]) == 0

    def test_ignores_columns_correctly(self) -> None:
        """Test that ignored columns are properly filtered during binary search."""
        mock_model = MagicMock()
        mock_model.load.return_value = {"ids": [1, 2], "messages": []}
        mock_connection = MagicMock()

        batch_header = ["id", "name", "ignored_field"]
        batch_lines = [
            ["rec1", "A", "ignore1"],
            ["rec2", "B", "ignore2"],
        ]

        _load_batch_with_binary_fallback(
            mock_model,
            mock_connection,
            batch_lines,
            batch_header,
            0,
            {},
            ["ignored_field"],
            "res.partner",
        )

        # Check that load was called without the ignored column
        call_args = mock_model.load.call_args
        header_sent = call_args[0][0]
        assert "ignored_field" not in header_sent
        assert "id" in header_sent
        assert "name" in header_sent


class TestImportDataWithDictConfig:
    """Tests for import_data with dict config."""

    @patch("odoo_data_flow.import_threaded._read_data_file")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_dict")
    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_import_data_with_dict_config(
        self,
        mock_run_pass: MagicMock,
        mock_get_conn: MagicMock,
        mock_read_file: MagicMock,
    ) -> None:
        """Test import_data accepts dict config."""
        mock_read_file.return_value = (["id", "name"], [["xml_a", "A"]])
        mock_run_pass.return_value = (
            {"id_map": {"xml_a": 101}, "failed_lines": []},
            False,
        )
        mock_get_conn.return_value.get_model.return_value = MagicMock()

        config_dict = {
            "hostname": "localhost",
            "database": "test",
            "login": "admin",
            "password": "admin",
        }
        result, _ = import_data(
            config=config_dict,
            model="res.partner",
            unique_id_field="id",
            file_csv="dummy.csv",
        )

        assert result is True
        mock_get_conn.assert_called_once_with(config_dict)


class TestStreamingCSV:
    """Tests for streaming CSV functionality."""

    def test_count_csv_rows(self, tmp_path: Path) -> None:
        """Test counting CSV rows."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name\nrec1,A\nrec2,B\nrec3,C\nrec4,D")

        count = _count_csv_rows(str(source_file), ",", "utf-8", skip=0)
        assert count == 4

    def test_count_csv_rows_with_skip(self, tmp_path: Path) -> None:
        """Test counting CSV rows with skip."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name\nskip1,A\nskip2,B\nkeep1,C\nkeep2,D")

        count = _count_csv_rows(str(source_file), ",", "utf-8", skip=2)
        assert count == 2

    def test_count_csv_rows_nonexistent_file(self) -> None:
        """Test counting CSV rows on nonexistent file returns 0."""
        count = _count_csv_rows("/nonexistent/file.csv", ",", "utf-8", skip=0)
        assert count == 0

    def test_stream_csv_batches_basic(self, tmp_path: Path) -> None:
        """Test basic streaming batch generation."""
        source_file = tmp_path / "source.csv"
        source_file.write_text(
            "id,name,age\nrec1,A,25\nrec2,B,30\nrec3,C,35\nrec4,D,40"
        )

        batches = list(
            _stream_csv_batches(
                str(source_file), ",", "utf-8", skip=0, batch_size=2, ignore=[]
            )
        )

        assert len(batches) == 2
        # First batch
        header1, num1, data1 = batches[0]
        assert header1 == ["id", "name", "age"]
        assert num1 == 1
        assert len(data1) == 2
        assert data1[0] == ["rec1", "A", "25"]

        # Second batch
        header2, num2, data2 = batches[1]
        assert header2 == ["id", "name", "age"]
        assert num2 == 2
        assert len(data2) == 2
        assert data2[0] == ["rec3", "C", "35"]

    def test_stream_csv_batches_with_ignore(self, tmp_path: Path) -> None:
        """Test streaming with column filtering."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name,age,city\nrec1,A,25,NYC\nrec2,B,30,LA")

        batches = list(
            _stream_csv_batches(
                str(source_file), ",", "utf-8", skip=0, batch_size=10, ignore=["age"]
            )
        )

        assert len(batches) == 1
        header, _, data = batches[0]
        assert header == ["id", "name", "city"]
        assert "age" not in header
        assert len(data[0]) == 3

    def test_stream_csv_batches_with_skip(self, tmp_path: Path) -> None:
        """Test streaming with skipped rows."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name\nskip1,A\nskip2,B\nkeep1,C\nkeep2,D")

        batches = list(
            _stream_csv_batches(
                str(source_file), ",", "utf-8", skip=2, batch_size=10, ignore=[]
            )
        )

        assert len(batches) == 1
        _, _, data = batches[0]
        assert len(data) == 2
        assert data[0][0] == "keep1"

    def test_stream_csv_batches_missing_id_column(self, tmp_path: Path) -> None:
        """Test streaming fails without id column."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("name,age\nA,25\nB,30")

        with pytest.raises(ValueError, match="must contain an 'id' column"):
            list(
                _stream_csv_batches(
                    str(source_file), ",", "utf-8", skip=0, batch_size=10, ignore=[]
                )
            )

    def test_stream_csv_batches_semicolon_separator(self, tmp_path: Path) -> None:
        """Test streaming with semicolon separator."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id;name;age\nrec1;A;25\nrec2;B;30")

        batches = list(
            _stream_csv_batches(
                str(source_file), ";", "utf-8", skip=0, batch_size=10, ignore=[]
            )
        )

        assert len(batches) == 1
        header, _, data = batches[0]
        assert header == ["id", "name", "age"]
        assert data[0] == ["rec1", "A", "25"]

    def test_stream_csv_batches_exact_batch_boundary(self, tmp_path: Path) -> None:
        """Test streaming when data aligns exactly with batch size."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name\nrec1,A\nrec2,B\nrec3,C\nrec4,D")

        batches = list(
            _stream_csv_batches(
                str(source_file), ",", "utf-8", skip=0, batch_size=2, ignore=[]
            )
        )

        assert len(batches) == 2
        assert len(batches[0][2]) == 2
        assert len(batches[1][2]) == 2


class TestImportDataStreamingMode:
    """Tests for import_data streaming mode."""

    @patch("odoo_data_flow.import_threaded._read_data_file")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_stream_mode_falls_back_when_not_compatible(
        self,
        mock_run_pass: MagicMock,
        mock_get_conn: MagicMock,
        mock_read_file: MagicMock,
    ) -> None:
        """Test that streaming mode falls back when not compatible."""
        mock_read_file.return_value = (["id", "name"], [["xml_a", "A"]])
        mock_run_pass.return_value = (
            {"id_map": {"xml_a": 101}, "failed_lines": []},
            False,
        )
        mock_get_conn.return_value.get_model.return_value = MagicMock()

        # With o2m=True, streaming should fall back to standard mode
        result, _ = import_data(
            config="dummy.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv="dummy.csv",
            stream=True,
            o2m=True,  # This makes streaming incompatible
        )

        # Should still succeed but use standard mode
        assert result is True
        # Standard mode reads the file
        mock_read_file.assert_called_once()

    @patch("odoo_data_flow.import_threaded._read_data_file")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_stream_mode_falls_back_with_deferred(
        self,
        mock_run_pass: MagicMock,
        mock_get_conn: MagicMock,
        mock_read_file: MagicMock,
    ) -> None:
        """Test streaming falls back when deferred_fields are present."""
        mock_read_file.return_value = (
            ["id", "name", "parent_id"],
            [["xml_a", "A", ""]],
        )
        mock_run_pass.return_value = (
            {"id_map": {"xml_a": 101}, "failed_lines": []},
            False,
        )
        mock_get_conn.return_value.get_model.return_value = MagicMock()

        result, _ = import_data(
            config="dummy.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv="dummy.csv",
            stream=True,
            deferred_fields=["parent_id"],  # Not compatible with streaming
        )

        assert result is True
        mock_read_file.assert_called_once()

    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded._execute_load_batch")
    def test_stream_mode_uses_streaming_orchestrator(
        self,
        mock_execute_batch: MagicMock,
        mock_get_conn: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that streaming mode uses the streaming orchestrator."""
        # Create a real CSV file for streaming
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name\nrec1,A\nrec2,B")

        mock_model = MagicMock()
        mock_model.load.return_value = {"ids": [1, 2]}
        mock_get_conn.return_value.get_model.return_value = mock_model

        # Mock _execute_load_batch to return proper results
        mock_execute_batch.return_value = {
            "success": True,
            "id_map": {"rec1": 1, "rec2": 2},
            "failed_lines": [],
        }

        result, _stats = import_data(
            config="dummy.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv=str(source_file),
            separator=",",
            stream=True,  # Enable streaming
        )

        assert result is True
        # Verify streaming was used (execute_load_batch should be called)
        mock_execute_batch.assert_called()

    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    def test_stream_mode_handles_missing_uid_field(
        self,
        mock_get_conn: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test streaming handles missing unique ID field gracefully."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name\nrec1,A\nrec2,B")

        mock_get_conn.return_value.get_model.return_value = MagicMock()

        result, _ = import_data(
            config="dummy.conf",
            model="res.partner",
            unique_id_field="nonexistent_field",  # Field not in CSV
            file_csv=str(source_file),
            separator=",",
            stream=True,
        )

        # Should fail gracefully
        assert result is False

    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    def test_stream_mode_handles_file_not_found(
        self,
        mock_get_conn: MagicMock,
    ) -> None:
        """Test streaming handles nonexistent file gracefully."""
        mock_get_conn.return_value.get_model.return_value = MagicMock()

        result, _ = import_data(
            config="dummy.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv="/nonexistent/file.csv",
            stream=True,
        )

        # Should fail gracefully
        assert result is False


class TestWarnEmptyIds:
    """Tests for the _warn_empty_ids function."""

    def test_counts_empty_id_values(self) -> None:
        """Test that empty id values are counted correctly."""
        header = ["id", "name", "email"]
        data = [
            ["partner_1", "Alice", "alice@example.com"],
            ["", "Bob", "bob@example.com"],  # Empty id
            ["partner_3", "Charlie", "charlie@example.com"],
        ]

        empty_count = _warn_empty_ids(header, data)

        assert empty_count == 1
        # Data should remain unchanged (warning only, no modification)
        assert data[0][0] == "partner_1"
        assert data[1][0] == ""
        assert data[2][0] == "partner_3"

    def test_counts_none_id_values(self) -> None:
        """Test that None id values are counted correctly."""
        header = ["id", "name"]
        data: list[list[Any]] = [
            [None, "Alice"],  # None id
            ["partner_2", "Bob"],
        ]

        empty_count = _warn_empty_ids(header, data)

        assert empty_count == 1
        # Data should remain unchanged
        assert data[0][0] is None
        assert data[1][0] == "partner_2"

    def test_counts_whitespace_only_id_values(self) -> None:
        """Test that whitespace-only id values are counted correctly."""
        header = ["id", "name"]
        data = [
            ["   ", "Alice"],  # Whitespace only
            ["\t", "Bob"],  # Tab only
        ]

        empty_count = _warn_empty_ids(header, data)

        assert empty_count == 2
        # Data should remain unchanged
        assert data[0][0] == "   "
        assert data[1][0] == "\t"

    def test_returns_zero_when_all_ids_present(self) -> None:
        """Test that zero is returned when all ids are present."""
        header = ["id", "name"]
        data = [
            ["partner_1", "Alice"],
            ["partner_2", "Bob"],
        ]

        empty_count = _warn_empty_ids(header, data)

        assert empty_count == 0

    def test_returns_zero_when_no_id_column(self) -> None:
        """Test that zero is returned if no id column exists."""
        header = ["name", "email"]
        data = [["Alice", "alice@example.com"]]

        empty_count = _warn_empty_ids(header, data)

        assert empty_count == 0

    def test_uses_start_row_for_logging(self) -> None:
        """Test that start_row parameter is used for row number calculation."""
        header = ["id", "name"]
        data = [
            ["", "Alice"],
            ["", "Bob"],
        ]

        # start_row affects logging output, not the count
        empty_count = _warn_empty_ids(header, data, start_row=100)

        assert empty_count == 2


class TestSkipExisting:
    """Tests for the skip_existing functionality."""

    @patch("odoo_data_flow.import_threaded._read_data_file")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_skip_existing_filters_out_existing_ids(
        self,
        mock_run_pass: MagicMock,
        mock_get_conn: MagicMock,
        mock_read_file: MagicMock,
    ) -> None:
        """Test that records with existing external IDs are skipped."""
        # Arrange - 3 records, 2 already exist
        mock_read_file.return_value = (
            ["id", "name"],
            [
                ["test.existing_1", "Existing 1"],
                ["test.new_1", "New 1"],
                ["test.existing_2", "Existing 2"],
            ],
        )

        mock_conn = MagicMock()
        mock_ir_model_data = MagicMock()
        # Batch query returns both existing IDs
        mock_ir_model_data.search.return_value = [1, 2]
        mock_ir_model_data.read.return_value = [
            {"module": "test", "name": "existing_1"},
            {"module": "test", "name": "existing_2"},
        ]

        def get_model(name: str) -> MagicMock:
            if name == "ir.model.data":
                return mock_ir_model_data
            return MagicMock()

        mock_conn.get_model.side_effect = get_model
        mock_get_conn.return_value = mock_conn

        # Only 1 record should be imported (test.new_1)
        mock_run_pass.return_value = (
            {"id_map": {"test.new_1": 101}, "failed_lines": []},
            False,
        )

        # Act
        success, stats = import_data(
            config="test.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv="test.csv",
            skip_existing=True,
        )

        # Assert
        assert success is True
        # Verify only 1 record was imported
        assert stats.get("created_records", 0) == 1

    @patch("odoo_data_flow.import_threaded._read_data_file")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_skip_existing_allows_all_new_records(
        self,
        mock_run_pass: MagicMock,
        mock_get_conn: MagicMock,
        mock_read_file: MagicMock,
    ) -> None:
        """Test that all records pass through when none exist."""
        mock_read_file.return_value = (
            ["id", "name"],
            [
                ["test.new_1", "New 1"],
                ["test.new_2", "New 2"],
            ],
        )

        mock_conn = MagicMock()
        mock_ir_model_data = MagicMock()
        # No records exist
        mock_ir_model_data.search.return_value = []

        def get_model(name: str) -> MagicMock:
            if name == "ir.model.data":
                return mock_ir_model_data
            return MagicMock()

        mock_conn.get_model.side_effect = get_model
        mock_get_conn.return_value = mock_conn

        mock_run_pass.return_value = (
            {"id_map": {"test.new_1": 101, "test.new_2": 102}, "failed_lines": []},
            False,
        )

        # Act
        success, stats = import_data(
            config="test.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv="test.csv",
            skip_existing=True,
        )

        # Assert
        assert success is True
        assert stats.get("created_records", 0) == 2

    @patch("odoo_data_flow.import_threaded._read_data_file")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded._run_threaded_pass")
    def test_skip_existing_handles_ids_without_module_prefix(
        self,
        mock_run_pass: MagicMock,
        mock_get_conn: MagicMock,
        mock_read_file: MagicMock,
    ) -> None:
        """Test that IDs without module prefix use __import__ module."""
        mock_read_file.return_value = (
            ["id", "name"],
            [
                ["existing_no_prefix", "Existing"],
                ["new_no_prefix", "New"],
            ],
        )

        mock_conn = MagicMock()
        mock_ir_model_data = MagicMock()
        # existing_no_prefix exists under __import__ module
        mock_ir_model_data.search.return_value = [1]
        mock_ir_model_data.read.return_value = [
            {"module": "__import__", "name": "existing_no_prefix"}
        ]

        def get_model(name: str) -> MagicMock:
            if name == "ir.model.data":
                return mock_ir_model_data
            return MagicMock()

        mock_conn.get_model.side_effect = get_model
        mock_get_conn.return_value = mock_conn

        mock_run_pass.return_value = (
            {"id_map": {"new_no_prefix": 101}, "failed_lines": []},
            False,
        )

        # Act
        success, stats = import_data(
            config="test.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv="test.csv",
            skip_existing=True,
        )

        # Assert
        assert success is True
        assert stats.get("created_records", 0) == 1

    @patch("odoo_data_flow.import_threaded._read_data_file")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    def test_skip_existing_skips_all_when_all_exist(
        self,
        mock_get_conn: MagicMock,
        mock_read_file: MagicMock,
    ) -> None:
        """Test that import completes with 0 records when all exist."""
        mock_read_file.return_value = (
            ["id", "name"],
            [
                ["test.existing_1", "Existing 1"],
            ],
        )

        mock_conn = MagicMock()
        mock_ir_model_data = MagicMock()
        mock_ir_model_data.search.return_value = [1]
        mock_ir_model_data.read.return_value = [
            {"module": "test", "name": "existing_1"}
        ]

        def get_model(name: str) -> MagicMock:
            if name == "ir.model.data":
                return mock_ir_model_data
            return MagicMock()

        mock_conn.get_model.side_effect = get_model
        mock_get_conn.return_value = mock_conn

        # Act
        success, stats = import_data(
            config="test.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv="test.csv",
            skip_existing=True,
        )

        # Assert - should succeed with 0 created records
        assert success is True
        assert stats.get("created_records", 0) == 0


class TestPreparePass2DataMany2Many:
    """Tests for many2many field handling in _prepare_pass_2_data."""

    def test_many2many_field_detection(self) -> None:
        """Test that many2many fields are detected via fields_get()."""
        # Arrange
        header = ["id", "name", "tag_ids/id"]
        all_data = [
            ["rec1", "Record 1", "tag.tag1"],
        ]
        id_map = {"rec1": 101, "tag.tag1": 201}
        deferred_fields = ["tag_ids/id"]

        mock_model = MagicMock()
        mock_model.fields_get.return_value = {
            "tag_ids": {"type": "many2many", "relation": "res.partner.category"}
        }

        # Act
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=mock_model
        )

        # Assert - should wrap in [(6, 0, [ids])] format
        assert len(result) == 1
        assert result[0][0] == 101  # db_id
        assert result[0][1]["tag_ids"] == [(6, 0, [201])]

    def test_many2many_multiple_values(self) -> None:
        """Test that comma-separated many2many values are split and resolved."""
        # Arrange
        header = ["id", "name", "tag_ids/id"]
        all_data = [
            ["rec1", "Record 1", "tag.tag1,tag.tag2,tag.tag3"],
        ]
        id_map = {"rec1": 101, "tag.tag1": 201, "tag.tag2": 202, "tag.tag3": 203}
        deferred_fields = ["tag_ids/id"]

        mock_model = MagicMock()
        mock_model.fields_get.return_value = {
            "tag_ids": {"type": "many2many", "relation": "res.partner.category"}
        }

        # Act
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=mock_model
        )

        # Assert - all three IDs should be in the list
        assert len(result) == 1
        assert result[0][0] == 101
        assert result[0][1]["tag_ids"] == [(6, 0, [201, 202, 203])]

    def test_many2many_single_value(self) -> None:
        """Test that single many2many value is properly wrapped in list."""
        # Arrange
        header = ["id", "name", "accessory_product_ids/id"]
        all_data = [
            ["prod1", "Product 1", "PRODUCT_TEMPLATE.12345"],
        ]
        id_map = {"prod1": 501, "PRODUCT_TEMPLATE.12345": 789}
        deferred_fields = ["accessory_product_ids/id"]

        mock_model = MagicMock()
        mock_model.fields_get.return_value = {
            "accessory_product_ids": {
                "type": "many2many",
                "relation": "product.template",
            }
        }

        # Act
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=mock_model
        )

        # Assert - single ID should still be wrapped correctly
        assert len(result) == 1
        assert result[0][0] == 501
        assert result[0][1]["accessory_product_ids"] == [(6, 0, [789])]

    def test_many2one_not_wrapped_in_list(self) -> None:
        """Test that many2one fields are NOT wrapped in [(6, 0, [])] format."""
        # Arrange
        header = ["id", "name", "parent_id/id"]
        all_data = [
            ["rec1", "Record 1", "parent1"],
        ]
        id_map = {"rec1": 101, "parent1": 50}
        deferred_fields = ["parent_id/id"]

        mock_model = MagicMock()
        mock_model.fields_get.return_value = {
            "parent_id": {"type": "many2one", "relation": "res.partner"}
        }

        # Act
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=mock_model
        )

        # Assert - many2one should be a single integer, not wrapped
        assert len(result) == 1
        assert result[0][0] == 101
        assert result[0][1]["parent_id"] == 50

    def test_many2many_with_whitespace(self) -> None:
        """Test that whitespace around comma-separated values is handled."""
        # Arrange
        header = ["id", "name", "tag_ids/id"]
        all_data = [
            ["rec1", "Record 1", "  tag.tag1 , tag.tag2 ,  tag.tag3  "],
        ]
        id_map = {"rec1": 101, "tag.tag1": 201, "tag.tag2": 202, "tag.tag3": 203}
        deferred_fields = ["tag_ids/id"]

        mock_model = MagicMock()
        mock_model.fields_get.return_value = {
            "tag_ids": {"type": "many2many", "relation": "res.partner.category"}
        }

        # Act
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=mock_model
        )

        # Assert - whitespace should be trimmed
        assert len(result) == 1
        assert result[0][1]["tag_ids"] == [(6, 0, [201, 202, 203])]

    def test_many2many_partial_resolution(self) -> None:
        """Test that only resolvable many2many IDs are included."""
        # Arrange
        header = ["id", "name", "tag_ids/id"]
        all_data = [
            ["rec1", "Record 1", "tag.found1,tag.missing,tag.found2"],
        ]
        # tag.missing is not in id_map
        id_map = {"rec1": 101, "tag.found1": 201, "tag.found2": 203}
        deferred_fields = ["tag_ids/id"]

        # Use spec to restrict attributes - no connection/client attrs
        mock_model = MagicMock(spec=["fields_get"])
        mock_model.fields_get.return_value = {
            "tag_ids": {"type": "many2many", "relation": "res.partner.category"}
        }

        # Act
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=mock_model
        )

        # Assert - only found IDs should be included
        assert len(result) == 1
        assert result[0][1]["tag_ids"] == [(6, 0, [201, 203])]

    def test_many2many_no_resolvable_values(self) -> None:
        """Test that empty result when no many2many values can be resolved."""
        # Arrange
        header = ["id", "name", "tag_ids/id"]
        all_data = [
            ["rec1", "Record 1", "tag.missing1,tag.missing2"],
        ]
        # None of the tags are in id_map
        id_map = {"rec1": 101}
        deferred_fields = ["tag_ids/id"]

        # Use spec to restrict attributes - no connection/client attrs
        mock_model = MagicMock(spec=["fields_get"])
        mock_model.fields_get.return_value = {
            "tag_ids": {"type": "many2many", "relation": "res.partner.category"}
        }

        # Act
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=mock_model
        )

        # Assert - no update for this record since all tags are missing
        assert len(result) == 0

    def test_fields_get_exception_handled(self) -> None:
        """Test that exception in fields_get is handled gracefully."""
        # Arrange
        header = ["id", "name", "tag_ids/id"]
        all_data = [
            ["rec1", "Record 1", "tag.tag1"],
        ]
        id_map = {"rec1": 101, "tag.tag1": 201}
        deferred_fields = ["tag_ids/id"]

        mock_model = MagicMock()
        mock_model.fields_get.side_effect = Exception("Connection error")

        # Act - should not raise, should fall back to non-m2m handling
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=mock_model
        )

        # Assert - without type info, it falls back to treating as regular field
        # This returns the integer ID directly, not wrapped in [(6, 0, [])]
        assert len(result) == 1
        assert result[0][0] == 101
        # Without many2many detection, it resolves as many2one (single ID)
        assert result[0][1]["tag_ids"] == 201

    def test_no_model_object(self) -> None:
        """Test Pass 2 works without model_obj (no type detection)."""
        # Arrange
        header = ["id", "name", "parent_id/id"]
        all_data = [
            ["rec1", "Record 1", "parent1"],
        ]
        id_map = {"rec1": 101, "parent1": 50}
        deferred_fields = ["parent_id/id"]

        # Act - no model_obj provided
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=None
        )

        # Assert - should work with basic ID resolution
        assert len(result) == 1
        assert result[0][0] == 101
        assert result[0][1]["parent_id"] == 50

    def test_mixed_field_types(self) -> None:
        """Test handling both many2many and many2one in same record."""
        # Arrange
        header = ["id", "name", "parent_id/id", "tag_ids/id"]
        all_data = [
            ["rec1", "Record 1", "parent1", "tag.tag1,tag.tag2"],
        ]
        id_map = {
            "rec1": 101,
            "parent1": 50,
            "tag.tag1": 201,
            "tag.tag2": 202,
        }
        deferred_fields = ["parent_id/id", "tag_ids/id"]

        mock_model = MagicMock()
        mock_model.fields_get.return_value = {
            "parent_id": {"type": "many2one", "relation": "res.partner"},
            "tag_ids": {"type": "many2many", "relation": "res.partner.category"},
        }

        # Act
        result = _prepare_pass_2_data(
            all_data, header, 0, id_map, deferred_fields, model_obj=mock_model
        )

        # Assert - both fields should be handled correctly
        assert len(result) == 1
        assert result[0][0] == 101
        assert result[0][1]["parent_id"] == 50  # many2one = integer
        assert result[0][1]["tag_ids"] == [(6, 0, [201, 202])]  # m2m = wrapped
