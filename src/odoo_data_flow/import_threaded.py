"""Import thread.

This module contains the low-level, multi-threaded logic for importing
data into an Odoo instance.
"""

import ast
import concurrent.futures
import csv
import sys
import time
import traceback
from collections.abc import Generator, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa
from typing import Any, Optional, TextIO, Union

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

from .lib import checkpoint as ckpt
from .lib import conf_lib
from .lib import idempotent as idempotent_lib
from .lib import retry as retry_lib
from .lib import throttle as throttle_lib
from .lib.internal.rpc_thread import RpcThread
from .lib.internal.tools import batch, to_xmlid
from .logging_config import log, suppress_console_handler

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**30)


# --- Helper Functions ---
def _format_odoo_error(error: Any) -> str:
    """Tries to extract the meaningful message from an Odoo RPC error."""
    if not isinstance(error, str):
        error = str(error)
    try:
        error_dict = ast.literal_eval(error)
        if (
            isinstance(error_dict, dict)
            and "data" in error_dict
            and "message" in error_dict["data"]
        ):
            return str(error_dict["data"]["message"])
    except (ValueError, SyntaxError):
        pass
    return str(error).strip().replace("\n", " ")


def _extract_per_row_errors(messages: list[dict[str, Any]]) -> dict[int, str]:
    """Extract per-row error messages from Odoo's load response.

    Odoo's load method sometimes includes row-specific error information
    in the messages. This function parses those messages to extract
    error information keyed by row number.

    Common patterns:
    - "Row 5: Validation error..."
    - "Line 3: Missing required field..."
    - Error messages with 'record' and row number in them

    Args:
        messages: List of message dictionaries from Odoo's load response.
                  Each dict typically has 'type', 'message', and sometimes 'rows'.

    Returns:
        A dictionary mapping row indices (0-based) to error messages.
    """
    import re

    per_row_errors: dict[int, str] = {}

    for msg in messages:
        message_text = msg.get("message", "")
        rows = msg.get("rows", {})

        # Check if Odoo provided row information directly
        if isinstance(rows, dict) and rows.get("from") is not None:
            row_from: int = rows.get("from", 0) or 0
            row_to: int = rows.get("to", row_from) or row_from
            for row_idx in range(row_from, row_to + 1):
                per_row_errors[row_idx] = message_text

        # Try to extract row numbers from the message text
        # Pattern: "Row X:" or "Line X:" at the beginning of the message
        row_match = re.match(
            r"^(?:Row|Line)\s+(\d+)\s*[:\-]?\s*(.*)", message_text, re.IGNORECASE
        )
        if row_match:
            row_num = int(row_match.group(1))
            error_text = row_match.group(2) or message_text
            # Convert 1-based row numbers to 0-based index
            per_row_errors[row_num - 1] = error_text

        # Pattern: "at row X" or "in row X" somewhere in the message
        row_in_match = re.search(
            r"(?:at|in|for)\s+row\s+(\d+)", message_text, re.IGNORECASE
        )
        if row_in_match:
            row_num = int(row_in_match.group(1))
            per_row_errors[row_num - 1] = message_text

    return per_row_errors


def _warn_empty_ids(
    header: list[str],
    data: list[list[Any]],
    start_row: int = 0,
) -> int:
    """Warn about rows with empty 'id' values.

    This function checks each row for empty 'id' values and logs warnings.
    Records with empty IDs may be created without XML IDs, making them
    unreferenceable by subsequent imports.

    Args:
        header: The CSV header row.
        data: The CSV data rows.
        start_row: The starting row number for logging (used in streaming mode).

    Returns:
        The count of rows with empty id values.
    """
    if "id" not in header:
        return 0

    id_index = header.index("id")
    empty_count = 0

    for row_idx, row in enumerate(data):
        if id_index < len(row):
            id_value = row[id_index]
            # Check for empty, None, or whitespace-only values
            if id_value is None or (isinstance(id_value, str) and not id_value.strip()):
                actual_row = start_row + row_idx + 2  # +2 for header and 1-based
                empty_count += 1
                log.warning(
                    f"Row {actual_row}: Empty 'id' value detected. "
                    f"Record will be created without an XML ID."
                )

    if empty_count > 0:
        log.warning(
            f"Found {empty_count} row(s) with empty 'id' values. "
            f"These records will not have XML IDs and cannot be referenced."
        )

    return empty_count


# Default maximum batch size in bytes (5MB)
DEFAULT_MAX_BATCH_BYTES = 5 * 1024 * 1024


def _estimate_payload_size(data: Any) -> int:
    """Estimate the size in bytes of data when serialized for RPC.

    This function provides a rough estimate of how large the data will be
    when sent over the network. It's used to implement size-based batching
    to prevent timeouts when importing records with large binary fields
    (like images).

    Args:
        data: The data to estimate size for. Can be a dict, list, or primitive.

    Returns:
        Estimated size in bytes.
    """
    import json

    try:
        # JSON serialization is a reasonable proxy for RPC payload size
        return len(json.dumps(data, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        # Fallback: estimate based on string representation
        return len(str(data).encode("utf-8"))


def _estimate_row_size(row: list[Any]) -> int:
    """Estimate the size in bytes of a single CSV row.

    Args:
        row: A list of values from a CSV row.

    Returns:
        Estimated size in bytes.
    """
    total = 0
    for value in row:
        if value is None:
            total += 4  # "null"
        elif isinstance(value, str):
            # String values - account for quotes and escaping
            total += len(value.encode("utf-8")) + 2
        else:
            total += len(str(value))
    return total


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
        log.error(f"Exception type: {type(e).__name__}")
        log.error(f"Exception args: {e.args}")

        log.error(f"Full traceback: {traceback.format_exc()}")
        if isinstance(e, ValueError):
            raise
        return [], []


def _count_csv_rows(file_path: str, separator: str, encoding: str, skip: int) -> int:
    """Quickly counts the number of data rows in a CSV file.

    This function reads through the file once to count rows, which is
    needed for progress bar initialization when streaming.

    Args:
        file_path: The full path to the source CSV file.
        separator: The delimiter character.
        encoding: The character encoding of the file.
        skip: The number of lines to skip after the header.

    Returns:
        The number of data rows (excluding header and skipped lines).
    """
    count = 0
    try:
        with open(file_path, encoding=encoding, newline="") as f:
            reader = csv.reader(f, delimiter=separator)
            next(reader)  # Skip header
            for _ in range(skip):
                next(reader)
            for _ in reader:
                count += 1
    except Exception as e:
        log.debug(f"Error counting lines: {e}")
    return count


def _stream_csv_batches(
    file_path: str,
    separator: str,
    encoding: str,
    skip: int,
    batch_size: int,
    ignore: list[str],
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
) -> Generator[tuple[list[str], int, list[list[Any]]], None, None]:
    """Streams CSV data in batches without loading the entire file into memory.

    This generator opens the CSV file and yields batches of rows along with
    the header. It is memory-efficient for large files as it only keeps
    one batch in memory at a time.

    Batching is controlled by both record count (batch_size) and payload size
    (max_batch_bytes). A new batch is started when either limit is reached.
    This prevents timeouts when importing records with large binary fields.

    Args:
        file_path: The full path to the source CSV file.
        separator: The delimiter character used to separate columns.
        encoding: The character encoding of the file.
        skip: The number of lines to skip at the top of the file.
        batch_size: The maximum number of records to include in each batch.
        ignore: A list of column names to ignore during import.
        max_batch_bytes: Maximum estimated payload size per batch in bytes.
            Defaults to 5MB. Set to 0 to disable size-based batching.

    Yields:
        Tuples of (header, batch_number, batch_data) where:
        - header: The list of column names (same for each batch)
        - batch_number: The sequential batch number (1-indexed)
        - batch_data: A list of rows for this batch

    Raises:
        ValueError: If the source file does not contain a required 'id' column.
        FileNotFoundError: If the source file does not exist.
    """
    with open(file_path, encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter=separator)
        header = next(reader)

        if "id" not in header:
            raise ValueError("Source file must contain an 'id' column.")

        for _ in range(skip):
            next(reader)

        # Pre-calculate indices to keep for filtering ignored columns
        ignore_set = set(ignore) if ignore else set()
        if ignore_set:
            indices_to_keep = [
                i for i, h in enumerate(header) if h.split("/")[0] not in ignore_set
            ]
            filtered_header = [header[i] for i in indices_to_keep]
        else:
            indices_to_keep = None
            filtered_header = header

        current_batch: list[list[Any]] = []
        current_batch_bytes = 0
        batch_number = 0

        for row in reader:
            # Apply column filtering if needed
            if indices_to_keep is not None:
                if len(row) < max(indices_to_keep) + 1:
                    # Skip malformed rows
                    continue
                row = [row[i] for i in indices_to_keep]

            row_size = _estimate_row_size(row)

            # Check if adding this row would exceed limits
            # Always include at least one row per batch
            size_limit_exceeded = (
                max_batch_bytes > 0
                and current_batch_bytes + row_size > max_batch_bytes
                and current_batch
            )
            count_limit_exceeded = len(current_batch) >= batch_size

            if size_limit_exceeded or count_limit_exceeded:
                batch_number += 1
                yield filtered_header, batch_number, current_batch
                current_batch = []
                current_batch_bytes = 0

            current_batch.append(row)
            current_batch_bytes += row_size

        # Yield any remaining rows
        if current_batch:
            batch_number += 1
            yield filtered_header, batch_number, current_batch


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
    ignore_set = set(ignore)
    indices_to_keep = [
        i for i, h in enumerate(header) if h.split("/")[0] not in ignore_set
    ]
    new_header = [header[i] for i in indices_to_keep]

    if not indices_to_keep:
        return new_header, [[] for _ in data]

    max_index_needed = max(indices_to_keep)
    new_data = []
    for row_idx, row in enumerate(data):
        if len(row) <= max_index_needed:
            log.warning(
                f"Skipping malformed row {row_idx + 2}: has {len(row)} columns, "
                f"but header implies at least {max_index_needed + 1} are needed."
            )
            continue
        new_data.append([row[i] for i in indices_to_keep])

    return new_header, new_data


def _setup_fail_file(
    fail_file: Optional[str], header: list[str], separator: str, encoding: str
) -> tuple[Optional[Any], Optional[TextIO]]:
    """Opens the fail file and returns the writer and file handle."""
    if not fail_file:
        return None, None
    try:
        fail_handle = open(fail_file, "w", newline="", encoding=encoding)
        fail_writer = csv.writer(
            fail_handle, delimiter=separator, quoting=csv.QUOTE_ALL
        )
        header_to_write = list(header)
        if "_ERROR_REASON" not in header_to_write:
            header_to_write.append("_ERROR_REASON")
        fail_writer.writerow(header_to_write)
        return fail_writer, fail_handle
    except OSError as e:
        log.error(f"Could not open fail file for writing: {fail_file}. Error: {e}")
        return None, None


def _prepare_pass_2_data(  # noqa: C901
    all_data: list[list[Any]],
    header: list[str],
    unique_id_field_index: int,
    id_map: dict[str, int],
    deferred_fields: list[str],
    model_obj: Any = None,
) -> list[tuple[int, dict[str, Any]]]:
    """Prepares the list of write operations for Pass 2.

    This function handles both self-referencing fields (like parent_id which
    references the same model) and non-self-referencing fields (like responsible_id
    which references a different model like res.users).

    For self-referencing fields, it looks up the related database ID in id_map.
    For non-self-referencing fields, it resolves the external ID to a database ID
    using Odoo's ir.model.data lookup.
    """
    pass_2_data_to_write: list[tuple[int, dict[str, Any]]] = []

    # Normalize deferred fields to handle both formats:
    # 'responsible_id' and 'responsible_id/id'
    # Track if field was originally specified with /id suffix
    deferred_fields_normalized = {}
    for df in deferred_fields:
        if df.endswith("/id"):
            base_name = df[:-3]  # Remove '/id' suffix
            deferred_fields_normalized[base_name] = True  # Marks as external ID field
        else:
            deferred_fields_normalized[df] = False

    # Pre-calculate a map of deferred field names to their actual index in the header
    # Also track if the column is an external ID column (ends with /id)
    # and if the field is a many2many type (requires special value formatting)
    deferred_field_indices: dict[str, tuple[int, bool, bool]] = {}

    # Get field type information from the model to identify many2many fields
    many2many_fields: set[str] = set()
    if model_obj is not None:
        try:
            # Get field names we need to check
            field_names_to_check = list(deferred_fields_normalized.keys())
            fields_info = model_obj.fields_get(field_names_to_check)
            for field_name, field_meta in fields_info.items():
                if field_meta.get("type") == "many2many":
                    many2many_fields.add(field_name)
            if many2many_fields:
                log.debug(f"Detected many2many deferred fields: {many2many_fields}")
        except Exception as e:
            log.debug(f"Could not get field types for deferred fields: {e}")

    for i, column_name in enumerate(header):
        field_base_name = column_name.split("/")[0]
        if field_base_name in deferred_fields_normalized:
            # Store (index, is_external_id_column, is_many2many)
            is_ext_id_col = column_name.endswith("/id")
            is_m2m = field_base_name in many2many_fields
            deferred_field_indices[field_base_name] = (i, is_ext_id_col, is_m2m)

    if not deferred_field_indices:
        log.warning(
            f"No deferred fields found in header. "
            f"Deferred fields requested: {deferred_fields}, "
            f"Available columns: {header[:20]}..."  # Show first 20 for debugging
        )
        return pass_2_data_to_write

    log.debug(f"Deferred field indices: {deferred_field_indices}")

    # Get ir.model.data proxy for XML-ID resolution (non-self-referencing)
    # Note: Using print() for diagnostics since we don't have progress object here
    print("  [Pass 2] Getting ir.model.data proxy...")
    ir_model_data_proxy = None
    if model_obj is not None:
        try:
            # Try to get the connection from the model object
            conn = None
            for attr in ["connection", "client", "_connection", "_client"]:
                try:
                    val = getattr(model_obj, attr, None)
                    if val and not callable(val):
                        conn = val
                        break
                    elif val and callable(val) and hasattr(val, "get_model"):
                        conn = val
                        break
                except Exception:  # noqa: S112
                    continue

            if conn:
                for method_name in ["model", "get_model"]:
                    if hasattr(conn, method_name):
                        try:
                            method = getattr(conn, method_name)
                            ir_model_data_proxy = method("ir.model.data")
                            if ir_model_data_proxy:
                                break
                        except Exception:  # noqa: S112
                            continue
        except Exception as e:
            log.debug(f"Could not get ir.model.data proxy: {e}")

    proxy_status = "found" if ir_model_data_proxy else "not found"
    print(f"  [Pass 2] ir.model.data proxy: {proxy_status}")
    print(f"  [Pass 2] Processing {len(all_data)} records...")

    # Import the sanitization function to match id_map key format
    import time

    from .lib.internal.tools import to_xmlid

    # Cache for external ID lookups to avoid repeated RPC calls
    external_id_cache: dict[str, Optional[int]] = {}

    processed = 0
    found_in_idmap = 0
    not_in_idmap = 0
    rpc_lookups = 0
    cache_hits = 0
    start_time = time.time()
    last_print_time = start_time

    for row in all_data:
        processed += 1
        current_time = time.time()
        # Print progress every 500 records OR every 5 seconds (whichever comes first)
        if processed % 500 == 0 or (current_time - last_print_time) > 5:
            elapsed = current_time - start_time
            rate = processed / elapsed if elapsed > 0 else 0
            print(
                f"  [Pass 2] {processed}/{len(all_data)} ({rate:.0f}/s) | "
                f"idmap: {found_in_idmap}, rpc: {rpc_lookups}, cache: {cache_hits}"
            )
            last_print_time = current_time
        source_id = row[unique_id_field_index]
        # Sanitize source_id to match id_map key format
        sanitized_source_id = to_xmlid(source_id) if source_id else source_id
        db_id = id_map.get(sanitized_source_id)
        if not db_id:
            continue

        update_vals = {}
        # Use the pre-calculated map to find the values to write.
        for field_name, (field_index, is_ext_id_col, is_m2m) in deferred_field_indices.items():
            if field_index < len(row):
                field_value = row[field_index]
                if field_value:  # Ensure there is a value
                    # For many2many fields, handle multiple comma-separated values
                    if is_m2m:
                        # Split by comma if multiple values
                        raw_values = [v.strip() for v in str(field_value).split(",") if v.strip()]
                        resolved_ids: list[int] = []

                        for raw_val in raw_values:
                            # Try id_map lookup first
                            sanitized_val = to_xmlid(raw_val)
                            db_id_resolved = id_map.get(sanitized_val)

                            if db_id_resolved:
                                resolved_ids.append(db_id_resolved)
                                found_in_idmap += 1
                            elif is_ext_id_col and ir_model_data_proxy:
                                # Try XML-ID resolution
                                not_in_idmap += 1
                                if raw_val in external_id_cache:
                                    cache_hits += 1
                                    cached_id = external_id_cache[raw_val]
                                    if cached_id:
                                        resolved_ids.append(cached_id)
                                else:
                                    rpc_lookups += 1
                                    ext_resolved = _resolve_external_id_for_pass2(
                                        ir_model_data_proxy, raw_val
                                    )
                                    external_id_cache[raw_val] = ext_resolved
                                    if ext_resolved:
                                        resolved_ids.append(ext_resolved)
                                    else:
                                        log.warning(
                                            f"Missing m2m reference for '{field_name}': "
                                            f"'{raw_val}' not found (source_id={source_id})"
                                        )
                            else:
                                log.warning(
                                    f"Cannot resolve m2m '{field_name}': '{raw_val}' "
                                    f"not in id_map (source_id={source_id})"
                                )

                        if resolved_ids:
                            # Use Odoo's (6, 0, [ids]) command to replace the m2m relation
                            update_vals[field_name] = [(6, 0, resolved_ids)]
                            log.debug(
                                f"Resolved many2many '{field_name}': "
                                f"{len(resolved_ids)} IDs -> {resolved_ids}"
                            )
                    else:
                        # Non-many2many field: original logic for many2one and other fields
                        # Sanitize field_value to match id_map key format
                        sanitized_field_value = to_xmlid(field_value)
                        related_db_id = id_map.get(sanitized_field_value)

                        if related_db_id:
                            # Value found in id_map - use the database ID
                            update_vals[field_name] = related_db_id
                            found_in_idmap += 1
                            log.debug(
                                f"Resolved self-reference '{field_name}': "
                                f"'{field_value}' -> db_id {related_db_id}"
                            )
                        elif is_ext_id_col:
                            # External ID column (e.g., responsible_id/id)
                            # Try XML-ID resolution for non-self-referencing fields
                            not_in_idmap += 1
                            if ir_model_data_proxy:
                                # Check cache first to avoid repeated RPC calls
                                if field_value in external_id_cache:
                                    cache_hits += 1
                                    resolved_id = external_id_cache[field_value]
                                else:
                                    rpc_lookups += 1
                                    resolved_id = _resolve_external_id_for_pass2(
                                        ir_model_data_proxy, field_value
                                    )
                                    # Cache the result (even if None)
                                    external_id_cache[field_value] = resolved_id

                                if resolved_id:
                                    update_vals[field_name] = resolved_id
                                    log.debug(
                                        f"Resolved external ID '{field_name}': "
                                        f"'{field_value}' -> db_id {resolved_id}"
                                    )
                                else:
                                    log.warning(
                                        f"Missing reference for '{field_name}': "
                                        f"'{field_value}' not in id_map/ir.model.data "
                                        f"(source_id={source_id})"
                                    )
                            else:
                                log.warning(
                                    f"Cannot resolve '{field_name}': '{field_value}' "
                                    f"not in id_map and no ir.model.data proxy available "
                                    f"(source_id={source_id})"
                                )
                        else:
                            # Non-relational deferred field (e.g., image_1920)
                            # Not in id_map and not an external ID column
                            # Use value directly - likely base64 binary data
                            update_vals[field_name] = field_value
                            val_len = len(str(field_value))
                            log.debug(
                                f"Direct value for '{field_name}' "
                            f"(source={source_id}, len={val_len})"
                        )

        if update_vals:
            pass_2_data_to_write.append((db_id, update_vals))

    num_to_update = len(pass_2_data_to_write)
    print(f"  [Pass 2] Data prep complete: {num_to_update} records to update")
    return pass_2_data_to_write


def _resolve_external_id_for_pass2(
    ir_model_data_proxy: Any,
    xml_id: str,
) -> Optional[int]:
    """Resolve an XML ID to a database ID for Pass 2 updates.

    This is used for non-self-referencing deferred fields like responsible_id
    which references res.users, not the model being imported.

    Args:
        ir_model_data_proxy: The ir.model.data model proxy
        xml_id: The external ID to resolve (e.g., 'RES_USERS.281')

    Returns:
        The database ID if found, None otherwise
    """
    if not xml_id or not isinstance(xml_id, str) or "." not in xml_id:
        return None

    try:
        module, name = xml_id.split(".", 1)

        # Variations to try for module and name
        module_norm = module.lower().replace(".", "_")
        variations = [
            (module, name),  # Exact match
            (module.lower(), name),  # Lowercase module
            ("__export__", f"{module.lower()}_{name}"),  # Standard export format
            ("__export__", f"{module_norm}_{name}"),  # Normalized module name
            ("base", name),  # Base module
        ]

        for m, n in variations:
            try:
                domain = [("module", "=", m), ("name", "=", n)]
                res_id_data = ir_model_data_proxy.search_read(domain, ["res_id"])
                if res_id_data:
                    res_id = int(res_id_data[0]["res_id"])
                    log.debug(f"Resolved {xml_id} via {m}.{n} -> {res_id}")
                    return res_id
            except Exception:  # noqa: S112
                continue

        # Fallback: Search for the entire string in the 'name' field
        try:
            domain_full = [("name", "=", xml_id)]
            res_id_data = ir_model_data_proxy.search_read(domain_full, ["res_id"])
            if res_id_data:
                res_id = int(res_id_data[0]["res_id"])
                log.debug(f"Resolved {xml_id} via full match -> {res_id}")
                return res_id
        except Exception:  # noqa: S110
            pass

    except Exception as e:
        log.debug(f"Error resolving XML-ID {xml_id}: {e}")

    return None


def _recursive_create_batches(  # noqa: C901
    current_data: list[list[Any]],
    group_cols: list[str],
    header: list[str],
    batch_size: int,
    o2m: bool,
    batch_prefix: str = "",
    level: int = 0,
) -> Generator[tuple[Any, list[list[Any]]], None, None]:
    """Recursively creates batches of data, handling grouping and o2m."""
    if not group_cols:
        # Base case: No more grouping, handle o2m or simple batching
        current_batch: list[list[Any]] = []
        try:
            id_index = header.index("id")
        except ValueError:
            # If no 'id' column, o2m cannot work, so just batch by size
            for i, data_batch in enumerate(batch(current_data, batch_size)):
                yield (f"{batch_prefix}-{i}", list(data_batch))
            return

        for row in current_data:
            is_new_parent = o2m and row[id_index] and current_batch
            is_batch_full = not o2m and len(current_batch) >= batch_size

            if is_new_parent or is_batch_full:
                yield (current_batch[0][id_index], current_batch)
                current_batch = []

            current_batch.append(row)

        if current_batch:
            yield (current_batch[0][id_index], current_batch)
        return

    current_group_col, remaining_group_cols = group_cols[0], group_cols[1:]
    try:
        split_index = header.index(current_group_col)
    except ValueError:
        log.error(
            f"Grouping column '{current_group_col}' not found. Cannot use --groupby."
        )
        return

    current_data.sort(
        key=lambda r: (
            r[split_index] is None or r[split_index] == "",
            r[split_index],
        )
    )
    current_batch, current_split_value, group_counter = [], None, 0
    for row in current_data:
        row_split_value = row[split_index]
        if not current_batch:
            current_split_value = row_split_value
        elif row_split_value != current_split_value:
            yield from _recursive_create_batches(
                current_batch,
                remaining_group_cols,
                header,
                batch_size,
                o2m,
                f"{batch_prefix}{level}-{group_counter}-"
                f"{current_split_value or 'empty'}",
            )
            current_batch, group_counter, current_split_value = (
                [],
                group_counter + 1,
                row_split_value,
            )
        current_batch.append(row)

    if current_batch:
        yield from _recursive_create_batches(
            current_batch,
            remaining_group_cols,
            header,
            batch_size,
            o2m,
            f"{batch_prefix}{level}-{group_counter}-{current_split_value or 'empty'}",
        )


def _create_batches(
    data: list[list[Any]],
    split_by_cols: Optional[list[str]],
    header: list[str],
    batch_size: int,
    o2m: bool,
) -> Generator[tuple[int, list[list[Any]]], None, None]:
    """A generator that yields batches of data, starting the recursive batching."""
    if not data:
        return
    for i, (_, batch_data) in enumerate(
        _recursive_create_batches(data, split_by_cols or [], header, batch_size, o2m),
        start=1,
    ):
        yield i, batch_data


class RPCThreadImport(RpcThread):
    """A specialized RpcThread for handling data import and write tasks."""

    def __init__(
        self,
        max_connection: int,
        progress: Progress,
        task_id: TaskID,
        writer: Optional[Any] = None,
        fail_handle: Optional[TextIO] = None,
    ) -> None:
        super().__init__(max_connection)
        (
            self.progress,
            self.task_id,
            self.writer,
            self.fail_handle,
            self.abort_flag,
        ) = (
            progress,
            task_id,
            writer,
            fail_handle,
            False,
        )


def _convert_external_id_field(
    connection: Any,
    field_name: str,
    field_value: str,
) -> tuple[str, Any]:
    """Convert an external ID field to a database ID.

    Args:
        connection: The Odoo connection object (used to look up external IDs)
        field_name: The field name (e.g., 'parent_id/id')
        field_value: The external ID value

    Returns:
        Tuple of (base_field_name, converted_value)
    """
    base_field_name = field_name[:-3]  # Remove '/id' suffix
    converted_value = False

    if not field_value:
        # Empty external ID means no value for this field
        log.debug(
            f"Converted empty external ID {field_name} -> {base_field_name} (False)"
        )
    else:
        # Convert external ID to database ID
        try:
            # Parse module and name from external ID
            if "." in field_value:
                module, name = field_value.split(".", 1)
            else:
                # Default module for IDs without prefix
                module = "__export__"
                name = field_value

            # Look up the database ID via ir.model.data
            # This avoids model.env.ref() which may not be allowed for some models
            ir_model_data = connection.get_model("ir.model.data")
            existing_ids = ir_model_data.search(
                [
                    ("module", "=", module),
                    ("name", "=", name),
                ],
                limit=1,
            )

            if existing_ids:
                existing = ir_model_data.read(existing_ids[0], ["res_id"])
                if existing and existing.get("res_id"):
                    converted_value = existing["res_id"]
                    log.debug(
                        f"Converted external ID {field_name} ({field_value}) -> "
                        f"{base_field_name} ({converted_value})"
                    )
                else:
                    log.warning(
                        f"Could not find record for external ID '{field_value}', "
                        f"setting {base_field_name} to False"
                    )
            else:
                # If we can't find the external ID, value remains False
                log.warning(
                    f"Could not find record for external ID '{field_value}', "
                    f"setting {base_field_name} to False"
                )
        except Exception as e:
            log.warning(
                f"Error looking up external ID '{field_value}' for field "
                f"'{field_name}': {e}"
            )
            # On error, value remains False

    return base_field_name, converted_value


def _process_external_id_fields(
    connection: Any,
    clean_vals: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Process all external ID fields in the clean values.

    Args:
        connection: The Odoo connection object (used to look up external IDs)
        clean_vals: Dictionary of clean field values

    Returns:
        Tuple of (converted_vals, external_id_fields)
    """
    converted_vals: dict[str, Any] = {}
    external_id_fields: list[str] = []

    for field_name, field_value in clean_vals.items():
        # Handle external ID references (e.g., 'parent_id/id' -> 'parent_id')
        if field_name.endswith("/id"):
            # _convert_external_id_field is now a pure function that returns
            # (base_field_name, converted_value) instead of modifying
            # converted_vals as a side effect
            base_name, value = _convert_external_id_field(
                connection, field_name, field_value
            )
            converted_vals[base_name] = value
            external_id_fields.append(field_name)
        else:
            # Regular field - pass through as-is
            converted_vals[field_name] = field_value

    return converted_vals, external_id_fields


def _extract_access_error_message(error_str: str) -> str:  # noqa: C901
    """Extract a clean, user-friendly message from an access error.

    Args:
        error_str: The full error string from Odoo

    Returns:
        A clean, user-friendly error message
    """
    import re

    # First, look for specific error patterns that are most informative

    # Look for "cannot be called remotely" pattern and extract the method name
    remote_match = re.search(
        r"Private methods \(such as '([^']+)'\) cannot be called remotely",
        error_str,
    )
    if remote_match:
        model_name = remote_match.group(1)
        return f"Access denied: insufficient permissions to access '{model_name}'"

    # Look for AccessError message pattern
    access_match = re.search(
        r"AccessError\(['\"]([^'\"]+)['\"]\)", error_str, re.IGNORECASE
    )
    if access_match:
        return access_match.group(1)

    # Try to parse as dict and extract data.message (more specific than top-level)
    try:
        error_dict = ast.literal_eval(error_str)
        if isinstance(error_dict, dict):
            # Prefer data.message over top-level message
            if "data" in error_dict and isinstance(error_dict["data"], dict):
                data_msg = error_dict["data"].get("message")
                if data_msg:
                    return str(data_msg)
            # Fall back to top-level message
            if "message" in error_dict:
                return str(error_dict["message"])
    except (ValueError, SyntaxError):
        pass

    # Fall back to regex for 'message': '...' pattern
    message_match = re.search(r"'message':\s*['\"]([^'\"]+)['\"]", error_str)
    if message_match:
        return message_match.group(1)

    # Default: return a shortened version of the error
    # Strip debug/traceback info
    if "Traceback" in error_str:
        error_str = error_str.split("Traceback")[0].strip()

    # Limit length
    if len(error_str) > 200:
        return error_str[:200] + "..."

    return error_str


def _handle_create_error(  # noqa C901
    i: int,
    create_error: Exception,
    line: list[Any],
    error_summary: str,
) -> tuple[str, list[Any], str]:
    """Handle errors during record creation.

    Args:
        i: The row index
        create_error: The exception that occurred
        line: The data line being processed
        error_summary: Current error summary

    Returns:
        Tuple of (error_message, failed_line, error_summary)
    """
    error_str = str(create_error)
    error_str_lower = error_str.lower()

    # Handle access/permission errors first (most common user issue)
    if (
        "accesserror" in error_str_lower
        or "access denied" in error_str_lower
        or "permission denied" in error_str_lower
        or "not allowed" in error_str_lower
        or "cannot be called remotely" in error_str_lower
        or "access rights" in error_str_lower
    ):
        clean_message = _extract_access_error_message(error_str)
        error_message = f"Access denied (row {i + 1}): {clean_message}"
        if "Fell back to" in error_summary:
            error_summary = "Access denied - check user permissions"

    # Handle constraint violation errors (e.g., XML ID space constraint)
    elif (
        "constraint" in error_str_lower
        or "check constraint" in error_str_lower
        or "nospaces" in error_str_lower
        or "violation" in error_str_lower
    ):
        error_message = f"Constraint violation in row {i + 1}: {create_error}"
        if "Fell back to" in error_summary:
            error_summary = "Database constraint violation detected"

    # Handle database connection pool exhaustion errors
    elif (
        "connection pool is full" in error_str_lower
        or "too many connections" in error_str_lower
        or "poolerror" in error_str_lower
    ):
        error_message = (
            f"Database connection pool exhaustion in row {i + 1}: {create_error}"
        )
        if "Fell back to" in error_summary:
            error_summary = "Database connection pool exhaustion detected"
    # Handle specific database serialization errors
    elif (
        "could not serialize access" in error_str_lower
        or "concurrent update" in error_str_lower
    ):
        error_message = f"Database serialization error in row {i + 1}: {create_error}"
        if "Fell back to" in error_summary:
            error_summary = "Database serialization conflict detected during create"
    elif (
        "tuple index out of range" in error_str_lower or "indexerror" in error_str_lower
    ):
        error_message = f"Tuple unpacking error in row {i + 1}: {create_error}"
        if "Fell back to" in error_summary:
            error_summary = "Tuple unpacking error detected"
    else:
        error_message = error_str.replace("\n", " | ")
        if "invalid field" in error_str_lower and "/id" in error_str_lower:
            error_message = (
                f"Invalid external ID field detected in row {i + 1}: {error_message}"
            )

        if "Fell back to" in error_summary:
            error_summary = error_message

    failed_line = [*line, error_message]
    return error_message, failed_line, error_summary


def _create_xmlid_entry(
    connection: Any,
    xml_id: str,
    res_id: int,
    model_name: str,
) -> bool:
    """Ensure an ir.model.data entry exists for a record.

    This function ensures the XML ID is persisted in ir.model.data. It handles
    cases where load() creates a record but fails to persist the XML ID, and
    also updates existing entries if they point to a different record.

    Args:
        connection: The Odoo connection object (used to access ir.model.data)
        xml_id: The external ID (e.g., 'MODULE.identifier' or just 'identifier')
        res_id: The database ID of the created record
        model_name: The model name (e.g., 'res.partner')

    Returns:
        True if the ir.model.data entry was created successfully, False otherwise.
    """
    try:
        # Parse module and name from XML ID
        if "." in xml_id:
            module, name = xml_id.split(".", 1)
        else:
            # Use __import__ as the default module for records without a prefix
            module = "__import__"
            name = xml_id

        # Get ir.model.data model directly from connection
        # This avoids using model.browse() which may not be allowed for some models
        ir_model_data = connection.get_model("ir.model.data")

        # Check if entry already exists
        existing_ids = ir_model_data.search(
            [
                ("module", "=", module),
                ("name", "=", name),
            ],
            limit=1,
        )

        if existing_ids:
            # Read the existing entry to check res_id
            existing = ir_model_data.read(existing_ids[0], ["res_id", "model"])
            # Update existing entry if it points to a different record
            if existing.get("res_id") != res_id:
                log.debug(
                    f"Updating existing ir.model.data entry for {xml_id} "
                    f"from res_id={existing.get('res_id')} to res_id={res_id}"
                )
                ir_model_data.write(
                    existing_ids[0], {"res_id": res_id, "model": model_name}
                )
            return True

        # Create new ir.model.data entry
        ir_model_data.create(
            {
                "module": module,
                "name": name,
                "model": model_name,
                "res_id": res_id,
            }
        )
        log.debug(
            f"Created ir.model.data entry: {module}.{name} -> {model_name}({res_id})"
        )
        return True
    except Exception as e:
        log.warning(f"Failed to create ir.model.data entry for {xml_id}: {e}")
        return False


def _load_records_individually(  # noqa: C901
    model: Any,
    connection: Any,
    batch_lines: list[list[Any]],
    batch_header: list[str],
    uid_index: int,
    context: dict[str, Any],
    ignore_list: list[str],
    model_name: str = "",
) -> dict[str, Any]:
    """Fallback to load records one-by-one using load() for proper XML ID creation.

    Uses Odoo's native load() method with single records instead of create().
    This ensures XML IDs are properly created in ir.model.data automatically,
    avoiding the need for manual XML ID creation which can fail independently.
    """
    from .lib.internal.tools import to_xmlid

    id_map: dict[str, int] = {}
    failed_lines: list[list[Any]] = []
    error_summary = "Fell back to single-record load"
    header_len = len(batch_header)
    ignore_set = set(ignore_list)

    # Build filtered header (excluding ignored columns)
    # We need to track which indices to keep
    keep_indices = []
    filtered_header = []
    for idx, col in enumerate(batch_header):
        base_field = col.split("/")[0]
        if base_field not in ignore_set:
            keep_indices.append(idx)
            filtered_header.append(col)

    for i, line in enumerate(batch_lines):
        source_id = None
        try:
            if len(line) != header_len:
                raise IndexError(
                    f"Row has {len(line)} columns, but header has {header_len}."
                )

            source_id = line[uid_index]
            sanitized_source_id = to_xmlid(source_id)

            # Build filtered line (excluding ignored columns)
            filtered_line = [line[idx] for idx in keep_indices]

            # Sanitize the id column in the filtered line
            # Find the id column index in the filtered header
            if "id" in filtered_header:
                id_idx_in_filtered = filtered_header.index("id")
                filtered_line[id_idx_in_filtered] = sanitized_source_id

            # Use load() with single record - this handles XML ID creation automatically
            res = model.load(filtered_header, [filtered_line], context=context)

            if res.get("ids") and res["ids"][0]:
                new_id = res["ids"][0]
                id_map[sanitized_source_id] = new_id

                # Ensure XML ID is persisted (load() sometimes fails to create it)
                if sanitized_source_id and sanitized_source_id.strip():
                    _create_xmlid_entry(
                        connection, sanitized_source_id, new_id, model_name
                    )
            else:
                # Load failed - extract error message
                error_msg = "Unknown error during load"
                if res.get("messages"):
                    msg = res["messages"][0]
                    error_msg = msg.get("message", str(msg))
                failed_lines.append([*line, error_msg])

        except IndexError as e:
            error_message = f"Malformed row detected (row {i + 1} in batch): {e}"
            failed_lines.append([*line, error_message])
            if "Fell back to" in error_summary:
                error_summary = "Malformed CSV row detected"
            continue

        except Exception as load_error:
            error_str_lower = str(load_error).lower()
            source_id_str = source_id if source_id else f"row {i + 1}"

            # Special handling for Odoo server internal errors
            if (
                "tuple index out of range" in error_str_lower
                and "odoo server error" in error_str_lower
            ):
                log.warning(
                    f"Odoo server internal error detected during load for "
                    f"record {source_id_str}. This is likely a bug in the Odoo server. "
                    f"Skipping record and continuing with other records."
                )
                error_message = (
                    f"Odoo server internal error (tuple index out of range) for record "
                    f"{source_id_str}: This is likely a bug in the Odoo server. "
                    f"See server logs for details."
                )
                failed_lines.append([*line, error_message])
                continue

            # Special handling for database connection pool exhaustion errors
            if (
                "connection pool is full" in error_str_lower
                or "too many connections" in error_str_lower
                or "poolerror" in error_str_lower
            ):
                log.warning(
                    f"Database connection pool exhaustion detected during load for "
                    f"record {source_id_str}. "
                    f"Marking as failed for retry in a subsequent run."
                )
                error_message = (
                    f"Retryable error (connection pool exhaustion) for record "
                    f"{source_id_str}: {load_error}"
                )
                failed_lines.append([*line, error_message])
                continue

            # Special handling for database serialization errors
            elif (
                "could not serialize access" in error_str_lower
                or "concurrent update" in error_str_lower
            ):
                log.warning(
                    f"Database serialization conflict detected during load for "
                    f"record {source_id_str}. "
                    f"This is often caused by concurrent processes. "
                    f"Continuing with other records."
                )
                # Don't add to failed lines for retryable errors
                continue

            error_message, new_failed_line, error_summary = _handle_create_error(
                i, load_error, line, error_summary
            )
            failed_lines.append(new_failed_line)

    return {
        "id_map": id_map,
        "failed_lines": failed_lines,
        "error_summary": error_summary,
    }


# Keep old name as alias for backward compatibility
_create_batch_individually = _load_records_individually


def _load_batch_with_binary_fallback(  # noqa: C901
    model: Any,
    connection: Any,
    batch_lines: list[list[Any]],
    batch_header: list[str],
    uid_index: int,
    context: dict[str, Any],
    ignore_list: list[str],
    model_name: str,
    progress: Any = None,
    depth: int = 0,
) -> dict[str, Any]:
    """Load records using binary search to efficiently identify failing records.

    Instead of loading all records individually when a batch fails, this function
    recursively splits the batch in half and tries each half. Good records get
    imported as batches, only bad records end up being processed individually.

    For a batch of N with 1 bad record:
    - Old approach: N individual loads
    - This approach: ~log2(N) batch attempts + 1 individual = ~log2(N)+1 calls

    Args:
        model: The Odoo model object to import into.
        connection: The Odoo connection object (used for XML ID creation).
        batch_lines: The raw CSV data rows to import.
        batch_header: The column names for the data.
        uid_index: The index of the "id" column in batch_header.
        context: The Odoo context for the import.
        ignore_list: List of column names to ignore.
        model_name: The model name (for XML ID creation).
        progress: Optional progress handler for console output.
        depth: Recursion depth (used for logging control).

    Returns:
        A dict with "id_map", "failed_lines", and "success" keys.
    """
    aggregated_id_map: dict[str, int] = {}
    aggregated_failed_lines: list[list[Any]] = []
    header_len = len(batch_header)

    # Pre-validate: separate valid rows from malformed rows
    valid_lines = []
    for line in batch_lines:
        if len(line) != header_len:
            error_msg = (
                f"Malformed row: Row has {len(line)} columns, "
                f"but header has {header_len}."
            )
            aggregated_failed_lines.append([*line, error_msg])
        else:
            valid_lines.append(line)

    # If no valid lines remain, return early
    if not valid_lines:
        return {
            "id_map": aggregated_id_map,
            "failed_lines": aggregated_failed_lines,
            "success": len(aggregated_failed_lines) == 0,
        }

    # Base case: single valid record - load individually for accurate error message
    if len(valid_lines) <= 1:
        result = _load_records_individually(
            model,
            connection,
            valid_lines,
            batch_header,
            uid_index,
            context,
            ignore_list,
            model_name,
        )
        aggregated_id_map.update(result.get("id_map", {}))
        aggregated_failed_lines.extend(result.get("failed_lines", []))
        return {
            "id_map": aggregated_id_map,
            "failed_lines": aggregated_failed_lines,
            "success": len(aggregated_failed_lines) == 0,
        }

    # Prepare data for load() - filter ignored columns and sanitize IDs
    filter_indices = [i for i, h in enumerate(batch_header) if h not in ignore_list]
    load_header = [batch_header[i] for i in filter_indices]
    uid_index_in_load = (
        filter_indices.index(uid_index) if uid_index in filter_indices else -1
    )

    sanitized_load_lines = []
    for line in valid_lines:
        filtered_line = [line[i] for i in filter_indices]
        # Sanitize ID field
        if uid_index_in_load >= 0 and uid_index_in_load < len(filtered_line):
            filtered_line[uid_index_in_load] = to_xmlid(
                filtered_line[uid_index_in_load]
            )
        sanitized_load_lines.append(filtered_line)

    needs_split = False
    try:
        # Try to load the batch
        res = model.load(load_header, sanitized_load_lines, context=context)
        created_ids = res.get("ids", [])

        # Check results - handle partial success
        # Must check all valid_lines, not just created_ids length
        if created_ids:
            success_indices = []
            fail_indices = []
            for i in range(len(valid_lines)):
                if i < len(created_ids) and created_ids[i] is not None:
                    success_indices.append(i)
                    db_id = created_ids[i]
                    # Record successful import
                    if uid_index_in_load >= 0:
                        sanitized_id = sanitized_load_lines[i][uid_index_in_load]
                        if sanitized_id:
                            aggregated_id_map[sanitized_id] = db_id
                            _create_xmlid_entry(
                                connection, sanitized_id, db_id, model_name
                            )
                else:
                    fail_indices.append(i)

            if not fail_indices:
                # All valid rows succeeded
                return {
                    "id_map": aggregated_id_map,
                    "failed_lines": aggregated_failed_lines,
                    "success": len(aggregated_failed_lines) == 0,
                }

            # Partial success - only recurse on failed records
            failed_batch_lines = [valid_lines[i] for i in fail_indices]
            if len(failed_batch_lines) == 1:
                # Single failure - get accurate error via individual load
                fail_result = _load_records_individually(
                    model,
                    connection,
                    failed_batch_lines,
                    batch_header,
                    uid_index,
                    context,
                    ignore_list,
                    model_name,
                )
                aggregated_failed_lines.extend(fail_result.get("failed_lines", []))
            else:
                # Multiple failures - recurse with binary search
                fail_result = _load_batch_with_binary_fallback(
                    model,
                    connection,
                    failed_batch_lines,
                    batch_header,
                    uid_index,
                    context,
                    ignore_list,
                    model_name,
                    progress,
                    depth + 1,
                )
                aggregated_id_map.update(fail_result.get("id_map", {}))
                aggregated_failed_lines.extend(fail_result.get("failed_lines", []))

            return {
                "id_map": aggregated_id_map,
                "failed_lines": aggregated_failed_lines,
                "success": len(aggregated_failed_lines) == 0,
            }
        else:
            # No IDs returned at all - batch failed entirely
            needs_split = True

    except Exception:
        # Batch failed with exception - need to split
        needs_split = True
        if progress and depth == 0:
            progress.console.print(
                f"[yellow]INFO:[/] Batch failed, using binary search to isolate "
                f"{len(valid_lines)} records..."
            )

    if needs_split:
        # Split in half and recurse
        mid = len(valid_lines) // 2
        left_half = valid_lines[:mid]
        right_half = valid_lines[mid:]

        left_result = _load_batch_with_binary_fallback(
            model,
            connection,
            left_half,
            batch_header,
            uid_index,
            context,
            ignore_list,
            model_name,
            progress,
            depth + 1,
        )
        right_result = _load_batch_with_binary_fallback(
            model,
            connection,
            right_half,
            batch_header,
            uid_index,
            context,
            ignore_list,
            model_name,
            progress,
            depth + 1,
        )

        # Merge results
        aggregated_id_map.update(left_result.get("id_map", {}))
        aggregated_id_map.update(right_result.get("id_map", {}))
        aggregated_failed_lines.extend(left_result.get("failed_lines", []))
        aggregated_failed_lines.extend(right_result.get("failed_lines", []))

    return {
        "id_map": aggregated_id_map,
        "failed_lines": aggregated_failed_lines,
        "success": len(aggregated_failed_lines) == 0,
    }


def _execute_load_batch(  # noqa: C901
    thread_state: dict[str, Any],
    batch_lines: list[list[Any]],
    batch_header: list[str],
    batch_number: int,
) -> dict[str, Any]:
    """Executes a batch import with dynamic scaling and `create` fallback.

    This is the core worker for Pass 1. It processes a given batch of records
    by first attempting a fast `load`. If a memory or gateway-related error
    (like a 502) is detected, it automatically reduces the size of the data
    chunks it sends and retries. For other errors, it falls back to a
    record-by-record `create` for only the failed chunk.

    Args:
        thread_state (dict[str, Any]): Shared state from the orchestrator.
        batch_lines (list[list[Any]]): The list of data rows for this batch.
        batch_header (list[str]): The list of header columns for this batch.
        batch_number (int): The identifier for this batch, used for logging.

    Returns:
        dict[str, Any]: A dictionary containing the aggregated results for
        the entire batch, including `id_map` and `failed_lines`.
    """
    model, context, progress = (
        thread_state["model"],
        thread_state.get(
            "context",
            {
                "tracking_disable": True,
                "mail_create_nolog": True,
                "mail_notrack": True,
                "mail_activity_automation_skip": True,
            },
        ),
        thread_state["progress"],
    )
    connection = thread_state.get("connection")
    uid_index = thread_state["unique_id_field_index"]
    ignore_list = thread_state.get("ignore_list", [])
    model_name = thread_state.get("model_name", "")

    if thread_state.get("force_create"):
        progress.console.print(
            f"Batch {batch_number}: Fail mode active, using single-record load."
        )
        result = _load_records_individually(
            model,
            connection,
            batch_lines,
            batch_header,
            uid_index,
            context,
            ignore_list,
            model_name,
        )
        result["success"] = bool(result.get("id_map"))
        return result

    lines_to_process = list(batch_lines)
    aggregated_id_map: dict[str, int] = {}
    aggregated_failed_lines: list[list[Any]] = []
    chunk_size = len(lines_to_process)

    # Track retry attempts for serialization errors to prevent infinite retries
    serialization_retry_count = 0
    max_serialization_retries = 3  # Maximum number of retries for serialization errors

    # Pre-calculate ignore filter indices ONCE before the loop (optimization).
    # These values don't change during batch processing, so calculate upfront.
    indices_to_keep: Optional[list[int]] = None
    filtered_header: Optional[list[str]] = None
    max_index_needed = 0

    if ignore_list:
        # Normalize ignore_set to handle both 'field' and 'field/id' formats
        ignore_set = set()
        for field in ignore_list:
            if field.endswith("/id"):
                ignore_set.add(field[:-3])  # Add base name
            else:
                ignore_set.add(field)
        indices_to_keep = [
            i for i, h in enumerate(batch_header) if h.split("/")[0] not in ignore_set
        ]
        filtered_header = [batch_header[i] for i in indices_to_keep]
        max_index_needed = max(indices_to_keep) if indices_to_keep else 0

    while lines_to_process:
        current_chunk = lines_to_process[:chunk_size]

        # Apply pre-calculated filter or use original data
        if indices_to_keep is not None and filtered_header is not None:
            load_header = filtered_header
            load_lines = [
                [row[i] for i in indices_to_keep]
                for row in current_chunk
                if len(row) > max_index_needed
            ]
        else:
            load_header, load_lines = batch_header, current_chunk

        if not load_lines:
            lines_to_process = lines_to_process[chunk_size:]
            continue

        try:
            log.debug(f"Attempting `load` for chunk of batch {batch_number}...")
            log.debug(f"Load header: {load_header}")
            log.debug(f"Load lines count: {len(load_lines)}")
            if load_lines:
                first_line_preview = (
                    load_lines[0][:10] if len(load_lines[0]) > 10 else load_lines[0]
                )
                log.debug(f"First load line (first 10 fields): {first_line_preview}")
                log.debug(f"Full header: {load_header}")
                # Log the full header and first line for debugging
                if len(load_header) > 10:
                    log.debug(f"Full load_header: {load_header}")
                if len(load_lines[0]) > 10:
                    log.debug(f"Full first load_line: {load_lines[0]}")

            # Sanitize the id column values to prevent XML ID constraint
            # violations
            sanitized_load_lines = []
            for _i, line in enumerate(load_lines):
                sanitized_line = list(line)
                if uid_index < len(sanitized_line):
                    # Sanitize the source_id (which is in the id column)
                    original_id = sanitized_line[uid_index]
                    sanitized_id = to_xmlid(original_id)
                    sanitized_line[uid_index] = sanitized_id
                    if _i < 3:  # Only log first 3 lines for debugging
                        log.debug(
                            f"Sanitized ID for line {_i}: '{original_id}' -> "
                            f"'{sanitized_id}'"
                        )
                else:
                    if _i < 3:  # Only log first 3 lines for debugging
                        log.warning(
                            f"Line {_i} does not have enough columns for "
                            f"uid_index {uid_index}. "
                            f"Line has {len(line)} columns."
                        )
                sanitized_load_lines.append(sanitized_line)

            # Log sample of sanitized data without large base64 content
            log.debug(f"Load header: {load_header}")
            log.debug(f"Load lines count: {len(sanitized_load_lines)}")
            if sanitized_load_lines and len(sanitized_load_lines) > 0:
                # Show first line but truncate large base64 data
                preview_line = []
                for _i, field_value in enumerate(
                    sanitized_load_lines[0][:10]
                    if len(sanitized_load_lines[0]) > 10
                    else sanitized_load_lines[0]
                ):
                    if isinstance(field_value, str) and len(field_value) > 100:
                        # Truncate large strings (likely base64 data)
                        preview_line.append(
                            f"{field_value[:50]}...[{len(field_value) - 100} "
                            f"chars truncated]...{field_value[-50:]}"
                        )
                    else:
                        preview_line.append(field_value)
                log.debug(
                    f"First load line (first 10 fields, truncated if large): "
                    f"{preview_line}"
                )

            # Record timing for throttle controller
            load_start = time.time()
            res = model.load(load_header, sanitized_load_lines, context=context)
            load_time = time.time() - load_start

            # Record response time for health-aware throttling
            throttle_ctrl = thread_state.get("throttle_controller")
            if throttle_ctrl:
                throttle_ctrl.record_response(load_time)

            # DEBUG: Log detailed information about the load response
            log.debug(f"Load response type: {type(res)}")
            log.debug(
                f"Load response keys: "
                f"{list(res.keys()) if hasattr(res, 'keys') else 'Not a dict'}"
            )
            log.debug(f"Load response full content: {res}")

            # DEBUG: Log what we got back from Odoo
            log.debug(
                f"Load response - messages: {res.get('messages', 'None')}, "
                f"ids: {res.get('ids', 'None')}, "
                f"data: {type(res)}"
            )
            if res.get("messages"):
                for message in res["messages"]:
                    msg_type = message.get("type", "unknown")
                    msg_text = message.get("message", "")
                    log.debug(f"Load message {msg_type}: {msg_text}")
                    if msg_type in ["warning", "error"]:
                        log.warning(f"Load operation returned {msg_type}: {msg_text}")
                    else:
                        log.info(f"Load operation returned {msg_type}: {msg_text}")

            # Check for any Odoo server errors in the response that should halt
            # processing
            if res.get("messages"):
                for message in res["messages"]:
                    msg_type = message.get("type", "unknown")
                    msg_text = message.get("message", "")
                    if msg_type == "error":
                        # Only raise for actual errors, not warnings
                        log.error(f"Load operation returned fatal error: {msg_text}")
                        raise ValueError(msg_text)
                    elif msg_type in ["warning", "info"]:
                        log.warning(f"Load operation returned {msg_type}: {msg_text}")
                    else:
                        log.info(f"Load operation returned {msg_type}: {msg_text}")

            created_ids = res.get("ids", [])
            log.debug(
                f"Expected records: {len(sanitized_load_lines)}, "
                f"Created records: {len(created_ids)}"
            )

            # Always log detailed information about record creation
            if len(created_ids) != len(sanitized_load_lines):
                log.warning(
                    f"Record creation mismatch: Expected "
                    f"{len(sanitized_load_lines)} records, "
                    f"but only {len(created_ids)} were created"
                )
                if len(created_ids) == 0:
                    log.error(
                        f"No records were created in this batch of "
                        f"{len(sanitized_load_lines)}. "
                        f"This may indicate silent failures in the Odoo load "
                        f"operation. "
                        f"Check Odoo server logs for validation errors."
                    )
                    # Log the actual data being sent for debugging
                    if sanitized_load_lines:
                        log.debug("First few lines being sent:")
                        for i, line in enumerate(sanitized_load_lines[:3]):
                            log.debug(f"  Line {i}: {dict(zip(load_header, line))}")
                else:
                    log.warning(
                        f"Partial record creation: {len(created_ids)}/"
                        f"{len(sanitized_load_lines)} "
                        f"records were created. Some records may have "
                        f"failed validation."
                    )
            # Check for any Odoo server errors in the response that should
            # halt processing
            if res.get("messages"):
                for message in res["messages"]:
                    msg_type = message.get("type", "unknown")
                    msg_text = message.get("message", "")
                    if msg_type == "error":
                        # Only raise for actual errors, not warnings
                        log.error(f"Load operation returned fatal error: {msg_text}")
                        raise ValueError(msg_text)
                    elif msg_type in ["warning", "info"]:
                        log.warning(f"Load operation returned {msg_type}: {msg_text}")
                    else:
                        log.info(f"Load operation returned {msg_type}: {msg_text}")

            created_ids = res.get("ids", [])
            log.debug(
                f"Expected records: {len(sanitized_load_lines)}, "
                f"Created records: {len(created_ids)}"
            )

            # Always log detailed information about record creation
            if len(created_ids) != len(sanitized_load_lines):
                log.warning(
                    f"Record creation mismatch: Expected "
                    f"{len(sanitized_load_lines)} records, "
                    f"but only {len(created_ids)} were created"
                )
                if len(created_ids) == 0:
                    log.error(
                        f"No records were created in this batch of "
                        f"{len(sanitized_load_lines)}. "
                        f"This may indicate silent failures in the Odoo load "
                        f"operation. "
                        f"Check Odoo server logs for validation errors."
                    )
                    # Log the actual data being sent for debugging
                    if sanitized_load_lines:
                        log.debug("First few lines being sent:")
                        for i, line in enumerate(sanitized_load_lines[:3]):
                            log.debug(f"  Line {i}: {dict(zip(load_header, line))}")
                else:
                    log.warning(
                        f"Partial record creation: {len(created_ids)}/"
                        f"{len(sanitized_load_lines)} "
                        f"records were created. "
                        f"Some records may have failed validation."
                    )

            # Instead of raising an exception, capture failures for the fail file
            # But still create what records we can
            if res.get("messages"):
                # Extract error information and add to failed_lines to be
                # written to fail file
                error_msg = res["messages"][0].get("message", "Batch load failed.")
                log.error(f"Capturing load failure for fail file: {error_msg}")
                # We'll add the failed lines to aggregated_failed_lines
                # at the end

            id_map = {}
            for i, line in enumerate(current_chunk):
                # Ensure there's a corresponding created ID and that
                # it's a valid integer.
                # The 'incompatible type' error happens when the
                # value could be None.
                if i < len(created_ids) and created_ids[i] is not None:
                    sanitized_id = to_xmlid(line[uid_index])
                    db_id = created_ids[i]
                    id_map[sanitized_id] = db_id

                    # Ensure XML ID is persisted (load() sometimes fails to create it)
                    if sanitized_id and sanitized_id.strip() and connection:
                        _create_xmlid_entry(connection, sanitized_id, db_id, model_name)

            # The update call remains the same and will now be type-safe.
            aggregated_id_map.update(id_map)

            # Log id_map information for debugging
            log.debug(f"Created {len(id_map)} records in batch {batch_number}")
            if id_map:
                log.debug(f"Sample id_map entries: {dict(list(id_map.items())[:3])}")
            else:
                log.warning(f"No id_map entries created for batch {batch_number}")

            # Capture failed lines for writing to fail file
            successful_count = len(created_ids)
            total_count = len(sanitized_load_lines)

            if successful_count < total_count:
                failed_count = total_count - successful_count
                log.info(f"Capturing {failed_count} failed records for fail file")

                # Build a map of row numbers to error messages from Odoo's response
                # Odoo often includes row information in error messages
                per_row_errors = _extract_per_row_errors(res.get("messages", []))

                # Get the batch-level error message as fallback
                batch_error_msg = "Record creation failed"
                if res.get("messages"):
                    batch_error_msg = res["messages"][0].get("message", batch_error_msg)

                # If we have many failed records but only one error message,
                # fall back to individual processing for accurate error reporting
                if failed_count > 1 and not per_row_errors:
                    log.info(
                        f"Batch had {failed_count} failures with single error message. "
                        f"Falling back to individual processing for accurate errors."
                    )
                    # Get only the failed lines
                    failed_lines_to_retry = [
                        line
                        for i, line in enumerate(current_chunk)
                        if i >= len(created_ids) or created_ids[i] is None
                    ]
                    if failed_lines_to_retry:
                        fallback_result = _load_batch_with_binary_fallback(
                            model,
                            connection,
                            failed_lines_to_retry,
                            batch_header,
                            uid_index,
                            context,
                            ignore_list,
                            model_name,
                            progress,
                        )
                        # Update id_map with new successes
                        aggregated_id_map.update(fallback_result.get("id_map", {}))
                        aggregated_failed_lines.extend(
                            fallback_result.get("failed_lines", [])
                        )
                else:
                    # Add error information to the lines that failed
                    first_failed = True
                    for i, line in enumerate(current_chunk):
                        # Check if this line corresponds to a created record
                        if i >= len(created_ids) or created_ids[i] is None:
                            # Try to get a specific error for this row
                            error_msg = per_row_errors.get(i)

                            if not error_msg:
                                if first_failed:
                                    # First failed record gets the batch error
                                    error_msg = batch_error_msg
                                    first_failed = False
                                else:
                                    # Other records reference batch error
                                    truncated_msg = batch_error_msg[:100]
                                    error_msg = (
                                        f"Failed in same batch: {truncated_msg}..."
                                    )

                            failed_line = [*list(line), f"Load failed: {error_msg}"]
                            aggregated_failed_lines.append(failed_line)

            aggregated_id_map.update(id_map)
            lines_to_process = lines_to_process[chunk_size:]

            # Reset serialization retry counter on successful processing
            serialization_retry_count = 0

        except Exception as e:
            error_str = str(e)
            error_str_lower = error_str.lower()

            # Use retry module to categorize the error
            error_category, error_pattern = retry_lib.categorize_error(error_str)

            # SPECIAL CASE: Client-side timeouts for local processing
            # These should be IGNORED entirely to allow long server processing
            if (
                "timed out" == error_str_lower.strip()
                or "read timeout" in error_str_lower
                or type(e).__name__ == "ReadTimeout"
            ):
                log.debug(
                    "Ignoring client-side timeout to allow server processing "
                    "to continue"
                )
                lines_to_process = lines_to_process[chunk_size:]
                continue

            # Transient errors: retry with exponential backoff
            is_transient = error_category == retry_lib.ErrorCategory.TRANSIENT

            # Detect server overload/crash for adaptive throttling
            # Includes HTTP errors, server crashes, and empty response patterns
            server_error_patterns = (
                "502",
                "503",
                "504",
                "500",
                "service unavailable",
                "bad gateway",
                "gateway timeout",
                "internal server error",
                # Server crash indicators (empty/malformed response)
                "jsondecode",
                "json decode",
                "expecting value",
                "empty response",
                "incomplete read",
                "eof occurred",
                "connection reset",
                "connection closed",
                "broken pipe",
                "server closed connection",
            )
            is_server_overload = error_pattern in server_error_patterns

            if is_server_overload:
                # Adaptive throttling with exponential backoff.
                # Use longer delays for crash recovery (worker may need time)
                retry_attempt = thread_state.get("retry_attempt", 0) + 1
                thread_state["retry_attempt"] = retry_attempt

                # Longer backoff for server crashes (up to 120s for worker restart)
                is_likely_crash = error_pattern in (
                    "jsondecode",
                    "json decode",
                    "expecting value",
                    "empty response",
                    "connection reset",
                    "eof occurred",
                )
                if is_likely_crash:
                    backoff_config = retry_lib.RetryConfig(
                        base_delay=5.0, max_delay=120.0, exponential_base=2.0
                    )
                    error_type = "Server crash/empty response"
                else:
                    backoff_config = retry_lib.RetryConfig(
                        base_delay=1.0, max_delay=60.0, exponential_base=2.0
                    )
                    error_type = "Server overload"

                delay = retry_lib.calculate_backoff_delay(retry_attempt, backoff_config)
                progress.console.print(
                    f"[yellow]WARN:[/] {error_type} detected ({error_pattern}). "
                    f"Backing off for {delay:.1f}s (attempt {retry_attempt})."
                )
                time.sleep(delay)

            if is_transient and chunk_size > 1:
                chunk_size = max(1, chunk_size // 2)
                progress.console.print(
                    f"[yellow]WARN:[/] Batch {batch_number} hit transient error "
                    f"({error_pattern}). Reducing chunk size to {chunk_size}."
                )

                # Serialization conflicts get exponential backoff
                if error_pattern in ("could not serialize access", "deadlock"):
                    backoff_config = retry_lib.RetryConfig(
                        base_delay=0.1, max_delay=5.0, exponential_base=2.0
                    )
                    delay = retry_lib.calculate_backoff_delay(
                        serialization_retry_count + 1, backoff_config
                    )
                    progress.console.print(
                        f"[yellow]INFO:[/] Database serialization conflict. "
                        f"Waiting {delay:.2f}s before retry."
                    )
                    time.sleep(delay)

                    serialization_retry_count += 1
                    if serialization_retry_count >= max_serialization_retries:
                        progress.console.print(
                            f"[yellow]WARN:[/] Max serialization retries "
                            f"({max_serialization_retries}) reached. "
                            f"Using binary search fallback for "
                            f"{len(current_chunk)} records."
                        )
                        clean_error = error_str.strip().replace("\n", " ")
                        fallback_result = _load_batch_with_binary_fallback(
                            model,
                            connection,
                            current_chunk,
                            batch_header,
                            uid_index,
                            context,
                            ignore_list,
                            model_name,
                            progress,
                        )
                        aggregated_id_map.update(fallback_result.get("id_map", {}))
                        aggregated_failed_lines.extend(
                            fallback_result.get("failed_lines", [])
                        )
                        lines_to_process = lines_to_process[chunk_size:]
                        serialization_retry_count = 0
                        thread_state["retry_attempt"] = 0  # Reset on success
                        continue
                continue

            # For permanent/recoverable errors, get recommendation and fall back
            recommendation = retry_lib.get_retry_recommendation(error_str)
            log.debug(
                f"Error category: {error_category.value}, "
                f"recommendation: {recommendation['action']}"
            )

            clean_error = error_str.strip().replace("\n", " ")
            progress.console.print(
                f"[yellow]WARN:[/] Batch {batch_number} failed `load` "
                f"('{clean_error}'). "
                f"Using binary search fallback for {len(current_chunk)} records."
            )
            fallback_result = _load_batch_with_binary_fallback(
                model,
                connection,
                current_chunk,
                batch_header,
                uid_index,
                context,
                ignore_list,
                model_name,
                progress,
            )
            aggregated_id_map.update(fallback_result.get("id_map", {}))
            aggregated_failed_lines.extend(fallback_result.get("failed_lines", []))
            lines_to_process = lines_to_process[chunk_size:]

    return {
        "id_map": aggregated_id_map,
        "failed_lines": aggregated_failed_lines,
        "success": True,
    }


def _execute_write_batch(
    thread_state: dict[str, Any],
    batch_writes: list[tuple[list[int], dict[str, Any]]],
    batch_number: int,
) -> dict[str, Any]:
    """Executes a super-batch of write operations for Pass 2.

    This is the core worker function for Pass 2. It processes multiple write
    operations sequentially within a single thread, reducing thread overhead
    and network round-trips. Each write operation updates records with the
    same values in one RPC call.

    Includes retry logic with exponential backoff for timeout errors.

    Args:
        thread_state (dict[str, Any]): Shared state from the orchestrator,
            containing the Odoo model object.
        batch_writes (list[tuple[list[int], dict[str, Any]]]): A list of
            write operations, where each operation is a tuple of (ids, vals).
        batch_number (int): The identifier for this batch, used for logging.

    Returns:
        dict[str, Any]: A dictionary containing the results of the batch,
        with a `failed_writes` key if any operations failed.
    """
    model = thread_state["model"]
    context = thread_state.get("context", {})
    progress = thread_state.get("progress")

    all_failed_writes: list[tuple[int, dict[str, Any], str]] = []
    total_successful = 0
    max_retries = 3
    base_delay = 2.0  # Starting delay for exponential backoff

    for ids, vals in batch_writes:
        retry_count = 0
        success = False

        while retry_count <= max_retries and not success:
            try:
                model.write(ids, vals, context=context)
                total_successful += len(ids)
                success = True

            except Exception as e:
                error_str = str(e)
                error_str_lower = error_str.lower()

                # Check if this is a timeout error that should be retried
                is_timeout = (
                    "timed out" in error_str_lower
                    or "timeout" in error_str_lower
                    or "read operation timed out" in error_str_lower
                    or type(e).__name__ in ("ReadTimeout", "Timeout", "TimeoutError")
                )

                if is_timeout and retry_count < max_retries:
                    retry_count += 1
                    delay = base_delay * (2 ** (retry_count - 1))  # Exponential backoff
                    if progress:
                        progress.console.print(
                            f"[yellow]WARN:[/] Pass 2 batch {batch_number} timed out. "
                            f"Retrying in {delay:.1f}s ({retry_count}/{max_retries})..."
                        )
                    time.sleep(delay)
                    continue

                # Non-retryable error or max retries exceeded
                error_message = error_str.replace("\n", " | ")
                if is_timeout and retry_count >= max_retries:
                    error_message = (
                        f"Timeout after {max_retries} retries: {error_message}"
                    )

                # All IDs in this operation are considered failed
                for db_id in ids:
                    all_failed_writes.append((db_id, vals, error_message))
                break

    return {
        "failed_writes": all_failed_writes,
        "successful_writes": total_successful,
        "success": len(all_failed_writes) == 0,
    }


def _run_threaded_pass(  # noqa: C901
    rpc_thread: RPCThreadImport,
    target_func: Any,
    batches: Iterable[tuple[int, Any]],
    thread_state: dict[str, Any],
    batch_delay: float = 0.0,
) -> tuple[dict[str, Any], bool]:
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
        batches (Iterable[tuple[int, Any]]): An iterable that yields
            batches of data, where each item is a tuple of `(batch_number,
            batch_data)`. The type of `batch_data` can vary between passes.
        thread_state (dict[str, Any]): A dictionary of shared state to be
            passed to each worker function.
        batch_delay (float): Delay in seconds between batch submissions to
            reduce server load. Default: 0.0 (no delay).

    Returns:
        tuple[dict[str, Any], bool]: A typle and a dictionary containing
        the aggregated results from all
        worker threads, such as `id_map` and `failed_lines`.
    """
    # This logic is brittle but preserved to minimize unrelated changes.
    # It dynamically constructs arguments based on the target function name.
    # Spawn threads with optional delay between batches to reduce server load.
    futures = set()
    batch_count = 0
    throttle_ctrl = thread_state.get("throttle_controller")
    original_batch_size = thread_state.get("original_batch_size", 0)
    last_logged_batch_size: Optional[int] = None

    for num, data in batches:
        if rpc_thread.abort_flag:
            break

        # Add delay between batches (except before the first batch).
        # Use throttle controller if available, otherwise use simple delay
        if throttle_ctrl and batch_count > 0:
            # Use health-aware throttle controller
            delay = throttle_ctrl.get_delay()
            if delay > 0:
                time.sleep(delay)
        elif batch_delay > 0 and batch_count > 0:
            # Fallback to simple delay
            adaptive_throttle = thread_state.get("adaptive_throttle", 0.0)
            total_delay = batch_delay + adaptive_throttle
            if total_delay > 0:
                time.sleep(total_delay)

        # Dynamic batch size scaling based on server health
        # If throttle controller recommends smaller batches, split the current batch
        sub_batches: list[Any] = [data]
        if throttle_ctrl and original_batch_size > 0:
            recommended_size = throttle_ctrl.get_batch_size(original_batch_size)
            current_batch_len = len(data) if isinstance(data, list) else 1
            if recommended_size < current_batch_len:
                # Split the batch into smaller sub-batches
                sub_batches = list(batch(data, recommended_size))
                if last_logged_batch_size != recommended_size:
                    log.info(
                        f"Adaptive batch scaling: reducing batch size from "
                        f"{current_batch_len} to {recommended_size} "
                        f"(server health: {throttle_ctrl.current_health.name})"
                    )
                    last_logged_batch_size = recommended_size
            elif (
                last_logged_batch_size is not None
                and recommended_size >= original_batch_size
            ):
                # Log when we've recovered to full batch size
                log.info(
                    f"Adaptive batch scaling: restored to full batch size "
                    f"{original_batch_size} (server health: HEALTHY)"
                )
                last_logged_batch_size = None

        for sub_idx, sub_data in enumerate(sub_batches):
            if rpc_thread.abort_flag:  # Can be set by other threads
                break  # type: ignore[unreachable]
            # Use sub-batch number for logging if we split
            sub_num = f"{num}.{sub_idx + 1}" if len(sub_batches) > 1 else num
            args = (
                [thread_state, sub_data, sub_num]
                if target_func.__name__ == "_execute_write_batch"
                else [thread_state, sub_data, thread_state.get("batch_header"), sub_num]
            )
            futures.add(rpc_thread.spawn_thread(target_func, args))
            batch_count += 1

    aggregated: dict[str, Any] = {
        "id_map": {},
        "failed_lines": [],
        "failed_writes": [],
        "successful_writes": 0,
    }
    consecutive_failures = 0
    successful_batches = 0
    original_description = rpc_thread.progress.tasks[rpc_thread.task_id].description

    try:
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                is_successful_batch = result.get("success", False)
                if is_successful_batch:
                    successful_batches += 1
                    consecutive_failures = 0
                    # Gradually reduce adaptive throttle after successful batches
                    current_throttle = thread_state.get("adaptive_throttle", 0.0)
                    if current_throttle > 0:
                        new_throttle = max(0.0, current_throttle - 0.5)
                        thread_state["adaptive_throttle"] = new_throttle
                        if new_throttle == 0:
                            rpc_thread.progress.console.print(
                                "[green]INFO:[/green] Server recovered. "
                                "Adaptive throttle disabled."
                            )
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 50:
                        log.error(
                            f"Aborting import: Multiple "
                            f"({consecutive_failures}) consecutive batches have"
                            f" failed."
                        )
                        rpc_thread.abort_flag = True

                aggregated["id_map"].update(result.get("id_map", {}))
                aggregated["failed_writes"].extend(result.get("failed_writes", []))
                aggregated["successful_writes"] += result.get("successful_writes", 0)
                failed_lines = result.get("failed_lines", [])
                if failed_lines:
                    aggregated["failed_lines"].extend(failed_lines)
                    if rpc_thread.writer and rpc_thread.fail_handle:
                        rpc_thread.writer.writerows(failed_lines)
                        rpc_thread.fail_handle.flush()  # Force write to disk

                error_summary = result.get("error_summary")
                if error_summary:
                    pretty_error = _format_odoo_error(error_summary)
                    rpc_thread.progress.console.print(
                        f"[bold red]Batch Error:[/bold red] {pretty_error}"
                    )

                rpc_thread.progress.update(rpc_thread.task_id, advance=1)

            except Exception as e:
                log.error(f"A worker thread failed unexpectedly: {e}", exc_info=True)
                rpc_thread.abort_flag = True
                rpc_thread.progress.console.print(
                    f"[bold red]Worker Failed: {e}[/bold red]"
                )
                rpc_thread.progress.update(
                    rpc_thread.task_id,
                    description="[bold red]FAIL:[/bold red] "
                    "Worker failed unexpectedly.",
                    refresh=True,
                )
                raise
            if rpc_thread.abort_flag:
                break
    except KeyboardInterrupt:
        log.warning("Ctrl+C detected! Aborting import gracefully...")
        rpc_thread.abort_flag = True
        rpc_thread.progress.console.print("[bold yellow]Aborted by user[/bold yellow]")
        rpc_thread.progress.update(
            rpc_thread.task_id,
            description="[bold yellow]Aborted by user[/bold yellow]",
            refresh=True,
        )
    finally:
        if futures and successful_batches == 0:
            log.error("Aborting import: All processed batches failed.")
            rpc_thread.abort_flag = True
        # Use console.print instead of log.info because logging is suppressed
        # during progress display (suppress_console_handler)
        rpc_thread.progress.console.print(
            "[blue]INFO:[/blue] All batches processed, shutting down thread pool..."
        )
        rpc_thread.executor.shutdown(wait=True, cancel_futures=True)
        rpc_thread.progress.console.print(
            "[blue]INFO:[/blue] Thread pool shutdown complete"
        )
        rpc_thread.progress.update(
            rpc_thread.task_id,
            description=original_description,
            completed=rpc_thread.progress.tasks[rpc_thread.task_id].total,
        )

    return aggregated, rpc_thread.abort_flag


def _orchestrate_pass_1(
    progress: Progress,
    model_obj: Any,
    model_name: str,
    connection: Any,
    header: list[str],
    all_data: list[list[Any]],
    unique_id_field: str,
    deferred_fields: list[str],
    ignore: list[str],
    context: dict[str, Any],
    fail_writer: Optional[Any],
    fail_handle: Optional[TextIO],
    max_connection: int,
    batch_size: int,
    batch_delay: float,
    o2m: bool,
    split_by_cols: Optional[list[str]],
    force_create: bool = False,
    throttle_controller: Optional[throttle_lib.ThrottleController] = None,
) -> dict[str, Any]:
    """Orchestrates the multi-threaded Pass 1 (load/create).

    This function manages the first pass of the import process. It prepares
    the data by filtering out ignored and deferred fields, then executes the
    import in parallel using the `load` method with a `create` fallback.
    It is responsible for building the crucial ID map needed for Pass 2.

    Args:
        progress (Progress): The rich Progress instance for updating the UI.
        model_obj (Any): The connected Odoo model object used for RPC calls.
        model_name (str): The technical name of the target Odoo model.
        connection (Any): The Odoo connection object for RPC calls.
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
        fail_handle (Optional[TextIO]): The file handle for the fail file.
        max_connection (int): The number of parallel worker threads to use.
        batch_size (int): The number of records to process in each batch.
        batch_delay (float): Delay in seconds between batch submissions to
            reduce server load.
        o2m (bool): Enables one-to-many batching logic.
        force_create (bool): If True, uses single-record load instead of
            batch load. Used for fail mode to get accurate per-record errors.
        split_by_cols: The column names to group records by to avoid concurrent updates.
        throttle_controller: Optional controller for adaptive throttling based
            on server response times.

    Returns:
        dict[str, Any]: A dictionary containing the results of the pass,
            including the `id_map` ({source_id: db_id}), a list of any
            `failed_lines`, and a `success` boolean flag.
    """
    rpc_pass_1 = RPCThreadImport(
        max_connection, progress, TaskID(0), fail_writer, fail_handle
    )
    pass_1_header, pass_1_data = header, all_data
    pass_1_ignore_list = deferred_fields + ignore

    try:
        pass_1_uid_index = pass_1_header.index(unique_id_field)
    except ValueError:
        log.error(
            f"Unique ID field '{unique_id_field}' was removed by the ignore list."
        )
        return {"success": False}

    pass_1_batches = list(
        _create_batches(pass_1_data, split_by_cols, pass_1_header, batch_size, o2m)
    )
    num_batches = len(pass_1_batches)
    pass_1_task = progress.add_task(
        f"Pass 1/2: Importing to [bold]{model_name}[/bold]",
        total=num_batches,
        last_error="",
    )
    rpc_pass_1.task_id = pass_1_task

    thread_state_1 = {
        "model": model_obj,
        "model_name": model_name,
        "connection": connection,
        "context": context,
        "unique_id_field_index": pass_1_uid_index,
        "batch_header": pass_1_header,
        "force_create": force_create,
        "progress": progress,
        "ignore_list": pass_1_ignore_list,
        "throttle_controller": throttle_controller,
        "original_batch_size": batch_size,
    }

    results, aborted = _run_threaded_pass(
        rpc_pass_1, _execute_load_batch, pass_1_batches, thread_state_1, batch_delay
    )
    results["success"] = not aborted
    return results


def _orchestrate_streaming_pass_1(  # noqa: C901
    progress: Progress,
    model_obj: Any,
    model_name: str,
    connection: Any,
    file_csv: str,
    separator: str,
    encoding: str,
    skip: int,
    unique_id_field: str,
    ignore: list[str],
    context: dict[str, Any],
    fail_writer: Optional[Any],
    fail_handle: Optional[TextIO],
    max_connection: int,
    batch_size: int,
    batch_delay: float,
    total_records: int,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
) -> dict[str, Any]:
    """Orchestrates a streaming Pass 1 import without loading all data into memory.

    This function is an alternative to _orchestrate_pass_1 that uses streaming
    to process the CSV file. It reads and processes batches directly from the
    file, never loading the entire dataset into memory. This is ideal for
    large files when no grouping (o2m, split_by_cols) is required.

    Args:
        progress: The rich Progress instance for updating the UI.
        model_obj: The connected Odoo model object used for RPC calls.
        model_name: The technical name of the target Odoo model.
        connection: The Odoo connection object for RPC calls.
        file_csv: Path to the source CSV file.
        separator: The CSV delimiter character.
        encoding: The character encoding of the file.
        skip: Number of lines to skip after header.
        unique_id_field: The name of the column containing the unique source ID.
        ignore: A list of fields to ignore during import.
        context: The context dictionary for the Odoo RPC call.
        fail_writer: The CSV writer object for recording failures.
        fail_handle: The file handle for the fail file.
        max_connection: The number of parallel worker threads to use.
        batch_size: The number of records to process in each batch.
        batch_delay: Delay in seconds between batch submissions.
        total_records: Total number of records for progress display.
        max_batch_bytes: Maximum estimated payload size per batch in bytes.

    Returns:
        dict[str, Any]: A dictionary containing the results of the pass,
            including the `id_map` ({source_id: db_id}), a list of any
            `failed_lines`, and a `success` boolean flag.
    """
    rpc_pass_1 = RPCThreadImport(
        max_connection, progress, TaskID(0), fail_writer, fail_handle
    )

    # Calculate number of batches for progress display
    num_batches = (total_records + batch_size - 1) // batch_size if total_records else 1

    pass_1_task = progress.add_task(
        f"Pass 1/1: Streaming import to [bold]{model_name}[/bold]",
        total=num_batches,
        last_error="",
    )
    rpc_pass_1.task_id = pass_1_task

    # Aggregated results
    combined_id_map: dict[str, int] = {}
    combined_failed_lines: list[list[Any]] = []
    aborted = False
    header: Optional[list[str]] = None
    unique_id_field_index: Optional[int] = None

    try:
        batch_generator = _stream_csv_batches(
            file_csv, separator, encoding, skip, batch_size, ignore, max_batch_bytes
        )

        # Track cumulative row count for proper row numbering in streaming mode
        cumulative_row_count = 0

        for batch_header, batch_num, batch_data in batch_generator:
            if rpc_pass_1.abort_flag:
                aborted = True
                break

            # First batch: set up header and field index
            if header is None:
                header = batch_header
                try:
                    unique_id_field_index = header.index(unique_id_field)
                except ValueError:
                    log.error(
                        f"Unique ID field '{unique_id_field}' not found in header."
                    )
                    return {"success": False, "id_map": {}, "failed_lines": []}

            # Warn about empty id values in this batch
            _warn_empty_ids(batch_header, batch_data, start_row=cumulative_row_count)
            cumulative_row_count += len(batch_data)

            thread_state = {
                "model": model_obj,
                "model_name": model_name,
                "connection": connection,
                "context": context,
                "unique_id_field_index": unique_id_field_index,
                "batch_header": header,
                "force_create": False,
                "progress": progress,
                "ignore_list": [],  # Already filtered by streaming
            }

            # Submit batch for processing
            rpc_pass_1.spawn_thread(
                _execute_load_batch, [thread_state, batch_data, header, batch_num]
            )

            # Apply batch delay if configured
            if batch_delay > 0:
                time.sleep(batch_delay)

        # Wait for all threads to complete
        rpc_pass_1.wait()

        # Collect results from all futures
        for future in rpc_pass_1.futures:
            if future.done() and not future.cancelled():
                try:
                    result = future.result()
                    if result:
                        combined_id_map.update(result.get("id_map", {}))
                        combined_failed_lines.extend(result.get("failed_lines", []))
                        # Update progress
                        progress.advance(pass_1_task)
                except Exception as e:
                    log.error(f"Streaming batch failed: {e}")

    except FileNotFoundError:
        log.error(f"Source file not found: {file_csv}")
        return {"success": False, "id_map": {}, "failed_lines": []}
    except ValueError as e:
        log.error(str(e))
        return {"success": False, "id_map": {}, "failed_lines": []}
    except KeyboardInterrupt:
        log.warning("Import interrupted by user.")
        rpc_pass_1.abort_flag = True
        aborted = True

    return {
        "success": not aborted,
        "id_map": combined_id_map,
        "failed_lines": combined_failed_lines,
    }


def _orchestrate_pass_2(  # noqa: C901
    progress: Progress,
    model_obj: Any,
    model_name: str,
    header: list[str],
    all_data: list[list[Any]],
    unique_id_field: str,
    id_map: dict[str, int],
    deferred_fields: list[str],
    context: dict[str, Any],
    fail_writer: Optional[Any],
    fail_handle: Optional[TextIO],
    max_connection: int,
    batch_size: int,
    throttle_controller: Optional[throttle_lib.ThrottleController] = None,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
) -> tuple[bool, int]:
    """Orchestrates the multi-threaded Pass 2 (write).

    This function manages the second pass of a deferred import. It prepares
    the data for updating relational fields by using the ID map from Pass 1.
    It then groups records that have the exact same update payload and runs
    the `write` operations in parallel batches for maximum efficiency.

    Batching is controlled by both record count (batch_size) and payload size
    (max_batch_bytes). This prevents timeouts when updating records with large
    binary fields like images.

    Args:
        progress (Progress): The rich Progress instance for updating the UI.
        model_obj (Any): The connected Odoo model object.
        model_name (str): The technical name of the target Odoo model.
        header (list[str]): The header list from the original source file.
        all_data (list[list[Any]]): The full data from the original source file.
        unique_id_field (str): The name of the unique identifier column.
        id_map (dict[str, int]): The map of source IDs to database IDs from Pass 1.
        deferred_fields (list[str]): The list of fields to update in this pass.
        context (dict[str, Any]): The context dictionary for the Odoo RPC call.
        fail_writer (Optional[Any]): The CSV writer for the fail file.
        fail_handle (Optional[TextIO]): The file handle for the fail file.
        max_connection (int): The number of parallel worker threads to use.
        batch_size (int): The maximum number of records per write batch.
        throttle_controller: Optional controller for adaptive throttling based
            on server response times.
        max_batch_bytes: Maximum estimated payload size per batch in bytes.
            Defaults to 5MB. Set to 0 to disable size-based batching.

    Returns:
        bool: True if the pass completed without any critical (abort-level)
        errors, False otherwise.
    """
    unique_id_field_index = header.index(unique_id_field)
    progress.console.print(
        f"[blue]INFO:[/blue] Pass 2: Preparing data for {len(all_data)} records..."
    )
    pass_2_data_to_write = _prepare_pass_2_data(
        all_data, header, unique_id_field_index, id_map, deferred_fields, model_obj
    )
    progress.console.print(
        f"[blue]INFO:[/blue] Pass 2: {len(pass_2_data_to_write)} records have "
        f"parent references to update"
    )

    if not pass_2_data_to_write:
        progress.console.print(
            "[blue]INFO:[/blue] No valid relations found to update in Pass 2. "
            "Import complete."
        )
        return True, 0

    # --- Grouping Logic ---
    from collections import defaultdict

    def _make_hashable(val: Any) -> Any:
        """Convert lists to tuples recursively to make values hashable."""
        if isinstance(val, list):
            return tuple(_make_hashable(v) for v in val)
        elif isinstance(val, tuple):
            # Also recurse into tuples to convert nested lists
            return tuple(_make_hashable(v) for v in val)
        return val

    def _make_unhashable(val: Any) -> Any:
        """Convert tuples back to lists recursively for Odoo RPC."""
        if isinstance(val, tuple) and len(val) == 3 and val[0] == 6 and val[1] == 0:
            # This is an Odoo m2m command (6, 0, ids) - convert inner to list
            return [val[0], val[1], list(_make_unhashable(v) for v in val[2])]
        elif isinstance(val, tuple):
            return [_make_unhashable(v) for v in val]
        return val

    grouped_writes = defaultdict(list)
    for db_id, vals in pass_2_data_to_write:
        # The key must be hashable. Convert lists (e.g., m2m commands) to tuples.
        # Sort by key only (string comparison is safe) to ensure consistent ordering.
        hashable_items = tuple(
            (k, _make_hashable(vals[k])) for k in sorted(vals.keys())
        )
        grouped_writes[hashable_items].append(db_id)

    progress.console.print(
        f"[blue]INFO:[/blue] Pass 2: Grouped into {len(grouped_writes)} unique "
        f"parent values"
    )

    # --- Batching Logic ---
    # Create individual write operations first
    individual_writes: list[tuple[list[int], dict[str, Any]]] = []
    for vals_key, ids in grouped_writes.items():
        # Convert back from hashable tuple format to dict with lists
        vals = {k: _make_unhashable(v) for k, v in vals_key}
        # Chunk the list of IDs into sub-batches of the desired size.
        for id_chunk in batch(ids, batch_size):
            individual_writes.append((list(id_chunk), vals))

    if not individual_writes:
        return True, 0

    # Aggregate small writes into "super-batches" to reduce RPC overhead
    # Each super-batch contains multiple write operations that will be executed
    # sequentially by a single worker thread. This dramatically reduces the number
    # of thread spawns and network round-trips.
    #
    # Batching is controlled by both record count (batch_size) and payload size
    # (max_batch_bytes). This prevents timeouts when updating records with large
    # binary fields like images.
    pass_2_batches: list[list[tuple[list[int], dict[str, Any]]]] = []
    current_super_batch: list[tuple[list[int], dict[str, Any]]] = []
    current_record_count = 0
    current_batch_bytes = 0

    for write_op in individual_writes:
        ids, vals = write_op
        op_record_count = len(ids)
        op_size_bytes = _estimate_payload_size({"ids": ids, "vals": vals})

        # Check if adding this operation would exceed limits
        # Always include at least one operation per batch
        count_limit_exceeded = (
            current_record_count + op_record_count > batch_size and current_super_batch
        )
        size_limit_exceeded = (
            max_batch_bytes > 0
            and current_batch_bytes + op_size_bytes > max_batch_bytes
            and current_super_batch
        )

        if count_limit_exceeded or size_limit_exceeded:
            pass_2_batches.append(current_super_batch)
            current_super_batch = []
            current_record_count = 0
            current_batch_bytes = 0

        current_super_batch.append(write_op)
        current_record_count += op_record_count
        current_batch_bytes += op_size_bytes

    # Don't forget the last super-batch
    if current_super_batch:
        pass_2_batches.append(current_super_batch)

    num_batches = len(pass_2_batches)
    total_ops = len(individual_writes)
    avg_ops = total_ops / max(num_batches, 1)
    progress.console.print(
        f"[blue]INFO:[/blue] Pass 2: Aggregated {total_ops} write ops into "
        f"{num_batches} super-batches (avg {avg_ops:.1f} ops/batch)"
    )
    pass_2_task = progress.add_task(
        f"Pass 2/2: Updating [bold]{model_name}[/bold] relations",
        total=num_batches,
        last_error="",
    )
    rpc_pass_2 = RPCThreadImport(
        max_connection, progress, pass_2_task, fail_writer, fail_handle
    )
    thread_state_2 = {
        "model": model_obj,
        "progress": progress,
        "context": context,
        "throttle_controller": throttle_controller,
        "original_batch_size": batch_size,
    }
    pass_2_results, aborted = _run_threaded_pass(
        rpc_pass_2,
        _execute_write_batch,
        list(enumerate(pass_2_batches, 1)),
        thread_state_2,
    )
    progress.console.print("[blue]INFO:[/blue] Pass 2: Threaded pass complete")

    failed_writes = pass_2_results.get("failed_writes", [])
    if fail_writer and failed_writes:
        log.warning("Writing failed Pass 2 records to fail file...")
        # Import sanitization function to match id_map key format
        from .lib.internal.tools import to_xmlid

        reverse_id_map = {v: k for k, v in id_map.items()}
        # Build source_data_map using sanitized IDs to match id_map keys
        source_data_map = {
            to_xmlid(row[unique_id_field_index]): row for row in all_data
        }
        failed_lines = []
        for db_id, _, error_message in failed_writes:
            source_id = reverse_id_map.get(db_id)
            if source_id and source_id in source_data_map:
                original_row = list(source_data_map[source_id])
                original_row.append(error_message)
                failed_lines.append(original_row)
            else:
                log.debug(
                    f"Could not find source data for db_id={db_id}, "
                    f"source_id={source_id}"
                )
        if failed_lines:
            fail_writer.writerows(failed_lines)
        else:
            log.warning(
                f"Pass 2 had {len(failed_writes)} failed writes but could not "
                "map them back to source data."
            )

    # Pass 2 is successful ONLY if not aborted AND no writes failed.
    successful_writes = pass_2_results.get("successful_writes", 0)
    return not aborted and not failed_writes, successful_writes


def import_data(  # noqa: C901
    config: Union[str, dict[str, Any]],
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
    batch_delay: float = 0.0,
    skip: int = 0,
    force_create: bool = False,
    o2m: bool = False,
    split_by_cols: Optional[list[str]] = None,
    stream: bool = False,
    resume: bool = True,
    enable_checkpoint: bool = True,
    skip_unchanged: bool = False,
    skip_existing: bool = False,
    adaptive_throttle: bool = True,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
) -> tuple[bool, dict[str, int]]:
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
        config (Union[str, dict]): Path to the Odoo connection file or a dict.
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
        batch_delay (float): Delay in seconds between batch submissions to
            reduce server load. Use 0.5-2.0 for busy servers.
        max_batch_bytes (int): Maximum estimated payload size per batch in bytes.
            When a batch exceeds this size, it is split regardless of record count.
        skip (int): The number of lines to skip at the top of the source file.
        force_create (bool): If True, uses single-record load instead of
            batch load. Used for fail mode to get accurate per-record errors.
        o2m (bool): Enables special handling for one-to-many imports where
            child lines follow a parent record.
        split_by_cols: The column names to group records by to avoid concurrent updates.
        stream (bool): If True, uses streaming mode to process the CSV file
            without loading it entirely into memory. Ideal for large files.
            Not compatible with o2m, split_by_cols, or deferred_fields.
        resume (bool): If True and a checkpoint exists, resume from the last
            successful batch instead of starting over.
        enable_checkpoint (bool): If True, saves progress checkpoints to allow
            resuming interrupted imports.
        skip_unchanged (bool): If True, skips records that haven't changed
            since the last import based on content hash.
        skip_existing (bool): If True, skips records whose external ID already
            exists in Odoo. Makes imports safely re-runnable without triggering
            update errors on models like stock.quant that restrict updates.
        adaptive_throttle (bool): If True, enables health-aware throttling that
            adjusts batch size and delays based on server response times.

    Returns:
        tuple[bool, int]: True if the entire import process completed without any
        critical, process-halting errors, False otherwise.
    """
    context, deferred, ignore = (
        context
        or {
            "tracking_disable": True,
            "mail_create_nolog": True,
            "mail_notrack": True,
            "mail_activity_automation_skip": True,
        },
        deferred_fields or [],
        ignore or [],
    )

    # --- Checkpoint: Check for resumable session ---
    checkpoint: Optional[ckpt.CheckpointData] = None
    session_id = ""
    if enable_checkpoint or resume:
        session_id = ckpt.generate_session_id(file_csv, config, model)

        if resume:
            checkpoint = ckpt.load_checkpoint(file_csv, config, model)
            if checkpoint:
                batch_num = checkpoint.last_completed_batch + 1
                log.info(
                    f"Resuming from checkpoint: {checkpoint.records_processed} records "
                    f"already processed, starting from batch {batch_num}"
                )

    # Determine if streaming mode is possible
    can_stream = (
        stream and not o2m and not split_by_cols and not deferred and not force_create
    )
    if stream and not can_stream:
        log.warning(
            "Streaming mode requested but not compatible with current options. "
            "Falling back to standard mode. Streaming requires: no o2m, no groupby, "
            "no deferred fields, and no force_create."
        )

    if can_stream:
        # Use streaming mode - don't load all data into memory
        log.info("Using streaming mode for memory-efficient import.")
        record_count = _count_csv_rows(file_csv, separator, encoding, skip)
        header = None  # Will be set during streaming
    else:
        # Standard mode - load all data
        header, all_data = _read_data_file(file_csv, separator, encoding, skip)
        record_count = len(all_data)

        if not header:
            return False, {}

        # Warn about empty id values
        _warn_empty_ids(header, all_data)

    try:
        if isinstance(config, dict):
            connection = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config)
        model_obj = connection.get_model(model)
    except Exception as e:
        from .lib.internal.ui import _show_error_panel

        error_message = str(e)
        title = "Odoo Connection Error"
        friendly_message = (
            "Could not connect to Odoo. This usually means the connection "
            "details in your configuration file are incorrect.\n\n"
            "Please verify the following:\n"
            "  - [bold]hostname[/bold] is correct\n"
            "  - [bold]database[/bold] name is correct\n"
            "  - [bold]login[/bold] (username) is correct\n"
            "  - [bold]password[/bold] is correct\n\n"
            f"[bold]Original Error:[/bold] {error_message}"
        )
        _show_error_panel(title, friendly_message)
        return False, {}

    # Apply idempotent filtering if enabled (skip unchanged records)
    idempotent_stats = None
    if skip_unchanged and not can_stream and header and all_data:
        log.info("Idempotent mode: checking for unchanged records...")
        try:
            # Get the ID field index
            id_field = unique_id_field or "id"
            if id_field in header:
                id_index = header.index(id_field)
                # Extract external IDs from the data
                external_ids = [
                    str(row[id_index]).strip()
                    for row in all_data
                    if id_index < len(row) and row[id_index]
                ]

                if external_ids:
                    # Get fields to compare (exclude ignored fields)
                    compare_fields = [
                        h for h in header if h != id_field and h not in (ignore or [])
                    ]

                    # Fetch existing records from Odoo
                    existing_records = idempotent_lib.get_existing_records(
                        connection, model, external_ids, compare_fields
                    )

                    if existing_records:
                        # Filter out unchanged rows
                        original_count = len(all_data)
                        all_data, idempotent_stats = (
                            idempotent_lib.filter_unchanged_rows(
                                all_data,
                                header,
                                existing_records,
                                id_field=id_field,
                                compare_fields=compare_fields,
                            )
                        )
                        record_count = len(all_data)

                        log.info(
                            f"Idempotent filter: {original_count} -> {record_count} "
                            f"records (skipped {idempotent_stats.skipped_records} "
                            f"unchanged)"
                        )
                    else:
                        log.debug("No existing records found, all records are new")
            else:
                log.warning(
                    f"ID field '{id_field}' not found in header, "
                    "skipping idempotent filtering"
                )
        except Exception as e:
            log.warning(f"Error during idempotent filtering, continuing: {e}")

    # Apply skip_existing filtering if enabled (skip records with existing external IDs)
    skip_existing_stats: dict[str, int] = {"skipped": 0, "total": 0}
    if skip_existing and not can_stream and header and all_data:
        log.info(
            "Skip-existing mode: checking for records with existing external IDs..."
        )
        try:
            id_field = unique_id_field or "id"
            if id_field in header:
                id_index = header.index(id_field)
                original_count = len(all_data)
                skip_existing_stats["total"] = original_count

                # Extract and sanitize external IDs, grouped by module
                ids_by_module: dict[str, list[str]] = {}
                for row in all_data:
                    if id_index < len(row) and row[id_index]:
                        ext_id = to_xmlid(str(row[id_index]).strip())
                        if ext_id:
                            if "." in ext_id:
                                module, name = ext_id.split(".", 1)
                            else:
                                module, name = "__import__", ext_id
                            ids_by_module.setdefault(module, []).append(name)

                if ids_by_module:
                    # Query ir.model.data for existing external IDs
                    ir_model_data = connection.get_model("ir.model.data")
                    existing_ext_ids: set[str] = set()

                    for module, names in ids_by_module.items():
                        # Batch query: find all existing names for this module
                        found_ids = ir_model_data.search(
                            [
                                ("module", "=", module),
                                ("name", "in", names),
                                ("model", "=", model),
                            ]
                        )
                        if found_ids:
                            # Read the found records to get their full external IDs
                            found_data = ir_model_data.read(
                                found_ids, ["module", "name"]
                            )
                            for rec in found_data:
                                existing_ext_ids.add(f"{rec['module']}.{rec['name']}")

                    if existing_ext_ids:
                        # Filter out rows with existing external IDs
                        filtered_data = []
                        for row in all_data:
                            if id_index < len(row) and row[id_index]:
                                ext_id = to_xmlid(str(row[id_index]).strip())
                                if ext_id not in existing_ext_ids:
                                    filtered_data.append(row)
                            else:
                                filtered_data.append(row)

                        skipped_count = original_count - len(filtered_data)
                        skip_existing_stats["skipped"] = skipped_count
                        all_data = filtered_data

                        new_count = len(all_data)
                        log.info(
                            f"Skip-existing: {original_count} -> {new_count} records "
                            f"(skipped {skipped_count} with existing external IDs)"
                        )

                        if skipped_count > 0:
                            # Log a few examples of skipped IDs
                            example_ids = list(existing_ext_ids)[:5]
                            log.info(
                                f"Example skipped external IDs: {example_ids}"
                                + (
                                    f" ... and {len(existing_ext_ids) - 5} more"
                                    if len(existing_ext_ids) > 5
                                    else ""
                                )
                            )
                    else:
                        log.debug("No existing external IDs found, all records are new")
            else:
                log.warning(
                    f"ID field '{id_field}' not found in header, "
                    "skipping skip-existing filtering"
                )
        except Exception as e:
            log.warning(f"Error during skip-existing filtering, continuing: {e}")

    # For streaming mode, we defer fail file setup (header not known yet)
    # For standard mode, set up fail file now
    fail_writer, fail_handle = None, None
    if not can_stream and fail_file and header is not None:
        fail_writer, fail_handle = _setup_fail_file(
            fail_file, header, separator, encoding
        )

    # Create throttle controller for adaptive throttling
    throttle_controller = None
    if adaptive_throttle:
        throttle_controller = throttle_lib.create_throttle_controller(
            base_delay=batch_delay
        )
        log.info("Adaptive throttle enabled: will adjust delays based on server health")

    console = Console()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        "•",
        TextColumn("[green]{task.completed} of {task.total} batches"),
        "•",
        TimeElapsedColumn(),
        console=console,
        expand=True,
    )

    overall_success = False
    with suppress_console_handler(), progress:
        try:
            if can_stream:
                # Use streaming mode - process batches directly from file
                pass_1_results = _orchestrate_streaming_pass_1(
                    progress,
                    model_obj,
                    model,
                    connection,
                    file_csv,
                    separator,
                    encoding,
                    skip,
                    unique_id_field,
                    ignore,
                    context,
                    fail_writer,
                    fail_handle,
                    max_connection,
                    batch_size,
                    batch_delay,
                    record_count,
                    max_batch_bytes,
                )
                # Streaming mode doesn't support Pass 2
                pass_2_successful = True
                updates_made = 0
            else:
                # --- Checkpoint: Check if Pass 1 was already completed ---
                if checkpoint and checkpoint.pass_1_complete:
                    log.info(
                        f"Pass 1 already completed in previous run. "
                        f"Restoring {len(checkpoint.id_map)} ID mappings."
                    )
                    pass_1_results = {
                        "success": True,
                        "id_map": {k: int(v) for k, v in checkpoint.id_map.items()},
                    }
                elif header is not None and all_data is not None:
                    # Standard mode - use pre-loaded data
                    pass_1_results = _orchestrate_pass_1(
                        progress,
                        model_obj,
                        model,
                        connection,
                        header,
                        all_data,
                        unique_id_field,
                        deferred,
                        ignore,
                        context,
                        fail_writer,
                        fail_handle,
                        max_connection,
                        batch_size,
                        batch_delay,
                        o2m,
                        split_by_cols,
                        force_create,
                        throttle_controller,
                    )

            # A pass is only successful if it wasn't aborted.
            pass_1_successful = pass_1_results.get("success", False)
            if not pass_1_successful:
                return False, {}

            # If we get here, Pass 1 was not aborted. Now determine final status.
            id_map = pass_1_results.get("id_map", {})
            # Use console.print - log.info is suppressed during progress display
            progress.console.print(
                f"[blue]INFO:[/blue] Pass 1 complete: {len(id_map)} records created"
            )

            # --- Checkpoint: Save after Pass 1 completes ---
            if enable_checkpoint and session_id and not can_stream:
                progress.console.print(
                    "[blue]INFO:[/blue] Saving checkpoint after Pass 1..."
                )
                file_hash = ckpt._compute_file_hash(file_csv)
                new_checkpoint = ckpt.CheckpointData(
                    session_id=session_id,
                    file_path=file_csv,
                    file_hash=file_hash,
                    model=model,
                    config_hash=ckpt._compute_config_hash(config),
                    last_completed_batch=0,  # Not tracking batch-level
                    total_batches=0,
                    records_processed=len(id_map),
                    records_created=len(id_map),
                    records_failed=0,
                    id_map={k: v for k, v in id_map.items()},
                    deferred_fields=deferred,
                    pass_1_complete=True,
                    pass_2_complete=False,
                )
                ckpt.save_checkpoint(new_checkpoint)
                progress.console.print(
                    f"[blue]INFO:[/blue] Checkpoint saved: {len(id_map)} records"
                )

            if not can_stream:
                pass_2_successful = True  # Assume success if no Pass 2 is needed.
                updates_made = 0

                if deferred and header is not None and all_data is not None:
                    progress.console.print(
                        f"[blue]INFO:[/blue] Starting Pass 2 for deferred fields: "
                        f"{deferred}"
                    )
                    pass_2_successful, updates_made = _orchestrate_pass_2(
                        progress,
                        model_obj,
                        model,
                        header,
                        all_data,
                        unique_id_field,
                        id_map,
                        deferred,
                        context,
                        fail_writer,
                        fail_handle,
                        max_connection,
                        batch_size,
                        throttle_controller,
                        max_batch_bytes,
                    )

        finally:
            if fail_handle:
                fail_handle.close()

    overall_success = pass_1_successful and pass_2_successful
    stats = {
        "total_records": record_count,
        "created_records": len(id_map),
        "updated_relations": updates_made,
        "id_map": id_map,
    }

    # Add idempotent stats if available
    if idempotent_stats:
        stats["skipped_unchanged"] = idempotent_stats.skipped_records
        stats["new_records"] = idempotent_stats.new_records
        stats["changed_records"] = idempotent_stats.changed_records

    # Add throttle stats if available
    if throttle_controller:
        throttle_stats = throttle_controller.stats
        stats["throttle_stats"] = {
            "total_delay_added": throttle_stats.total_delay_added,
            "batch_size_reductions": throttle_stats.batch_size_reductions,
            "health_recoveries": throttle_stats.health_recoveries,
            "avg_response_time": throttle_stats.avg_response_time,
        }
        if throttle_stats.total_delay_added > 0:
            delay = throttle_stats.total_delay_added
            recoveries = throttle_stats.health_recoveries
            log.info(f"Throttle summary: {delay:.1f}s delay, {recoveries} recoveries")

    # --- Checkpoint: Clean up on success ---
    if overall_success and enable_checkpoint and session_id:
        ckpt.delete_checkpoint(file_csv, session_id)
        log.debug("Import completed successfully, checkpoint deleted.")

    return overall_success, stats
