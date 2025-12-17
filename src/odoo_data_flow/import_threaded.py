"""Import thread.

This module contains the low-level, multi-threaded logic for importing
data into an Odoo instance.
"""

import ast
import concurrent.futures
import csv
import os
import sys
import time
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

from .constants import DEFAULT_TRACKING_CONTEXT
from .lib import conf_lib
from .lib.internal.rpc_thread import RpcThread
from .lib.internal.tools import batch, to_xmlid
from .logging_config import log

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**30)


# --- Configuration ---
# Common placeholder values that often represent empty/invalid data
COMMON_PLACEHOLDER_VALUES = frozenset(
    [
        "invalid_text",
        "invalid",
        "missing",
        "unknown",
        "blank",
        "empty",
        "null",
        "bad_value",
        "invalid_input",
    ]
)

# Common patterns that indicate external ID errors
EXTERNAL_ID_ERROR_PATTERNS = frozenset(
    [
        "external id",
        "reference",
        "does not exist",
        "no matching record",
        "res_id not found",
        "xml id",
        "invalid reference",
        "unknown external id",
        "missing record",
        "referenced record",
        "not found",
        "lookup failed",
        "product_template.",
        "res_partner.",
        "account_account.",  # Common module prefixes
    ]
)

# Common patterns that indicate tuple index errors
TUPLE_INDEX_ERROR_PATTERNS = frozenset(
    [
        "tuple index out of range",
        "does not seem to be an integer",
    ]
)


def _is_client_timeout_error(error: Exception) -> bool:
    """Check if the error is a client-side timeout that should be ignored.

    Args:
        error: The exception to check

    Returns:
        True if this is a client-side timeout error that should be ignored
    """
    error_str = str(error).lower().strip()
    return (
        error_str == "timed out"
        or "read timeout" in error_str
        or type(error).__name__ == "ReadTimeout"
    )


def _is_database_connection_error(error: Exception) -> bool:
    """Check if the error is a database connection pool exhaustion error.

    Args:
        error: The exception to check

    Returns:
        True if this is a database connection error that should be
        handled by scaling back
    """
    error_str = str(error).lower()
    return (
        "connection pool is full" in error_str
        or "too many connections" in error_str
        or "poolerror" in error_str
    )


def _is_tuple_index_error(error: Exception) -> bool:
    """Check if the error is a tuple index out of range error that indicates
    data type issues.

    Args:
        error: The exception to check

    Returns:
        True if this is a tuple index error that suggests data type problems
    """
    error_str = str(error).lower()
    return (
        any(pattern in error_str for pattern in TUPLE_INDEX_ERROR_PATTERNS)
        or "indexerror" in error_str
        or ("does not seem to be an integer" in error_str and "for field" in error_str)
    )


def _is_external_id_error(error: Exception, line_content: Optional[str] = None) -> bool:
    """Check if the error is related to external ID resolution issues.

    Args:
        error: The exception to check
        line_content: Optional content of the data line for additional context

    Returns:
        True if this appears to be an external ID resolution error
    """
    error_str = str(error).lower()

    # Check if error message contains external ID patterns
    if any(pattern in error_str for pattern in EXTERNAL_ID_ERROR_PATTERNS):
        return True

    # If we have line content, also check for external ID patterns there
    if line_content:
        line_str = line_content.lower()
        return any(pattern in line_str for pattern in EXTERNAL_ID_ERROR_PATTERNS)

    return False


# --- Helper Functions ---
def _sanitize_error_message(error_msg: Union[str, None]) -> str:
    """Sanitizes error messages to ensure they are safe for CSV output.

    Args:
        error_msg: The raw error message string

    Returns:
        A sanitized error message that is safe for CSV output
    """
    if error_msg is None:
        return ""

    error_msg = str(error_msg)

    # Replace newlines with a safe alternative
    error_msg = error_msg.replace("\n", " | ").replace("\r", " | ")

    # Replace tabs with spaces
    error_msg = error_msg.replace("\t", " ")

    error_msg = error_msg.replace('"""', '"""')

    return error_msg
def _extract_clean_error_message(error: Exception) -> str:
    """Extracts a clean error message from an exception object.

    Args:
        error: The exception object

    Returns:
        A clean error message string
    """
    error_msg = str(error)
    
    # Try to extract meaningful error message from Odoo's error format
    # Odoo errors often come in formats like:
    # - odoo.exceptions.ValidationError: ('Error message', 'Details')
    # - {"data": {"message": "Actual error message"}}
    # - Regular string errors
    # - Direct Odoo server messages like "The values for the fields... already exist"
    
    # First, check if this is already a clean Odoo server message
    # These messages typically contain specific patterns that indicate they're
    # direct from Odoo and don't need further processing
    odoo_server_message_patterns = [
        "The values for the fields",
        "already exist",
        "No matching record found",
        "The field",
        "is required",
        "does not exist",
        "cannot be empty",
        "must be unique",
        "invalid value for field",
        "constraint violation",
        "external id",
    ]
    
    if any(pattern in error_msg for pattern in odoo_server_message_patterns):
        # This appears to be a direct Odoo server message, preserve it as-is
        # after basic sanitization for CSV safety
        error_msg = _sanitize_error_message(error_msg)
        return error_msg
    
    try:
        # Try to parse as JSON/dict format first
        import ast
        error_dict = ast.literal_eval(error_msg)
        if isinstance(error_dict, dict):
            if error_dict.get("data") and error_dict["data"].get("message"):
                error_msg = str(error_dict["data"]["message"])
            elif error_dict.get("message"):
                error_msg = str(error_dict["message"])
        elif isinstance(error_dict, (list, tuple)) and len(error_dict) > 0:
            # Handle tuple/list format like ('Error message', 'Details')
            # Extract just the first element which contains the main error message
            error_msg = str(error_dict[0])
    except (ValueError, SyntaxError):
        # Not a parseable format, try to extract from common Odoo patterns
        # Handle patterns like: odoo.exceptions.ValidationError: ('message', 'details')
        if ": ('" in error_msg or ": (\"" in error_msg:
            # Extract the content between the first set of quotes after the colon
            import re
            # Handle both: 'text' and: ('text', patterns
            match = re.search(r":\s*\(['\"]([^'\"]+)['\"]", error_msg)
            if match:
                error_msg = match.group(1)
            else:
                # Try without parenthesis for other formats
                match = re.search(r":\s*['\"]([^'\"]+)['\"]", error_msg)
                if match:
                    error_msg = match.group(1)
        else:
            # Try to extract tuple content if it exists
            # Look for patterns like ('message', 'details')
            import re
            tuple_match = re.search(r"\('([^']+)'", error_msg)
            if tuple_match:
                error_msg = tuple_match.group(1)
            else:
                # Try double quotes version
                tuple_match = re.search(r'"\(([^"]+)",\s*"([^"]+)"\)', error_msg)
                if tuple_match:
                    error_msg = tuple_match.group(1)
    
    # Clean up common Odoo error message patterns
    # Remove exception type prefixes like "odoo.exceptions.ValidationError: "
    error_msg = error_msg.replace("odoo.exceptions.ValidationError: ", "")
    error_msg = error_msg.replace("odoo.exceptions.UserError: ", "")
    error_msg = error_msg.replace("odoo.exceptions.AccessError: ", "")
    error_msg = error_msg.replace("odoo.exceptions.MissingError: ", "")
    error_msg = error_msg.replace("odoo.exceptions.Except_Odoo: ", "")
    
    # Remove common wrapper text - handle both single and double quotes
    error_msg = error_msg.replace("('", "").replace("',)", "")
    error_msg = error_msg.replace('("', '').replace('",)', '')
    error_msg = error_msg.replace("('", "").replace("')", "")
    error_msg = error_msg.replace('("', '').replace('")', '')
    
    # Remove trailing tuple/formatting characters
    if error_msg.endswith(",'"):
        error_msg = error_msg[:-2]
    if error_msg.endswith(",\""):
        error_msg = error_msg[:-2]
    if error_msg.endswith(",)"):
        error_msg = error_msg[:-2]
    
    # Replace newlines with a safe alternative to prevent CSV parsing issues
    error_msg = error_msg.replace("\n", " | ").replace("\r", " | ")

    # Replace tabs with spaces
    error_msg = error_msg.replace("\t", " ")

    # Properly escape quotes for CSV (double the quotes)
    # This is important for CSV format when QUOTE_ALL is used
    error_msg = error_msg.replace('"', '""')

    # Remove or replace other potentially problematic characters that might
    # interfere with CSV parsing, especially semicolons that can cause column splitting
    # Note: Even with QUOTE_ALL, some combinations of characters might still cause issues
    # when error messages are combined from multiple sources
    error_msg = error_msg.replace(";", ":")

    # Remove other potentially problematic control characters
    # that might interfere with CSV parsing
    for char in [
        "\x00",
        "\x01",
        "\x02",
        "\x03",
        "\x04",
        "\x05",
        "\x06",
        "\x07",
        "\x08",
        "\x0b",
        "\x0c",
        "\x0e",
        "\x0f",
        "\x10",
        "\x11",
        "\x12",
        "\x13",
        "\x14",
        "\x15",
        "\x16",
        "\x17",
        "\x18",
        "\x19",
        "\x1a",
        "\x1b",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x1f",
        "\x7f",
    ]:
        error_msg = error_msg.replace(char, " ")

    # Additional protection against malformed concatenated error messages
    # that might contain phrases like "second cell" which might be typos from
    # "second cell" in JSON parsing errors
    error_msg = error_msg.replace("sencond", "second")

    # Additional sanitization for JSON characters that might interfere with CSV structure
    # Replace characters that might be interpreted as field separators
    error_msg = error_msg.replace("{", " { ").replace(
        "}", " } "
    )  # Add spaces around braces
    error_msg = error_msg.replace("[", " [ ").replace(
        "]", " ] "
    )  # Add spaces around brackets
    # More comprehensive sanitization for potential CSV structure issues
    error_msg = error_msg.replace(",,", ", ").replace(";;", "; ")  # Multiple separators
    error_msg = error_msg.replace(
        ": ", " : "
    )  # Ensure spacing around colons in case of JSON

    # Final safeguard: remove import re and simplify to avoid potential runtime issues
    # Ensure we return a properly sanitized error message
    return error_msg


def _is_self_referencing_field(model: Any, field_name: str) -> bool:
    """Check if a field is self-referencing (references the same model).

    This function determines whether a field like 'parent_id' references the same
    model as the one it belongs to. For example, in res.partner, 'parent_id'
    references res.partner (self-referencing), but in product.supplierinfo,
    'partner_id' references res.partner (not self-referencing).

    Args:
        model: The Odoo model object
        field_name: The name of the field to check

    Returns:
        True if the field references the same model, False otherwise
    """
    try:
        # Get model fields information using existing safe function
        model_fields = _get_model_fields_safe(model)
        if not model_fields:
            # If we can't determine field info, assume it's not self-referencing to be safe
            return False

        # Check if the field exists in the model fields
        if field_name not in model_fields:
            return False

        field_info = model_fields[field_name]
        field_type = field_info.get("type")

        # Only check relational fields (many2one, many2many, one2many)
        if field_type not in ("many2one", "many2many", "one2many"):
            return False

        # Get the relation model name
        relation_model: Optional[str] = field_info.get("relation")
        if not relation_model:
            # If no relation info, it's not self-referencing
            return False

        # Get the current model name
        current_model: Optional[str] = getattr(model, "_name", None)
        if not current_model:
            return False

        # Check if the relation model is the same as the current model
        return bool(relation_model == current_model)

    except Exception:
        # On any error, assume it's not self-referencing to avoid deferring unnecessarily
        return False


def _format_odoo_error(error: Any) -> str:
    """Tries to extract the meaningful message from an Odoo RPC error.

    Args:
        error: The raw error object from Odoo RPC call

    Returns:
        A formatted string with the meaningful error message
    """
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


def _parse_csv_data(
    f: TextIO, separator: str, skip: int
) -> tuple[list[str], list[list[Any]]]:
    """Parses CSV data from a file handle, handling headers and skipping rows.

    Args:
        f: File handle to read CSV data from
        separator: Field separator character (e.g., ',', ';')
        skip: Number of initial rows to skip

    Returns:
        A tuple containing:
        - List of header column names
        - List of data rows (each row is a list of values)

    Raises:
        ValueError: If the source file doesn't contain an 'id' column
    """
    reader = csv.reader(f, delimiter=separator)

    try:
        # Skip initial lines before the header
        for _ in range(skip):
            next(reader)

        # Read header
        header = next(reader)
    except StopIteration:
        # File is too short to have a header after skipping
        return [], []

    # Validate that the 'id' column is present in the header
    if "id" not in header:
        raise ValueError("Source file must contain an 'id' column.")

    # Read the rest of the data into a list
    all_data = list(reader)
    return header, all_data


def _read_data_file(
    file_path: str, separator: str, encoding: str, skip: int
) -> tuple[list[str], list[list[Any]]]:
    """Reads a CSV file and returns its header and data.

    This function handles opening and parsing a CSV file, skipping any
    specified number of leading rows. It's the primary entry point for
    getting CSV data into the import system.

    Args:
        file_path (str): Path to the CSV file to read.
        separator (str): Field separator character (e.g., ',', ';').
        encoding (str): The character encoding of the file.
        skip (int): Number of leading rows to skip.

    Returns:
        tuple[list[str], list[list[Any]]]: A tuple containing the header row
        and a list of data rows. Returns `([], [])` if the file cannot be read.
    """
    # First try with the specified encoding
    try:
        with open(file_path, encoding=encoding, newline="") as f:
            return _parse_csv_data(f, separator, skip)
    except UnicodeDecodeError:
        # If UnicodeDecodeError occurs, try fallback encodings
        log.warning(
            f"Unicode decode error with encoding '{encoding}', "
            f"trying fallback encodings..."
        )
        encodings_to_try = ["utf-8", "latin-1", "cp1252", "iso-8859-1"]

        for enc in encodings_to_try:
            try:
                with open(file_path, encoding=enc, newline="") as f:
                    header, all_data = _parse_csv_data(f, separator, skip)
                log.warning(
                    f"File {file_path} was read successfully with encoding '{enc}' "
                    f"instead of '{encoding}'"
                )
                return header, all_data
            except UnicodeDecodeError:
                continue  # Try next encoding
            except OSError:  # File-related errors
                continue  # Try next encoding
            except Exception:
                # For non-file-related exceptions, don't try other encodings
                # just propagate the original exception
                raise

        # If all fallback encodings also fail with UnicodeDecodeError
        log.error(
            f"Could not read data file {file_path} with any of the tried encodings"
        )
        return [], []
    except OSError:  # File-related errors like FileNotFoundError
        # If the original encoding attempt fails due to file issues, return empty lists
        # to maintain backward compatibility
        log.error(f"Could not read data file {file_path}: file access error")
        return [], []
    except Exception:
        # For any other exception (not file-related or UnicodeDecodeError), propagate it
        raise


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


def _get_environment_from_connection(connection: Union[str, dict[str, Any]]) -> str:
    """Extract environment name from connection file path or config.
    
    Args:
        connection: Either a path to connection file or connection config dict
        
    Returns:
        Environment name extracted from connection (e.g., 'local', 'prod', 'test')
        
    Examples:
        >>> _get_environment_from_connection("conf/local_connection.conf")
        'local'
        >>> _get_environment_from_connection("conf/prod_connection.conf")
        'prod'
    """
    if isinstance(connection, dict):
        # If connection is already a dict, try to get environment from it
        return connection.get('environment', 'unknown')
    
    # Handle connection file path
    filename = os.path.basename(str(connection))
    if '_connection.conf' in filename:
        return filename.replace('_connection.conf', '')
    elif '.conf' in filename:
        # Handle cases like "connection.conf" -> "connection"
        return filename.replace('.conf', '')
    
    return 'unknown'


def _get_fail_file_path(
    original_file: str, 
    environment: str, 
    fail_type: str = "fail",
    preserve_timestamp: bool = True
) -> str:
    """Generate environment-specific fail file path with optional timestamp preservation.
    
    Args:
        original_file: Path to the original CSV file being imported
        environment: Environment name (e.g., 'local', 'prod', 'test')
        fail_type: Type of fail file ('fail' or 'failed')
        preserve_timestamp: Whether to preserve original file timestamp in failed files
        
    Returns:
        Full path to the environment-specific fail file
        
    Examples:
        >>> _get_fail_file_path("data/res_partner.csv", "local", "fail")
        'fail_files/local/res_partner_fail.csv'
        >>> _get_fail_file_path("data/res_partner_bank_8.csv", "prod", "failed")
        'fail_files/prod/res_partner_bank_8_failed.csv'
    """
    # Create fail_files directory if it doesn't exist
    fail_dir = os.path.join("fail_files", environment)
    os.makedirs(fail_dir, exist_ok=True)
    
    # Preserve original filename (remove .csv extension if present)
    filename = os.path.basename(original_file)
    if filename.endswith('.csv'):
        filename = filename[:-4]  # Remove .csv extension
    
    if fail_type == "fail":
        return os.path.join(fail_dir, f"{filename}_fail.csv")
    elif fail_type == "failed":
        # Remove .csv extension for failed files too
        if filename.endswith('.csv'):
            filename = filename[:-4]
        fail_file = os.path.join(fail_dir, f"{filename}_failed.csv")
        
        # Preserve timestamp if requested and file exists
        if preserve_timestamp and os.path.exists(original_file):
            original_stat = os.stat(original_file)
            try:
                # Create the file to set its timestamp
                with open(fail_file, 'w') as f:
                    pass
                os.utime(fail_file, (original_stat.st_atime, original_stat.st_mtime))
            except OSError as e:
                log.warning(f"Could not preserve timestamp for {fail_file}: {e}")
        
        return fail_file
    else:
        return os.path.join(fail_dir, f"{filename}_{fail_type}.csv")


def _setup_fail_file(
    fail_file: Optional[str], 
    header: list[str], 
    separator: str, 
    encoding: str,
    merge_existing: bool = False
) -> tuple[Optional[Any], Optional[TextIO]]:
    """Opens the fail file and returns the writer and file handle.
    
    Args:
        fail_file: Path to the fail file
        header: Original data header
        separator: CSV separator
        encoding: File encoding
        merge_existing: If True, reads existing fail file and merges with new errors
        
    Returns:
        Tuple of (fail_writer, fail_handle)
    """
    if not fail_file:
        return None, None
    try:
        # Check if file exists and we should merge existing errors
        existing_errors = {}
        if merge_existing and os.path.exists(fail_file):
            existing_errors = _read_existing_fail_file(fail_file, encoding, separator)
        
        fail_handle = open(fail_file, "w", newline="", encoding=encoding)
        fail_writer = csv.writer(
            fail_handle, delimiter=separator, quoting=csv.QUOTE_ALL
        )
        header_to_write = list(header)
        if "_ERROR_REASON" not in header_to_write:
            header_to_write.append("_ERROR_REASON")
        if "_LOAD_ERROR_REASON" not in header_to_write:
            header_to_write.append("_LOAD_ERROR_REASON")
        fail_writer.writerow(header_to_write)
        
        # Write existing errors back to the file
        if existing_errors:
            for error_line in existing_errors.values():
                fail_writer.writerow(error_line)
        
        return fail_writer, fail_handle
    except OSError as e:
        log.error(f"Could not open fail file for writing: {fail_file}. Error: {e}")
        return None, None


def _pad_line_to_header_length(line: list[Any], header_length: int) -> list[Any]:
    """Pad or truncate a line to match the header length.

    This ensures all lines have consistent column counts for CSV output.
    Lines shorter than header_length are padded with empty strings.
    Lines longer than header_length are truncated to match header_length.

    Args:
        line: The data line to pad/truncate
        header_length: The expected number of columns

    Returns:
        A new list with the line padded/truncated to match header_length
    """
    if len(line) > header_length:
        # Truncate to header_length
        return list(line[:header_length])
    elif len(line) < header_length:
        # Pad with empty strings
        return list(line) + [""] * (header_length - len(line))
    else:
        # Exact match, return as-is
        return list(line)


def _read_existing_fail_file(
    fail_file: str, encoding: str, separator: str
) -> dict[str, list[str]]:
    """Read an existing fail file and return a dictionary of failed records.
    
    Args:
        fail_file: Path to the existing fail file
        encoding: File encoding
        separator: CSV separator
        
    Returns:
        Dictionary mapping record IDs to full failed lines (including error message)
    """
    existing_errors = {}
    try:
        with open(fail_file, 'r', encoding=encoding, newline='') as f:
            reader = csv.reader(f, delimiter=separator)
            header = next(reader)  # Skip header
            
            # Find the index of the ID column and ERROR_REASON column
            id_index = 0  # Default to first column
            error_index = len(header) - 1  # Default to last column
            
            # Try to find 'id' column (case insensitive)
            for i, col_name in enumerate(header):
                if col_name.lower() in ['id', 'xml_id', 'external_id']:
                    id_index = i
                    break
            
            # Try to find ERROR_REASON column
            for i, col_name in enumerate(header):
                if 'error' in col_name.lower():
                    error_index = i
                    break
            
            # Read existing failed records
            for row in reader:
                if len(row) > id_index:
                    record_id = row[id_index]
                    existing_errors[record_id] = row
        
        log.info(f"Read {len(existing_errors)} existing failed records from {fail_file}")
    except Exception as e:
        log.warning(f"Could not read existing fail file {fail_file}: {e}")
    
    return existing_errors


def _create_padded_failed_line(
    line: list[Any], header_length: int, error_message: str, load_error: str = ""
) -> list[Any]:
    """Create a properly padded failed line with error message.

    Ensures the failed line has consistent column count by padding to header length
    and appending the error messages as the final columns.

    Args:
        line: The original data line that failed
        header_length: The expected number of columns in the original header
        error_message: The error message to append
        load_error: Optional load error message to append

    Returns:
        A properly padded line with the error messages as the final columns
    """
    # Debug: Log the load error if available
    if load_error:
        log.debug(f"Creating fail line with load error: {load_error[:100]}...")
    else:
        log.debug("Creating fail line without load error")
    
    # Sanitize the error messages to prevent CSV formatting issues
    sanitized_error = _sanitize_error_message(error_message)
    
    # FINAL FIX: Directly extract load error from error_message if it contains the full error object
    if not load_error and isinstance(error_message, str) and 'message' in error_message:
        try:
            import re
            # First try to get the nested message in data section
            match = re.search(r"'data'\s*:\s*\{[^}]*'message'\s*:\s*'([^']+)'", error_message)
            if not match:
                # Fallback to any message
                match = re.search(r"'message'\s*:\s*'([^']+)'", error_message)
            if match:
                load_error = match.group(1)
                log.debug(f"Directly extracted load error from error_message: {load_error[:100]}...")
        except Exception as ex:
            log.error(f"Failed to extract load error from error_message: {ex}")
    
    sanitized_load_error = _sanitize_error_message(load_error) if load_error else ""
    
    # Debug: Check if load error was properly sanitized
    if load_error and not sanitized_load_error:
        log.warning(f"Load error was lost during sanitization. Original: {load_error[:100]}...")
    
    padded_line = _pad_line_to_header_length(line, header_length)
    return [*padded_line, sanitized_error, sanitized_load_error]


def _prepare_pass_2_data(
    all_data: list[list[Any]],
    header: list[str],
    unique_id_field_index: int,
    id_map: dict[str, int],
    deferred_fields: list[str],
) -> list[tuple[int, dict[str, Any]]]:
    """Prepares the list of write operations for Pass 2."""
    pass_2_data_to_write = []

    # FIX: Pre-calculate a map of deferred field names (e.g., 'parent_id')
    # to their actual index in the header.
    deferred_field_indices = {}
    deferred_fields_set = set(deferred_fields)
    for i, column_name in enumerate(header):
        field_base_name = column_name.split("/")[0]
        if field_base_name in deferred_fields_set:
            deferred_field_indices[field_base_name] = i

    for row in all_data:
        source_id = row[unique_id_field_index]
        db_id = id_map.get(source_id)
        if not db_id:
            continue

        update_vals = {}
        # Use the pre-calculated map to find the values to write.
        for field_name, field_index in deferred_field_indices.items():
            if field_index < len(row):
                related_source_id = row[field_index]
                if related_source_id:  # Ensure there is a value to look up
                    related_db_id = id_map.get(related_source_id)
                    if related_db_id:
                        update_vals[field_name] = related_db_id

        if update_vals:
            pass_2_data_to_write.append((db_id, update_vals))

    return pass_2_data_to_write  # This fixed it


def _recursive_create_batches(
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


def _get_model_fields(model: Any) -> Optional[dict[str, Any]]:
    """Safely retrieves the fields metadata from an Odoo model with minimal RPC calls.

    This version avoids the problematic fields_get() call that causes
    'tuple index out of range' errors in the Odoo server.

    Args:
        model: The Odoo model object.

    Returns:
        A dictionary of field metadata, or None if it cannot be retrieved.
    """
    # Use only the _fields attribute to completely avoid RPC calls that can cause errors
    if not hasattr(model, "_fields"):
        log.debug(
            "Model has no _fields attribute and RPC call avoided to "
            "prevent 'tuple index out of range' error"
        )
        return None

    model_fields_attr = model._fields
    model_fields = None

    if isinstance(model_fields_attr, dict):
        # It's a property/dictionary, use it directly
        model_fields = model_fields_attr
    elif callable(model_fields_attr):
        # In rare cases, some customizations might make _fields a callable
        # that returns the fields dictionary.
        try:
            model_fields_result = model_fields_attr()
            # Only use the result if it's a dictionary/mapping
            if isinstance(model_fields_result, dict):
                model_fields = model_fields_result
        except Exception:
            # If calling fails, fall back to None
            log.warning("Could not retrieve model fields by calling _fields method.")
            model_fields = None
    else:
        log.warning(
            "Model `_fields` attribute is of unexpected type: %s",
            type(model_fields_attr),
        )

    # Cast to the expected type to satisfy MyPy
    if model_fields is not None and isinstance(model_fields, dict):
        fields_dict: dict[str, Any] = model_fields
        return fields_dict
    else:
        return None


def _get_model_fields_safe(model: Any) -> Optional[dict[str, Any]]:
    """Safely retrieves the fields metadata from an Odoo model with minimal RPC calls.

    This version avoids the problematic fields_get() call that causes
    'tuple index out of range' errors in the Odoo server.

    Args:
        model: The Odoo model object.

    Returns:
        A dictionary of field metadata, or None if it cannot be retrieved.
    """
    # Use only the _fields attribute to completely avoid RPC calls that can cause errors
    if not hasattr(model, "_fields"):
        log.debug(
            "Model has no _fields attribute and RPC call avoided to "
            "prevent 'tuple index out of range' error"
        )
        return None

    model_fields_attr = model._fields

    if isinstance(model_fields_attr, dict):
        # Return directly if it's already a dictionary
        return model_fields_attr
    else:
        # For any other type, return None to avoid potential RPC issues
        log.debug(
            "Model _fields attribute is not a dict (%s), "
            "avoiding RPC calls to prevent errors",
            type(model_fields_attr),
        )
        return None


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
    model: Any,
    field_name: str,
    field_value: Optional[str],
) -> tuple[str, Any]:
    """Convert an external ID field to a database ID.

    Args:
        model: The Odoo model object
        field_name: The field name (e.g., 'parent_id/id')
        field_value: The external ID value, can be None or empty

    Returns:
        Tuple of (base_field_name, converted_value)
    """
    base_field_name = field_name[:-3]  # Remove '/id' suffix

    if not field_value:
        # Empty external ID means no value for this field
        # Return None to indicate the field should be omitted entirely
        # This prevents setting many2many fields to False which creates
        # empty combinations
        log.debug(
            f"Converted empty external ID {field_name} -> omitting field entirely"
        )
        return base_field_name, None
    else:
        # Convert external ID to database ID
        try:
            # Look up the database ID for this external ID
            record_ref = model.env.ref(field_value, raise_if_not_found=False)
            if record_ref:
                converted_value = record_ref.id
                log.debug(
                    f"Converted external ID {field_name} ({field_value}) -> "
                    f"{base_field_name} ({record_ref.id})"
                )
                return base_field_name, converted_value
            else:
                # If we can't find the external ID, for the create fallback method,
                # it might be better to return the original value instead of None,
                # so that Odoo can provide its own validation error message
                # However, returning the original external ID string to create
                # might cause type mismatch errors in some cases
                # So instead, we'll log the issue and return None to omit the field
                log.warning(
                    f"Could not find record for external ID '{field_value}', "
                    f"omitting field {base_field_name} entirely. "
                    f"This may cause record creation to fail if the field is required."
                )
                return base_field_name, None
        except Exception as e:
            log.warning(
                f"Error looking up external ID '{field_value}' for field "
                f"'{field_name}': {e}"
            )
            # On error, omit the field entirely
            return base_field_name, None


def _safe_convert_field_value(
    field_name: str, field_value: Any, field_type: str
) -> Any:
    """Safely convert field values to prevent type-related errors.

    Args:
        field_name: Name of the field being converted
        field_value: Raw field value to convert
        field_type: Target Odoo field type (integer, float, etc.)

    Returns:
        Safely converted field value, or original value if conversion unsafe
    """
    if field_value is None or field_value == "":
        # Handle empty values appropriately by field type
        # SPECIAL CASE: For external ID fields, return empty string instead of False
        # to prevent tuple index errors when False is sent instead of ""
        if field_name.endswith("/id"):
            return ""  # Return empty string, not False
        elif field_type in ("integer", "float", "positive", "negative"):
            return 0  # Use 0 for empty numeric fields
        elif field_type in ("many2one", "many2many", "one2many"):
            return (
                False  # Use False for empty relational fields to indicate no relation
            )
        elif field_type == "boolean":
            return False  # Use False for empty boolean fields
        else:
            return field_value  # Keep original for other field types

    # Convert to string for processing
    str_value = str(field_value).strip()

    # Handle external ID fields specially (they should remain as strings)
    if field_name.endswith("/id"):
        # For external ID fields, return empty string if value is empty after stripping
        if not str_value:
            return ""
        return str_value

    # Handle string values that are empty after stripping (whitespace-only strings)
    if not str_value:
        # Return appropriate empty value based on field type
        if field_type in ("integer", "float", "positive", "negative"):
            return 0  # Use 0 for empty numeric fields
        elif field_type in ("many2one", "many2many", "one2many"):
            return (
                False  # Use False for empty relational fields to indicate no relation
            )
        elif field_type == "boolean":
            return False  # Use False for empty boolean fields
        else:
            return ""  # Use empty string for other field types

    # Handle numeric field conversions with enhanced safety
    if field_type in ("integer", "positive", "negative"):
        try:
            # Handle float strings like "1.0", "2.0" by converting to int
            if "." in str_value:
                float_val = float(str_value)
                if float_val.is_integer():
                    return int(float_val)
                else:
                    # Non-integer float - return original value to maintain data integrity
                    # This prevents changing "1.5" to 1.5 float, preserving the original data for server to handle
                    log_msg = (
                        f"Non-integer float value '{str_value}' in {field_type} field '{field_name}', "
                        f"returning original value for server-side validation"
                    )
                    log.debug(log_msg)
                    return field_value
            elif str_value.lstrip("+-").isdigit():
                # Integer string like "1", "-5", or "+5"
                return int(str_value)
            else:
                # Check if string looks like a common placeholder for missing/invalid data
                # For such strings, convert to default to maintain data integrity
                # For other strings, return original for server validation
                is_common_placeholder = str_value.lower() in COMMON_PLACEHOLDER_VALUES

                if is_common_placeholder:
                    # Known placeholder text - return default to maintain data integrity
                    log_msg = (
                        f"Known placeholder value '{str_value}' in {field_type} field '{field_name}', "
                        f"converting to 0 to prevent tuple index errors"
                    )
                    log.debug(log_msg)
                    return 0
                else:
                    # Non-numeric or other string - return original for server validation
                    log.debug(
                        f"Non-numeric or other value '{str_value}' in {field_type} field '{field_name}', "
                        f"returning original value for server-side validation"
                    )
                    return field_value
        except (ValueError, TypeError):
            # Conversion failed - return 0 for numeric fields to prevent tuple index errors
            log.debug(
                f"Failed to convert '{str_value}' to {field_type} for field '{field_name}', "
                f"returning 0 to prevent tuple index errors"
            )
            return 0

    elif field_type == "float":
        try:
            # Convert numeric strings to float with enhanced safety
            # Handle international decimal notation (comma as decimal separator)
            # Handle cases like "1.234,56" -> "1234.56" (European thousands separator with decimal comma)
            normalized_value = str_value

            # Handle European decimal notation (comma as decimal separator)
            if "," in str_value and "." in str_value:
                # Has both comma and period - likely European format with thousands separator
                # e.g., "1.234,56" should become "1234.56"
                # Replace periods (thousands separators) with nothing, then replace comma with period
                normalized_value = str_value.replace(".", "").replace(",", ".")
            elif "," in str_value:
                # Only comma - likely European decimal separator
                # e.g., "123,45" should become "123.45"
                normalized_value = str_value.replace(",", ".")

            # Check if it's a valid float after normalization
            # Allow digits, one decimal point, plus/minus signs
            test_value = (
                normalized_value.replace(".", "").replace("-", "").replace("+", "")
            )
            if test_value.isdigit() and normalized_value.count(".") <= 1:
                return float(normalized_value)
            else:
                # Check if string looks like a common placeholder for missing/invalid data
                # For such strings, convert to default to maintain data integrity
                # For other strings, return original for server validation
                is_common_placeholder = str_value.lower() in COMMON_PLACEHOLDER_VALUES

                if is_common_placeholder:
                    # Known placeholder text - return default to maintain data integrity
                    log_msg = (
                        f"Known placeholder value '{str_value}' in float field '{field_name}', "
                        f"converting to 0.0 to prevent tuple index errors"
                    )
                    log.debug(log_msg)
                    return 0.0
                else:
                    # Non-numeric or other string - return original for server validation
                    log.debug(
                        f"Non-numeric or other value '{str_value}' in float field '{field_name}', "
                        f"returning original value for server-side validation"
                    )
                    return field_value
        except (ValueError, TypeError):
            # Check if string looks like a common placeholder for missing/invalid data
            # For such strings, convert to default to maintain data integrity
            # For other strings, return original for server validation
            is_common_placeholder = str_value.lower() in COMMON_PLACEHOLDER_VALUES

            if is_common_placeholder:
                # Known placeholder text - return default to maintain data integrity
                log.debug(
                    f"Known placeholder value '{str_value}' in float field '{field_name}', "
                    f"converting to 0.0 to prevent tuple index errors"
                )
                return 0.0
            else:
                # Conversion failed - return original value to allow server-side validation
                log.debug(
                    f"Failed to convert '{str_value}' to float for field '{field_name}', "
                    f"returning original value for server-side validation"
                )
                return field_value

    # Special handling for res_partner fields that commonly cause tuple index errors
    # These fields often contain text values where numeric IDs are expected
    partner_numeric_fields = {
        "parent_id",
        "company_id",
        "country_id",
        "state_id",
        "title",
        "category_id",
        "user_id",
        "industry_id",
    }

    if field_name in partner_numeric_fields and field_type in (
        "many2one",
        "many2many",
    ):
        # For res_partner fields that should be numeric but contain text values,
        # return 0 to prevent tuple index errors when text is sent to numeric fields
        try:
            # Try to convert to integer first
            if str_value.lstrip("+-").isdigit():
                return int(str_value)
            elif "." in str_value:
                # Handle float strings like "1.0", "2.0" by converting to int
                float_val = float(str_value)
                if float_val.is_integer():
                    return int(float_val)
                else:
                    # Non-integer float - return 0 to prevent tuple index errors
                    log.debug(
                        f"Non-integer float value '{str_value}' in {field_type} field '{field_name}', "
                        f"converting to 0 to prevent tuple index errors"
                    )
                    return 0
            else:
                # Non-numeric string in many2one field - return 0 to prevent tuple index errors
                # This specifically addresses the issue where text values are sent to numeric fields
                log.debug(
                    f"Non-numeric value '{str_value}' in {field_type} field '{field_name}', "
                    f"converting to 0 to prevent tuple index errors"
                )
                return 0
        except (ValueError, TypeError):
            # Conversion failed - return 0 for numeric fields to prevent tuple index errors
            log.debug(
                f"Failed to convert '{str_value}' to integer for field '{field_name}', "
                f"returning 0 to prevent tuple index errors"
            )
            return 0

    # Special handling for string data that might cause CSV parsing issues
    if isinstance(field_value, str):
        # Sanitize field values that might cause CSV parsing issues
        # especially important for data with quotes, newlines, etc.
        sanitized_value = field_value.replace("\n", " | ").replace("\r", " | ")
        sanitized_value = sanitized_value.replace("\t", " ")
        # Double quotes need to be escaped for CSV format
        sanitized_value = sanitized_value.replace('"', '""')
        # Replace semicolons that might interfere with field separation
        # (only for non-external ID fields, as they may legitimately contain semicolons)
        if not field_name.endswith("/id"):
            sanitized_value = sanitized_value.replace(";", ":")
        # Remove control characters that might interfere with CSV processing
        for char in [
            "\x00",
            "\x01",
            "\x02",
            "\x03",
            "\x04",
            "\x05",
            "\x06",
            "\x07",
            "\x08",
            "\x0b",
            "\x0c",
            "\x0e",
            "\x0f",
            "\x10",
            "\x11",
            "\x12",
            "\x13",
            "\x14",
            "\x15",
            "\x16",
            "\x17",
            "\x18",
            "\x19",
            "\x1a",
            "\x1b",
            "\x1c",
            "\x1d",
            "\x1e",
            "\x1f",
            "\x7f",
        ]:
            sanitized_value = sanitized_value.replace(char, " ")
        return sanitized_value

    # For all other field types, return original value
    return field_value


def _process_external_id_fields(
    model: Any,
    clean_vals: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Process all external ID fields in the clean values.

    Args:
        model: The Odoo model object
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
                model, field_name, field_value
            )
            # Only add the field to converted_vals if the value is not None
            # This allows us to omit fields entirely when appropriate (e.g., for
            # empty many2many fields)
            if value is not None:
                converted_vals[base_name] = value
            external_id_fields.append(field_name)
        else:
            # Regular field - pass through as-is
            converted_vals[field_name] = field_value

    return converted_vals, external_id_fields


def _handle_create_error(
    i: int,
    create_error: Exception,
    line: list[Any],
    error_summary: str,
    header_length: Optional[int] = None,
    override_error_message: Optional[str] = None,
) -> tuple[str, list[Any], str]:
    """Handle errors during record creation.

    Args:
        i: The row index
        create_error: The exception that occurred
        line: The data line being processed
        error_summary: Current error summary
        header_length: Number of columns expected in the header (optional)
        override_error_message: Optional error message to use instead of exception string

    Returns:
        Tuple of (error_message, failed_line, error_summary)
    """
    if override_error_message:
        error_str = override_error_message
    else:
        error_str = str(create_error)
    error_str_lower = error_str.lower()

    # Handle constraint violation errors (e.g., XML ID space constraint)
    if (
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
    elif _is_tuple_index_error(create_error):
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

    # Apply comprehensive error message sanitization to ensure CSV safety
    sanitized_error = _sanitize_error_message(error_message)

    # Create properly padded failed line with consistent column count
    if header_length is not None:
        # Use the standardized function to ensure proper column count when header length is provided
        padded_failed_line = _create_padded_failed_line(
            line, header_length, sanitized_error
        )
    else:
        # Maintain backward compatibility for existing test calls
        # Add error message to the end of the line (legacy format)
        padded_failed_line = [*line, sanitized_error]

    return sanitized_error, padded_failed_line, error_summary


def _handle_tuple_index_error(
    progress: Optional[Any],
    source_id: str,
    line: list[Any],
    failed_lines: list[list[Any]],
    header_length: int,
) -> None:
    """Handles tuple index out of range errors by logging and recording failure."""
    if progress is not None:
        progress.console.print(
            f"[yellow]WARN:[/] Tuple index error for record '{source_id}'. "
            "This often happens when sending text values to numeric "
            "fields. Check your data types."
        )
    error_message = (
        f"Tuple index out of range error for record {source_id}: "
        "This is often caused by sending incorrect data types to Odoo "
        "fields. Check your data types and ensure they match the Odoo "
        "field types."
    )
    # Apply comprehensive error message sanitization to ensure CSV safety
    sanitized_error = _sanitize_error_message(error_message)
    # Create properly padded failed line with consistent column count
    padded_failed_line = _create_padded_failed_line(
        line, header_length, sanitized_error
    )
    failed_lines.append(padded_failed_line)


def _create_batch_individually(
    model: Any,
    batch_lines: list[list[Any]],
    batch_header: list[str],
    uid_index: int,
    context: dict[str, Any],
    ignore_list: list[str],
    progress: Any = None,  # Optional progress object for user-facing messages
    prior_error: Optional[
        str
    ] = None,  # Optional error message from the failed load attempt
) -> dict[str, Any]:
    """Fallback to create records one-by-one to get detailed errors."""
    id_map: dict[str, int] = {}
    failed_lines: list[list[Any]] = []
    error_summary = "Fell back to create"
    header_len = len(batch_header)
    ignore_set = set(ignore_list)
    model_fields = _get_model_fields_safe(model)

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

            # 1. EARLY PROBLEM DETECTION: Check if this record contains patterns that are likely to cause server errors
            # This includes specific problematic patterns that have been identified in the past
            " ".join(str(x) for x in line if x is not None).lower()

            # 1. SEARCH BEFORE CREATE
            existing_record = model.browse().env.ref(
                f"__export__.{sanitized_source_id}", raise_if_not_found=False
            )

            if existing_record:
                id_map[sanitized_source_id] = existing_record.id
                # Add error message to failed_lines to indicate record already exists
                error_message = f"Record already exists with ID {existing_record.id}"
                sanitized_error = _sanitize_error_message(error_message)
                padded_failed_line = _create_padded_failed_line(
                    line, header_len, sanitized_error
                )
                failed_lines.append(padded_failed_line)
                continue

            # 2. PREPARE FOR CREATE - Check if this record contains known problematic external ID references
            # that will likely cause server-side tuple index errors during individual processing
            vals = dict(zip(batch_header, line))

            # Check if this record contains external ID references that are known to be problematic
            # Apply safe field value conversion to prevent type errors
            # Only skip self-referencing external ID fields that would cause import dependencies
            # Non-self-referencing fields (like partner_id, product_id) should be processed normally
            safe_vals = {}
            for field_name, field_value in vals.items():
                if field_name.endswith("/id"):
                    # External ID fields like 'partner_id/id' should map to 'partner_id' in the database
                    # Process them normally unless they are self-referencing
                    base_field_name = field_name[
                        :-3
                    ]  # Remove '/id' suffix to get base field name like 'partner_id'

                    # Check if this is a self-referencing field by examining the external ID value
                    str(field_value).lower() if field_value else ""

                    # For non-self-referencing external ID fields, process them normally
                    if field_value and str(field_value).upper() not in [
                        "",
                        "False",
                        "None",
                    ]:
                        # Process non-self-referencing external ID fields normally
                        clean_field_name = (
                            base_field_name  # Use the base field name (without /id)
                        )
                        field_type = "unknown"
                        if model_fields and clean_field_name in model_fields:
                            field_info = model_fields[clean_field_name]
                            field_type = field_info.get("type", "unknown")
                        # Use the base field name as the key, but keep the original external ID value
                        safe_vals[base_field_name] = _safe_convert_field_value(
                            field_name, field_value, field_type
                        )
                    # If it contains problematic values, it will be handled later in the CREATE section
                else:
                    # Process non-external ID fields normally
                    clean_field_name = field_name.split("/")[0]
                    field_type = "unknown"
                    if model_fields and clean_field_name in model_fields:
                        field_info = model_fields[clean_field_name]
                        field_type = field_info.get("type", "unknown")

                    # Apply safe conversion based on field type
                    safe_vals[field_name] = _safe_convert_field_value(
                        field_name, field_value, field_type
                    )

            clean_vals = {
                k: v
                for k, v in safe_vals.items()
                if k.split("/")[0] not in ignore_set
                # Keep all fields including external ID fields (processed normally above)
            }

            # 3. CREATE
            # Process all fields normally, including external ID fields
            # Only skip records with known problematic external ID values

            vals_for_create = {}
            skip_record = False

            for field_name, field_value in clean_vals.items():
                # For external ID fields, check if they contain known problematic values
                if field_name.endswith("/id"):
                    # This shouldn't happen anymore since we converted them during safe_vals creation
                    # But handle it just in case
                    base_field_name = (
                        field_name[:-3] if field_name.endswith("/id") else field_name
                    )
                    if field_value and field_value not in ["", "False", "None"]:
                        # For valid external ID fields, add them to the values for create
                        # Use the base field name (without /id) which maps to the database field
                        vals_for_create[base_field_name] = field_value
                    else:
                        # For empty/invalid external ID values, add them as the base field name
                        vals_for_create[base_field_name] = field_value
                else:
                    # For non-external ID fields, ensure safe values
                    if field_value is not None:
                        # Only add values that are safe for RPC serialization
                        if isinstance(field_value, (str, int, float, bool)):
                            vals_for_create[field_name] = field_value
                        else:
                            # Convert other types to string to prevent RPC serialization issues
                            vals_for_create[field_name] = str(field_value)
                    # Skip None values to prevent potential server issues

            # If we need to skip this record, continue to the next one
            if skip_record:
                continue

            log.debug(f"Values sent to create: {list(vals_for_create.keys())}")

            # Only attempt create if we have valid values to send
            if vals_for_create:
                # Use the absolute safest approach for the create call to prevent server-side tuple index errors
                # The error in odoo/api.py:525 suggests the RPC call format is being misinterpreted
                # Use a more explicit approach to ensure proper argument structure
                try:
                    # Ensure we're calling create with the cleanest possible data
                    # Make sure context is clean too to avoid any formatting issues
                    clean_context = {}
                    if context:
                        # Only include context values that are basic types or specific keys to avoid RPC serialization issues
                        for k, v in context.items():
                            if k in (
                                "tracking_disable",
                                "mail_create_nolog",
                                "mail_notrack",
                                "import_file",
                            ) or isinstance(v, (str, int, float, bool)):
                                clean_context[k] = v
                            else:
                                # Convert complex types to strings to prevent RPC issues
                                clean_context[k] = str(v)
                    # Ensure tracking off
                    if "tracking_disable" not in clean_context:
                        clean_context["tracking_disable"] = True

                    log.info(
                        f"DEBUG: _create_batch_individually context: {clean_context}"
                    )

                    # Call create with extremely clean data to avoid server-side argument unpacking errors
                    # Use the safest possible call format to prevent server-side tuple index errors
                    # The error in odoo/api.py:525 suggests issues with argument unpacking format
                    if clean_context:
                        new_record = model.create(
                            vals_for_create, context=clean_context
                        )
                    else:
                        new_record = model.create(vals_for_create)
                except IndexError as ie:
                    if _is_tuple_index_error(ie):
                        # This is the specific server-side error from odoo/api.py
                        # The RPC argument format is being misinterpreted by the server
                        error_message = f"Server API error creating record {source_id}: {_extract_clean_error_message(ie)}. This indicates the RPC call structure is incompatible with this server version or the record has unresolvable references."
                        sanitized_error = _sanitize_error_message(error_message)
                        # Create properly padded failed line with consistent column count
                        padded_failed_line = _create_padded_failed_line(
                            line, header_len, sanitized_error
                        )
                        failed_lines.append(padded_failed_line)
                        continue  # Skip this record and continue processing others
                    else:
                        # Some other IndexError
                        raise
                except Exception as e:
                    # Handle any other errors from create operation
                    # Extract the clean error message using our improved function
                    clean_error_message = _extract_clean_error_message(e)
                    
                    # Include prior error if available (from failed load attempt)
                    if prior_error:
                        error_message = f"Load failed: {prior_error} | Create failed: {clean_error_message}"
                    else:
                        error_message = f"Error creating record {source_id}: {clean_error_message}"
                    
                    sanitized_error = _sanitize_error_message(error_message)
                    # Create properly padded failed line with consistent column count
                    padded_failed_line = _create_padded_failed_line(
                        line, header_len, sanitized_error, prior_error
                    )
                    failed_lines.append(padded_failed_line)
                    continue  # Skip this record and continue processing others
            else:
                # If no valid values to create with, skip this record
                error_message = f"No valid values to create for record {source_id} - all fields were filtered out"
                sanitized_error = _sanitize_error_message(error_message)
                # Create properly padded failed line with consistent column count
                padded_failed_line = _create_padded_failed_line(
                    line, header_len, sanitized_error
                )
                failed_lines.append(padded_failed_line)
                continue
            id_map[sanitized_source_id] = new_record.id
        except IndexError as e:
            # If we have a prior error (e.g. from load), prioritize it over the generic IndexError/Traceback
            if prior_error:
                sanitized_error = _sanitize_error_message(prior_error)
                padded_failed_line = _create_padded_failed_line(
                    line, header_len, sanitized_error
                )
                failed_lines.append(padded_failed_line)
                continue

            error_str = str(e)
            error_str_lower = error_str.lower()

            # Enhanced detection for external ID related errors that might cause tuple index errors
            # Check the content of the line for external ID patterns that caused original load failure
            line_str_full = " ".join(str(x) for x in line if x is not None).lower()

            # Look for external ID patterns in the error or the line content
            external_id_in_error = any(
                pattern in error_str_lower
                for pattern in [
                    "external id",
                    "reference",
                    "does not exist",
                    "no matching record",
                    "res_id not found",
                    "xml id",
                    "invalid reference",
                    "unknown external id",
                    "missing record",
                    "referenced record",
                    "not found",
                    "lookup failed",
                ]
            )

            # More comprehensive check for external ID patterns in the data
            # Check for general external ID patterns
            external_id_in_line = any(
                pattern in line_str_full for pattern in EXTERNAL_ID_ERROR_PATTERNS
            )

            # Check for field names that are external ID fields
            any(field_name.endswith("/id") for field_name in batch_header)

            # Check if this is exactly the problematic scenario we know about
            known_problematic_scenario = False

            is_external_id_related = (
                external_id_in_error
                or external_id_in_line
                or known_problematic_scenario
            )

            # Check if the error is a tuple index error that's NOT related to external IDs
            is_pure_tuple_error = (
                _is_tuple_index_error(e)
                and not is_external_id_related
                and not (
                    "violates" in error_str_lower and "constraint" in error_str_lower
                )
                and not (
                    "null value in column" in error_str_lower
                    and "violates not-null" in error_str_lower
                )
                and "duplicate key value violates unique constraint"
                not in error_str_lower
            )

            if is_pure_tuple_error:
                # Only treat as tuple index error if it's definitely not external ID related
                _handle_tuple_index_error(
                    progress, source_id, line, failed_lines, len(batch_header)
                )
                continue
            else:
                # Handle as external ID related error or other IndexError
                if is_external_id_related:
                    # This is the problematic external ID error that was being misclassified
                    # Try to extract the actual error message from the error object
                    actual_error_message = "tuple index out of range"  # Default fallback
                    
                    try:
                        # Handle both object attributes and dictionary structures
                        error_data = None
                        
                        # First try as object with attributes
                        if hasattr(e, 'data'):
                            error_data = e.data
                        # Then try as dictionary
                        elif isinstance(e, dict) and 'data' in e:
                            error_data = e['data']
                        
                        # Extract message from error_data
                        if error_data:
                            if hasattr(error_data, 'message'):
                                actual_error_message = error_data.message
                            elif isinstance(error_data, dict) and 'message' in error_data:
                                actual_error_message = error_data['message']
                            elif hasattr(error_data, 'arguments') and len(error_data.arguments) > 0:
                                actual_error_message = error_data.arguments[0]
                            elif isinstance(error_data, dict) and 'arguments' in error_data and len(error_data['arguments']) > 0:
                                actual_error_message = error_data['arguments'][0]
                        
                        # Fallback to args if data not available
                        if actual_error_message == "tuple index out of range" and hasattr(e, 'args') and len(e.args) > 0:
                            actual_error_message = e.args[0]
                        elif actual_error_message == "tuple index out of range" and isinstance(e, dict) and 'args' in e and len(e['args']) > 0:
                            actual_error_message = e['args'][0]
                        
                        # Final fallback: extract from string representation
                        if actual_error_message == "tuple index out of range":
                            error_str = str(e)
                            # Try to find the actual message in the string
                            if 'message' in error_str:
                                # Look for the pattern: 'message': 'actual_message'
                                import re
                                # Look for the pattern in the data section specifically first
                                match = re.search(r"'data'\s*:\s*\{[^}]*'message'\s*:\s*'([^']+)'", error_str)
                                if match:
                                    actual_error_message = match.group(1)
                                else:
                                    # Fallback to any message
                                    match = re.search(r"'message'\s*:\s*'([^']+)'", error_str)
                                    if match:
                                        actual_error_message = match.group(1)
                            elif ':' in error_str:
                                actual_error_message = error_str.split(':')[-1].strip()
                    except Exception as ex:
                        print(f"Exception during error extraction: {ex}")
                        # If extraction fails, try to extract from string representation
                        error_str = str(e)
                        if 'message' in error_str:
                            import re
                            # Look for the pattern in the data section specifically first
                            match = re.search(r"'data'\s*:\s*\{[^}]*'message'\s*:\s*'([^']+)'", error_str)
                            if match:
                                actual_error_message = match.group(1)
                            else:
                                # Fallback to any message
                                match = re.search(r"'message'\s*:\s*'([^']+)'", error_str)
                                if match:
                                    actual_error_message = match.group(1)
                        elif ':' in error_str:
                            actual_error_message = error_str.split(':')[-1].strip()
                    
                    # Include prior error if available (from failed load attempt)
                    if prior_error:
                        error_message = f"Load failed: {prior_error} | External ID resolution error for record {source_id}: {actual_error_message}. Original error typically caused by missing external ID references."
                    else:
                        error_message = f"External ID resolution error for record {source_id}: {actual_error_message}. Original error typically caused by missing external ID references."
                    
                    # Debug: Ensure we're not using the full error object
                    if isinstance(error_message, dict) or str(error_message).startswith('{'):
                        # Fallback extraction if somehow the error object wasn't processed
                        try:
                            if isinstance(e, dict) and 'data' in e and 'message' in e['data']:
                                actual_fallback = e['data']['message']
                            elif hasattr(e, 'data') and hasattr(e.data, 'message'):
                                actual_fallback = e.data.message
                            else:
                                actual_fallback = str(e).split(':')[-1].strip()
                            
                            if prior_error:
                                error_message = f"Load failed: {prior_error} | External ID resolution error for record {source_id}: {actual_fallback}. Original error typically caused by missing external ID references."
                            else:
                                error_message = f"External ID resolution error for record {source_id}: {actual_fallback}. Original error typically caused by missing external ID references."
                        except Exception:
                            # Last resort fallback
                            error_message = f"External ID resolution error for record {source_id}: tuple index out of range. Original error typically caused by missing external ID references."
                    sanitized_error = _sanitize_error_message(error_message)
                    # Create properly padded failed line with consistent column count
                    padded_failed_line = _create_padded_failed_line(
                        line, len(batch_header), sanitized_error
                    )
                    failed_lines.append(padded_failed_line)
                    continue
                else:
                    # Handle other IndexError as malformed row
                    error_message = (
                        f"Malformed row detected (row {i + 1} in batch): {e}"
                    )
                    sanitized_error = _sanitize_error_message(error_message)
                    # Create properly padded failed line with consistent column count
                    padded_failed_line = _create_padded_failed_line(
                        line, len(batch_header), sanitized_error
                    )
                    failed_lines.append(padded_failed_line)
                    if "Fell back to create" in error_summary:
                        error_summary = "Malformed CSV row detected"
                    continue
        except Exception as create_error:
            error_str = str(create_error)
            error_str_lower = error_str.lower()

            # Check if this is specifically an external ID error FIRST (takes precedence)
            # Common external ID error patterns in Odoo, including partial matches
            is_external_id_error = _is_external_id_error(create_error)

            # Also check if this specifically mentions the problematic external ID from the load failure
            # The error might reference the same ID that caused the original load failure
            is_external_id_error = (
                _is_external_id_error(create_error) or is_external_id_error
            )

            # Handle external ID resolution errors first (takes priority)
            if is_external_id_error:
                # Try to extract the actual error message from the error object
                actual_error_message = "tuple index out of range"  # Default fallback
                
                try:
                    # Check if the error object has a data.message field
                    if hasattr(create_error, 'data') and hasattr(create_error.data, 'message'):
                        actual_error_message = create_error.data.message
                    elif hasattr(create_error, 'data') and hasattr(create_error.data, 'arguments') and len(create_error.data.arguments) > 0:
                        actual_error_message = create_error.data.arguments[0]
                    elif hasattr(create_error, 'args') and len(create_error.args) > 0:
                        actual_error_message = create_error.args[0]
                    else:
                        # Try to extract from string representation
                        error_str = str(create_error)
                        if ':' in error_str:
                            actual_error_message = error_str.split(':')[-1].strip()
                except Exception:
                    # If extraction fails, use the string representation as fallback
                    actual_error_message = str(create_error).split(':')[-1].strip() if ':' in str(create_error) else str(create_error)
                
                # Include prior error if available (from failed load attempt)
                if prior_error:
                    error_message = f"Load failed: {prior_error} | External ID resolution error for record {source_id}: {actual_error_message}"
                else:
                    error_message = f"External ID resolution error for record {source_id}: {actual_error_message}"
                
                # Debug: Ensure we're not using the full error object
                if isinstance(error_message, dict) or str(error_message).startswith('{'):
                    # Fallback extraction if somehow the error object wasn't processed
                    try:
                        if isinstance(create_error, dict) and 'data' in create_error and 'message' in create_error['data']:
                            actual_fallback = create_error['data']['message']
                        elif hasattr(create_error, 'data') and hasattr(create_error.data, 'message'):
                            actual_fallback = create_error.data.message
                        else:
                            actual_fallback = str(create_error).split(':')[-1].strip()
                        
                        error_message = f"External ID resolution error for record {source_id}: {actual_fallback}"
                    except Exception:
                        # Last resort fallback
                        error_message = f"External ID resolution error for record {source_id}: tuple index out of range"
                sanitized_error = _sanitize_error_message(error_message)
                # Create properly padded failed line with consistent column count
                padded_failed_line = _create_padded_failed_line(
                    line, len(batch_header), sanitized_error, prior_error
                )
                failed_lines.append(padded_failed_line)
                continue
            # Special handling for tuple index out of range errors
            # These can occur when sending wrong types to Odoo fields
            # But check if this is related to external ID issues first (takes priority)

            # Check if this error is related to external ID issues that caused the original load failure
            line_str_full = " ".join(str(x) for x in line if x is not None).lower()
            is_external_id_related = _is_external_id_error(create_error, line_str_full)

            # Handle tuple index errors that are NOT related to external IDs
            if _is_tuple_index_error(create_error) and not is_external_id_related:
                _handle_tuple_index_error(
                    progress, source_id, line, failed_lines, len(batch_header)
                )
                continue
            elif is_external_id_related:
                # Handle as external ID error instead of tuple index error
                # Try to extract the actual error message from the error object
                actual_error_message = "tuple index out of range"  # Default fallback
                
                try:
                    # Check if the error object has a data.message field
                    if hasattr(create_error, 'data') and hasattr(create_error.data, 'message'):
                        actual_error_message = create_error.data.message
                    elif hasattr(create_error, 'data') and hasattr(create_error.data, 'arguments') and len(create_error.data.arguments) > 0:
                        actual_error_message = create_error.data.arguments[0]
                    elif hasattr(create_error, 'args') and len(create_error.args) > 0:
                        actual_error_message = create_error.args[0]
                    else:
                        # Try to extract from string representation
                        error_str = str(create_error)
                        if ':' in error_str:
                            actual_error_message = error_str.split(':')[-1].strip()
                except Exception:
                    # If extraction fails, use the string representation as fallback
                    actual_error_message = str(create_error).split(':')[-1].strip() if ':' in str(create_error) else str(create_error)
                
                error_message = f"External ID resolution error for record {source_id}: {actual_error_message}. Original error typically caused by missing external ID references."
                sanitized_error = _sanitize_error_message(error_message)
                # Create properly padded failed line with consistent column count
                padded_failed_line = _create_padded_failed_line(
                    line, len(batch_header), sanitized_error
                )
                failed_lines.append(padded_failed_line)
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
                sanitized_error = _sanitize_error_message(error_message)
                # Create properly padded failed line with consistent column count
                padded_failed_line = _create_padded_failed_line(
                    line, header_len, sanitized_error
                )
                failed_lines.append(padded_failed_line)
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
                i,
                create_error,
                line,
                error_summary,
                header_len,
                override_error_message=prior_error,
            )
            failed_lines.append(new_failed_line)
    return {
        "id_map": id_map,
        "failed_lines": failed_lines,
        "error_summary": error_summary,
    }


def _handle_fallback_create(
    model: Any,
    current_chunk: list[list[Any]],
    batch_header: list[str],
    uid_index: int,
    context: dict[str, Any],
    ignore_list: list[str],
    progress: Any,
    aggregated_id_map: dict[str, int],
    aggregated_failed_lines: list[list[Any]],
    batch_number: int,
    error_message: str = "",
) -> None:
    """Handles fallback to individual record creation and updates aggregated results."""
    if progress is not None:
        progress.console.print(
            f"[yellow]WARN:[/] Batch {batch_number} failed `load` "
            f"({error_message}). "
            f"Falling back to `create` for {len(current_chunk)} records."
        )
    fallback_result = _create_batch_individually(
        model,
        current_chunk,
        batch_header,
        uid_index,
        context,
        ignore_list,
        progress,
        error_message,  # Pass the original load error message
    )
    # Safely update the aggregated map by filtering for valid integer IDs
    id_map = fallback_result.get("id_map", {})
    filtered_id_map = {
        key: value for key, value in id_map.items() if isinstance(value, int)
    }
    aggregated_id_map.update(filtered_id_map)
    aggregated_failed_lines.extend(fallback_result.get("failed_lines", []))


def _execute_load_batch(
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
        thread_state.get("context", DEFAULT_TRACKING_CONTEXT),
        thread_state["progress"],
    )
    uid_index = thread_state["unique_id_field_index"]
    log.info(f"DEBUG: _execute_load_batch context: {context}")
    ignore_list = thread_state.get("ignore_list", [])

    if thread_state.get("force_create"):
        # Use progress console for user-facing messages to avoid flooding logs
        # Only if progress object is available
        if progress is not None:
            progress.console.print(
                f"Batch {batch_number}: Fail mode active, using `create` method."
            )
        result = _create_batch_individually(
            model,
            batch_lines,
            batch_header,
            uid_index,
            context,
            ignore_list,
            progress,
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

    while lines_to_process:
        current_chunk = lines_to_process[:chunk_size]
        load_header, load_lines = batch_header, current_chunk

        if ignore_list:
            ignore_set = set(ignore_list)
            indices_to_keep = [
                i
                for i, h in enumerate(batch_header)
                if h.split("/")[0] not in ignore_set
            ]
            load_header = [batch_header[i] for i in indices_to_keep]
            # If all fields are ignored, we should not attempt to run load
            if not indices_to_keep:
                log.warning(
                    f"All fields in batch are in ignore list {ignore_list}. "
                    f"Skipping load operation for {len(current_chunk)} records and processing individually."
                )
                # Process each row individually
                for row in current_chunk:
                    padded_row = list(row) + [""] * (len(batch_header) - len(row))
                    error_msg = f"All fields in row were ignored by {ignore_list}"
                    sanitized_error = _sanitize_error_message(error_msg)
                    # Use the standardized function to ensure proper column count
                    failed_line = _create_padded_failed_line(
                        row, len(batch_header), f"Load failed: {sanitized_error}"
                    )
                    aggregated_failed_lines.append(failed_line)
                # Move to next chunk
                lines_to_process = lines_to_process[chunk_size:]
                continue
            else:
                max_index = max(indices_to_keep) if indices_to_keep else 0
                load_lines = []
                # Process all rows and handle those with insufficient columns
                for row in current_chunk:
                    if len(row) > max_index:
                        # Row has enough columns, process normally
                        processed_row = [row[i] for i in indices_to_keep]
                        load_lines.append(processed_row)
                    else:
                        # Row doesn't have enough columns, add to failed lines
                        # Pad the row to match the original header length
                        # before adding error message
                        # This ensures the fail file has consistent column counts
                        padded_row = list(row) + [""] * (len(batch_header) - len(row))
                        error_msg = (
                            f"Row has {len(row)} columns but requires "
                            f"at least {max_index + 1} columns based on header"
                        )
                        sanitized_error = _sanitize_error_message(error_msg)
                        failed_line = [
                            *padded_row,
                            f"Load failed: {sanitized_error}",
                        ]
                        aggregated_failed_lines.append(failed_line)

        if not load_lines:
            # If all records were filtered out due to insufficient columns,
            # lines_to_process will be updated below to move to next chunk
            # and the failed records have already been added to aggregated_failed_lines
            lines_to_process = lines_to_process[chunk_size:]
            continue

        # DEBUG: Log what we're sending to Odoo
        log.debug(
            f"Sending to Odoo - load_header (first 10): {load_header[:10]}"
            f"{'...' if len(load_header) > 10 else ''}"
        )
        if load_lines:
            log.debug(
                f"Sending to Odoo - first load_line (first 10 fields): "
                f"{load_lines[0][:10] if len(load_lines[0]) > 10 else load_lines[0]}"
                f"{'...' if len(load_lines[0]) > 10 else ''}"
            )
            log.debug(f"Sending to Odoo - load_lines count: {len(load_lines)}")
            # Log the full header and first line for debugging
            if len(load_header) > 10:
                log.debug(f"Full load_header: {load_header}")
            if len(load_lines[0]) > 10:
                log.debug(f"Full first load_line: {load_lines[0]}")

            # PRE-PROCESSING: Clean up field values to prevent type errors
            # For the load method, we largely send data as-is to let Odoo handle
            # field processing internally, similar to the predecessor approach
            # Only sanitize critical fields to prevent XML ID constraint violations
            for row in load_lines:
                # Only sanitize unique ID field values to prevent
                # XML ID constraint violations - this is the minimal processing
                # needed for the load method, similar to predecessor
                if uid_index < len(row) and row[uid_index] is not None:
                    row[uid_index] = to_xmlid(str(row[uid_index]))

                # For other fields, avoid complex type conversions that could
                # cause issues with the load method - let Odoo handle them
                # The load method is designed to handle raw data properly
        try:
            log.debug(f"Attempting `load` for chunk of batch {batch_number}...")

            # Defensive check: ensure all load_lines have same length as load_header
            # This is essential for Odoo's load method to work properly
            if load_lines and load_header:
                for idx, line in enumerate(load_lines):
                    if len(line) != len(load_header):
                        log.warning(
                            f"Mismatch in row {idx}: {len(line)} values vs {len(load_header)} headers. "
                            f"This may cause a 'tuple index out of range' error."
                        )
                        # Fallback to individual processing for this chunk to avoid the error
                        raise IndexError(
                            f"Row {idx} has {len(line)} values but header has {len(load_header)} fields. "
                            f"Load requires equal lengths. Data: {line[:10]}{'...' if len(line) > 10 else ''}. "
                            f"Header: {load_header[:10]}{'...' if len(load_header) > 10 else ''}"
                        )

            # Additional validation: Check for potentially problematic data that might
            # cause internal Odoo server errors during load processing
            if load_lines and load_header:
                validated_load_lines = []
                for _idx, line in enumerate(load_lines):
                    validated_line = []
                    for _col_idx, (_header_field, field_value) in enumerate(
                        zip(load_header, line)
                    ):
                        # Handle potentially problematic values that could cause internal Odoo errors
                        if field_value is None:
                            # Replace None values which might cause issues in some contexts
                            validated_value = ""
                        elif (
                            isinstance(field_value, (list, tuple))
                            and len(field_value) == 0
                        ):
                            # Empty lists/tuples might cause issues
                            validated_value = ""
                        # Ensure all values are in safe formats for the load method
                        elif not isinstance(field_value, (str, int, float, bool)):
                            validated_value = (
                                str(field_value) if field_value is not None else ""
                            )
                        else:
                            # Convert numeric types to strings to prevent tuple index errors during import
                            # This specifically addresses the issue where numeric values are sent to string fields
                            validated_value = (
                                str(field_value) if field_value is not None else ""
                            )
                        validated_line.append(validated_value)
                    validated_load_lines.append(validated_line)
                load_lines = validated_load_lines  # Use validated data

            # Validate that we have headers and data before calling load
            if not load_header or not load_lines:
                log.warning(
                    f"No header or data to load for batch {batch_number}, skipping."
                )
                continue

            # Final validation: Double-check that all rows have the correct length
            # This is critical for preventing server-side tuple index errors
            for idx, line in enumerate(load_lines):
                if len(line) != len(load_header):
                    raise IndexError(
                        f"Row {idx} has {len(line)} values but header has {len(load_header)} fields. "
                        f"This will cause a 'tuple index out of range' error in Odoo's server API. "
                        f"Data: {line[:10]}{'...' if len(line) > 10 else ''}. "
                        f"Header: {load_header[:10]}{'...' if len(load_header) > 10 else ''}"
                    )

            # Additional validation: Check for potentially problematic data values
            # that could cause RPC serialization issues leading to the tuple index error
            for row_idx, row in enumerate(load_lines):
                for col_idx, value in enumerate(row):
                    # Check for None values which could cause issues in certain contexts
                    if value is None:
                        # Convert to empty string to prevent RPC serialization issues
                        load_lines[row_idx][col_idx] = ""
                    # Check for problematic types that might cause serialization issues
                    elif isinstance(value, (list, tuple)) and len(value) == 0:
                        # Empty lists/tuples might cause server-side errors
                        load_lines[row_idx][col_idx] = ""
                    elif not isinstance(value, (str, int, float, bool)):
                        # Convert other types to string to prevent RPC issues
                        load_lines[row_idx][col_idx] = (
                            str(value) if value is not None else ""
                        )

            try:
                res = model.load(load_header, load_lines, context=context)
            except IndexError as e:
                # Catch the specific tuple index error that occurs server-side
                # This can happen when RPC arguments are malformed due to data issues
                if "tuple index out of range" in str(e):
                    log.error(
                        f"Server-side tuple index error caught for batch {batch_number}: {e}. "
                        f"This typically occurs when data values cause RPC serialization issues."
                    )
                    # Process each record individually to avoid server-side tuple index errors
                    raise
                else:
                    # Some other IndexError
                    raise

            if res.get("messages"):
                res["messages"][0].get("message", "Batch load failed.")
                # Don't raise immediately, log and continue to capture in fail file
            # Check for any Odoo server errors in the response that should halt
            # processing
            if res.get("messages"):
                for message in res["messages"]:
                    msg_type = message.get("type", "unknown")
                    msg_text = message.get("message", "")
                    if msg_type == "error":
                        # Only raise for actual errors, not warnings
                        log.error(f"Load operation returned fatal error: {msg_text}")
                        
                        # SIMPLEST SOLUTION: Pass the error message directly to fail file generation
                        # Store it in a way that's easy to retrieve
                        import sys
                        if not hasattr(sys.modules[__name__], 'direct_load_errors'):
                            sys.modules[__name__].direct_load_errors = {}
                        
                        # Just store the message as-is - no processing, no extraction
                        sys.modules[__name__].direct_load_errors[batch_number] = msg_text
                        
                        raise ValueError(msg_text)
                    elif msg_type in ["warning", "info"]:
                        log.warning(f"Load operation returned {msg_type}: {msg_text}")
                    else:
                        log.info(f"Load operation returned {msg_type}: {msg_text}")

            created_ids = res.get("ids", [])
            log.debug(
                f"Expected records: {len(load_lines)}, "
                f"Created records: {len(created_ids)}"
            )

            # Always log detailed information about record creation
            if len(created_ids) != len(load_lines):
                log.warning(
                    f"Record creation mismatch: Expected {len(load_lines)} records, "
                    f"but only {len(created_ids)} were created"
                )
                if len(created_ids) == 0:
                    log.error(
                        f"No records were created in this batch of {len(load_lines)}. "
                        f"This may indicate silent failures in the Odoo load operation."
                        f" Check Odoo server logs for validation errors."
                    )
                    # Log the actual data being sent for debugging
                    if load_lines:
                        log.debug("First few lines being sent:")
                        for i, line in enumerate(load_lines[:3]):
                            log.debug(f"  Line {i}: {dict(zip(load_header, line))}")
                else:
                    log.warning(
                        f"Partial record creation: {len(created_ids)}/{len(load_lines)}"
                        f"records were created. "
                        f"Some records may have failed validation."
                    )

            # Create id_map and track failed records separately
            id_map = {}
            successful_count = 0
            len(
                current_chunk
            )  # Use current_chunk instead of load_lines to match correctly
            aggregated_failed_lines_batch = []  # Track failed lines for this
            # batch specifically

            # Create id_map by matching records with created_ids
            for i, line in enumerate(current_chunk):
                if i < len(created_ids):
                    db_id = created_ids[i]
                    if db_id is not None:
                        sanitized_id = to_xmlid(line[uid_index])
                        id_map[sanitized_id] = db_id
                        successful_count += 1
                    else:
                        # Record was returned as None in the created_ids list
                        error_msg = (
                            f"Record creation failed - Odoo returned None "
                            f"for record index {i}"
                        )
                        sanitized_error = _sanitize_error_message(error_msg)
                        failed_line = [
                            *list(line),
                            f"Load failed: {sanitized_error}",
                        ]
                        aggregated_failed_lines_batch.append(failed_line)
                else:
                    # Record wasn't in the created_ids list (fewer IDs
                    # returned than sent)
                    error_msg = (
                        f"Record creation failed - expected "
                        f"{len(current_chunk)} records, "
                        f"only {len(created_ids)} returned by Odoo "
                        f"load() method"
                    )
                    sanitized_error = _sanitize_error_message(error_msg)
                    failed_line = [
                        *list(line),
                        f"Load failed: {sanitized_error}",
                    ]
                    aggregated_failed_lines_batch.append(failed_line)

            # Log id_map information for debugging
            log.debug(f"Created {len(id_map)} records in batch {batch_number}")
            if id_map:
                log.debug(f"Sample id_map entries: {dict(list(id_map.items())[:3])}")
            else:
                log.warning(f"No id_map entries created for batch {batch_number}")

            # Capture failed lines for writing to fail file
            successful_count = len(created_ids)
            len(load_lines)

            # Check if Odoo server returned messages with validation errors
            # Only mark records as failed if they weren't already successfully created
            if res.get("messages"):
                log.info(
                    f"Processing {len(res.get('messages', []))} Odoo server messages "
                    f"for chunk of {len(current_chunk)} records, "
                    f"{len(created_ids)} of which were successfully created"
                )
                # Add records to failed lines if they have server errors (even if they got IDs) or weren't successfully created
                # This ensures records that got placeholder/null IDs but had server errors are properly captured
                for i, line in enumerate(current_chunk):
                    should_mark_as_failed = False

                    # Mark as failed if record wasn't successfully created
                    if i >= len(created_ids) or created_ids[i] is None:
                        should_mark_as_failed = True
                    else:
                        # Check if this record might have been involved in the server error
                        # For errors like "No matching record found for external id",
                        # we should check if this record contains the problematic external ID
                        message_details = res.get("messages", [])
                        if message_details:
                            error_msg = str(
                                message_details[0].get(
                                    "message", "Unknown error from Odoo server"
                                )
                            )
                            # If error message contains external ID info, check if this record references it
                            if (
                                "external id" in error_msg.lower()
                                or "not found" in error_msg.lower()
                            ):
                                # Check if current line has problematic external ID references in any field
                                line_str = " ".join(
                                    str(x) for x in line if x is not None
                                ).lower()
                                # Check if any field contains external ID patterns that might be related to the error
                                if any(field.endswith("/id") for field in batch_header):
                                    # This record has external ID fields, which could be affected by external ID errors
                                    should_mark_as_failed = True
                                # Or check if the error message mentions a specific external ID that might be related
                                # Just be more cautious and assume records with external ID fields are potentially affected
                                elif (
                                    "product_template." in line_str
                                    or "res_partner." in line_str
                                ):
                                    should_mark_as_failed = True
                                # If it's a general external ID error affecting the batch, all records might be impacted
                                else:
                                    # Default to being more inclusive about failures to avoid missing records
                                    should_mark_as_failed = True

                    if should_mark_as_failed:
                        message_details = res.get("messages", [])
                        error_msg = (
                            str(
                                message_details[0].get(
                                    "message", "Unknown error from Odoo server"
                                )
                            )
                            if message_details
                            else "Unknown error"
                        )
                        sanitized_error = _sanitize_error_message(error_msg)
                        # Create properly padded failed line with consistent column count using the standard function
                        # This ensures all failed lines have the same column structure as the header
                        padded_failed_line = _create_padded_failed_line(
                            line, len(batch_header), f"Load failed: {sanitized_error}"
                        )
                        if (
                            padded_failed_line not in aggregated_failed_lines
                        ):  # Avoid duplicates
                            aggregated_failed_lines.append(padded_failed_line)
            elif len(aggregated_failed_lines_batch) > 0:
                # Add the specific records that failed to the aggregated failed lines
                log.info(
                    f"Capturing {len(aggregated_failed_lines_batch)} "
                    f"failed records for fail file"
                )
                aggregated_failed_lines.extend(aggregated_failed_lines_batch)

            # Always update the aggregated map with successful records
            # Create a new dictionary containing only the items with integer values
            filtered_id_map = {
                key: value for key, value in id_map.items() if isinstance(value, int)
            }
            aggregated_id_map.update(filtered_id_map)
            lines_to_process = lines_to_process[chunk_size:]

            # Reset serialization retry counter on successful processing
            serialization_retry_count = 0

        except IndexError:
            # Handle tuple index out of range errors specifically in load operations
            log.warning(
                "Tuple index out of range error detected, falling back to individual "
                "record processing"
            )
            progress.console.print(
                "[yellow]WARN:[/] Tuple index out of range error, falling back to "
                "individual record processing"
            )
            # Check if this might be related to external ID fields
            external_id_fields = [
                field for field in batch_header if field.endswith("/id")
            ]
            if external_id_fields:
                log.info(
                    f"Detected external ID fields ({external_id_fields}) that may be "
                    f"causing the issue. Falling back to individual record processing "
                    f"which handles external IDs differently."
                )
            _handle_fallback_create(
                model,
                current_chunk,
                batch_header,
                uid_index,
                context,
                ignore_list,
                progress,
                aggregated_id_map,
                aggregated_failed_lines,
                batch_number,
                error_message="type conversion error or invalid external ID reference",
            )
            lines_to_process = lines_to_process[chunk_size:]

        except Exception as e:
            error_str = str(e).lower()
            
            # Extract load error from SIMPLE storage
            load_error = ""
            import sys
            if hasattr(sys.modules[__name__], 'direct_load_errors'):
                current_batch_load_errors = sys.modules[__name__].direct_load_errors
                if batch_number in current_batch_load_errors:
                    load_error = current_batch_load_errors[batch_number]
                    # Clean up to avoid memory leak
                    del current_batch_load_errors[batch_number]
                    log.debug(f"Extracted load error from SIMPLE storage: {load_error[:100]}...")
                else:
                    log.debug(f"No load error found in SIMPLE storage for batch {batch_number}")
            else:
                log.debug("No SIMPLE module-level load error storage found")

            # Debug: If we still don't have a load error, log the exception details
            if not load_error:
                log.debug(f"No load error found. Exception details: {str(e)[:200]}...")

            # SPECIAL CASE: Client-side timeouts for local processing
            # Instead of ignoring, add to failed lines so they can be retried later
            if _is_client_timeout_error(e):
                log.warning(
                    f"Client-side timeout error for chunk of {len(current_chunk)} records. "
                    f"Adding records to fail file for retry: {e}"
                )
                error_msg = f"Client-side timeout: {_extract_clean_error_message(e)}"
                sanitized_error = _sanitize_error_message(error_msg)
                for line in current_chunk:
                    padded_failed_line = _create_padded_failed_line(
                        line, len(batch_header), sanitized_error
                    )
                    aggregated_failed_lines.append(padded_failed_line)
                lines_to_process = lines_to_process[chunk_size:]
                continue

            # SPECIAL CASE: Database connection pool exhaustion
            # These should be treated as scalable errors to reduce load on the server
            if _is_database_connection_error(e):
                log.warning(
                    "Database connection pool exhaustion detected. "
                    "Reducing chunk size and retrying to reduce server load."
                )
                is_scalable_error = True

            # SPECIAL CASE: Tuple index out of range errors
            # These can occur when sending wrong types to Odoo fields
            # Particularly common with external ID references that don't exist
            elif _is_tuple_index_error(e):
                # Check if this might be related to external ID fields
                external_id_fields = [
                    field for field in batch_header if field.endswith("/id")
                ]
                if external_id_fields:
                    log.info(
                        f"Detected external ID fields ({external_id_fields}) that may be "
                        f"causing the tuple index error. Falling back to individual "
                        f"record processing which handles external IDs differently."
                    )
                # Extract load error if available (from ValueError with load_error attribute)
                load_error = getattr(e, 'load_error', '')
                # Use progress console for user-facing messages to avoid flooding logs
                # Only if progress object is available
                _handle_fallback_create(
                    model,
                    current_chunk,
                    batch_header,
                    uid_index,
                    context,
                    ignore_list,
                    progress,
                    aggregated_id_map,
                    aggregated_failed_lines,
                    batch_number,
                    error_message="type conversion error or invalid external ID reference" if not load_error else load_error,
                )
                lines_to_process = lines_to_process[chunk_size:]
                continue

            # For all other exceptions, use the original scalable error detection
            # Also check for constraint violations which should be treated as
            # non-scalable
            is_constraint_violation = (
                "constraint" in error_str
                or "violation" in error_str
                or "not-null constraint" in error_str
                or "mandatory field" in error_str
            )
            is_scalable_error = (
                "memory" in error_str
                or "out of memory" in error_str
                or "502" in error_str
                or "gateway" in error_str
                or "proxy" in error_str
                or "timeout" in error_str
                or "could not serialize access" in error_str
                or "concurrent update" in error_str
                or "connection pool is full" in error_str.lower()
                or "too many connections" in error_str.lower()
                or "poolerror" in error_str.lower()
            )

            # Handle constraint violations separately - these are data issues,
            #  not scalable issues
            if is_constraint_violation:
                # Constraint violations are data problems, add all records to
                # failed lines
                clean_error = _extract_clean_error_message(e).strip().replace("\\n", " ")
                log.error(
                    f"Constraint violation in batch {batch_number}: {clean_error}"
                )
                error_msg = f"Constraint violation: {clean_error}"

                for line in current_chunk:
                    # Create properly padded failed line with consistent column count
                    padded_failed_line = _create_padded_failed_line(
                        line, len(batch_header), error_msg
                    )
                    aggregated_failed_lines.append(padded_failed_line)

                lines_to_process = lines_to_process[chunk_size:]
                continue

            if is_scalable_error and chunk_size > 1:
                chunk_size = max(1, chunk_size // 2)
                progress.console.print(
                    f"[yellow]WARN:[/] Batch {batch_number} hit scalable error. "
                    f"Reducing chunk size to {chunk_size} and retrying."
                )
                if (
                    "could not serialize access" in error_str
                    or "concurrent update" in error_str
                ):
                    progress.console.print(
                        "[yellow]INFO:[/] Database serialization conflict detected. "
                        "This is often caused by concurrent processes updating the "
                        "same records. Retrying with smaller batch size."
                    )

                    # Add a small delay for serialization conflicts
                    # to give other processes time to complete.
                    time.sleep(
                        0.1 * serialization_retry_count
                    )  # Linear backoff: 0.1s, 0.2s, 0.3s

                    # Track serialization retries to prevent infinite loops
                    serialization_retry_count += 1
                    if serialization_retry_count >= max_serialization_retries:
                        progress.console.print(
                            f"[yellow]WARN:[/] Max serialization retries "
                            f"({max_serialization_retries}) reached. "
                            f"Moving records to fallback processing to prevent infinite"
                            f" retry loop."
                        )
                        # Fall back to individual create processing
                        # instead of continuing to retry
                        clean_error = _extract_clean_error_message(e).strip().replace("\\n", " ")
                        progress.console.print(
                            f"[yellow]WARN:[/] Batch {batch_number} failed `load` "
                            f"('{clean_error}'). "
                            f"Falling back to `create` for {len(current_chunk)} "
                            f"records due to persistent serialization conflicts."
                        )
                        _handle_fallback_create(
                            model,
                            current_chunk,
                            batch_header,
                            uid_index,
                            context,
                            ignore_list,
                            progress,
                            aggregated_id_map,
                            aggregated_failed_lines,
                            batch_number,
                            error_message=clean_error,
                        )
                        lines_to_process = lines_to_process[chunk_size:]
                        serialization_retry_count = 0  # Reset counter for next batch
                        continue
                continue

            clean_error = _extract_clean_error_message(e).strip().replace("\\n", " ")
            progress.console.print(
                f"[yellow]WARN:[/] Batch {batch_number} failed `load` "
                f"('{clean_error}'). "
                f"Falling back to `create` for {len(current_chunk)} records."
            )
            _handle_fallback_create(
                model,
                current_chunk,
                batch_header,
                uid_index,
                context,
                ignore_list,
                progress,
                aggregated_id_map,
                aggregated_failed_lines,
                batch_number,
                error_message=clean_error,
            )
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
        # Sanitize values to prevent tuple index errors during write operations
        # Similar to predecessor approach: avoid complex value processing that might cause issues
        sanitized_vals = {}
        for key, value in vals.items():
            # For external ID fields (e.g., fields ending with '/id'),
            # process them normally to avoid not-null constraint violations
            # Convert external ID field names like 'partner_id/id' to 'partner_id'
            if key.endswith("/id"):
                base_key = key[
                    :-3
                ]  # Remove '/id' suffix to get base field name like 'partner_id'
                # Add all external ID fields to sanitized values using base field name
                sanitized_vals[base_key] = value
            else:
                # For other fields, ensure valid values
                if value is None:
                    # Skip None values which might cause tuple index errors
                    continue
                else:
                    sanitized_vals[key] = value

        # The core of the fix: use model.write(ids, sanitized_vals) for batch updates.
        model.write(ids, sanitized_vals, context=context)
        return {
            "failed_writes": [],
            "successful_writes": len(ids),
            "success": True,
        }
    except Exception as e:
        error_message = _extract_clean_error_message(e).replace("\n", " | ")
        # If the batch fails, all IDs in it are considered failed.
        failed_writes = [(db_id, vals, error_message) for db_id in ids]
        return {
            "failed_writes": failed_writes,
            "error_summary": error_message,
            "successful_writes": 0,
            "success": False,
        }


def _run_threaded_pass(
    rpc_thread: RPCThreadImport,
    target_func: Any,
    batches: Iterable[tuple[int, Any]],
    thread_state: dict[str, Any],
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

    Returns:
        tuple[dict[str, Any], bool]: A typle and a dictionary containing
        the aggregated results from all
        worker threads, such as `id_map` and `failed_lines`.
    """
    # This logic is brittle but preserved to minimize unrelated changes.
    # It dynamically constructs arguments based on the target function name.
    futures = {
        rpc_thread.spawn_thread(
            target_func,
            (
                [thread_state, data, num]
                if target_func.__name__ == "_execute_write_batch"
                else [thread_state, data, thread_state.get("batch_header"), num]
            ),
        )
        for num, data in batches
        if not rpc_thread.abort_flag
    }

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
                else:
                    consecutive_failures += 1
                    # Only abort after a very large number of consecutive failures
                    # to allow processing of datasets with many validation errors
                    if consecutive_failures >= 1000:  # Increased from 50 to 1000
                        log.warning(
                            f"Stopping import: {consecutive_failures} "
                            f"consecutive batches have failed. "
                            f"This indicates a persistent systemic issue "
                            f"that needs investigation."
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
                # This handles exceptions that occur during the processing of the result,
                # not exceptions from the future itself. If this happens, it's likely
                # a programming error, so we should still try to process remaining futures
                # but flag that there was an issue
                log.error(f"Error processing worker thread result: {e}", exc_info=True)
                rpc_thread.abort_flag = True
                rpc_thread.progress.console.print(
                    f"[bold red]Error processing thread result: {e}[/bold red]"
                )
                rpc_thread.progress.update(
                    rpc_thread.task_id,
                    description="[bold red]Error processing results[/bold red]",
                    refresh=True,
                )
                # Continue to process any remaining completed futures
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
        # Don't abort the import if all batches failed - this just means
        # all records had errors
        # which should still result in a fail file with all the problematic records
        if futures and successful_batches == 0:
            log.warning(
                "All batches failed, but import completed. Check fail file for details."
            )
        rpc_thread.executor.shutdown(wait=True, cancel_futures=True)
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
    header: list[str],
    all_data: list[list[Any]],
    unique_id_field: str,
    deferred_fields: list[str],
    ignore: Union[str, list[str]],
    context: dict[str, Any],
    fail_writer: Optional[Any],
    fail_handle: Optional[TextIO],
    max_connection: int,
    batch_size: int,
    o2m: bool,
    split_by_cols: Optional[list[str]],
    force_create: bool = False,
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
        o2m (bool): Enables one-to-many batching logic.
        force_create (bool): If True, bypasses the `load` method and uses
            the `create` method directly. Used for fail mode.
        split_by_cols: The column names to group records by to avoid concurrent updates.

    Returns:
        dict[str, Any]: A dictionary containing the results of the pass,
            including the `id_map` ({source_id: db_id}), a list of any
            `failed_lines`, and a `success` boolean flag.
    """
    rpc_pass_1 = RPCThreadImport(
        max_connection, progress, TaskID(0), fail_writer, fail_handle
    )
    pass_1_header, pass_1_data = header, all_data
    # Ensure ignore is a list before concatenation
    if isinstance(ignore, str):
        ignore_list = [ignore]
    else:
        ignore_list = ignore
    # Add ALL deferred fields to the ignore list for Pass 1, not just self-referencing ones
    # This allows main records to be imported successfully in Pass 1 without external ID dependencies
    # The deferred fields will be processed safely in Pass 2 when all records should exist in the database
    # Note: The filtering logic uses header.split("/")[0] to compare field names,
    # so we need to add the base field names like 'optional_product_ids' for fields like 'optional_product_ids/id'
    deferred_fields_list = deferred_fields or []

    # Extract base field names for filtering (remove '/id' suffix if present)
    # The filtering logic compares header_field.split('/')[0] with items in ignore_set
    deferred_fields_base = []
    for field in deferred_fields_list:
        base_name = field.split("/")[
            0
        ]  # This extracts 'optional_product_ids' from 'optional_product_ids/id'
        if base_name not in deferred_fields_base:  # Avoid duplicates
            deferred_fields_base.append(base_name)

    pass_1_ignore_list = deferred_fields_base + ignore_list

    # Validate that the unique ID field exists in the header
    # This is critical for the import process to function correctly
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
        "context": context,
        "unique_id_field_index": pass_1_uid_index,
        "batch_header": pass_1_header,
        "force_create": force_create,
        "progress": progress,
        "ignore_list": pass_1_ignore_list,
    }

    results, aborted = _run_threaded_pass(
        rpc_pass_1, _execute_load_batch, pass_1_batches, thread_state_1
    )
    results["success"] = not aborted
    return results


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
        return True, 0

    # --- Grouping Logic ---
    from collections import defaultdict

    grouped_writes = defaultdict(list)
    for db_id, vals in pass_2_data_to_write:
        # The key must be hashable, so we convert the dict to a frozenset of items.
        vals_key = frozenset(vals.items())
        grouped_writes[vals_key].append(db_id)

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
    }
    pass_2_results, aborted = _run_threaded_pass(
        rpc_pass_2,
        _execute_write_batch,
        list(enumerate(pass_2_batches, 1)),
        thread_state_2,
    )

    failed_writes = pass_2_results.get("failed_writes", [])
    if fail_writer and failed_writes:
        log.warning("Writing failed Pass 2 records to fail file...")
        reverse_id_map = {v: k for k, v in id_map.items()}
        source_data_map = {row[unique_id_field_index]: row for row in all_data}
        failed_lines = []
        header_length = len(header)  # Expected number of columns
        
        # Read existing Phase 1 errors to merge with Phase 2 errors
        existing_phase1_errors = _read_existing_fail_file(fail_file, encoding, separator)
        
        for db_id, _, error_message in failed_writes:
            source_id = reverse_id_map.get(db_id)
            if source_id and source_id in source_data_map:
                original_row = list(source_data_map[source_id])
                
                # Check if this record already has a Phase 1 error
                if str(source_id) in existing_phase1_errors:
                    # Merge Phase 1 and Phase 2 errors
                    existing_line = existing_phase1_errors[str(source_id)]
                    phase1_error = existing_line[-1] if len(existing_line) > header_length else ""
                    
                    # Combine errors with clear separation
                    if phase1_error and error_message:
                        combined_error = f"Phase 1: {phase1_error} | Phase 2: {error_message}"
                    elif phase1_error:
                        combined_error = f"Phase 1: {phase1_error}"
                    elif error_message:
                        combined_error = f"Phase 2: {error_message}"
                    else:
                        combined_error = "Unknown error"
                    
                    # Create new failed line with combined error
                    padded_failed_line = _create_padded_failed_line(
                        original_row, header_length, combined_error
                    )
                    failed_lines.append(padded_failed_line)
                    
                    log.debug(f"Merged errors for record {source_id}: {combined_error}")
                else:
                    # No existing Phase 1 error, just use Phase 2 error
                    if error_message:
                        error_with_phase = f"Phase 2: {error_message}"
                    else:
                        error_with_phase = "Phase 2: Unknown error"
                    
                    padded_failed_line = _create_padded_failed_line(
                        original_row, header_length, error_with_phase
                    )
                    failed_lines.append(padded_failed_line)
        
        if failed_lines:
            fail_writer.writerows(failed_lines)

    # Pass 2 is successful ONLY if not aborted AND no writes failed.
    successful_writes = pass_2_results.get("successful_writes", 0)
    return not aborted and not failed_writes, successful_writes


def import_data(
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
    skip: int = 0,
    force_create: bool = False,
    o2m: bool = False,
    split_by_cols: Optional[list[str]] = None,
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
        skip (int): The number of lines to skip at the top of the source file.
        force_create (bool): If True, bypasses the `load` method and uses
            the `create` method directly. Used for fail mode.
        o2m (bool): Enables special handling for one-to-many imports where
            child lines follow a parent record.
        split_by_cols: The column names to group records by to avoid concurrent updates.

    Returns:
        tuple[bool, int]: True if the entire import process completed without any
        critical, process-halting errors, False otherwise.
    """
    deferred = deferred_fields or []
    ignore = ignore or []

    # Merge provided context with default tracking context
    # This ensures that even if a custom context is passed, we still get the defaults
    # unless they are explicitly overridden in the passed context.
    final_context = DEFAULT_TRACKING_CONTEXT.copy()
    if context:
        final_context.update(context)

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
    
    # Generate environment-specific fail file paths if not provided
    if not fail_file:
        environment = _get_environment_from_connection(config)
        fail_file = _get_fail_file_path(file_csv, environment, "fail")
        log.info(f"Using auto-generated fail file: {fail_file}")
    
    fail_writer, fail_handle = _setup_fail_file(fail_file, header, separator, encoding)
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
    with progress:
        try:
            pass_1_results = _orchestrate_pass_1(
                progress,
                model_obj,
                model,
                header,
                all_data,
                unique_id_field,
                deferred,
                ignore,
                final_context,
                fail_writer,
                fail_handle,
                max_connection,
                batch_size,
                o2m,
                split_by_cols,
                force_create,
            )
            # A pass is only successful if it wasn't aborted.
            pass_1_successful = pass_1_results.get("success", False)
            if not pass_1_successful:
                return False, {}

            # If we get here, Pass 1 was not aborted. Now determine final status.
            id_map = pass_1_results.get("id_map", {})
            pass_2_successful = True  # Assume success if no Pass 2 is needed.
            updates_made = 0

            if deferred:
                pass_2_successful, updates_made = _orchestrate_pass_2(
                    progress,
                    model_obj,
                    model,
                    header,
                    all_data,
                    unique_id_field,
                    id_map,
                    deferred,
                    final_context,
                    fail_writer,
                    fail_handle,
                    max_connection,
                    batch_size,
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
    return overall_success, stats
