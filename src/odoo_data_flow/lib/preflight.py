"""This module provides a registry and functions for pre-flight checks.

These checks are run before the main import process to catch common,
systemic errors early (e.g., missing languages, incorrect configuration).
"""

from typing import Any, Callable, Optional, cast

import polars as pl
from polars.exceptions import ColumnNotFoundError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from odoo_data_flow.enums import PreflightMode

from ..logging_config import log
from . import conf_lib
from .actions import language_installer
from .internal.ui import _show_error_panel

# A registry to hold all pre-flight check functions
PREFLIGHT_CHECKS: list[Callable[..., bool]] = []


def register_check(func: Callable[..., bool]) -> Callable[..., bool]:
    """A decorator to register a new pre-flight check function."""
    PREFLIGHT_CHECKS.append(func)
    return func


@register_check
def connection_check(
    preflight_mode: "PreflightMode", config: str, **kwargs: Any
) -> bool:
    """Pre-flight check to verify connection to Odoo."""
    log.info("Running pre-flight check: Verifying Odoo connection...")
    try:
        # This line implicitly checks the connection
        conf_lib.get_connection_from_config(config_file=config)
        log.info("Connection to Odoo successful.")
        return True
    except Exception as e:
        _show_error_panel(
            "Odoo Connection Error",
            f"Could not establish connection to Odoo. "
            f"Please check your configuration.\nError: {e}",
        )
        return False


def _get_installed_languages(config_file: str) -> Optional[set[str]]:
    """Connects to Odoo and returns the set of installed language codes."""
    try:
        connection = conf_lib.get_connection_from_config(config_file)
        lang_obj = connection.get_model("res.lang")
        installed_langs_data = lang_obj.search_read([("active", "=", True)], ["code"])
        return {lang["code"] for lang in installed_langs_data}
    except Exception as e:
        error_message = str(e)
        title = "Odoo Connection Error"
        friendly_message = (
            "Could not fetch installed languages from Odoo. This usually means "
            "the connection details in your configuration file are incorrect.\n\n"
            "Please verify the following:\n"
            "  - [bold]hostname[/bold] is correct\n"
            "  - [bold]database[/bold] name is correct\n"
            "  - [bold]login[/bold] (username) is correct\n"
            "  - [bold]password[/bold] is correct\n\n"
            f"[bold]Original Error:[/bold] {error_message}"
        )
        _show_error_panel(title, friendly_message)
        return None


@register_check
def language_check(
    preflight_mode: PreflightMode,
    model: str,
    filename: str,
    config: str,
    headless: bool,
    **kwargs: Any,
) -> bool:
    """Pre-flight check to verify that all required languages are installed."""
    if preflight_mode == PreflightMode.FAIL_MODE:
        log.debug("Skipping language pre-flight check in --fail mode.")
        return True

    if model not in ("res.partner", "res.users"):
        return True

    log.info("Running pre-flight check: Verifying required languages...")

    try:
        # FIX 2: Add `truncate_ragged_lines` to handle malformed CSV files.
        required_languages = (
            pl.read_csv(
                filename,
                separator=kwargs.get("separator", ";"),
                truncate_ragged_lines=True,
            )
            .get_column("lang")
            .unique()
            .drop_nulls()
            .to_list()
        )
    except ColumnNotFoundError:
        log.debug("No 'lang' column found in source file. Skipping language check.")
        return True
    except Exception as e:
        log.warning(
            f"Could not read languages from source file. Skipping check. Error: {e}"
        )
        return True

    if not required_languages:
        return True

    installed_languages = _get_installed_languages(config)
    if installed_languages is None:
        return False  # Connection failed, error already shown.

    missing_languages = set(required_languages) - installed_languages

    if not missing_languages:
        log.info("All required languages are installed.")
        return True

    # This part of the logic now only runs in NORMAL mode.
    console = Console(stderr=True, style="bold yellow")
    message = (
        "The following required languages are not installed in the target "
        f"database:\n\n"
        f"[bold red]{', '.join(sorted(list(missing_languages)))}[/bold red]"
        f"\n\nThis is likely to cause the import to fail."
    )
    console.print(
        Panel(
            message,
            title="Missing Languages Detected",
            border_style="yellow",
        )
    )

    if headless:
        log.info("--headless mode detected. Auto-confirming language installation.")
        return language_installer.run_language_installation(
            config, list(missing_languages)
        )

    proceed = Confirm.ask("Do you want to install them now?", default=True)
    if proceed:
        return language_installer.run_language_installation(
            config, list(missing_languages)
        )
    else:
        log.warning("Language installation cancelled by user. Aborting import.")
        return False


def _get_odoo_fields(config: str, model: str) -> Optional[dict[str, Any]]:
    """Fetches the field schema for a given model from Odoo.

    Args:
        config: The path to the connection configuration file.
        model: The target Odoo model name.

    Returns:
        A dictionary of the model's fields, or None on failure.
    """
    try:
        connection: Any = conf_lib.get_connection_from_config(config_file=config)
        model_obj = connection.get_model(model)
        # FIX: Use `cast` to inform mypy of the expected return type.
        return cast(dict[str, Any], model_obj.fields_get())
    except Exception as e:
        _show_error_panel(
            "Odoo Connection Error",
            f"Could not connect to Odoo to get model fields. Error: {e}",
        )
        return None


def _get_csv_header(filename: str, separator: str) -> Optional[list[str]]:
    """Reads the header from a CSV file.

    Args:
        filename: The path to the source CSV file.
        separator: The delimiter used in the CSV file.

    Returns:
        A list of strings representing the header, or None on failure.
    """
    try:
        return pl.read_csv(filename, separator=separator, n_rows=0).columns
    except Exception as e:
        _show_error_panel("File Read Error", f"Could not read CSV header. Error: {e}")
        return None


def _validate_header(
    csv_header: list[str], odoo_fields: dict[str, Any], model: str
) -> bool:
    """Validates that all CSV columns exist as fields on the Odoo model."""
    odoo_field_names = set(odoo_fields.keys())
    missing_fields = [
        field
        for field in csv_header
        if (field.split("/")[0] not in odoo_field_names) or (field.endswith("/.id"))
    ]

    if missing_fields:
        error_message = "The following columns do not exist on the Odoo model:\n"
        for field in missing_fields:
            error_message += f"  - '{field}' is not a valid field on model '{model}'\n"
        _show_error_panel("Invalid Fields Found", error_message)
        return False
    return True


def _detect_and_plan_deferrals(
    csv_header: list[str],
    odoo_fields: dict[str, Any],
    model: str,
    import_plan: Optional[dict[str, Any]],
    kwargs: dict[str, Any],
) -> bool:
    """Detects deferrable fields and updates the import plan."""
    deferrable_fields = []
    for field_name in csv_header:
        clean_field_name = field_name.replace("/id", "")
        if clean_field_name in odoo_fields:
            field_info = odoo_fields[clean_field_name]
            is_m2o_self = (
                field_info.get("type") == "many2one"
                and field_info.get("relation") == model
            )
            is_m2m = field_info.get("type") == "many2many"
            if is_m2o_self or is_m2m:
                deferrable_fields.append(clean_field_name)

    if deferrable_fields:
        log.info(f"Detected deferrable fields: {deferrable_fields}")
        unique_id_field = kwargs.get("unique_id_field")

        # --- NEW: Automatic 'id' column detection ---
        if not unique_id_field:
            if "id" in csv_header:
                log.info("Automatically using 'id' column as the unique identifier.")
                unique_id_field = "id"
                if import_plan is not None:
                    import_plan["unique_id_field"] = "id"  # Store the inferred field
            else:
                _show_error_panel(
                    "Action Required for Two-Pass Import",
                    "Deferrable fields were detected, but no 'id' column was found.\n"
                    "Please specify the unique ID column using the "
                    "[bold cyan]--unique-id-field[/bold cyan] option.",
                )
                return False

        if import_plan is not None:
            import_plan["deferred_fields"] = deferrable_fields
    return True


@register_check
def field_existence_check(
    preflight_mode: "PreflightMode",
    model: str,
    filename: str,
    config: str,
    import_plan: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> bool:
    """Verifies fields exist and detects which fields require deferred import.

    Args:
        preflight_mode: The current pre-flight mode.
        model: The target Odoo model name.
        filename: The path to the source CSV file.
        config: The path to the connection configuration file.
        import_plan: A dictionary to be populated with import strategy details.
        **kwargs: Additional arguments passed from the importer.

    Returns:
        True if all checks pass, False otherwise.
    """
    log.info(f"Running pre-flight check: Verifying fields for model '{model}'...")
    csv_header = _get_csv_header(filename, kwargs.get("separator", ";"))
    if not csv_header:
        return False

    # FIX: Filter the header based on the ignore list BEFORE validation.
    ignore_list = kwargs.get("ignore", [])
    header_to_validate = [h for h in csv_header if h not in ignore_list]

    odoo_fields = _get_odoo_fields(config, model)
    if not odoo_fields:
        return False

    # Step 1: Validate that all columns in the CSV exist on the Odoo model.
    # This check is crucial and should run in both NORMAL and FAIL modes.
    if not _validate_header(header_to_validate, odoo_fields, model):
        return False

    # Step 2: Detect deferrable fields and plan a two-pass strategy.
    # This should ONLY run in NORMAL mode, as fail runs are always single-pass.
    if preflight_mode == PreflightMode.NORMAL:
        if not _detect_and_plan_deferrals(
            header_to_validate, odoo_fields, model, import_plan, kwargs
        ):
            return False

    log.info("Pre-flight Check Successful: All columns are valid fields on the model.")
    return True
