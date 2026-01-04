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
) -> Generator[tuple[list[str], int, list[list[Any]]], None, None]:
    """Streams CSV data in batches without loading the entire file into memory.

    This generator opens the CSV file and yields batches of rows along with
    the header. It is memory-efficient for large files as it only keeps
    one batch in memory at a time.

    Args:
        file_path: The full path to the source CSV file.
        separator: The delimiter character used to separate columns.
        encoding: The character encoding of the file.
        skip: The number of lines to skip at the top of the file.
        batch_size: The number of records to include in each batch.
        ignore: A list of column names to ignore during import.

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
        batch_number = 0

        for row in reader:
            # Apply column filtering if needed
            if indices_to_keep is not None:
                if len(row) < max(indices_to_keep) + 1:
                    # Skip malformed rows
                    continue
                row = [row[i] for i in indices_to_keep]

            current_batch.append(row)

            if len(current_batch) >= batch_size:
                batch_number += 1
                yield filtered_header, batch_number, current_batch
                current_batch = []

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
    deferred_field_indices: dict[str, tuple[int, bool]] = {}
    for i, column_name in enumerate(header):
        field_base_name = column_name.split("/")[0]
        if field_base_name in deferred_fields_normalized:
            # Store (index, is_external_id_column)
            is_ext_id_col = column_name.endswith("/id")
            deferred_field_indices[field_base_name] = (i, is_ext_id_col)

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

    print(f"  [Pass 2] ir.model.data proxy: {'found' if ir_model_data_proxy else 'not found'}")
    print(f"  [Pass 2] Processing {len(all_data)} records...")

    # Import the sanitization function to match id_map key format
    from .lib.internal.tools import to_xmlid

    processed = 0
    for row in all_data:
        processed += 1
        if processed % 1000 == 0:
            print(f"  [Pass 2] Processed {processed}/{len(all_data)} records...")
        source_id = row[unique_id_field_index]
        # Sanitize source_id to match id_map key format
        sanitized_source_id = to_xmlid(source_id) if source_id else source_id
        db_id = id_map.get(sanitized_source_id)
        if not db_id:
            continue

        update_vals = {}
        # Use the pre-calculated map to find the values to write.
        for field_name, (field_index, is_ext_id_col) in deferred_field_indices.items():
            if field_index < len(row):
                field_value = row[field_index]
                if field_value:  # Ensure there is a value
                    # First, always try id_map lookup (for self-referencing fields)
                    # Sanitize field_value to match id_map key format
                    sanitized_field_value = to_xmlid(field_value)
                    related_db_id = id_map.get(sanitized_field_value)

                    if related_db_id:
                        # Value found in id_map - use the database ID
                        update_vals[field_name] = related_db_id
                        log.debug(
                            f"Resolved self-reference '{field_name}': "
                            f"'{field_value}' -> db_id {related_db_id}"
                        )
                    elif is_ext_id_col:
                        # External ID column (e.g., responsible_id/id)
                        # Try XML-ID resolution for non-self-referencing fields
                        if ir_model_data_proxy:
                            resolved_id = _resolve_external_id_for_pass2(
                                ir_model_data_proxy, field_value
                            )
                            if resolved_id:
                                update_vals[field_name] = resolved_id
                                log.debug(
                                    f"Resolved external ID '{field_name}': "
                                    f"'{field_value}' -> db_id {resolved_id}"
                                )
                            else:
                                log.warning(
                                    f"Missing reference for '{field_name}': "
                                    f"'{field_value}' not found in id_map or ir.model.data "
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

    print(f"  [Pass 2] Data preparation complete: {len(pass_2_data_to_write)} records to update")
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


def _extract_access_error_message(error_str: str) -> str:
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
        return f"Access denied: insufficient permissions to access '{remote_match.group(1)}'"

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
        if "Fell back to create" in error_summary:
            error_summary = "Access denied - check user permissions"

    # Handle constraint violation errors (e.g., XML ID space constraint)
    elif (
        "constraint" in error_str_lower
        or "check constraint" in error_str_lower
        or "nospaces" in error_str_lower
        or "violation" in error_str_lower
    ):
        error_message = f"Constraint violation in row {i + 1}: {create_error}"
        if "Fell back to create" in error_summary:
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
        if "Fell back to create" in error_summary:
            error_summary = "Database connection pool exhaustion detected"
    # Handle specific database serialization errors
    elif (
        "could not serialize access" in error_str_lower
        or "concurrent update" in error_str_lower
    ):
        error_message = f"Database serialization error in row {i + 1}: {create_error}"
        if "Fell back to create" in error_summary:
            error_summary = "Database serialization conflict detected during create"
    elif (
        "tuple index out of range" in error_str_lower or "indexerror" in error_str_lower
    ):
        error_message = f"Tuple unpacking error in row {i + 1}: {create_error}"
        if "Fell back to create" in error_summary:
            error_summary = "Tuple unpacking error detected"
    else:
        error_message = error_str.replace("\n", " | ")
        if "invalid field" in error_str_lower and "/id" in error_str_lower:
            error_message = (
                f"Invalid external ID field detected in row {i + 1}: {error_message}"
            )

        if "Fell back to create" in error_summary:
            error_summary = error_message

    failed_line = [*line, error_message]
    return error_message, failed_line, error_summary


def _create_xmlid_entry(
    connection: Any,
    xml_id: str,
    res_id: int,
    model_name: str,
) -> bool:
    """Create an ir.model.data entry for a record created via create().

    When records are created using Odoo's create() method instead of load(),
    the XML ID is not automatically persisted. This function creates the
    ir.model.data entry to ensure the XML ID is saved.

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
                ir_model_data.write(existing_ids[0], {"res_id": res_id, "model": model_name})
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


def _create_batch_individually(  # noqa: C901
    model: Any,
    connection: Any,
    batch_lines: list[list[Any]],
    batch_header: list[str],
    uid_index: int,
    context: dict[str, Any],
    ignore_list: list[str],
    model_name: str = "",
) -> dict[str, Any]:
    """Fallback to create records one-by-one to get detailed errors."""
    id_map: dict[str, int] = {}
    failed_lines: list[list[Any]] = []
    error_summary = "Fell back to create"
    header_len = len(batch_header)
    ignore_set = set(ignore_list)

    # Get ir.model.data once for the whole batch (used for looking up existing records)
    ir_model_data = connection.get_model("ir.model.data")

    for i, line in enumerate(batch_lines):
        try:
            if len(line) != header_len:
                raise IndexError(
                    f"Row has {len(line)} columns, but header has {header_len}."
                )

            source_id = line[uid_index]
            # Sanitize source_id to ensure it's a valid XML ID
            from .lib.internal.tools import to_xmlid

            sanitized_source_id = to_xmlid(source_id)

            # 1. SEARCH BEFORE CREATE
            # Use ir.model.data to look up existing record by external ID
            # This avoids model.browse() which may not be allowed for some models
            existing_ids = ir_model_data.search(
                [
                    ("module", "=", "__export__"),
                    ("name", "=", sanitized_source_id),
                ],
                limit=1,
            )

            if existing_ids:
                existing = ir_model_data.read(existing_ids[0], ["res_id"])
                if existing and existing.get("res_id"):
                    id_map[sanitized_source_id] = existing["res_id"]
                    continue

            # 2. PREPARE FOR CREATE
            vals = dict(zip(batch_header, line))
            clean_vals = {
                k: v
                for k, v in vals.items()
                if k.split("/")[0] not in ignore_set
                # Allow external ID fields through for conversion
            }

            # 3. CREATE
            # Convert external ID references to actual database IDs before creating
            converted_vals, external_id_fields = _process_external_id_fields(
                connection, clean_vals
            )

            log.debug(f"External ID fields found: {external_id_fields}")
            log.debug(f"Converted vals keys: {list(converted_vals.keys())}")

            new_record = model.create(converted_vals, context=context)
            # Handle both cases: create() returns either an int ID or a record object
            # Accessing .id on a record object can trigger browse() which may fail
            new_id = new_record if isinstance(new_record, int) else int(new_record)
            id_map[sanitized_source_id] = new_id

            # Create ir.model.data entry for XML ID since create() doesn't do it
            if model_name:
                _create_xmlid_entry(
                    connection, sanitized_source_id, new_id, model_name
                )
        except IndexError as e:
            error_message = f"Malformed row detected (row {i + 1} in batch): {e}"
            failed_lines.append([*line, error_message])
            if "Fell back to create" in error_summary:
                error_summary = "Malformed CSV row detected"
            continue
        except Exception as create_error:
            error_str_lower = str(create_error).lower()

            # Special handling for Odoo server internal errors
            if (
                "tuple index out of range" in error_str_lower
                and "odoo server error" in error_str_lower
            ):
                log.warning(
                    f"Odoo server internal error detected during create for "
                    f"record {source_id}. This is likely a bug in the Odoo server. "
                    f"Skipping record and continuing with other records."
                )
                error_message = (
                    f"Odoo server internal error (tuple index out of range) for record "
                    f"{source_id}: This is likely a bug in the Odoo server. "
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
                # These are retryable errors
                # - log and add to failed lines for a later run.
                log.warning(
                    f"Database connection pool exhaustion detected during create for "
                    f"record {source_id}. "
                    f"Marking as failed for retry in a subsequent run."
                )
                error_message = (
                    f"Retryable error (connection pool exhaustion) for record "
                    f"{source_id}: {create_error}"
                )
                failed_lines.append([*line, error_message])
                continue

            # Special handling for database serialization errors in create operations
            elif (
                "could not serialize access" in error_str_lower
                or "concurrent update" in error_str_lower
            ):
                # These are retryable errors - log and continue processing other records
                log.warning(
                    f"Database serialization conflict detected during create for "
                    f"record {source_id}. "
                    f"This is often caused by concurrent processes. "
                    f"Continuing with other records."
                )
                # Don't add to failed lines for retryable errors
                # - let the record be processed in next batch
                continue

            error_message, new_failed_line, error_summary = _handle_create_error(
                i, create_error, line, error_summary
            )
            failed_lines.append(new_failed_line)
    return {
        "id_map": id_map,
        "failed_lines": failed_lines,
        "error_summary": error_summary,
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
        thread_state.get("context", {"tracking_disable": True}),
        thread_state["progress"],
    )
    connection = thread_state.get("connection")
    uid_index = thread_state["unique_id_field_index"]
    ignore_list = thread_state.get("ignore_list", [])
    model_name = thread_state.get("model_name", "")

    if thread_state.get("force_create"):
        progress.console.print(
            f"Batch {batch_number}: Fail mode active, using `create` method."
        )
        result = _create_batch_individually(
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
                        fallback_result = _create_batch_individually(
                            model,
                            connection,
                            failed_lines_to_retry,
                            batch_header,
                            uid_index,
                            context,
                            ignore_list,
                            model_name,
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

            # Detect server overload for adaptive throttling
            is_server_overload = error_pattern in (
                "502",
                "503",
                "service unavailable",
                "bad gateway",
            )

            if is_server_overload:
                # Adaptive throttling with exponential backoff
                retry_attempt = thread_state.get("retry_attempt", 0) + 1
                thread_state["retry_attempt"] = retry_attempt
                backoff_config = retry_lib.RetryConfig(
                    base_delay=1.0, max_delay=30.0, exponential_base=2.0
                )
                delay = retry_lib.calculate_backoff_delay(retry_attempt, backoff_config)
                progress.console.print(
                    f"[yellow]WARN:[/] Server overload detected ({error_pattern}). "
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
                            f"Falling back to individual processing."
                        )
                        clean_error = error_str.strip().replace("\n", " ")
                        fallback_result = _create_batch_individually(
                            model,
                            connection,
                            current_chunk,
                            batch_header,
                            uid_index,
                            context,
                            ignore_list,
                            model_name,
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
                f"Falling back to `create` for {len(current_chunk)} records."
            )
            fallback_result = _create_batch_individually(
                model,
                connection,
                current_chunk,
                batch_header,
                uid_index,
                context,
                ignore_list,
                model_name,
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
    batch_writes: tuple[list[int], dict[str, Any]],
    batch_number: int,
) -> dict[str, Any]:
    """Executes a batch of write operations for a group of records.

    This is the core worker function for Pass 2. It takes a list of database
    IDs and a single dictionary of values and updates all records in one RPC call.

    Args:
        thread_state (dict[str, Any]): Shared state from the orchestrator,
            containing the Odoo model object.
        batch_writes (tuple[list[int], dict[str, Any]]): A tuple containing
            the list of database IDs and the dictionary of values to write.
        batch_number (int): The identifier for this batch, used for logging.

    Returns:
        dict[str, Any]: A dictionary containing the results of the batch,
        with a `failed_writes` key if the operation failed.
    """
    model = thread_state["model"]
    context = thread_state.get("context", {})  # Get context
    ids, vals = batch_writes
    try:
        # The core of the fix: use model.write(ids, vals) for batch updates.
        model.write(ids, vals, context=context)
        return {
            "failed_writes": [],
            "successful_writes": len(ids),
            "success": True,
        }
    except Exception as e:
        error_message = str(e).replace("\n", " | ")
        # If the batch fails, all IDs in it are considered failed.
        failed_writes = [(db_id, vals, error_message) for db_id in ids]
        return {
            "failed_writes": failed_writes,
            "error_summary": error_message,
            "successful_writes": 0,
            "success": False,
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
        force_create (bool): If True, bypasses the `load` method and uses
            the `create` method directly. Used for fail mode.
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
            file_csv, separator, encoding, skip, batch_size, ignore
        )

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


def _orchestrate_pass_2(
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
) -> tuple[bool, int]:
    """Orchestrates the multi-threaded Pass 2 (write).

    This function manages the second pass of a deferred import. It prepares
    the data for updating relational fields by using the ID map from Pass 1.
    It then groups records that have the exact same update payload and runs
    the `write` operations in parallel batches for maximum efficiency.

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
        batch_size (int): The number of records per write batch.
        throttle_controller: Optional controller for adaptive throttling based
            on server response times.

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

    grouped_writes = defaultdict(list)
    for db_id, vals in pass_2_data_to_write:
        # The key must be hashable, so we convert the dict to a frozenset of items.
        vals_key = frozenset(vals.items())
        grouped_writes[vals_key].append(db_id)

    progress.console.print(
        f"[blue]INFO:[/blue] Pass 2: Grouped into {len(grouped_writes)} unique "
        f"parent values"
    )

    # --- Batching Logic ---
    pass_2_batches = []
    for vals_key, ids in grouped_writes.items():
        vals = dict(vals_key)
        # Chunk the list of IDs into sub-batches of the desired size.
        for id_chunk in batch(ids, batch_size):
            pass_2_batches.append((list(id_chunk), vals))

    if not pass_2_batches:
        return True, 0

    num_batches = len(pass_2_batches)
    progress.console.print(
        f"[blue]INFO:[/blue] Pass 2: Starting {num_batches} batches..."
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
    progress.console.print(
        f"[blue]INFO:[/blue] Pass 2: Threaded pass complete"
    )

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
    adaptive_throttle: bool = False,
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
        skip (int): The number of lines to skip at the top of the source file.
        force_create (bool): If True, bypasses the `load` method and uses
            the `create` method directly. Used for fail mode.
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
        adaptive_throttle (bool): If True, enables health-aware throttling that
            adjusts batch size and delays based on server response times.

    Returns:
        tuple[bool, int]: True if the entire import process completed without any
        critical, process-halting errors, False otherwise.
    """
    context, deferred, ignore = (
        context or {"tracking_disable": True},
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
