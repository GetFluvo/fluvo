"""Handles writing failed records to CSV files."""

import csv
import re
from pathlib import Path
from typing import Any, Optional, Union

from .internal.ui import _show_error_panel


def _get_env_from_config(config: Union[str, dict[str, Any], None]) -> Optional[str]:
    """Extracts the environment name from a config file path.

    Supports patterns like:
    - test_connection.conf -> test
    - uat.conf -> uat
    - prod_connection.conf -> prod

    Args:
        config: Either a config file path (str), a config dict, or None.

    Returns:
        The environment name, or None if it cannot be determined.
    """
    if config is None:
        return None

    if isinstance(config, dict):
        # Config dict may have _config_file key
        config_file = config.get("_config_file", "")
    else:
        config_file = config

    if not config_file:
        return None

    # Get the filename without extension
    basename = Path(config_file).stem

    # Remove common suffixes like _connection, _conn
    env_name = re.sub(r"(_connection|_conn)$", "", basename, flags=re.IGNORECASE)

    return env_name if env_name else None


def write_relational_failures_to_csv(
    model: str,
    field: str,
    original_filename: str,
    failed_records: list[dict[str, Any]],
    config: Union[str, dict[str, Any], None] = None,
) -> None:
    """Writes failed relational link records to a dedicated CSV file.

    Args:
        model: The main Odoo model being imported (e.g., 'res.partner').
        field: The relational field that failed (e.g., 'category_id').
        original_filename: The path to the original source CSV file.
        failed_records: A list of dictionaries, each representing a failed link.
        config: Optional config file path or dict to determine environment folder.
    """
    if not failed_records:
        return

    # Determine environment-specific output directory from config
    original_path = Path(original_filename).resolve()
    env_name = _get_env_from_config(config)
    if env_name:
        env_output_dir = original_path.parent / env_name
        env_output_dir.mkdir(parents=True, exist_ok=True)
    else:
        env_output_dir = original_path.parent

    fail_filename = f"{original_path.stem}_relations_fail.csv"
    fail_filepath = env_output_dir / fail_filename

    try:
        file_exists = fail_filepath.exists()
        with open(fail_filepath, "a", newline="", encoding="utf-8") as f:
            header = [
                "model",
                "field",
                "parent_external_id",
                "related_external_id",
                "error_reason",
            ]
            writer = csv.DictWriter(f, fieldnames=header)
            if not file_exists:
                writer.writeheader()
            writer.writerows(failed_records)

    except OSError as e:
        _show_error_panel(
            "File Write Error", f"Could not write to fail file {fail_filepath}: {e}"
        )
