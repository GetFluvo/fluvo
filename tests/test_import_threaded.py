"""Tests for the refactored, low-level, multi-threaded import logic."""

from pathlib import Path
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
    _orchestrate_pass_1,
    _orchestrate_pass_2,
    _read_data_file,
    _setup_fail_file,
    _stream_csv_batches,
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
            "[yellow]WARN:[/] Batch 1 hit scalable error. "
            "Reducing chunk size to 2 and retrying."
        )
        mock_progress.console.print.assert_any_call(
            "[yellow]WARN:[/] Batch 1 hit scalable error. "
            "Reducing chunk size to 1 and retrying."
        )

    @patch("odoo_data_flow.import_threaded._create_batch_individually")
    def test_batch_scales_down_on_gateway_error(
        self, mock_create_individually: MagicMock
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
        mock_progress.console.print.assert_any_call(
            "[yellow]WARN:[/] Server overload detected (502/503). "
            "Adding 1.0s delay between batches."
        )
        mock_progress.console.print.assert_any_call(
            "[yellow]WARN:[/] Batch 1 hit scalable error. "
            "Reducing chunk size to 2 and retrying."
        )

    @patch("odoo_data_flow.import_threaded._create_batch_individually")
    def test_batch_falls_back_for_non_scalable_error(
        self, mock_create_individually: MagicMock
    ) -> None:
        """Verify fallback to create for regular errors."""
        mock_model = MagicMock()
        mock_model.load.side_effect = [ValueError("Invalid field value")]
        mock_create_individually.return_value = {
            "id_map": {"rec1": 1},
            "failed_lines": [["rec2", "B", "Error"]],
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
        mock_create_individually.assert_called_once()


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
        # We expect two separate write calls because the vals are different
        assert mock_run_pass.call_count == 1

        # Get the batches that were passed to the runner
        call_args = mock_run_pass.call_args[0]
        batches = list(call_args[2])  # The batches iterable

        assert len(batches) == 3  # Three unique sets of values to write

        # Convert batches to a more easily searchable dict
        batch_dict = {
            frozenset(vals.items()): ids for (ids, vals) in [b[1] for b in batches]
        }

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
        batch_header = ["id", "name"]
        # This row has only one column, but the header has two
        batch_lines = [["record1"]]

        result = _create_batch_individually(
            mock_model, batch_lines, batch_header, 0, {}, []
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

        mock_model = MagicMock()
        mock_ir_model_data = MagicMock()
        mock_ir_model_data.search.return_value = []  # No existing entry
        mock_model.browse.return_value.env = {"ir.model.data": mock_ir_model_data}

        result = _create_xmlid_entry(mock_model, "my_module.partner_001", 42, "res.partner")

        assert result is True
        mock_ir_model_data.create.assert_called_once_with({
            "module": "my_module",
            "name": "partner_001",
            "model": "res.partner",
            "res_id": 42,
        })

    def test_create_xmlid_entry_without_module_prefix(self) -> None:
        """Test XML ID creation without module prefix (uses __import__)."""
        from odoo_data_flow.import_threaded import _create_xmlid_entry

        mock_model = MagicMock()
        mock_ir_model_data = MagicMock()
        mock_ir_model_data.search.return_value = []  # No existing entry
        mock_model.browse.return_value.env = {"ir.model.data": mock_ir_model_data}

        result = _create_xmlid_entry(mock_model, "PARTNER_001", 42, "res.partner")

        assert result is True
        mock_ir_model_data.create.assert_called_once_with({
            "module": "__import__",
            "name": "PARTNER_001",
            "model": "res.partner",
            "res_id": 42,
        })

    def test_create_xmlid_entry_existing_entry_same_res_id(self) -> None:
        """Test that existing entries with same res_id are not updated."""
        from odoo_data_flow.import_threaded import _create_xmlid_entry

        mock_model = MagicMock()
        mock_existing = MagicMock()
        mock_existing.res_id = 42  # Same res_id
        mock_ir_model_data = MagicMock()
        mock_ir_model_data.search.return_value = mock_existing
        mock_model.browse.return_value.env = {"ir.model.data": mock_ir_model_data}

        result = _create_xmlid_entry(mock_model, "my_module.partner_001", 42, "res.partner")

        assert result is True
        mock_ir_model_data.create.assert_not_called()
        mock_existing.write.assert_not_called()

    def test_create_xmlid_entry_existing_entry_different_res_id(self) -> None:
        """Test that existing entries with different res_id are updated."""
        from odoo_data_flow.import_threaded import _create_xmlid_entry

        mock_model = MagicMock()
        mock_existing = MagicMock()
        mock_existing.res_id = 99  # Different res_id
        mock_ir_model_data = MagicMock()
        mock_ir_model_data.search.return_value = mock_existing
        mock_model.browse.return_value.env = {"ir.model.data": mock_ir_model_data}

        result = _create_xmlid_entry(mock_model, "my_module.partner_001", 42, "res.partner")

        assert result is True
        mock_ir_model_data.create.assert_not_called()
        mock_existing.write.assert_called_once_with({"res_id": 42, "model": "res.partner"})

    def test_create_xmlid_entry_handles_exception(self) -> None:
        """Test that exceptions during XML ID creation are handled gracefully."""
        from odoo_data_flow.import_threaded import _create_xmlid_entry

        mock_model = MagicMock()
        mock_model.browse.side_effect = Exception("Connection error")

        result = _create_xmlid_entry(mock_model, "my_module.partner_001", 42, "res.partner")

        assert result is False


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
        new_header, new_data = _filter_ignored_columns(["age"], header, data)
        # Malformed row should be skipped
        assert len(new_data) == 2
        assert new_data[0][0] == "1"
        assert new_data[1][0] == "3"

    def test_filter_ignored_columns_with_subfield_notation(self) -> None:
        """Test that parent_id/id is filtered when parent_id is ignored."""
        header = ["id", "name", "parent_id/id"]
        data = [["1", "A", "p1"]]
        new_header, new_data = _filter_ignored_columns(["parent_id"], header, data)
        assert "parent_id/id" not in new_header
        assert new_header == ["id", "name"]


class TestExecuteWriteBatch:
    """Tests for the _execute_write_batch function."""

    def test_execute_write_batch_success(self) -> None:
        """Test successful batch write operation."""
        mock_model = MagicMock()
        thread_state = {"model": mock_model, "context": {"tracking_disable": True}}
        batch_writes = ([1, 2, 3], {"name": "Updated"})

        result = _execute_write_batch(thread_state, batch_writes, 1)

        assert result["success"] is True
        assert result["successful_writes"] == 3
        assert result["failed_writes"] == []
        mock_model.write.assert_called_once_with(
            [1, 2, 3], {"name": "Updated"}, context={"tracking_disable": True}
        )

    def test_execute_write_batch_failure(self) -> None:
        """Test batch write operation that fails."""
        mock_model = MagicMock()
        mock_model.write.side_effect = Exception("Access denied")
        thread_state = {"model": mock_model, "context": {}}
        batch_writes = ([1, 2], {"parent_id": 10})

        result = _execute_write_batch(thread_state, batch_writes, 1)

        assert result["success"] is False
        assert result["successful_writes"] == 0
        assert len(result["failed_writes"]) == 2
        assert result["failed_writes"][0][0] == 1
        assert result["failed_writes"][1][0] == 2
        assert "Access denied" in result["error_summary"]


class TestExecuteLoadBatchEdgeCases:
    """Additional edge case tests for _execute_load_batch."""

    def test_execute_load_batch_force_create_mode(self) -> None:
        """Test that force_create bypasses load and uses create directly."""
        mock_model = MagicMock()
        mock_record = MagicMock()
        mock_record.id = 42
        mock_model.create.return_value = mock_record
        mock_model.browse.return_value.env.ref.return_value = None

        mock_progress = MagicMock()
        thread_state = {
            "model": mock_model,
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

        # In force_create mode, load should NOT be called
        mock_model.load.assert_not_called()
        # create should be called via _create_batch_individually
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
            "[yellow]WARN:[/] Batch 1 hit scalable error. "
            "Reducing chunk size to 1 and retrying."
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


class TestCreateBatchIndividuallyEdgeCases:
    """Additional tests for _create_batch_individually edge cases."""

    def test_create_batch_individually_serialization_error(self) -> None:
        """Test handling of database serialization errors."""
        mock_model = MagicMock()
        mock_model.browse.return_value.env.ref.return_value = None
        mock_model.create.side_effect = Exception("could not serialize access")

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _create_batch_individually(
            mock_model, batch_lines, batch_header, 0, {}, []
        )

        # Serialization errors should not add to failed_lines (retryable)
        assert len(result["failed_lines"]) == 0

    def test_create_batch_individually_connection_pool_error(self) -> None:
        """Test handling of connection pool exhaustion errors."""
        mock_model = MagicMock()
        mock_model.browse.return_value.env.ref.return_value = None
        mock_model.create.side_effect = Exception("connection pool is full")

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _create_batch_individually(
            mock_model, batch_lines, batch_header, 0, {}, []
        )

        # Pool errors should add to failed_lines for retry
        assert len(result["failed_lines"]) == 1
        assert "connection pool exhaustion" in result["failed_lines"][0][-1]

    def test_create_batch_individually_odoo_server_error(self) -> None:
        """Test handling of Odoo server internal errors."""
        mock_model = MagicMock()
        mock_model.browse.return_value.env.ref.return_value = None
        mock_model.create.side_effect = Exception(
            "Odoo Server Error: tuple index out of range"
        )

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _create_batch_individually(
            mock_model, batch_lines, batch_header, 0, {}, []
        )

        # Server internal errors should be recorded
        assert len(result["failed_lines"]) == 1
        assert "Odoo server internal error" in result["failed_lines"][0][-1]

    def test_create_batch_individually_constraint_violation(self) -> None:
        """Test handling of database constraint violations."""
        mock_model = MagicMock()
        mock_model.browse.return_value.env.ref.return_value = None
        mock_model.create.side_effect = Exception(
            "check constraint 'nospaces' violated"
        )

        batch_header = ["id", "name"]
        batch_lines = [["rec1", "A"]]

        result = _create_batch_individually(
            mock_model, batch_lines, batch_header, 0, {}, []
        )

        assert len(result["failed_lines"]) == 1
        assert "constraint" in result["error_summary"].lower()


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
        source_file.write_text("id,name,age\nrec1,A,25\nrec2,B,30\nrec3,C,35\nrec4,D,40")

        batches = list(_stream_csv_batches(
            str(source_file), ",", "utf-8", skip=0, batch_size=2, ignore=[]
        ))

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

        batches = list(_stream_csv_batches(
            str(source_file), ",", "utf-8", skip=0, batch_size=10, ignore=["age"]
        ))

        assert len(batches) == 1
        header, _, data = batches[0]
        assert header == ["id", "name", "city"]
        assert "age" not in header
        assert len(data[0]) == 3

    def test_stream_csv_batches_with_skip(self, tmp_path: Path) -> None:
        """Test streaming with skipped rows."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name\nskip1,A\nskip2,B\nkeep1,C\nkeep2,D")

        batches = list(_stream_csv_batches(
            str(source_file), ",", "utf-8", skip=2, batch_size=10, ignore=[]
        ))

        assert len(batches) == 1
        _, _, data = batches[0]
        assert len(data) == 2
        assert data[0][0] == "keep1"

    def test_stream_csv_batches_missing_id_column(self, tmp_path: Path) -> None:
        """Test streaming fails without id column."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("name,age\nA,25\nB,30")

        with pytest.raises(ValueError, match="must contain an 'id' column"):
            list(_stream_csv_batches(
                str(source_file), ",", "utf-8", skip=0, batch_size=10, ignore=[]
            ))

    def test_stream_csv_batches_semicolon_separator(self, tmp_path: Path) -> None:
        """Test streaming with semicolon separator."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id;name;age\nrec1;A;25\nrec2;B;30")

        batches = list(_stream_csv_batches(
            str(source_file), ";", "utf-8", skip=0, batch_size=10, ignore=[]
        ))

        assert len(batches) == 1
        header, _, data = batches[0]
        assert header == ["id", "name", "age"]
        assert data[0] == ["rec1", "A", "25"]

    def test_stream_csv_batches_exact_batch_boundary(self, tmp_path: Path) -> None:
        """Test streaming when data aligns exactly with batch size."""
        source_file = tmp_path / "source.csv"
        source_file.write_text("id,name\nrec1,A\nrec2,B\nrec3,C\nrec4,D")

        batches = list(_stream_csv_batches(
            str(source_file), ",", "utf-8", skip=0, batch_size=2, ignore=[]
        ))

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
        mock_read_file.return_value = (["id", "name", "parent_id"], [["xml_a", "A", ""]])
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

        result, stats = import_data(
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
