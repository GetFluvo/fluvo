"""Export thread.

This module contains the low-level, multi-threaded logic for exporting
data from an Odoo instance.
"""

import concurrent.futures
import csv
import sys
from time import time
from typing import Any, Optional

import polars as pl
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)

from .lib import conf_lib
from .lib.internal.rpc_thread import RpcThread
from .lib.internal.tools import batch
from .logging_config import log

# --- Fix for csv.field_size_limit OverflowError ---
max_int = sys.maxsize
decrement = True
while decrement:
    decrement = False
    try:
        csv.field_size_limit(max_int)
    except OverflowError:
        max_int = int(max_int / 10)
        decrement = True


class RPCThreadExport(RpcThread):
    """Export Thread handler."""

    def __init__(
        self,
        max_connection: int,
        model: Any,
        header: list[str],
        context: Optional[dict[str, Any]] = None,
        technical_names: bool = False,
    ) -> None:
        """Initializes the export thread handler."""
        super().__init__(max_connection)
        self.model = model
        self.header = header
        self.context = context or {}
        self.technical_names = technical_names

    def _execute_batch(
        self, ids_to_export: list[int], num: int
    ) -> list[dict[str, Any]]:
        """The actual function executed by the worker thread."""
        start_time = time()
        try:
            log.debug(
                f"Exporting batch {num} with {len(ids_to_export)} records..."
            )
            if self.technical_names:
                records: list[dict[str, Any]] = self.model.read(
                    ids_to_export, self.header
                )
                return records
            else:
                exported_data = self.model.export_data(
                    ids_to_export, self.header, context=self.context
                ).get("datas", [])
                return [dict(zip(self.header, row)) for row in exported_data]
        except Exception as e:
            log.error(f"Export for batch {num} failed: {e}", exc_info=True)
            return []
        finally:
            log.debug(f"Batch {num} finished in {time() - start_time:.2f}s.")

    def launch_batch(self, data_ids: list[int], batch_number: int) -> None:
        """Submits a batch of IDs to be exported by a worker thread."""
        self.spawn_thread(self._execute_batch, [data_ids, batch_number])


def _clean_batch(
    batch_data: list[dict[str, Any]], field_types: dict[str, str]
) -> pl.DataFrame:
    """Converts a batch of data to a DataFrame and cleans it."""
    if not batch_data:
        return pl.DataFrame()

    df = pl.DataFrame(batch_data)
    cleaning_exprs = []
    for field_name, field_type in field_types.items():
        if field_name in df.columns and field_type != "boolean":
            cleaning_exprs.append(
                pl.when(pl.col(field_name) == False)  # noqa: E712
                .then(None)
                .otherwise(pl.col(field_name))
                .alias(field_name)
            )
    if cleaning_exprs:
        df = df.with_columns(cleaning_exprs)
    return df


def _initialize_export(
    config_file: str, model_name: str, header: list[str]
) -> tuple[Optional[Any], Optional[dict[str, str]]]:
    """Connects to Odoo and fetches field metadata."""
    try:
        connection = conf_lib.get_connection_from_config(config_file)
        model_obj = connection.get_model(model_name)
        field_metadata = model_obj.fields_get(header)
        field_types = {
            field: details["type"] for field, details in field_metadata.items()
        }
        return model_obj, field_types
    except Exception as e:
        log.error(
            f"Failed to connect to Odoo or get model '{model_name}'. "
            f"Please check your configuration. Error: {e}"
        )
        return None, None


def _process_export_batches(
    rpc_thread: RPCThreadExport,
    total_ids: int,
    model_name: str,
    output: Optional[str],
    field_types: dict[str, str],
    separator: str,
) -> Optional[pl.DataFrame]:
    """Processes exported batches, cleans them, and writes or collects them."""
    all_data_for_memory: list[pl.DataFrame] = []
    header_written = False
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}", justify="right"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TextColumn("•"),
        TextColumn("[green]{task.completed} of {task.total} records"),
        TextColumn("•"),
        TimeRemainingColumn(),
    )
    with progress:
        task = progress.add_task(
            f"[cyan]Exporting {model_name}...", total=total_ids
        )
        for future in concurrent.futures.as_completed(rpc_thread.futures):
            try:
                batch_result = future.result()
                if not batch_result:
                    continue

                cleaned_df = _clean_batch(batch_result, field_types)
                if cleaned_df.is_empty():
                    continue

                if output:
                    cleaned_df.write_csv(
                        output,
                        separator=separator,
                        include_header=not header_written,
                    )
                    header_written = True
                else:
                    all_data_for_memory.append(cleaned_df)

                progress.update(task, advance=len(cleaned_df))
            except Exception as e:
                log.error(
                    f"A task in a worker thread failed: {e}", exc_info=True
                )

    rpc_thread.executor.shutdown(wait=True)

    if output:
        log.info(f"Export complete. Data written to {output}")
        return pl.read_csv(output, separator=separator)
    else:
        log.info("In-memory export complete.")
        if not all_data_for_memory:
            return pl.DataFrame(schema=list(field_types.keys()))
        return pl.concat(all_data_for_memory)


def export_data(
    config_file: str,
    model: str,
    domain: list[Any],
    header: list[str],
    output: Optional[str],
    context: Optional[dict[str, Any]] = None,
    max_connection: int = 1,
    batch_size: int = 1000,
    separator: str = ";",
    encoding: str = "utf-8",
    technical_names: bool = False,
) -> Optional[pl.DataFrame]:
    """Exports data from an Odoo model."""
    model_obj, field_types = _initialize_export(config_file, model, header)
    if not model_obj or field_types is None:
        return None

    log.info(f"Searching for records in model '{model}' to export...")
    ids = model_obj.search(domain, context=context)
    if not ids:
        log.warning("No records found for the given domain.")
        if output:
            pl.DataFrame(schema=header).write_csv(output, separator=separator)
        return pl.DataFrame(schema=header)

    log.info(
        f"Found {len(ids)} records to export. Splitting into batches of {batch_size}."
    )
    id_batches = list(batch(ids, batch_size))

    rpc_thread = RPCThreadExport(
        max_connection, model_obj, header, context, technical_names
    )
    for i, id_batch in enumerate(id_batches):
        rpc_thread.launch_batch(list(id_batch), i)

    return _process_export_batches(
        rpc_thread, len(ids), model, output, field_types, separator
    )
