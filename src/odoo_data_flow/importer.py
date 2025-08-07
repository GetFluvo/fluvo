"""Main importer module.

This module contains the high-level logic for orchestrating the import process.
It handles file I/O, pre-flight checks, and the delegation of the core
import tasks to the multi-threaded `import_threaded` module.
"""

import csv
import os
import re
import tempfile
import time
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
        with open(filepath, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def _infer_model_from_filename(filename: str) -> Optional[str]:
    """Tries to guess the Odoo model from a CSV filename."""
    basename = Path(filename).stem
    # Remove common suffixes like _fail, _transformed, etc.
    clean_name = re.sub(r"(_fail|_transformed|\d+)$", "", basename)
    # Convert underscores to dots
    model_name = clean_name.replace("_", ".")
    if "." in model_name:
        return model_name
    return None


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


def run_import(  # noqa: C901
    config: str,
    filename: str,
    model: Optional[str],
    deferred_fields: Optional[list[str]],
    unique_id_field: Optional[str],
    no_preflight_checks: bool,
    headless: bool,
    worker: int,
    batch_size: int,
    skip: int,
    fail: bool,
    separator: str,
    ignore: Optional[list[str]],
    context: dict[str, Any],
    encoding: str,
    o2m: bool,
    groupby: Optional[list[str]],
) -> None:
    """Main entry point for the import command, handling all orchestration.

    This function serves as the primary orchestrator for the import process.
    It handles model name inference, argument parsing, fail-mode logic,
    pre-flight checks, and routing to the correct import strategy (single-pass
    or two-pass deferred).

    Args:
        config (str): Path to the connection configuration file.
        filename (str): Path to the source CSV file to import.
        model (Optional[str]): The target Odoo model. Inferred from filename
            if not set.
        deferred_fields (Optional[list[str]]): A list of fields to defer to a
            second pass.
        unique_id_field (Optional[str]): The name of the unique ID column.
        no_preflight_checks (bool): If True, skips all pre-flight checks.
        headless (bool): If True, runs in non-interactive mode.
        worker (int): The number of simultaneous connections to use.
        batch_size (int): The number of records per batch.
        skip (int): The number of initial lines to skip.
        fail (bool): If True, runs in fail mode, processing a _fail.csv file.
        separator (str): The delimiter used in the CSV file.
        ignore (Optional[list[str]]): A list of columns to ignore
            from the import.
        context (dict[str, Any]): The Odoo context dictionary to use for the
            import.
        encoding (str): The file encoding of the source file.
        o2m (bool): If True, enables special handling for one-to-many files.
        groupby (Optional[list[str]]): A list of columns to group data by.
    """
    log.info("Starting data import process from file...")
    if not model:
        model = _infer_model_from_filename(filename)
        if not model:
            _show_error_panel(
                "Model Not Found",
                "Could not infer model from filename. Please use the --model option.",
            )
            return

    file_to_process = filename
    if fail:
        fail_path = Path(filename).parent / _get_fail_filename(model, False)
        line_count = _count_lines(str(fail_path))
        if line_count <= 1:
            Console().print(
                Panel(
                    f"No records to retry in '{fail_path}'.",
                    title="[bold green]No Recovery Needed[/bold green]",
                )
            )
            return
        log.info(
            f"Running in --fail mode. Retrying {line_count - 1} records from: "
            f"{fail_path}"
        )
        file_to_process = str(fail_path)
        # When in fail mode, we must ignore the _ERROR_REASON column that was
        # added to the fail file so it isn't sent to Odoo.
        if ignore is None:
            ignore = []
        if "_ERROR_REASON" not in ignore:
            log.info("Ignoring the internal '_ERROR_REASON' column for re-import.")
            ignore.append("_ERROR_REASON")

    import_plan: dict[str, Any] = {}
    if not no_preflight_checks:
        validation_filename = filename if fail else file_to_process
        if not _run_preflight_checks(
            preflight_mode=PreflightMode.FAIL_MODE if fail else PreflightMode.NORMAL,
            import_plan=import_plan,
            model=model,
            filename=file_to_process,
            validation_filename=validation_filename,
            config=config,
            headless=headless,
            separator=separator,
            unique_id_field=unique_id_field,
        ):
            return

    # Determine final arguments for the core import engine
    final_deferred = deferred_fields or import_plan.get("deferred_fields", [])
    final_uid_field = unique_id_field or import_plan.get("unique_id_field") or "id"

    fail_output_file = str(Path(filename).parent / _get_fail_filename(model, fail))
    # Determine the import strategy to set the correct execution parameters.

    # A. Fail-mode runs must be processed one-by-one to ensure accuracy.
    # B. Two-pass imports uses batches for maximum perforamance on pass 1.
    #    The relational updates are done in batches in Pass 2 to ensure
    #    optimal performance.
    # C. Single-pass imports can run fully batched for maximum performance.
    if fail:
        log.info("Single-record batching enabled for this import strategy.")
        max_conn = 1
        batch_size_run = 1
        # force_create is a specific flag for fail-recovery mode only.
        force_create = True if fail else False
    else:
        # This is a standard, normal-mode import.
        max_conn = worker
        batch_size_run = batch_size
        force_create = False

    start_time = time.time()
    success, count = import_threaded.import_data(
        config_file=config,
        model=model,
        unique_id_field=final_uid_field,
        file_csv=file_to_process,
        deferred_fields=final_deferred,
        context=context,
        fail_file=fail_output_file,
        encoding=encoding,
        separator=separator,
        ignore=ignore or [],
        max_connection=max_conn,
        batch_size=batch_size_run,
        skip=skip,
        force_create=force_create,
        o2m=o2m,
        split_by_cols=groupby,
    )
    elapsed = time.time() - start_time

    fail_file_was_created = _count_lines(fail_output_file) > 1
    is_truly_successful = success and not fail_file_was_created

    if is_truly_successful:
        log.info(f"{count} records processed. Total time: {elapsed:.2f}s.")
        Console().print(
            Panel(
                f"Import for [cyan]{model}[/cyan] finished successfully.",
                title="[bold green]Import Complete[/bold green]",
            )
        )
    else:
        _show_error_panel(
            "Import Failed",
            "The import process failed. Check logs for details.",
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
