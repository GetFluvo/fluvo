"""Import thread.

This module contains the low-level, multi-threaded logic for importing
data into an Odoo instance.
"""

import concurrent.futures
import csv
import sys
from collections.abc import Generator, Iterable
from typing import Any, Optional, TextIO

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
)

from .lib import conf_lib
from .lib.internal.rpc_thread import RpcThread
from .lib.internal.tools import batch
from .logging_config import log

# --- Set a large CSV field size limit safely ---
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**30)


# --- Helper Functions ---
def _read_data_file(
    file_path: str, separator: str, encoding: str, skip: int
) -> tuple[list[str], list[list[Any]]]:
    """Reads a CSV file and returns its header and data.

    This function handles opening and parsing a CSV file, skipping any
    initial lines as specified. It validates that an 'id' column exists,
    which is required for all import operations. It also handles common
    file I/O errors like FileNotFoundError.

    Args:
        file_path (str): The full path to the source CSV file.
        separator (str): The delimiter character used to separate columns.
        encoding (str): The character encoding of the file.
        skip (int): The number of lines to skip at the top of the file before
            reading the header.

    Returns:
        tuple[list[str], list[list[Any]]]: A tuple containing the header
        (as a list of strings) and the data (as a list of lists). Returns
        an empty tuple `([], [])` if the file cannot be read.

    Raises:
        ValueError: If the source file does not contain a required 'id' column.
    """
    try:
        with open(file_path, encoding=encoding, newline="") as f:
            reader = csv.reader(f, delimiter=separator)
            header = next(reader)
            if "id" not in header:
                raise ValueError("Source file must contain an 'id' column.")
            for _ in range(skip):
                next(reader)
            return header, list(reader)
    except FileNotFoundError:
        log.error(f"Source file not found: {file_path}")
        return [], []
    except Exception as e:
        log.error(f"Failed to read file {file_path}: {e}")
        return [], []


def _filter_ignored_columns(
    ignore: list[str], header: list[str], data: list[list[Any]]
) -> tuple[list[str], list[list[Any]]]:
    """Removes ignored columns from header and data.

    This function filters a dataset by removing columns specified in the
    `ignore` list. It identifies the indices of columns to keep and rebuilds
    the header and each data row accordingly. If the `ignore` list is empty,
    it returns the original data and header without modification.

    Args:
        ignore (list[str]): A list of column header names to remove.
        header (list[str]): The original list of header columns.
        data (list[list[Any]]): The original data as a list of rows.

    Returns:
        tuple[list[str], list[list[Any]]]: A tuple containing two elements:
        the new header and the new data, both with the specified columns
        removed.
    """
    if not ignore:
        return header, data
    indices_to_keep = [i for i, h in enumerate(header) if h not in ignore]
    new_header = [header[i] for i in indices_to_keep]
    new_data = [[row[i] for i in indices_to_keep] for row in data]
    return new_header, new_data


def _create_batches(
    data: list[list[Any]], batch_size: int
) -> Generator[tuple[int, list[list[Any]]], None, None]:
    """A generator that yields batches of data by size."""
    for i, data_batch in enumerate(batch(data, batch_size), 1):
        yield i, list(data_batch)


def _setup_fail_file(
    fail_file: Optional[str], header: list[str], separator: str, encoding: str
) -> tuple[Optional[Any], Optional[TextIO]]:
    """Opens the fail file and returns the writer and file handle.

    This helper function prepares a CSV file for logging failed import
    records. It creates a `csv.writer` and writes the initial header row,
    appending an `_ERROR_REASON` column to capture failure details.

    Args:
        fail_file (Optional[str]): The full path for the output fail file. If
            None, the function returns immediately.
        header (list[str]): The header list from the source data file.
        separator (str): The delimiter to use for the output CSV file.
        encoding (str): The text encoding to use for the output file.

    Returns:
        tuple[Optional[Any], Optional[TextIO]]: A tuple containing the
        `csv.writer` object and the open file handle. Returns `(None, None)`
        if `fail_file` was not provided or if an `OSError` occurred.
    """
    if not fail_file:
        return None, None
    try:
        fail_handle = open(fail_file, "w", newline="", encoding=encoding)
        fail_writer = csv.writer(
            fail_handle, delimiter=separator, quoting=csv.QUOTE_ALL
        )
        header_to_write = list(header)
        header_to_write.append("_ERROR_REASON")
        fail_writer.writerow(header_to_write)
        return fail_writer, fail_handle
    except OSError as e:
        log.error(f"Could not open fail file for writing: {fail_file}. Error: {e}")
        return None, None


class RPCThreadImport(RpcThread):
    """A specialized RpcThread for handling data import and write tasks."""

    def __init__(
        self,
        max_connection: int,
        progress: Progress,
        task_id: TaskID,
        writer: Optional[Any] = None,
    ) -> None:
        super().__init__(max_connection)
        self.progress = progress
        self.task_id = task_id
        self.writer = writer
        self.abort_flag = False


def _execute_load_batch(
    thread_state: dict[str, Any],
    batch_lines: list[list[Any]],
    batch_header: list[str],
    batch_number: int,
) -> dict[str, Any]:
    """Executes a batch import with a `load` to `create` fallback.

    This is the core worker function for Pass 1. It first attempts a fast,
    bulk import using the `load` method. If the entire batch fails for any
    reason (e.g., a single bad record causes a rollback), it automatically
    falls back to a slower, record-by-record `create` loop. This strategy
    combines the speed of `load` for valid batches with the detailed error
    reporting of `create` for problematic ones.

    Args:
        thread_state (dict[str, Any]): Shared state from the orchestrator,
            containing the Odoo model object, context, and unique ID index.
        batch_lines (list[list[Any]]): The list of data rows for this batch.
        batch_header (list[str]): The list of header columns for this batch.
        batch_number (int): The identifier for this batch, used for logging.

    Returns:
        dict[str, Any]: A dictionary containing the results of the batch,
        with two keys: `id_map` (a dict of `{source_id: db_id}` for
        successful records) and `failed_lines` (a list of rows for
        records that failed the `create` fallback).
    """
    model = thread_state["model"]
    context = thread_state["context"]
    unique_id_field_index = thread_state["unique_id_field_index"]
    id_map: dict[str, int] = {}
    failed_lines: list[list[Any]] = []
    try:
        log.debug(f"Attempting `load` for batch {batch_number}...")
        res = model.load(batch_header, batch_lines, context=context)
        if res.get("messages"):
            raise ValueError("Batch failed with messages, falling back to create.")
        created_ids = res.get("ids", [])
        if len(created_ids) != len(batch_lines):
            raise ValueError("Record count mismatch, falling back to create.")
        for i, line in enumerate(batch_lines):
            source_id = line[unique_id_field_index]
            id_map[source_id] = created_ids[i]
    except Exception as e:
        log.warning(
            f"Batch {batch_number} failed with `load` ('{e}'). "
            "Falling back to slower, single-record `create` method for this batch."
        )
        for line in batch_lines:
            source_id = line[unique_id_field_index]
            vals = dict(zip(batch_header, line))
            try:
                new_record = model.create(vals)
                id_map[source_id] = new_record.id
            except Exception as create_error:
                error_message = str(create_error).replace("\n", " | ")
                failed_line = list(line)
                failed_line.append(error_message)
                failed_lines.append(failed_line)
    return {"id_map": id_map, "failed_lines": failed_lines}


def _execute_write_batch(
    thread_state: dict[str, Any],
    batch_writes: list[tuple[int, dict[str, Any]]],
    batch_number: int,
) -> dict[str, Any]:
    """Executes a batch of write operations.

    This is the core worker function for Pass 2 of a deferred import. It
    iterates through a list of pre-prepared write payloads and calls the
    standard Odoo `write` method for each record. Any exceptions during
    the write are caught and collected for reporting.

    Args:
        thread_state (dict[str, Any]): Shared state from the orchestrator,
            containing the Odoo model object.
        batch_writes (list[tuple[int, dict[str, Any]]]): A list of write
            payloads for this batch. Each item is a tuple of
            `(database_id, {values_to_write})`.
        batch_number (int): The identifier for this batch, used for logging.

    Returns:
        dict[str, Any]: A dictionary containing the results of the batch,
        with a `failed_writes` key. The value is a list of tuples for
        each failed write, including the original data and the error message.
    """
    model = thread_state["model"]
    failed_writes = []
    log.debug(f"Executing write batch {batch_number}...")
    for db_id, vals in batch_writes:
        try:
            model.browse(db_id).write(vals)
        except Exception as e:
            error_message = str(e).replace("\n", " | ")
            failed_writes.append((db_id, vals, error_message))
    return {"failed_writes": failed_writes}


def _run_threaded_pass(
    rpc_thread: RPCThreadImport,
    target_func: Any,
    batches: Iterable[tuple[int, list[Any]]],
    thread_state: dict[str, Any],
) -> dict[str, Any]:
    """Orchestrates a multi-threaded pass and aggregates results.

    This is a generic function that manages a multi-threaded operation,
    used for both Pass 1 (load/create) and Pass 2 (write). It spawns worker
    threads for each batch of data and then collects and aggregates the
    results as they are completed, updating the progress bar in real-time.

    Args:
        rpc_thread (RPCThreadImport): The thread manager instance that controls
            the thread pool and progress bar.
        target_func (Any): The worker function to be executed in each thread
            (e.g., `_execute_load_batch`).
        batches (Iterable[tuple[int, list[Any]]]): An iterable that yields
            batches of data, where each item is a tuple of `(batch_number,
            batch_data)`.
        thread_state (dict[str, Any]): A dictionary of shared state to be
            passed to each worker function.

    Returns:
        dict[str, Any]: A dictionary containing the aggregated results from all
        worker threads, such as `id_map` and `failed_lines`.
    """
    future_to_batch = {}
    for batch_number, batch_data in batches:
        if rpc_thread.abort_flag:
            break
        future = rpc_thread.spawn_thread(
            target_func,
            [
                thread_state,
                batch_data,
                thread_state.get("batch_header"),
                batch_number,
            ],
        )
        future_to_batch[future] = batch_data

    aggregated_results: dict[str, Any] = {
        "id_map": {},
        "failed_lines": [],
        "failed_writes": [],
    }
    for future in concurrent.futures.as_completed(future_to_batch):
        if rpc_thread.abort_flag:
            break
        try:
            result = future.result()
            # Aggregate all results from the worker
            aggregated_results["id_map"].update(result.get("id_map", {}))
            aggregated_results["failed_lines"].extend(result.get("failed_lines", []))
            aggregated_results["failed_writes"].extend(result.get("failed_writes", []))

            # FIX: Write failures immediately as they are processed
            failed_lines_from_batch = result.get("failed_lines", [])
            if rpc_thread.writer and failed_lines_from_batch:
                log.debug(
                    f"Writing {len(failed_lines_from_batch)} failed lines to fail file."
                )
                rpc_thread.writer.writerows(failed_lines_from_batch)

            # Update progress bar based on the size of the original batch
            original_batch_size = len(future_to_batch[future])
            rpc_thread.progress.update(rpc_thread.task_id, advance=original_batch_size)
        except Exception as e:
            log.error(f"A worker thread failed unexpectedly: {e}", exc_info=True)
            rpc_thread.abort_flag = True

    rpc_thread.executor.shutdown(wait=True)
    return aggregated_results


def _orchestrate_pass_1(
    progress: Progress,
    model_obj: Any,
    header: list[str],
    all_data: list[list[Any]],
    unique_id_field: str,
    deferred_fields: list[str],
    ignore: list[str],
    context: dict[str, Any],
    fail_writer: Optional[Any],
    max_connection: int,
    batch_size: int,
) -> dict[str, Any]:
    """Orchestrates the multi-threaded Pass 1 (load/create).

    This function manages the first pass of the import process. It prepares
    the data by filtering out ignored and deferred fields, then executes the
    import in parallel using the `load` method with a `create` fallback.
    It is responsible for building the crucial ID map needed for Pass 2.

    Args:
        progress (Progress): The rich Progress instance for updating the UI.
        model_obj (Any): The connected Odoo model object used for RPC calls.
        header (list[str]): The complete header from the source CSV file.
        all_data (list[list[Any]]): The complete data from the source CSV.
        unique_id_field (str): The name of the column containing the unique
            source ID for each record.
        deferred_fields (list[str]): A list of relational fields to ignore in
            this pass.
        ignore (list[str]): A list of additional fields to ignore, specified
            by the user.
        context (dict[str, Any]): The context dictionary for the Odoo RPC call.
        fail_writer (Optional[Any]): The CSV writer object for recording failures.
        max_connection (int): The number of parallel worker threads to use.
        batch_size (int): The number of records to process in each batch.

    Returns:
        dict[str, Any]: A dictionary containing the results of the pass,
            including the `id_map` ({source_id: db_id}), a list of any
            `failed_lines`, and a `success` boolean flag.
    """
    pass_1_header, pass_1_data = _filter_ignored_columns(
        deferred_fields + ignore, header, all_data
    )
    pass_1_batches = list(_create_batches(pass_1_data, batch_size))

    task_description = f"Pass 1/2: Importing to [bold]{model_obj._name}[/bold]"
    pass_1_task = progress.add_task(
        task_description, total=len(pass_1_data), last_error=""
    )

    rpc_pass_1 = RPCThreadImport(max_connection, progress, pass_1_task, fail_writer)

    try:
        pass_1_uid_index = pass_1_header.index(unique_id_field)
    except ValueError:
        log.error(
            f"Unique ID field '{unique_id_field}' was removed by the ignore list."
        )
        return {"success": False}

    thread_state_1 = {
        "model": model_obj,
        "context": context,
        "unique_id_field_index": pass_1_uid_index,
        "batch_header": pass_1_header,
    }

    results = _run_threaded_pass(
        rpc_pass_1, _execute_load_batch, pass_1_batches, thread_state_1
    )
    results["success"] = not rpc_pass_1.abort_flag
    return results


def _prepare_pass_2_data(
    all_data: list[list[Any]],
    header: list[str],
    unique_id_field_index: int,
    id_map: dict[str, int],
    deferred_fields: list[str],
) -> list[tuple[int, dict[str, Any]]]:
    """Prepares the list of write operations for Pass 2.

    Iterates through the original data, using the ID map from Pass 1 to
    resolve relational fields and construct a list of payloads suitable for
    Odoo's `write` method.

    Args:
        all_data (list[list[Any]]): The complete data from the source file.
        header (list[str]): The complete header from the source file.
        unique_id_field_index (int): The column index of the unique identifier.
        id_map (dict[str, int]): The map of source IDs to database IDs from Pass 1.
        deferred_fields (list[str]): A list of the relational fields to process.

    Returns:
        list[tuple[int, dict[str, Any]]]: A list of write payloads, where each
        item is a tuple containing the database ID of the record to update
        and a dictionary of the values to write.
    """
    pass_2_data_to_write = []
    for row in all_data:
        source_id = row[unique_id_field_index]
        db_id = id_map.get(source_id)
        if not db_id:
            continue

        update_vals = {}
        for field in deferred_fields:
            if field in header:
                field_index = header.index(field)
                related_source_id = row[field_index]
                related_db_id = id_map.get(related_source_id)
                if related_source_id and related_db_id:
                    update_vals[field] = related_db_id
        if update_vals:
            pass_2_data_to_write.append((db_id, update_vals))
    return pass_2_data_to_write


def _orchestrate_pass_2(
    progress: Progress,
    model_obj: Any,
    header: list[str],
    all_data: list[list[Any]],
    unique_id_field: str,
    id_map: dict[str, int],
    deferred_fields: list[str],
    fail_writer: Optional[Any],
    max_connection: int,
    batch_size: int,
) -> bool:
    """Orchestrates the multi-threaded Pass 2 (write).

    This function manages the second pass of a deferred import. It prepares
    the data for updating relational fields by using the ID map from Pass 1.
    It then runs the `write` operations in parallel and handles any
    failures by reconstructing the original source records and logging them
    to the fail file.

    Args:
        progress (Progress): The rich Progress instance for updating the UI.
        model_obj (Any): The connected Odoo model object.
        header (list[str]): The header list from the original source file.
        all_data (list[list[Any]]): The full data from the original source file.
        unique_id_field (str): The name of the unique identifier column.
        id_map (dict[str, int]): The map of source IDs to database IDs from Pass 1.
        deferred_fields (list[str]): The list of fields to update in this pass.
        fail_writer (Optional[Any]): The CSV writer for the fail file.
        max_connection (int): The number of parallel worker threads to use.
        batch_size (int): The number of records per write batch.

    Returns:
        bool: True if the pass completed without any critical (abort-level)
        errors, False otherwise.
    """
    unique_id_field_index = header.index(unique_id_field)
    pass_2_data_to_write = _prepare_pass_2_data(
        all_data, header, unique_id_field_index, id_map, deferred_fields
    )

    if not pass_2_data_to_write:
        log.info("No valid relations found to update in Pass 2. Import complete.")
        return True

    pass_2_batches = list(enumerate(batch(pass_2_data_to_write, batch_size), 1))

    task_description = f"Pass 2/2: Updating [bold]{model_obj._name}[/bold] relations"
    pass_2_task = progress.add_task(
        task_description, total=len(pass_2_batches), last_error=""
    )
    rpc_pass_2 = RPCThreadImport(max_connection, progress, pass_2_task, fail_writer)
    thread_state_2 = {"model": model_obj}

    pass_2_results = _run_threaded_pass(
        rpc_pass_2, _execute_write_batch, pass_2_batches, thread_state_2
    )

    failed_writes = pass_2_results["failed_writes"]
    if fail_writer and failed_writes:
        log.warning("Writing failed Pass 2 records to fail file...")
        reverse_id_map = {v: k for k, v in id_map.items()}
        source_data_map = {row[unique_id_field_index]: row for row in all_data}
        failed_lines = []
        for db_id, _, error_message in failed_writes:
            source_id = reverse_id_map.get(db_id)
            if source_id and source_id in source_data_map:
                original_row = list(source_data_map[source_id])
                original_row.append(error_message)
                failed_lines.append(original_row)
        fail_writer.writerows(failed_lines)

    return not rpc_pass_2.abort_flag


def import_data(
    config_file: str,
    model: str,
    unique_id_field: str,
    file_csv: str,
    deferred_fields: Optional[list[str]] = None,
    context: Optional[dict[str, Any]] = None,
    fail_file: Optional[str] = None,
    encoding: str = "utf-8",
    separator: str = ";",
    ignore: Optional[list[str]] = None,
    max_connection: int = 1,
    batch_size: int = 10,
    skip: int = 0,
) -> bool:
    """Orchestrates a robust, multi-threaded, two-pass import process.

    This is the main entry point for the low-level import engine. It manages
    the entire workflow, including reading the source file, connecting to
    Odoo, and coordinating the import passes.

    The import is performed in one or two passes:
    - Pass 1: Creates base records using a multi-threaded `load` method with
      a `create` fallback for robustness. It builds a map of source IDs to
      new database IDs.
    - Pass 2: If `deferred_fields` are provided, it performs a second
      multi-threaded pass to `write` the relational data.

    Args:
        config_file (str): Path to the Odoo connection configuration file.
        model (str): The technical name of the target Odoo model.
        unique_id_field (str): The column name in the source file that
            uniquely identifies each record.
        file_csv (str): The full path to the source CSV data file.
        deferred_fields (Optional[list[str]]): A list of relational fields to
            process in a second pass. If None or empty, a single-pass
            import is performed.
        context (Optional[dict[str, Any]]): A context dictionary for Odoo
            RPC calls.
        fail_file (Optional[str]): Path to write any failed records to.
        encoding (str): The character encoding of the source file.
        separator (str): The delimiter character used in the source CSV.
        ignore (Optional[list[str]]): A list of columns to completely ignore
            from the source file.
        max_connection (int): The number of parallel threads to use.
        batch_size (int): The number of records to process in each batch.
        skip (int): The number of lines to skip at the top of the source file.

    Returns:
        bool: True if the entire import process completed without any
        critical, process-halting errors, False otherwise.
    """
    _context, _deferred, _ignore = (
        context or {},
        deferred_fields or [],
        ignore or [],
    )
    header, all_data = _read_data_file(file_csv, separator, encoding, skip)
    if not header:
        return False

    try:
        connection = conf_lib.get_connection_from_config(config_file)
        model_obj = connection.get_model(model)
    except Exception as e:
        log.error(f"Setup failed: {e}")
        return False

    fail_writer, fail_handle = _setup_fail_file(fail_file, header, separator, encoding)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        "•",
        TextColumn("[green]{task.completed} of {task.total} records"),
    )

    overall_success = False
    try:
        pass_1_results = _orchestrate_pass_1(
            progress,
            model_obj,
            header,
            all_data,
            unique_id_field,
            _deferred,
            _ignore,
            _context,
            fail_writer,
            max_connection,
            batch_size,
        )
        if not pass_1_results.get("success"):
            return False

        if not _deferred:
            return True  # Successful single-pass import

        overall_success = _orchestrate_pass_2(
            progress,
            model_obj,
            header,
            all_data,
            unique_id_field,
            pass_1_results["id_map"],
            _deferred,
            fail_writer,
            max_connection,
            batch_size,
        )
    finally:
        if fail_handle:
            fail_handle.close()

    return overall_success
