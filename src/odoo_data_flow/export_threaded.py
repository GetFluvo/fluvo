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
from .lib.odoo_lib import ODOO_TO_POLARS_MAP
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
            log.debug(f"Exporting batch {num} with {len(ids_to_export)} records...")
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

    df = pl.DataFrame(batch_data, infer_schema_length=None)
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


def _process_export_batches(  # noqa C901
    rpc_thread: RPCThreadExport,
    total_ids: int,
    model_name: str,
    output: Optional[str],
    field_types: dict[str, str],
    separator: str,
    streaming: bool,
) -> Optional[pl.DataFrame]:
    """Processes exported batches.

    Uses streaming for large files if requested,
    otherwise concatenates in memory for best performance.
    """
    polars_schema: dict[str, pl.DataType] = {
        field: ODOO_TO_POLARS_MAP.get(odoo_type, pl.String)()
        for field, odoo_type in field_types.items()
    }
    all_cleaned_dfs: list[pl.DataFrame] = []
    header_written = False  # Only used in streaming mode
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
        task = progress.add_task(f"[cyan]Exporting {model_name}...", total=total_ids)
        for future in concurrent.futures.as_completed(rpc_thread.futures):
            try:
                batch_result = future.result()
                if not batch_result:
                    continue

                cleaned_df = _clean_batch(batch_result, field_types)
                if cleaned_df.is_empty():
                    continue

                # Cast the DataFrame to the consistent schema
                casted_df = cleaned_df.cast(polars_schema, strict=False)  # type: ignore[arg-type]

                if output and streaming:
                    # STREAMING MODE: Write batch-by-batch to disk
                    if not header_written:
                        casted_df.write_csv(
                            output, separator=separator, include_header=True
                        )
                        header_written = True
                    else:
                        with open(output, "ab") as f:
                            casted_df.write_csv(
                                f, separator=separator, include_header=False
                            )
                else:
                    # IN-MEMORY MODE: Collect for later concatenation
                    all_cleaned_dfs.append(casted_df)

                progress.update(task, advance=len(batch_result))
            except Exception as e:
                log.error(f"A task in a worker thread failed: {e}", exc_info=True)

    rpc_thread.executor.shutdown(wait=True)

    # --- Post-Loop Logic ---
    if output and streaming:
        log.info(f"Streaming export complete. Data written to {output}")
        # In streaming mode, we don't load the (potentially huge) file back into memory.
        # We return None to signal completion.
        return None

    if not all_cleaned_dfs:
        log.warning("No data was returned from the export.")
        empty_df = pl.DataFrame(schema=list(field_types.keys()))
        if output:
            empty_df.write_csv(output, separator=separator)
        return empty_df

    # Concatenate all dataframes for the default in-memory mode
    final_df = pl.concat(all_cleaned_dfs)
    if output:
        log.info(f"Writing {len(final_df)} records to {output}...")
        final_df.write_csv(output, separator=separator)
        log.info("Export complete.")
    else:
        log.info("In-memory export complete.")

    return final_df


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
    streaming: bool = False,
) -> Optional[pl.DataFrame]:
    """Exports data from an Odoo model."""
    model_obj, field_types = _initialize_export(config_file, model, header)
    if not model_obj or field_types is None:
        return None

    if streaming and not output:
        log.error("Streaming mode requires an output file path. Aborting.")
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
        rpc_thread, len(ids), model, output, field_types, separator, streaming
    )
