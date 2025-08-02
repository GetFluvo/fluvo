"""Main writer module.

This module contains the high-level logic for orchestrating the 'write' process.
"""

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

from . import write_threaded
from .logging_config import log


def _read_data_file(
    file_path: str, separator: str, encoding: str
) -> tuple[list[str], list[list[Any]]]:
    """Reads a CSV file and returns its header and data.

    This function reads the specified CSV file, validates that it contains an 'id'
    column, and returns the header and data rows. It handles potential BOM
    characters at the start of the file.

    Args:
        file_path: The full path to the CSV file.
        separator: The delimiter character used in the CSV file.
        encoding: The file encoding to use when reading.

    Returns:
        A tuple containing the list of header columns and a list of data rows.
        Returns ([], []) if the file is not found or an error occurs.
    """
    log.info(f"Reading data from file: {file_path}")
    try:
        with open(file_path, encoding=encoding, newline="") as f:
            reader = csv.reader(f, delimiter=separator)
            try:
                # Read and clean the header in one step
                raw_header = next(reader)
                header = [h.strip() for h in raw_header]
            except StopIteration:
                # This handles the case where the file is completely empty
                return [], []

            # The single, definitive check for the 'id' column
            if "id" not in header:
                log.error(
                    "Failed to read file %s: Source file for writing must "
                    "contain an 'id' column.",
                    file_path,
                )
                return [], []

            # If the header is valid, read the rest of the data
            data = [row for row in reader]
            return header, data

    except FileNotFoundError:
        log.error(f"Source file not found: {file_path}")
        return [], []
    except Exception as e:
        log.error(f"Failed to read file {file_path}: {e}")
        return [], []


def run_write(
    config: str,
    filename: str,
    model: str,
    fail: bool,
    **kwargs: Any,
) -> None:
    """Orchestrates the entire batch write process from a CSV file.

    This function serves as the main entry point for the 'write' command. It
    handles the `--fail` mode logic, reads the source data file, and delegates
    the core multi-threaded write operations to the `write_threaded` module.

    Args:
        config: Path to the connection configuration file.
        filename: Path to the source CSV file containing records to update.
        model: The Odoo model to write data to.
        fail: If True, runs in fail mode, retrying records from the
              corresponding `_write_fail.csv` file.
        **kwargs: A dictionary of additional keyword arguments passed from the
                  CLI, such as 'separator', 'encoding', 'worker', 'batch_size',
                  and 'context'.
    """
    log.info("Starting data write process from file...")

    source_file = filename
    is_fail_run = fail

    if fail:
        model_filename = model.replace(".", "_")
        fail_file_path = Path(filename).parent / f"{model_filename}_write_fail.csv"

        file_has_records = False
        if fail_file_path.exists():
            with open(fail_file_path, encoding="utf-8") as f:
                reader = csv.reader(f)
                try:
                    next(reader)  # Skip header
                    next(reader)  # Check for first data row
                    file_has_records = True
                except StopIteration:
                    pass

        if not file_has_records:
            console = Console()
            console.print(
                Panel(
                    f"No records found in '{fail_file_path}'. Nothing to retry.",
                    title="[bold green]No Recovery Needed[/bold green]",
                    border_style="green",
                )
            )
            return

        log.info(f"Running in --fail mode. Retrying records from: {fail_file_path}")
        source_file = str(fail_file_path)

    header, data = _read_data_file(
        source_file,
        kwargs.get("separator", ";"),
        kwargs.get("encoding", "utf-8"),
    )

    if not data:
        log.warning("No data rows found in the source file. Nothing to write.")
        return

    if not data:
        log.warning("No data rows found in the source file. Nothing to write.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_filename = model.replace(".", "_")

    if is_fail_run:
        fail_output_file = (
            Path(filename).parent / f"{model_filename}_{timestamp}_write_failed.csv"
        )
    else:
        fail_output_file = Path(filename).parent / f"{model_filename}_write_fail.csv"

    log.info(f"Target model: {model}")
    log.info(
        f"Workers: {kwargs.get('worker', 1)}, Batch Size: "
        f"{kwargs.get('batch_size', 1000)}"
    )
    log.info(f"Failed records will be saved to: {fail_output_file}")

    success = write_threaded.write_data(
        config_file=config,
        model=model,
        header=header,
        data=data,
        fail_file=str(fail_output_file),
        is_fail_run=fail,
        max_connection=kwargs.get("worker", 1),
        batch_size=kwargs.get("batch_size", 1000),
        context=kwargs.get("context"),
    )

    if success:
        console = Console()
        console.print(
            Panel(
                f"Write process for model [bold]{model}[/bold] finished successfully.",
                title="[bold green]Write Complete[/bold green]",
                border_style="green",
            )
        )
    else:
        console = Console()
        console.print(
            Panel(
                "The write process was aborted or failed. "
                "Please check the logs and the failed file for details.",
                title="[bold red]Write Failed[/bold red]",
                border_style="red",
            )
        )
