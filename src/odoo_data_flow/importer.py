"""Main importer module.

This module contains the high-level logic for orchestrating the import process.
It handles file I/O, pre-flight checks, and the delegation of the core
import tasks to the multi-threaded `import_threaded` module.
"""

import ast
import csv
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel

from . import import_threaded
from .enums import PreflightMode
from .lib import preflight
from .lib.internal.ui import _show_error_panel
from .logging_config import log


def _count_lines(filepath: str) -> int:
    """Counts the number of lines in a file, returning 0 if it doesn't exist."""
    try:
        # This method streams the file without loading it all into memory.
        with open(filepath) as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def _get_fail_filename(model: str, is_fail_run: bool) -> str:
    """Generates a standardized filename for failed records.

    Args:
        model (str): The Odoo model name being imported.
        is_fail_run (bool): If True, indicates a recovery run, and a
            timestamp will be added to the filename.

    Returns:
        str: The generated filename for the fail file.
    """
    model_filename = model.replace(".", "_")
    if is_fail_run:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{model_filename}_{timestamp}_failed.csv"
    return f"{model_filename}_fail.csv"


def _run_preflight_checks(
    preflight_mode: PreflightMode, import_plan: dict[str, Any], **kwargs: Any
) -> bool:
    """Iterates through and runs all registered pre-flight checks.

    Args:
        preflight_mode (PreflightMode): The current mode (NORMAL or FAIL_MODE).
        import_plan (dict[str, Any]): A dictionary that checks can populate
            with strategy details (e.g., detected deferred fields).
        **kwargs (Any): A dictionary of arguments to pass to each check.

    Returns:
        bool: True if all checks pass, False otherwise.
    """
    for check_func in preflight.PREFLIGHT_CHECKS:
        if not check_func(
            preflight_mode=preflight_mode, import_plan=import_plan, **kwargs
        ):
            return False
    return True


def _orchestrate_import(
    config: str,
    filename: str,
    model: str,
    deferred_fields: Optional[str],
    unique_id_field: Optional[str],
    no_preflight_checks: bool,
    headless: bool,
    worker: int,
    batch_size: int,
    skip: int,
    fail: bool,
    separator: str,
    ignore: Optional[str],
    context: str,
    encoding: str,
    o2m: bool,
    split_by_cols: Optional[list[str]],
) -> None:
    """Orchestrates the main import workflow, including pre-flight and routing.

    This function contains the core logic for running pre-flight checks and then
    deciding whether to run a standard single-pass import or a two-pass
    deferred import based on user input and automatic detection.

    Args:
        config (str): Path to the connection configuration file.
        filename (str): Path to the source CSV file to import.
        model (str): The target Odoo model.
        deferred_fields (Optional[str]): A comma-separated string of fields to defer.
        unique_id_field (Optional[str]): The name of the unique ID column.
        no_preflight_checks (bool): If True, skips all pre-flight checks.
        headless (bool): If True, runs in non-interactive mode.
        worker (int): The number of simultaneous connections to use.
        batch_size (int): The number of records per batch.
        skip (int): The number of initial lines to skip.
        fail (bool): If True, runs in fail mode.
        separator (str): The delimiter used in the CSV file.
        ignore (Optional[str]): A comma-separated string of columns to ignore.
        context (str): A string representation of the Odoo context dictionary.
        encoding (str): The file encoding of the source file.
        o2m (bool): If True, enables special handling for one-to-many files.
        split_by_cols (Optional[list[str]]): A list of column names to group records by.
    """
    file_to_process = filename
    if fail:
        fail_path = Path(filename).parent / _get_fail_filename(model, False)

        # Performant Check: Count lines only once and store the result.
        line_count = _count_lines(str(fail_path))

        # A file with 1 line (the header) or less has no data records to process.
        if line_count <= 1:
            Console().print(
                Panel(
                    f"No records to retry in '{fail_path}'.",
                    title="[bold green]No Recovery Needed[/bold green]",
                )
            )
            return

        # New Feature: Log the number of records being recovered.
        record_count = line_count - 1
        log.info(
            f"Running in --fail mode. Attempting to recover {record_count} "
            f"records from: {fail_path}"
        )
        file_to_process = str(fail_path)

    import_plan: dict[str, Any] = {}
    if not no_preflight_checks:
        if not _run_preflight_checks(
            preflight_mode=PreflightMode.FAIL_MODE if fail else PreflightMode.NORMAL,
            import_plan=import_plan,
            model=model,
            filename=file_to_process,
            config=config,
            headless=headless,
            separator=separator,
            unique_id_field=unique_id_field,
        ):
            return

    final_deferred = (
        deferred_fields.split(",")
        if deferred_fields
        else import_plan.get("deferred_fields", [])
    )
    final_uid_field = unique_id_field or import_plan.get("unique_id_field") or "id"

    if final_deferred and not fail:
        log.info(f"Using two-pass strategy for deferred fields: {final_deferred}")
        run_import_deferred(
            config=config,
            filename=file_to_process,
            model_name=model,
            unique_id_field=final_uid_field,
            deferred_fields=final_deferred,
            encoding=encoding,
            separator=separator,
        )
        return

    log.info("Using standard single-pass import strategy.")
    try:
        parsed_context = ast.literal_eval(context)
        if not isinstance(parsed_context, dict):
            raise TypeError("Context must be a dictionary.")
    except Exception as e:
        _show_error_panel("Invalid Context", f"Invalid --context dictionary: {e}")
        return

    ignore_list = ignore.split(",") if ignore else []

    if fail:
        max_conn, batch_size_run, force_create = 1, 1, True
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fail_output_file = str(
            Path(filename).parent / f"{model.replace('.', '_')}_{timestamp}_failed.csv"
        )
    else:
        max_conn, batch_size_run, force_create = (
            int(worker),
            int(batch_size),
            False,
        )
        fail_output_file = str(Path(filename).parent / _get_fail_filename(model, False))

    success = import_threaded.import_data(
        config_file=config,
        model=model,
        unique_id_field=(unique_id_field or "id"),
        file_csv=file_to_process,
        context=parsed_context,
        fail_file=fail_output_file,
        encoding=encoding,
        separator=separator,
        ignore=ignore_list,
        max_connection=max_conn,
        batch_size=batch_size_run,
        skip=int(skip),
        force_create=force_create,
        o2m=o2m,
        split_by_cols=split_by_cols,
    )

    console = Console()
    if success:
        console.print(
            Panel(
                f"Import for [cyan]{model}[/cyan] finished successfully.",
                title="[bold green]Import Complete[/bold green]",
            )
        )
    else:
        _show_error_panel(
            "Import Failed",
            "The import process failed or was aborted. Check logs for details.",
        )


def run_import(
    config: str,
    filename: str,
    model: Optional[str] = None,
    deferred_fields: Optional[str] = None,
    unique_id_field: Optional[str] = None,
    no_preflight_checks: bool = False,
    headless: bool = False,
    worker: int = 1,
    batch_size: int = 10,
    skip: int = 0,
    fail: bool = False,
    separator: str = ";",
    ignore: Optional[str] = None,
    context: str = "{'tracking_disable' : True}",
    encoding: str = "utf-8",
    o2m: bool = False,
    groupby: Optional[str] = None,  # Only one argument for grouping
) -> None:
    """Main entry point for the import command.

    This function is a wrapper that handles initial setup (like model name
    inference) and then delegates the core logic to the `_orchestrate_import`
    helper function.

    Args:
        config (str): Path to the connection configuration file.
        filename (str): Path to the source CSV file to import.
        model (Optional[str]): The target Odoo model. Inferred from filename if not set.
        deferred_fields (Optional[str]): A comma-separated string of fields to defer.
        unique_id_field (Optional[str]): The name of the unique ID column.
        no_preflight_checks (bool): If True, skips all pre-flight checks.
        headless (bool): If True, runs in non-interactive mode.
        worker (int): The number of simultaneous connections to use.
        batch_size (int): The number of records per batch.
        skip (int): The number of initial lines to skip.
        fail (bool): If True, runs in fail mode.
        separator (str): The delimiter used in the CSV file.
        ignore (Optional[str]): A comma-separated string of columns to ignore.
        context (str): A string representation of the Odoo context dictionary.
        encoding (str): The file encoding of the source file.
        o2m (bool): If True, enables special handling for one-to-many file
            formats where child records follow their parent on subsequent lines.
        groupby (Optional[str]): A comma-separated string of columns to
            group records by.
    """
    log.info("Starting data import process from file...")

    final_model = model
    # FIX: Restore the validation logic for inferred model names.
    if not final_model:
        base_name = os.path.basename(filename)
        inferred_model = Path(base_name).stem.replace("_", ".")
        if not inferred_model or inferred_model.startswith("."):
            _show_error_panel(
                "Model Not Found",
                f"Could not infer model from filename '{base_name}'. "
                "Please use the --model option.",
            )
            return  # Exit early as intended
        final_model = inferred_model
        log.info(f"No model provided. Inferred model '{final_model}' from filename.")

    if fail and deferred_fields:
        _show_error_panel(
            "Invalid Arguments", "Cannot use --fail with --deferred-fields."
        )
        return

    split_by_cols_list = [c.strip() for c in groupby.split(",")] if groupby else None
    _orchestrate_import(
        config=config,
        filename=filename,
        model=final_model,
        deferred_fields=deferred_fields,
        unique_id_field=unique_id_field,
        no_preflight_checks=no_preflight_checks,
        headless=headless,
        worker=worker,
        batch_size=batch_size,
        skip=skip,
        fail=fail,
        separator=separator,
        ignore=ignore,
        context=context,
        encoding=encoding,
        o2m=o2m,
        split_by_cols=split_by_cols_list,
    )


def run_import_for_migration(
    config: str,
    model: str,
    header: list[str],
    data: list[list[Any]],
    worker: int = 1,
    batch_size: int = 10,
) -> None:
    """Orchestrates the data import process from in-memory data.

    This function adapts in-memory data to the file-based import engine by
    writing the data to a temporary file. This allows it to leverage all the
    robust features of the main importer.

    Args:
        config (str): Path to the connection configuration file.
        model (str): The Odoo model to import data into.
        header (list[str]): A list of strings representing the column headers.
        data (list[list[Any]]): A list of lists representing the data rows.
        worker (int): The number of simultaneous connections to use.
        batch_size (int): The number of records to process in each batch.
    """
    log.info("Starting data import from in-memory data...")
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+", delete=False, suffix=".csv", newline=""
        ) as tmp:
            writer = csv.writer(tmp)
            writer.writerow(header)
            writer.writerows(data)
            tmp_path = tmp.name

        log.info(f"In-memory data written to temporary file: {tmp_path}")

        import_threaded.import_data(
            config_file=config,
            model=model,
            unique_id_field="id",  # Migration import assumes 'id'
            file_csv=tmp_path,
            context={"tracking_disable": True},
            max_connection=int(worker),
            batch_size=int(batch_size),
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    log.info("In-memory import process finished.")


def run_import_deferred(
    config: str,
    filename: str,
    model_name: str,
    unique_id_field: str,
    deferred_fields: list[str],
    encoding: str = "utf-8",
    separator: str = ";",
) -> bool:
    """Performs a two-pass import from a CSV file to handle deferred relations.

    Args:
        config (str): Path to the connection configuration file.
        filename (str): Path to the source CSV file.
        model_name (str): The technical name of the Odoo model.
        unique_id_field (str): The column in the CSV that uniquely identifies each row.
        deferred_fields (list[str]): A list of column names for the second pass.
        encoding (str): The file encoding of the source file.
        separator (str): The delimiter used in the CSV file.

    Returns:
        bool: True if the import process completes successfully, False otherwise.
    """
    log.info(f"Starting two-pass deferred import for model '{model_name}'...")

    success = import_threaded.import_data(
        config_file=config,
        model=model_name,
        unique_id_field=unique_id_field,
        file_csv=filename,
        deferred_fields=deferred_fields,
        encoding=encoding,
        separator=separator,
        max_connection=4,  # TODO
        batch_size=200,  # TODO
    )

    console = Console()
    if success:
        console.print(
            Panel(
                f"Two-pass import for [cyan]{model_name}[/cyan] finished.",
                title="[bold green]Import Complete[/bold green]",
                expand=False,
            )
        )
    else:
        _show_error_panel(
            "Import Failed",
            "The deferred import process failed. Check logs for details.",
        )

    return success
