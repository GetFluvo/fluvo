"""Test the low-level, multi-threaded import logic."""

from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, call, mock_open, patch

import pytest
import requests
from rich.progress import Progress, TaskID

from odoo_data_flow.import_threaded import (
    RPCThreadImport,
    _create_batches,
    _filter_ignored_columns,
    _read_data_file,
    _setup_fail_file,
    import_data,
)


class TestRPCThreadImport:
    """Tests for the RPCThreadImport class."""

    def test_handle_rpc_error_without_adding_reason(self) -> None:
        """Test _handle_rpc_error when add_error_reason is False.

        This covers the else branch in the error handling logic, ensuring
        the original lines are returned unmodified.
        """
        # Arrange
        rpc_thread = RPCThreadImport(
            max_connection=1, model=None, header=[], add_error_reason=False
        )
        lines = [["1", "A"], ["2", "B"]]
        error = Exception("Test Error")

        # Act
        failed_lines, _ = rpc_thread._handle_rpc_error(error, lines)

        # Assert that the error reason was NOT appended
        assert len(failed_lines[0]) == 2
        # Ensure the original list is returned, not a modified copy
        assert failed_lines is lines

    def test_launch_batch_does_nothing_if_aborted(self) -> None:
        """Test launch_batch does not spawn a thread if abort_flag is set.

        This covers the early return path in the launch_batch method.
        """
        # Arrange
        rpc_thread = RPCThreadImport(max_connection=1, model=None, header=[])

        with patch(
            "odoo_data_flow.import_threaded.RPCThreadImport.spawn_thread"
        ) as mock_spawn_thread:
            # Act
            rpc_thread.abort_flag = True
            rpc_thread.launch_batch(data_lines=[["1"]], batch_number=1)

            # Assert
            mock_spawn_thread.assert_not_called()

    @patch("concurrent.futures.as_completed")
    def test_wait_method_aborts_and_cancels_futures(
        self, mock_as_completed: MagicMock
    ) -> None:
        """Test wait() aborts immediately if abort_flag is set.

        This ensures that if the abort_flag is set, the wait() method
        immediately attempts to shut down the executor and cancel pending futures.
        """
        # Arrange: Create an instance with a mock progress bar to enter the main logic
        mock_progress = MagicMock(spec=Progress)
        mock_task_id = MagicMock(spec=TaskID)
        rpc_thread = RPCThreadImport(
            1, None, [], progress=mock_progress, task_id=mock_task_id
        )

        # Arrange: Mock the executor to monitor calls to it
        rpc_thread.executor = MagicMock()

        # Arrange: Add mock futures to the list
        future1, future2 = MagicMock(), MagicMock()
        rpc_thread.futures = [future1, future2]

        # Arrange: Ensure the mocked as_completed returns the futures to allow iteration
        mock_as_completed.return_value = rpc_thread.futures

        # Act: Set the abort flag and call wait()
        rpc_thread.abort_flag = True
        rpc_thread.wait()

        # Assert: The executor was shut down with cancel_futures=True
        rpc_thread.executor.shutdown.assert_called_once_with(
            wait=True, cancel_futures=True
        )

    def test_handle_odoo_messages_with_error_reason(self) -> None:
        """Test _handle_odoo_messages with an error reason.

        Tests that when add_error_reason is True, the reason is appended to
        the failed lines.
        """
        header = ["id", "name"]
        lines = [["1", "A"], ["2", "B"]]
        rpc_thread = RPCThreadImport(1, None, header, add_error_reason=True)
        messages = [{"message": "Generic Error"}]
        failed_lines, _ = rpc_thread._handle_odoo_messages(messages, lines)
        assert failed_lines[0][-1] == "Generic Error"

    def test_handle_odoo_messages_no_error_reason(self) -> None:
        """Test _handle_odoo_messages without an error reason.

        Tests that when add_error_reason is False, the reason is not appended.
        """
        header = ["id", "name"]
        lines = [["1", "A"], ["2", "B"]]

        rpc_thread = RPCThreadImport(1, None, header, add_error_reason=False)
        messages = [{"message": "Generic Error", "record": 0}]
        failed_lines, _ = rpc_thread._handle_odoo_messages(messages, lines)
        assert len(failed_lines[0]) == 2

    def test_execute_batch_sets_abort_flag_on_connection_error(self) -> None:
        """Test that a ConnectionError sets the abort_flag.

        This test ensures that a critical connection error during a batch
        import correctly sets the thread's abort_flag to stop further processing.
        """
        mock_model = MagicMock()
        mock_model.load.side_effect = requests.exceptions.ConnectionError("Odoo down")

        rpc_thread = RPCThreadImport(max_connection=1, model=mock_model, header=[])
        assert rpc_thread.abort_flag is False  # Pre-condition

        # Run the batch that will fail
        rpc_thread._execute_batch(lines=[["1"]], num=1, do_check=False)

        # Assert that the flag was set
        assert rpc_thread.abort_flag is True

    def test_handle_record_mismatch(self) -> None:
        """Test _handle_record_mismatch.

        Tests the logic for handling a mismatch between the number of records
        sent and the number of IDs returned by Odoo.
        """
        header = ["id", "name"]
        lines = [["1", "A"], ["2", "B"]]

        rpc_thread = RPCThreadImport(1, None, header, add_error_reason=True)
        response = {"ids": [123]}
        failed_lines, _ = rpc_thread._handle_record_mismatch(response, lines)
        assert len(failed_lines) == 2
        assert "Record count mismatch" in failed_lines[0][2]

    def test_handle_rpc_error(self) -> None:
        """Test _handle_rpc_error.

        Tests the logic for handling a general RPC exception during the call.
        """
        header = ["id", "name"]
        lines = [["1", "A"], ["2", "B"]]

        rpc_thread = RPCThreadImport(1, None, header, add_error_reason=True)
        error = Exception("Connection Timed Out")
        failed_lines, _ = rpc_thread._handle_rpc_error(error, lines)
        assert len(failed_lines) == 2
        assert failed_lines[0][2] == "Connection Timed Out"

    def test_execute_batch_handles_json_decode_error(self) -> None:
        """Test _execute_batch with a JSONDecodeError.

        Tests that a JSONDecodeError is handled gracefully with a
        user-friendly message.
        """
        header = ["id", "name"]
        lines = [["xml_id_1", "Record 1"]]
        mock_writer = MagicMock()
        mock_model = MagicMock()
        mock_model.load.side_effect = requests.exceptions.JSONDecodeError(
            "Expecting value", "", 0
        )

        rpc_thread = RPCThreadImport(
            1, mock_model, header, mock_writer, add_error_reason=True
        )

        with patch("odoo_data_flow.import_threaded.log.error") as mock_log_error:
            rpc_thread._execute_batch(lines, "0", do_check=False)

            mock_log_error.assert_called_once()
            assert "invalid (non-JSON) response" in mock_log_error.call_args[0][0]
            mock_writer.writerows.assert_called_once()
            failed_data = mock_writer.writerows.call_args[0][0]
            assert "invalid (non-JSON) response" in failed_data[0][-1]

    def test_handle_odoo_messages_saves_all_records_from_failed_batch(
        self,
    ) -> None:
        """Test _handle_odoo_messages saves all records from a failed batch.

        Tests that when Odoo reports specific record errors, all other records
        in that same batch are also marked as failed to simulate a rollback.
        """
        header = ["id", "name"]
        lines = [
            ["xml_id_1", "Alice"],
            ["xml_id_2", ""],
            ["xml_id_3", "Charlie"],
        ]

        rpc_thread = RPCThreadImport(1, None, header, add_error_reason=True)
        messages = [{"message": "Name is required", "record": 1}]

        failed_lines, first_error = rpc_thread._handle_odoo_messages(messages, lines)

        assert len(failed_lines) == 3
        assert first_error == "Name is required"

        failed_record = next(line for line in failed_lines if line[0] == "xml_id_2")
        rolled_back_record = next(
            line for line in failed_lines if line[0] == "xml_id_1"
        )
        assert failed_record[-1] == "Name is required"
        assert "rolled back" in rolled_back_record[-1]

    @patch("odoo_data_flow.lib.internal.rpc_thread.RpcThread.wait")
    def test_wait_fallback_without_progress(self, mock_super_wait: MagicMock) -> None:
        """Tests that wait() calls super().wait() if no progress bar is given."""
        rpc_thread = RPCThreadImport(1, None, [], None)
        rpc_thread.wait()
        mock_super_wait.assert_called_once()

    def test_wait_updates_progress_bar(self) -> None:
        """Test wait() updates the progress bar.

        Tests that the wait method correctly updates the rich progress bar
        on task completion.
        """
        mock_progress = MagicMock(spec=Progress)
        mock_task_id = MagicMock(spec=TaskID)
        rpc_thread = RPCThreadImport(
            1, None, [], progress=mock_progress, task_id=mock_task_id
        )
        future = MagicMock()
        future.result.return_value = 5
        rpc_thread.futures = [future]

        with patch("concurrent.futures.as_completed", return_value=[future]):
            rpc_thread.wait()

        mock_progress.update.assert_called_once_with(mock_task_id, advance=5)

    @patch("odoo_data_flow.lib.internal.rpc_thread.log.error")
    def test_wait_handles_and_logs_exception(self, mock_log_error: MagicMock) -> None:
        """Test wait() handles exceptions from futures.

        Tests that the wait method correctly catches and logs exceptions
        that occur within a worker thread's future.
        """
        mock_progress = MagicMock(spec=Progress)
        mock_task_id = MagicMock(spec=TaskID)

        rpc_thread = RPCThreadImport(
            1, None, [], progress=mock_progress, task_id=mock_task_id
        )
        future = MagicMock()
        future.result.side_effect = ValueError("Worker thread failed")
        rpc_thread.futures = [future]

        with patch("concurrent.futures.as_completed", return_value=[future]):
            rpc_thread.wait()

        mock_log_error.assert_called_once()
        assert "task in a worker thread failed" in mock_log_error.call_args[0][0]
        assert mock_log_error.call_args.kwargs["exc_info"] is True

    def test_execute_batch_writes_failed_lines(self) -> None:
        """Test _execute_batch writes failed lines.

        Tests that when a batch fails the Odoo `load` call, the failed
        records are correctly written to the fail file writer.
        """
        header = ["id", "name"]
        lines_to_process = [["1", "Record A"]]
        mock_writer = MagicMock()
        mock_model = MagicMock()
        mock_model.load.return_value = {
            "messages": [{"message": "A specific Odoo error", "record": 0}],
            "ids": False,
        }
        # FIX: Corrected constructor arguments and removed invalid 'thread_id' kwarg.
        rpc_thread = RPCThreadImport(
            max_connection=1,
            model=mock_model,
            header=header,
            writer=mock_writer,
            add_error_reason=True,
        )

        rpc_thread._execute_batch(lines_to_process, "batch_1", do_check=True)

        expected_failed_line = [["1", "Record A", "A specific Odoo error"]]
        mock_writer.writerows.assert_called_once_with(expected_failed_line)


class TestHelperFunctions:
    """Tests for the standalone helper functions in the module."""

    def test_read_data_file_not_found(self) -> None:
        """Test _read_data_file with a non-existent file.

        Tests that _read_data_file returns empty lists for a file that
        does not exist.
        """
        header, data = _read_data_file("non_existent_file.csv", ";", "utf-8", 0)
        assert header == []
        assert data == []

    @patch("builtins.open", new_callable=mock_open)
    def test_setup_fail_file_os_error(self, mock_open: MagicMock) -> None:
        """Test _setup_fail_file when OSError occurs."""
        mock_open.side_effect = OSError("Permission denied")
        writer, handle = _setup_fail_file("fail.csv", ["id"], False, ";", "utf-8")
        assert writer is None
        assert handle is None

    @patch("odoo_data_flow.import_threaded._read_data_file", return_value=([], []))
    def test_import_data_empty_file(self, mock_read_data_file: MagicMock) -> None:
        """Test import_data when the data file is empty."""
        result = import_data("dummy.conf", "model", file_csv="dummy.csv")
        assert result is False

    def test_handle_odoo_messages_with_error_reason_generic(self) -> None:
        """Test _handle_odoo_messages.

        Test _handle_odoo_messages with generic messages and add_error_reason=True.
        """
        rpc_thread = RPCThreadImport(1, MagicMock(), [], add_error_reason=True)
        messages = [{"message": "Generic Error"}]
        original_lines = [["1", "Test"], ["2", "Test2"]]
        failed_lines, _ = rpc_thread._handle_odoo_messages(messages, original_lines)
        assert failed_lines == [
            ["1", "Test", "Generic Error"],
            ["2", "Test2", "Generic Error"],
        ]

    def test_read_data_file_with_skip(self, tmp_path: Path) -> None:
        """Tests that _read_data_file correctly skips the specified number of lines."""
        source_file = tmp_path / "test.csv"
        source_file.write_text("id,name\n1,A\n2,B\n3,C")
        header, data = _read_data_file(str(source_file), ",", "utf-8", skip=1)
        assert header == ["id", "name"]
        assert data == [["2", "B"], ["3", "C"]]

    def test_create_batches_with_o2m(self) -> None:
        """Tests batch creation with o2m handling."""
        header = ["id", "rel_id", "value"]
        data = [
            ["rec1", "relA", "v1"],
            ["", "relA", "v2"],  # o2m line for relA
            ["rec2", "relB", "v3"],
            ["rec3", "relB", "v4"],
        ]
        batches = list(_create_batches(data, ["rel_id"], header, 2, o2m=True))
        assert len(batches) == 2
        assert batches[0][0] == 1
        assert len(batches[0][1]) == 2  # rec1 and its o2m line
        assert batches[1][0] == 2
        assert len(batches[1][1]) == 2  # rec2 and rec3 for relB

    def test_handle_odoo_messages_record_index_out_of_bounds(self) -> None:
        """Tests that messages with out-of-bounds record indices are handled."""
        rpc_thread = RPCThreadImport(1, MagicMock(), [], add_error_reason=True)
        messages = [{"message": "Error", "record": 99}]  # Index 99 is out of bounds
        original_lines = [["1", "Test"]]
        failed_lines, _ = rpc_thread._handle_odoo_messages(messages, original_lines)
        # The message is generic now, so it applies to all lines
        assert len(failed_lines) == 1
        assert failed_lines[0][-1] == "Error"

    def test_create_batches_value_error(self) -> None:
        """Test _create_batches when split_by_col is not found."""
        header = ["id", "name"]
        data = [["1", "Test"]]
        batches = list(_create_batches(data, ["non_existent_col"], header, 1, False))
        assert batches == []

    @patch(
        "odoo_data_flow.import_threaded.open",
        side_effect=Exception("Read error"),
    )
    def test_read_data_file_generic_exception(self, mock_open: MagicMock) -> None:
        """Test _read_data_file with a generic exception.

        Tests that _read_data_file handles generic exceptions during file read.
        """
        header, data = _read_data_file("any_file.csv", ";", "utf-8", 0)
        assert header == []
        assert data == []

    def test_read_data_file_no_id_column(self, tmp_path: Path) -> None:
        """Test _read_data_file with a missing 'id' column.

        Tests that _read_data_file raises a ValueError if the required 'id'
        column for external IDs is missing from the header.
        """
        source_file = tmp_path / "no_id.csv"
        source_file.write_text("name,value\nTest,100")
        with pytest.raises(ValueError, match="'id' column for external IDs"):
            _read_data_file(str(source_file), ",", "utf-8", 0)

    def test_create_batches_split_by_size(self) -> None:
        """Test _create_batches splits by size correctly.

        Tests that batches are created based on `batch_size` when the group
        value remains the same across records.
        """
        header = ["id", "group_id"]
        data = [["1", "A"], ["2", "A"], ["3", "A"], ["4", "A"], ["5", "A"]]
        batches = list(_create_batches(data, ["group_id"], header, 3, False))
        assert len(batches) == 2
        assert len(batches[0][1]) == 3
        assert len(batches[1][1]) == 2

    def test_create_batches_value_error_split_by_col_not_found(self) -> None:
        """Test create batches value error by split col.

        _create_batches logs error and returns empty when split_by_col
        is not found.
        """
        header = ["col1", "col2"]
        data = [["a", "1"], ["b", "2"]]
        with patch("odoo_data_flow.import_threaded.log") as mock_log:
            batches = list(
                _create_batches(data, ["non_existent_col"], header, 1, False)
            )
            assert batches == []
            mock_log.error.assert_called_once()
            # FIX: Check if the expected message is contained in the actual log call
            assert "not found in header" in mock_log.error.call_args[0][0]

    @patch(
        "odoo_data_flow.import_threaded._setup_fail_file",
        return_value=(None, None),
    )
    @patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
    def test_import_data_setup_fail_file_fails(
        self, mock_get_connection: MagicMock, mock_setup_fail_file: MagicMock
    ) -> None:
        """Test import_data returns False if _setup_fail_file fails."""
        mock_model = mock_get_connection.return_value.get_model.return_value
        # The mock must return a dictionary with 1 ID to match the data
        mock_model.load.return_value = {"ids": [123], "messages": []}
        result = import_data(
            "dummy.conf",
            "model",
            header=["id"],
            data=[["1"]],
            fail_file="fail.csv",
        )
        assert result is False

    @patch("odoo_data_flow.import_threaded.log.error")
    def test_read_data_file_value_error(self, mock_log_error: MagicMock) -> None:
        """Test _read_data_file handles ValueError."""
        with patch(
            "builtins.open", mock_open(read_data="id,name\n1,test")
        ) as mock_file:
            mock_file.side_effect = ValueError("Test error")
            with pytest.raises(ValueError):
                _read_data_file("dummy.csv", ",", "utf-8", 0)
            mock_log_error.assert_called_once_with(
                f"Failed to read file dummy.csv: {ValueError('Test error')}"
            )

    def test_handle_rpc_error_no_error_reason(self) -> None:
        """Tests that when add_error_reason is False, the reason is not appended."""
        header = ["id", "name"]
        lines = [["1", "A"], ["2", "B"]]
        mock_writer = MagicMock()
        rpc_thread = RPCThreadImport(
            1, None, header, mock_writer, add_error_reason=False
        )
        error = Exception("Connection Timed Out")
        failed_lines, _ = rpc_thread._handle_rpc_error(error, lines)
        assert len(failed_lines) == 2
        assert len(failed_lines[0]) == 2  # No error reason appended

    @patch("odoo_data_flow.import_threaded.log.error")
    @patch("odoo_data_flow.import_threaded.RPCThreadImport.spawn_thread")
    def test_launch_batch_fun_generic_exception(
        self, mock_spawn_thread: MagicMock, mock_log_error: MagicMock
    ) -> None:
        """Tests that a generic exception in launch_batch_fun is handled."""
        mock_model = MagicMock()
        mock_model.load.side_effect = Exception("Generic Error")
        rpc_thread = RPCThreadImport(1, mock_model, ["id"], None)

        def spawn_side_effect(
            func: Callable[..., Any],
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            # Call the function immediately
            func(*args, **kwargs)

        mock_spawn_thread.side_effect = spawn_side_effect

        rpc_thread.launch_batch([["1"]], 1, False)

        mock_log_error.assert_called_once()
        assert (
            "RPC call for batch 1 failed: Generic Error"
            in mock_log_error.call_args[0][0]
        )

    @patch("odoo_data_flow.import_threaded.log.error")
    @patch("odoo_data_flow.import_threaded.RPCThreadImport.spawn_thread")
    def test_launch_batch_fun_record_mismatch(
        self, mock_spawn_thread: MagicMock, mock_log_error: MagicMock
    ) -> None:
        """Tests that a record mismatch in launch_batch_fun is handled."""
        mock_model = MagicMock()
        mock_model.load.return_value = {"ids": [1]}  # Mismatch: 2 lines sent, 1 id back
        rpc_thread = RPCThreadImport(1, mock_model, ["id"], None, add_error_reason=True)

        def spawn_side_effect(
            func: Callable[..., Any],
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            # Call the function immediately
            func(*args, **kwargs)

        mock_spawn_thread.side_effect = spawn_side_effect

        lines_to_send = [["1"], ["2"]]
        with patch.object(
            rpc_thread,
            "_handle_record_mismatch",
        ) as mock_handle_mismatch:
            rpc_thread.launch_batch(lines_to_send, 1, True)
            mock_handle_mismatch.assert_called_once()

    @patch("odoo_data_flow.import_threaded.log.error")
    @patch("odoo_data_flow.import_threaded.RPCThreadImport.spawn_thread")
    def test_launch_batch_connection_error(
        self, mock_spawn_thread: MagicMock, mock_log_error: MagicMock
    ) -> None:
        """Tests that a ConnectionError in launch_batch_fun is handled."""
        mock_model = MagicMock()
        mock_model.load.side_effect = requests.exceptions.ConnectionError(
            "Connection Error"
        )
        rpc_thread = RPCThreadImport(1, mock_model, ["id"], None)

        def spawn_side_effect(
            func: Callable[..., Any],
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            func(*args, **kwargs)

        mock_spawn_thread.side_effect = spawn_side_effect

        rpc_thread.launch_batch([["1"]], 1, False)

        assert rpc_thread.abort_flag is True
        mock_log_error.assert_called_once()
        assert (
            "Connection to Odoo failed: Connection Error"
            in mock_log_error.call_args[0][0]
        )

    def test_create_batches_with_grouping_and_o2m(self) -> None:
        """Test _create_batches with both grouping and o2m enabled.

        This test covers the complex interaction where records are grouped by a
        specific column, and some of those records have one-to-many child lines
        that must stay with their parent.
        """
        # --- Arrange ---
        header = ["id", "rel_id", "value"]
        data = [
            ["rec1", "relA", "v1"],
            ["", "relA", "v2"],  # o2m line for rec1
            ["rec2", "relB", "v3"],
            ["rec3", "relA", "v4"],  # Another record for group relA
            ["", "relA", "v5"],  # o2m line for rec3
        ]

        # --- Act ---
        # Group by 'rel_id', which should create two separate batches.
        batches = list(_create_batches(data, ["rel_id"], header, 10, o2m=True))

        # --- Assert ---
        # We expect two batches, one for 'relA' and one for 'relB'.
        assert len(batches) == 2

        # Find the batches by checking the rel_id of their first record.
        batch_a = next(b for b in batches if b[1][0][1] == "relA")
        batch_b = next(b for b in batches if b[1][0][1] == "relB")

        # The 'relA' batch should contain rec1,
        # its o2m line, and rec3 with its o2m line.
        assert len(batch_a[1]) == 4
        assert batch_a[1][0][0] == "rec1"
        assert batch_a[1][1][0] == ""  # o2m line
        assert batch_a[1][2][0] == "rec3"
        assert batch_a[1][3][0] == ""  # o2m line

        # The 'relB' batch should contain only rec2.
        assert len(batch_b[1]) == 1
        assert batch_b[1][0][0] == "rec2"


class TestImportData:
    """Tests for the main import_data orchestrator function."""

    def test_import_data_no_header_or_data(self) -> None:
        """Test import_data with no data provided.

        Tests that import_data raises a ValueError if it is called without a
        data file or explicit header/data arguments.
        """
        with pytest.raises(ValueError, match="Please provide either a data file"):
            import_data(config_file="dummy.conf", model="dummy.model")

    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    def test_import_data_connection_fails(self, mock_get_conn: MagicMock) -> None:
        """Test import_data when the Odoo connection fails.

        Tests that the function exits gracefully and returns False if the
        initial connection to Odoo cannot be established.
        """
        mock_get_conn.side_effect = Exception("Cannot connect")
        result = import_data(
            config_file="bad.conf",
            model="dummy.model",
            header=["id"],
            data=[["1"]],
        )
        assert result is False

    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded.open")
    @patch("odoo_data_flow.import_threaded.os.makedirs")
    def test_import_data_fail_file_oserror(
        self,
        mock_makedirs: MagicMock,
        mock_open: MagicMock,
        mock_get_conn: MagicMock,
    ) -> None:
        """Tests that the function handles an OSError when opening the fail file."""
        mock_get_conn.return_value = MagicMock()
        mock_open.side_effect = OSError("Permission denied")
        result = import_data(
            config_file="dummy.conf",
            model="dummy.model",
            header=["id"],
            data=[["1"]],
            fail_file="protected/fail.csv",
        )
        assert result is False
        mock_open.assert_called_once()

    @patch("odoo_data_flow.import_threaded.RPCThreadImport")
    def test_import_data_ignore_columns(self, mock_rpc_thread: MagicMock) -> None:
        """Tests that the 'ignore' parameter correctly filters columns."""
        header = ["id", "name", "field_to_ignore"]
        data = [["1", "A", "ignore_me"]]
        with patch(
            "odoo_data_flow.import_threaded.conf_lib.get_connection_from_config"
        ):
            import_data(
                config_file="dummy.conf",
                model="dummy.model",
                header=header,
                data=data,
                ignore=["field_to_ignore"],
            )
        init_args = mock_rpc_thread.call_args.args
        filtered_header = init_args[2]
        assert filtered_header == ["id", "name"]

    @patch("odoo_data_flow.import_threaded.RPCThreadImport")
    def test_import_data_aborts_on_connection_error(
        self, mock_rpc_thread_class: MagicMock
    ) -> None:
        """Tests that the import process aborts on a connection error."""
        mock_rpc_instance = mock_rpc_thread_class.return_value

        def launch_side_effect(*args: Any, **kwargs: Any) -> None:
            mock_rpc_instance.abort_flag = True

        mock_rpc_instance.launch_batch.side_effect = launch_side_effect
        with patch(
            "odoo_data_flow.import_threaded.conf_lib.get_connection_from_config"
        ):
            result = import_data(
                config_file="dummy.conf",
                model="dummy.model",
                header=["id"],
                data=[["1"], ["2"], ["3"]],
                batch_size=1,
            )
        assert result is False

    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    def test_import_data_returns_true_on_success(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test import_data returns True on a successful run.

        Mocks a successful Odoo `load` call and asserts that the final
        return value of the import is True.
        """
        mock_model = mock_get_conn.return_value.get_model.return_value
        mock_model.load.return_value = {"ids": [123], "messages": []}
        result = import_data(
            config_file="dummy.conf",
            model="dummy.model",
            header=["id"],
            data=[["1"]],
        )
        assert result is True

    def test_handle_odoo_messages_no_record_details(self) -> None:
        """Test _handle_odoo_messages when Odoo sends generic messages."""
        rpc_thread = RPCThreadImport(1, MagicMock(), [], add_error_reason=True)
        messages = [{"message": "Generic Error"}]
        original_lines = [["1", "Test"], ["2", "Test2"]]
        failed_lines, _ = rpc_thread._handle_odoo_messages(messages, original_lines)
        assert failed_lines == [
            ["1", "Test", "Generic Error"],
            ["2", "Test2", "Generic Error"],
        ]

    def test_handle_rpc_error_with_error_reason_appends_message(self) -> None:
        """Test _handle_rpc_error.

        Test _handle_rpc_error appends error message when add_error_reason is True.
        """
        rpc_thread = RPCThreadImport(1, MagicMock(), [], add_error_reason=True)
        error = Exception("Test RPC Error")
        lines = [["1", "data1"], ["2", "data2"]]
        failed_lines, _ = rpc_thread._handle_rpc_error(error, lines)
        assert failed_lines == [
            ["1", "data1", "Test RPC Error"],
            ["2", "data2", "Test RPC Error"],
        ]

    def test_handle_record_mismatch_with_error_reason_appends_message(
        self,
    ) -> None:
        """Test _handle_record_mismatch.

        Test _handle_record_mismatch appends error message when add_error_reason
        is True.
        """
        rpc_thread = RPCThreadImport(1, MagicMock(), [], add_error_reason=True)
        response = {"ids": [1]}
        lines = [["1", "data1"], ["2", "data2"]]
        failed_lines, error_summary = rpc_thread._handle_record_mismatch(
            response, lines
        )
        assert failed_lines[0][-1].startswith("Record count mismatch")
        assert error_summary.startswith("Record count mismatch")

    @patch("odoo_data_flow.import_threaded.RPCThreadImport.spawn_thread")
    def test_launch_batch_with_failed_lines_and_writer(
        self,
        mock_spawn_thread: MagicMock,
    ) -> None:
        """Test launch_batch writes failed lines when writer is present."""
        mock_writer = MagicMock()
        mock_model = MagicMock()
        rpc_thread = RPCThreadImport(1, mock_model, [], writer=mock_writer)

        # Simulate a failed batch by making model.load return messages
        mock_model.load.return_value = {"messages": [{"message": "Error", "record": 0}]}

        def spawn_side_effect(
            func: Callable[..., Any],
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> None:
            # Call the function synchronously to simulate the thread's execution
            func(*args, **kwargs)

        mock_spawn_thread.side_effect = spawn_side_effect
        # Call the correct method to trigger the spawn_thread mock
        rpc_thread.launch_batch(data_lines=[["1", "Test"]], batch_number=1, check=False)

        mock_writer.writerows.assert_called_once()

    def test_wait_method_without_progress_bar(self) -> None:
        """Test the wait() method's fallback when no progress bar is given.

        This test ensures that the wait() method completes without errors when
        the RPCThreadImport instance is created without a progress bar.
        """
        rpc_thread = RPCThreadImport(max_connection=1, model=None, header=[])

        def trivial_task() -> int:
            return 5

        future = rpc_thread.executor.submit(trivial_task)
        rpc_thread.futures = [future]

        try:
            rpc_thread.wait()
        except Exception as e:
            pytest.fail(f"rpc_thread.wait() raised an unexpected exception: {e}")

        assert rpc_thread.executor._shutdown

    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    def test_import_data_aborts_on_rpc_error(self, mock_get_conn: MagicMock) -> None:
        """Test import_data aborts and returns False on an RPC error.

        Simulates a connection error during the `load` call and asserts that
        the import process stops and returns False.
        """
        mock_model = mock_get_conn.return_value.get_model.return_value
        mock_model.load.side_effect = requests.exceptions.ConnectionError("RPC Error")
        result = import_data(
            config_file="dummy.conf",
            model="dummy.model",
            header=["id"],
            data=[["1"], ["2"]],
            batch_size=1,
        )
        assert result is False

    def test_filter_ignored_columns_empty_ignore(self) -> None:
        """Test _filter_ignored_columns when ignore list is empty."""
        header = ["id", "name"]
        data = [["1", "Test"]]
        new_header, new_data = _filter_ignored_columns([], header, data)
        assert new_header == header
        assert new_data == data

    @patch("odoo_data_flow.import_threaded.RPCThreadImport.spawn_thread")
    def test_launch_batch_abort_flag(self, mock_spawn_thread: MagicMock) -> None:
        """Test launch_batch when abort_flag is True."""
        rpc_thread = RPCThreadImport(1, MagicMock(), [])
        rpc_thread.abort_flag = True
        rpc_thread.launch_batch([["1", "Test"]], 1)
        mock_spawn_thread.assert_not_called()

    @patch("concurrent.futures.as_completed")
    @patch("concurrent.futures.ThreadPoolExecutor")
    def test_wait_abort_flag(
        self, mock_thread_pool_executor: MagicMock, mock_as_completed: MagicMock
    ) -> None:
        """Test wait when abort_flag is True."""
        mock_executor_instance = MagicMock()
        mock_thread_pool_executor.return_value = mock_executor_instance
        mock_progress = MagicMock()
        mock_task_id = MagicMock()
        rpc_thread = RPCThreadImport(
            1, MagicMock(), [], progress=mock_progress, task_id=mock_task_id
        )
        rpc_thread.abort_flag = True
        mock_future = MagicMock()
        mock_future.result.return_value = {"processed": 1}
        mock_as_completed.return_value = [mock_future]
        rpc_thread.futures = [mock_future]
        rpc_thread.wait()
        mock_executor_instance.shutdown.assert_called_once_with(
            wait=True, cancel_futures=True
        )

    @patch("concurrent.futures.as_completed")
    def test_wait_future_exception(self, mock_as_completed: MagicMock) -> None:
        """Test wait when a future raises an exception."""
        rpc_thread = RPCThreadImport(
            1, MagicMock(), [], progress=MagicMock(), task_id=MagicMock()
        )
        mock_future = MagicMock()
        mock_future.result.side_effect = Exception("Future error")
        mock_as_completed.return_value = [mock_future]
        rpc_thread.futures = [mock_future]
        rpc_thread.executor = MagicMock()
        rpc_thread.wait()
        rpc_thread.executor.shutdown.assert_called_once_with(wait=True)

    @patch("odoo_data_flow.import_threaded.Progress")
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    def test_import_data_with_progress(
        self, mock_get_conn: MagicMock, mock_progress_class: MagicMock
    ) -> None:
        """Test import_data uses the rich progress bar.

        Tests that the main import function correctly initializes and uses the
        rich Progress bar during its operation.
        """
        mock_model = mock_get_conn.return_value.get_model.return_value
        mock_model.load.return_value = {"ids": [1], "messages": []}

        # Get the mock instance that will be created inside import_data
        mock_progress_instance = mock_progress_class.return_value

        import_data(
            config_file="dummy.conf",
            model="dummy.model",
            header=["id"],
            data=[["1"]],
        )

        # Assert that the progress bar was used
        mock_progress_class.assert_called_once()
        mock_progress_instance.add_task.assert_called_once()
        mock_progress_instance.update.assert_called()

    @patch("builtins.open", new_callable=mock_open)
    @patch("odoo_data_flow.import_threaded.conf_lib.get_connection_from_config")
    @patch("odoo_data_flow.import_threaded.os.makedirs")
    def test_import_data_returns_false_if_setup_fail_file_fails(
        self,
        mock_makedirs: MagicMock,
        mock_get_conn: MagicMock,
        mock_open_file: MagicMock,
    ) -> None:
        """Test import_data returns False if _setup_fail_file fails.

        Patches the fail file setup function to simulate a failure and asserts
        that the main import function returns False as a result.
        """
        # 1. Simulate a failure within the real _setup_fail_file by making
        #    the 'open' call it depends on raise an error.
        mock_open_file.side_effect = OSError("Permission denied")
        mock_get_conn.return_value.get_model.return_value = MagicMock()

        # 2. Call the function under test
        result = import_data(
            config_file="dummy.conf",
            model="model",
            header=["id"],
            data=[["1"]],
            fail_file="protected/fail.csv",
        )

        # 3. Assert that the error was handled and the function returned False
        assert result is False
        mock_open_file.assert_called_once_with(
            "protected/fail.csv", "w", newline="", encoding="utf-8"
        )


@patch("csv.field_size_limit")
def test_csv_field_size_limit_overflow(
    mock_field_size_limit: MagicMock,
) -> None:
    """Test that csv.field_size_limit handles OverflowError."""
    # Arrange: Make the first call fail, subsequent calls succeed.
    mock_field_size_limit.side_effect = [OverflowError, None]

    # Act: Reload the module to trigger the top-level code.
    import importlib

    from odoo_data_flow import import_threaded

    # We need to patch sys.maxsize for the reload to have a predictable start value
    with patch("sys.maxsize", 100):
        importlib.reload(import_threaded)

    # Assert
    # The function should have been called twice.
    assert mock_field_size_limit.call_count == 2
    mock_field_size_limit.assert_has_calls([call(100), call(2**30)])
