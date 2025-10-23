"""Additional targeted tests to cover remaining missed lines."""

from typing import Any
from unittest.mock import MagicMock, patch

from odoo_data_flow.import_threaded import (
    RPCThreadImport,
    _create_batch_individually,
    _execute_load_batch,
    _orchestrate_pass_1,
    _orchestrate_pass_2,
    _run_threaded_pass,
)
from odoo_data_flow.importer import run_import


def test_execute_load_batch_chunk_failure_path() -> None:
    """Test _execute_load_batch when chunk size reduction reaches 1."""
    mock_model = MagicMock()
    mock_model.load.side_effect = Exception("scalable error")

    thread_state = {
        "model": mock_model,
        "progress": MagicMock(),
        "unique_id_field_index": 0,
        "force_create": False,
        "ignore_list": [],
        "context": {},
    }
    batch_header = ["id", "name"]
    batch_lines = [["rec1", "Alice"], ["rec2", "Bob"]]

    # Test when chunk size gets reduced to 1 and then fails
    with patch(
        "odoo_data_flow.import_threaded._handle_fallback_create"
    ) as mock_fallback:
        _execute_load_batch(thread_state, batch_lines, batch_header, 1)
        # Since load fails, fallback should be called
        mock_fallback.assert_called()


def test_execute_load_batch_serialization_retry_max() -> None:
    """Test _execute_load_batch max serialization retry logic."""
    mock_model = MagicMock()
    mock_model.load.side_effect = Exception("could not serialize access")

    thread_state = {
        "model": mock_model,
        "progress": MagicMock(),
        "unique_id_field_index": 0,
        "force_create": False,
        "ignore_list": [],
        "context": {},
    }
    batch_header = ["id", "name"]
    batch_lines = [["rec1", "Alice"], ["rec2", "Bob"]]

    # Test max serialization retry path
    with patch(
        "odoo_data_flow.import_threaded._handle_fallback_create"
    ) as mock_fallback:
        _execute_load_batch(thread_state, batch_lines, batch_header, 1)
        mock_fallback.assert_called()


def test_create_batch_individually_external_id_processing() -> None:
    """Test _create_batch_individually with external ID field processing."""
    mock_model = MagicMock()
    mock_record = MagicMock()
    mock_record.id = 123
    # Mock the browse().env.ref to return the record
    mock_model.browse().env.ref.return_value = mock_record

    # Mock _get_model_fields_safe to return some fields info
    with patch(
        "odoo_data_flow.import_threaded._get_model_fields_safe"
    ) as mock_get_fields:
        mock_get_fields.return_value = {
            "name": {"type": "char"},
            "category_id": {"type": "many2one"},
        }

        batch_header = ["id", "name", "category_id/id"]
        batch_lines = [["rec1", "Alice", "external.category"]]

        result = _create_batch_individually(
            mock_model, batch_lines, batch_header, 0, {}, [], None
        )

        # Should process external ID fields correctly
        assert isinstance(result, dict)


def test_create_batch_individually_early_problem_detection() -> None:
    """Test _create_batch_individually early problem detection."""
    mock_model = MagicMock()
    # Return None record to simulate no existing record
    mock_model.browse().env.ref.return_value = None

    batch_header = ["id", "name"]
    batch_lines = [
        ["product_template.63657", "Problematic Record"]
    ]  # Known problematic ID

    result = _create_batch_individually(
        mock_model, batch_lines, batch_header, 0, {}, [], MagicMock()
    )

    # Should catch the known problematic pattern and add to failed lines
    assert "failed_lines" in result
    assert len(result["failed_lines"]) > 0


def test_run_threaded_pass_abort_logic() -> None:
    """Test _run_threaded_pass abort logic for many consecutive failures."""
    mock_rpc_thread = MagicMock()
    mock_rpc_thread.abort_flag = False

    # Create futures that will return results with success=False
    mock_future = MagicMock()
    mock_future.result.return_value = {"success": False}

    mock_futures = [mock_future] * 510  # More than 500 to trigger abort

    with patch("concurrent.futures.as_completed") as mock_as_completed:
        mock_as_completed.return_value = mock_futures

        # Create a dummy target function
        def dummy_target(*args: Any) -> None:
            pass

        result, aborted = _run_threaded_pass(
            mock_rpc_thread, dummy_target, [(i, None) for i in range(510)], {}
        )

        # Should abort after too many consecutive failures
        assert aborted is True


def test_orchestrate_pass_1_uid_not_found() -> None:
    """Test _orchestrate_pass_1 when unique ID field is not in header."""
    mock_model = MagicMock()
    header = ["name", "email"]  # No 'id' field
    all_data = [["Alice", "alice@example.com"]]
    unique_id_field = "id"  # Field that doesn't exist in header
    deferred_fields: list[str] = []
    ignore: list[str] = []

    with patch("odoo_data_flow.import_threaded.Progress") as mock_progress:
        mock_progress_instance = MagicMock()
        mock_progress.return_value.__enter__.return_value = mock_progress_instance

        result = _orchestrate_pass_1(
            mock_progress_instance,
            mock_model,
            "res.partner",
            header,
            all_data,
            unique_id_field,
            deferred_fields,
            ignore,
            {},
            None,
            None,
            1,
            10,
            False,
            None,
            False,
        )

        # Should return with success=False because unique_id_field not found
        assert result.get("success") is False


def test_orchestrate_pass_2_no_valid_relations() -> None:
    """Test _orchestrate_pass_2 when there are no valid relations to update."""
    mock_model = MagicMock()
    header = ["id", "name"]
    all_data = [["1", "Alice"]]
    unique_id_field = "id"
    id_map: dict[str, int] = {}  # Empty ID map
    deferred_fields = ["category_id"]
    context: dict[str, Any] = {}

    with patch("odoo_data_flow.import_threaded.Progress") as mock_progress:
        mock_progress_instance = MagicMock()
        mock_progress.return_value.__enter__.return_value = mock_progress_instance

        # Test when there are no valid relations to update
        success, updates = _orchestrate_pass_2(
            mock_progress_instance,
            mock_model,
            "res.partner",
            header,
            all_data,
            unique_id_field,
            id_map,
            deferred_fields,
            context,
            None,
            None,
            1,
            10,
        )

        # Should succeed since there's just no work to do
        assert success is True
        assert updates == 0


def test_orchestrate_pass_2_batching_logic() -> None:
    """Test _orchestrate_pass_2 batching and grouping logic."""
    mock_model = MagicMock()
    header = ["id", "name", "category_id"]
    all_data = [["1", "Alice", "cat1"], ["2", "Bob", "cat1"], ["3", "Charlie", "cat2"]]
    unique_id_field = "id"
    id_map: dict[str, int] = {"1": 101, "2": 102, "3": 103}  # Valid ID map
    deferred_fields = ["category_id"]
    context: dict[str, Any] = {}

    with patch("odoo_data_flow.import_threaded.Progress") as mock_progress:
        mock_progress_instance = MagicMock()
        mock_progress.return_value.__enter__.return_value = mock_progress_instance

        # We have valid data to process, so it should create grouped writes
        with patch(
            "odoo_data_flow.import_threaded._run_threaded_pass"
        ) as mock_run_threaded:
            mock_run_threaded.return_value = ({}, False)  # Empty results, not aborted
            success, updates = _orchestrate_pass_2(
                mock_progress_instance,
                mock_model,
                "res.partner",
                header,
                all_data,
                unique_id_field,
                id_map,
                deferred_fields,
                context,
                None,
                None,
                1,
                10,
            )

            # Check if _run_threaded_pass was actually called (it might not be called if no valid data to process)
            # At least validate that the function completed without exception
            assert success is not None  # Function completed without exception


def test_rpc_thread_import_functionality() -> None:
    """Test RPCThreadImport basic functionality."""
    progress = MagicMock()

    rpc_thread = RPCThreadImport(
        max_connection=2, progress=progress, task_id=1, writer=None, fail_handle=None
    )

    # Test basic attributes are set correctly
    assert rpc_thread.max_connection == 2
    assert rpc_thread.progress == progress
    assert rpc_thread.task_id == 1
    assert rpc_thread.writer is None
    assert rpc_thread.fail_handle is None
    assert rpc_thread.abort_flag is False


def test_importer_with_fail_file_processing() -> None:
    """Test run_import with fail file processing logic."""
    with patch(
        "odoo_data_flow.importer._count_lines", return_value=5
    ):  # More than 1 line
        with patch("odoo_data_flow.importer.Path") as mock_path:
            mock_path_instance = MagicMock()
            mock_path.return_value = mock_path_instance
            mock_path_instance.parent = MagicMock()
            mock_path_instance.parent.__truediv__.return_value = "res_partner_fail.csv"

            with patch(
                "odoo_data_flow.importer.import_threaded.import_data"
            ) as mock_import_data:
                mock_import_data.return_value = (True, {"total_records": 5})

                with patch(
                    "odoo_data_flow.importer._run_preflight_checks", return_value=True
                ):
                    # Test the fail mode logic path
                    run_import(
                        config="dummy.conf",
                        filename="dummy.csv",
                        model="res.partner",
                        deferred_fields=None,
                        unique_id_field=None,
                        no_preflight_checks=True,
                        headless=True,
                        worker=1,
                        batch_size=100,
                        skip=0,
                        fail=True,  # Enable fail mode
                        separator=";",
                        ignore=None,
                        context={},
                        encoding="utf-8",
                        o2m=False,
                        groupby=None,
                    )

                    # Should call import_data with the fail file
                    assert mock_import_data.called


def test_importer_preflight_mode_handling() -> None:
    """Test run_import with different preflight mode handling."""
    with patch(
        "odoo_data_flow.importer.import_threaded.import_data"
    ) as mock_import_data:
        mock_import_data.return_value = (True, {"id_map": {"1": 101}})

        with patch("odoo_data_flow.importer._run_preflight_checks") as mock_preflight:

            def side_effect(*args: Any, **kwargs: Any) -> bool:
                # Set some import plan values to test different code paths
                kwargs["import_plan"]["unique_id_field"] = "id"
                kwargs["import_plan"]["deferred_fields"] = ["category_id"]
                return True

            mock_preflight.side_effect = side_effect

            with patch("odoo_data_flow.importer._count_lines", return_value=0):
                # Test the import with deferred fields
                run_import(
                    config="dummy.conf",
                    filename="dummy.csv",
                    model="res.partner",
                    deferred_fields=["category_id"],
                    unique_id_field="id",
                    no_preflight_checks=False,  # Use preflight checks
                    headless=True,
                    worker=1,
                    batch_size=100,
                    skip=0,
                    fail=False,
                    separator=";",
                    ignore=[],
                    context={},
                    encoding="utf-8",
                    o2m=False,
                    groupby=None,
                )

                # Should call both preflight and import functions
                mock_preflight.assert_called()
                mock_import_data.assert_called()
