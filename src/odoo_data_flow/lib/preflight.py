"""This module provides a registry and functions for pre-flight checks.

These checks are run before the main import process to catch common,
systemic errors early (e.g., missing languages, incorrect configuration).
"""

import csv
from typing import Any, Callable, Optional, Union, cast

import polars as pl
from polars.exceptions import ColumnNotFoundError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from odoo_data_flow.enums import PreflightMode

from ..logging_config import log
from . import cache, conf_lib, sort
from .actions import language_installer
from .internal.ui import _show_error_panel, _show_warning_panel

# A registry to hold all pre-flight check functions
PREFLIGHT_CHECKS: list[Callable[..., bool]] = []


def _handle_m2m_field(
    field_name: str,
    clean_field_name: str,
    field_info: dict[str, Any],
    df: pl.DataFrame,
) -> tuple[bool, dict[str, Any]]:
    """Handle many2many field processing and strategy selection."""
    # Ensure the column is treated as string for splitting
    relation_count = (
        df.lazy()
        .select(pl.col(field_name).cast(pl.Utf8).str.split(","))
        .select(pl.col(field_name).list.len())
        .sum()
        .collect()
        .item()
    )
    # Check if required keys exist for many2many fields
    relation_table = field_info.get("relation_table")
    relation_field = field_info.get("relation_field")
    relation = field_info.get("relation")

    strategy_details = {}
    if relation_table and relation_field:
        if relation_count >= 500:
            strategy_details = {
                "strategy": "direct_relational_import",
                "relation_table": relation_table,
                "relation_field": relation_field,
                "relation": relation,
            }
        else:
            strategy_details = {
                "strategy": "write_tuple",
                "relation_table": relation_table,
                "relation_field": relation_field,
                "relation": relation,
            }
    else:
        # Log a warning when relation information is incomplete
        log.warning(
            f"Field '{clean_field_name}' is missing relation_table or relation_field "
            f"in Odoo metadata. This may cause issues with relational import."
        )
        # Fallback strategy when relation information is incomplete
        # Fallback to 'write_tuple' strategy. The import process will later
        # attempt to derive the missing relational information.
        strategy_details = {
            "strategy": "write_tuple",
            "relation_table": relation_table,
            "relation_field": relation_field,
            "relation": relation,
        }

    return True, strategy_details


def register_check(func: Callable[..., bool]) -> Callable[..., bool]:
    """A decorator to register a new pre-flight check function."""
    PREFLIGHT_CHECKS.append(func)
    return func


@register_check
def connection_check(
    preflight_mode: "PreflightMode", config: Union[str, dict[str, Any]], **kwargs: Any
) -> bool:
    """Pre-flight check to verify connection to Odoo."""
    log.info("Running pre-flight check: Verifying Odoo connection...")
    try:
        if isinstance(config, dict):
            conf_lib.get_connection_from_dict(config)
        else:
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


@register_check
def self_referencing_check(
    preflight_mode: "PreflightMode",
    filename: str,
    import_plan: dict[str, Any],
    **kwargs: Any,
) -> bool:
    """Detects self-referencing hierarchies and plans the sorting strategy."""
    if kwargs.get("o2m"):
        return True  # Skip this check if o2m is enabled

    log.info("Running pre-flight check: Detecting self-referencing hierarchy...")
    # We assume 'id' and 'parent_id' as conventional names.
    # This could be made configurable later if needed.
    result = sort.sort_for_self_referencing(
        filename,
        id_column="id",
        parent_column="parent_id",
        separator=kwargs.get("separator", ";"),
    )
    if result is False:
        # This means there was an error in sort_for_self_referencing
        # The error would have been displayed by the function itself
        return False
    elif result:
        # This means sorting was performed and we have a file path
        log.info(
            "Detected self-referencing hierarchy. Planning one-pass sort strategy."
        )
        import_plan["strategy"] = "sort_and_one_pass_load"
        import_plan["id_column"] = "id"
        import_plan["parent_column"] = "parent_id"
        return True
    else:
        # result is None, meaning no hierarchy detected
        log.info("No self-referencing hierarchy detected.")
        return True


def _get_installed_languages(config: Union[str, dict[str, Any]]) -> Optional[set[str]]:
    """Connects to Odoo and returns the set of installed language codes."""
    try:
        if isinstance(config, dict):
            connection = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config)

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


def _get_required_languages(filename: str, separator: str) -> Optional[list[str]]:
    """Extracts the list of required languages from the source file."""
    try:
        lang_series = (
            pl.read_csv(
                filename,
                separator=separator,
                truncate_ragged_lines=True,
                infer_schema_length=0,  # Read all columns as strings
            )
            .get_column("lang")
            .unique()
            .drop_nulls()
        )
        # Filter out empty strings (polars Series filter takes a boolean mask)
        return lang_series.filter(lang_series != "").to_list()
    except ColumnNotFoundError:
        log.debug("No 'lang' column found in source file. Skipping language check.")
        return []
    except Exception as e:
        log.warning(
            f"Could not read languages from source file. Skipping check. Error: {e}"
        )
        return None


def _handle_missing_languages(
    config: Union[str, dict[str, Any]],
    missing_languages: set[str],
    headless: bool,
) -> bool:
    """Handles the process of installing missing languages."""
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
        if isinstance(config, dict):
            log.error("Language installation from a dict config is not supported.")
            return False
        return language_installer.run_language_installation(
            config, list(missing_languages)
        )

    if not Confirm.ask("Do you want to install them now?", default=True):
        log.warning("Language installation cancelled by user. Aborting import.")
        return False

    if isinstance(config, dict):
        log.error("Language installation from a dict config is not supported.")
        return False
    return language_installer.run_language_installation(config, list(missing_languages))


@register_check
def language_check(
    preflight_mode: PreflightMode,
    model: str,
    filename: str,
    config: Union[str, dict[str, Any]],
    headless: bool,
    **kwargs: Any,
) -> bool:
    """Pre-flight check to verify that all required languages are installed."""
    if preflight_mode == PreflightMode.FAIL_MODE or model not in (
        "res.partner",
        "res.users",
    ):
        log.debug("Skipping language pre-flight check.")
        return True

    log.info("Running pre-flight check: Verifying required languages...")

    required_languages = _get_required_languages(filename, kwargs.get("separator", ";"))
    if required_languages is None or not required_languages:
        return True

    installed_languages = _get_installed_languages(config)
    if installed_languages is None:
        return False

    missing_languages = set(required_languages) - installed_languages
    if not missing_languages:
        log.info("All required languages are installed.")
        return True

    return _handle_missing_languages(config, missing_languages, headless)


def _get_odoo_fields(
    config: Union[str, dict[str, Any]], model: str
) -> Optional[dict[str, Any]]:
    """Fetches the field schema for a given model from Odoo.

    Args:
        config: The path to the connection configuration file or a config dict.
        model: The target Odoo model name.

    Returns:
        A dictionary of the model's fields, or None on failure.
    """
    # 1. Try to load from cache first
    if isinstance(config, str):
        cached_fields = cache.load_fields_get_cache(config, model)
        if cached_fields:
            return cached_fields

    # 2. If cache miss, fetch from Odoo
    log.info(f"Cache miss for '{model}' fields, fetching from Odoo...")
    try:
        connection_obj: Any
        if isinstance(config, dict):
            connection_obj = conf_lib.get_connection_from_dict(config)
        else:
            connection_obj = conf_lib.get_connection_from_config(config_file=config)
        model_obj = connection_obj.get_model(model)
        odoo_fields = cast(dict[str, Any], model_obj.fields_get())

        # 3. Save the result to the cache for next time
        if isinstance(config, str):
            cache.save_fields_get_cache(config, model, odoo_fields)
        return odoo_fields
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
        return pl.read_csv(
            filename,
            separator=separator,
            n_rows=0,
            infer_schema_length=0,  # Avoid type inference errors on header-only read
        ).columns
    except Exception as e:
        _show_error_panel("File Read Error", f"Could not read CSV header. Error: {e}")
        return None


def _validate_header(  # noqa: C901
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

    # Check for readonly fields that will be silently ignored
    readonly_fields = []
    for field in csv_header:
        clean_field = field.split("/")[
            0
        ]  # Handle external ID fields like 'parent_id/id'
        # Skip 'id' field - it's always mandatory for imports as external ID
        if clean_field == "id":
            continue
        if clean_field in odoo_fields:
            field_info = odoo_fields[clean_field]
            is_readonly = field_info.get("readonly", False)
            is_stored = field_info.get(
                "store", True
            )  # Default to True for stored fields

            if is_readonly:
                readonly_fields.append(
                    {
                        "field": field,
                        "stored": is_stored,
                        "type": field_info.get("type", "unknown"),
                    }
                )

    # Warn about readonly fields, especially non-stored ones
    if readonly_fields:
        warning_message = (
            "The following readonly fields will be silently ignored during import:\n"
        )
        non_stored_count = 0
        for field_info in readonly_fields:
            storage_status = "non-stored" if not field_info["stored"] else "stored"
            if not field_info["stored"]:
                non_stored_count += 1
            warning_message += (
                f"  - '{field_info['field']}' "
                f"({storage_status} readonly {field_info['type']})\n"
            )

        if non_stored_count > 0:
            warning_message += (
                f"\n⚠️  {non_stored_count} non-stored readonly "
                f"fields will be completely ignored!\n"
            )
        warning_message += (
            "\nValues for these fields will be silently discarded during import."
        )
        _show_warning_panel("ReadOnly Fields Detected", warning_message)

    # Check for company-dependent fields that require special handling
    company_dependent_fields = []
    for field in csv_header:
        clean_field = field.split("/")[0]
        if clean_field == "id":
            continue
        if clean_field in odoo_fields:
            field_info = odoo_fields[clean_field]
            is_company_dependent = field_info.get("company_dependent", False)

            if is_company_dependent:
                company_dependent_fields.append(
                    {
                        "field": field,
                        "type": field_info.get("type", "unknown"),
                    }
                )

    # Warn about company-dependent fields
    if company_dependent_fields:
        warning_message = "The following fields are [bold]company-dependent[/bold]:\n"
        for field_info in company_dependent_fields:
            warning_message += f"  - '{field_info['field']}' ({field_info['type']})\n"
        warning_message += (
            "\n[bold]Important:[/bold] These fields store separate values per "
            "company.\nWithout --company-id, values will only be set for the first "
            "company\n"
            "in allowed_company_ids (usually company 1).\n\n"
            "[bold]Recommended workflow:[/bold]\n"
            "  1. Import products WITHOUT these fields (or --ignore them)\n"
            "  2. Import these fields separately per company using --company-id X\n\n"
            "Example:\n"
            "  odoo-data-flow import --file costs.csv --company-id 1\n"
            "  odoo-data-flow import --file costs.csv --company-id 2"
        )
        _show_warning_panel("Company-Dependent Fields Detected", warning_message)

    return True


def _plan_deferrals_and_strategies(  # noqa: C901
    header: list[str],
    odoo_fields: dict[str, Any],
    model: str,
    filename: str,
    separator: str,
    import_plan: dict[str, Any],
    **kwargs: Any,
) -> bool:
    """Analyzes fields to plan deferrals and select import strategies.

    When auto_defer is enabled, all non-required many2one fields are automatically
    deferred to Pass 2, enabling progressive import where records are created first
    and relational fields are populated afterwards.
    """
    auto_defer = kwargs.get("auto_defer", False)
    deferrable_fields = []
    required_relational_fields = []
    strategies = {}
    df = pl.read_csv(
        filename,
        separator=separator,
        truncate_ragged_lines=True,
        infer_schema_length=0,  # Read all columns as strings to avoid type errors
    )

    for field_name in header:
        clean_field_name = field_name.replace("/id", "")
        if clean_field_name in odoo_fields:
            field_info = odoo_fields[clean_field_name]
            field_type = field_info.get("type")
            is_required = field_info.get("required", False)

            is_m2o_self = (
                field_type == "many2one" and field_info.get("relation") == model
            )
            is_m2o_other = (
                field_type == "many2one" and field_info.get("relation") != model
            )
            is_m2m = field_type == "many2many"
            is_o2m = field_type == "one2many"

            # Record required relational fields so the importer can refuse to
            # defer them: deferring a required relation makes the Pass-1 create
            # fail with 'Missing required value'.
            if is_required and (is_m2o_self or is_m2o_other or is_m2m or is_o2m):
                required_relational_fields.append(clean_field_name)

            # Auto-defer: defer all non-required m2o fields
            if auto_defer and is_m2o_other and not is_required:
                deferrable_fields.append(clean_field_name)
                log.debug(
                    f"Auto-deferring many2one field '{clean_field_name}' "
                    f"(relation: {field_info.get('relation')})"
                )
            elif is_m2o_self:
                deferrable_fields.append(clean_field_name)
            elif is_m2m:
                deferrable_fields.append(clean_field_name)
                success, strategy_details = _handle_m2m_field(
                    field_name, clean_field_name, field_info, df
                )
                if success:
                    strategies[clean_field_name] = strategy_details
            elif is_o2m:
                deferrable_fields.append(clean_field_name)
                strategies[clean_field_name] = {"strategy": "write_o2m_tuple"}

    # Always expose required relational fields so the importer can guard against
    # an explicit --deferred-fields that would otherwise break Pass 1.
    import_plan["required_relational_fields"] = required_relational_fields

    if deferrable_fields:
        if auto_defer:
            # Auto-defer mode: actually defer these fields to Pass 2
            log.info(
                f"Auto-defer enabled. Deferring {len(deferrable_fields)} fields to "
                f"Pass 2: {deferrable_fields}"
            )
            unique_id_field = kwargs.get("unique_id_field")
            if not unique_id_field and "id" in header:
                log.info("Automatically using 'id' column as the unique identifier.")
                import_plan["unique_id_field"] = "id"
            elif not unique_id_field:
                _show_error_panel(
                    "Action Required for Two-Pass Import",
                    "Deferrable fields were detected, but no 'id' column was found.\n"
                    "Please specify the unique ID column using the "
                    "[bold cyan]--unique-id-field[/bold cyan] option.",
                )
                return False

            import_plan["deferred_fields"] = deferrable_fields
            import_plan["strategies"] = strategies
        else:
            # Not auto-deferring: just log at debug level for informational purposes
            log.debug(
                f"Deferrable fields detected but not applied (use --auto-defer to "
                f"enable): {deferrable_fields}"
            )
    return True


@register_check
def deferral_and_strategy_check(
    preflight_mode: "PreflightMode",
    model: str,
    filename: str,
    config: Union[str, dict[str, Any]],
    import_plan: dict[str, Any],
    **kwargs: Any,
) -> bool:
    """Verifies fields, detects deferrals, and plans import strategies."""
    log.info(f"Running pre-flight check: Verifying fields for model '{model}'...")
    separator = kwargs.get("separator", ";")
    csv_header = _get_csv_header(filename, separator)
    if not csv_header:
        return False

    ignore_list = kwargs.get("ignore", [])
    header_to_validate = [h for h in csv_header if h not in ignore_list]

    odoo_fields = _get_odoo_fields(config, model)
    if not odoo_fields:
        return False

    if not _validate_header(header_to_validate, odoo_fields, model):
        return False

    if preflight_mode == PreflightMode.FAIL_MODE:
        log.debug("Skipping deferral and strategy check in fail mode.")
        return True

    kwargs.pop("separator", None)
    if not _plan_deferrals_and_strategies(
        header_to_validate,
        odoo_fields,
        model,
        filename,
        separator,
        import_plan,
        **kwargs,
    ):
        return False

    log.info("Pre-flight Check Successful: All columns are valid fields on the model.")
    return True


def _extract_ids_from_csv(
    filename: str,
    header: list[str],
    separator: str = ";",
    encoding: str = "utf-8",
) -> set[str]:
    """Extract all IDs defined in the 'id' column of the CSV.

    These are records that will be created by this import, so references
    to them should not be flagged as missing.

    Returns set of external IDs defined in the file.
    """
    defined_ids: set[str] = set()

    # Find the 'id' column
    id_index = -1
    for i, col in enumerate(header):
        if col.lower() == "id":
            id_index = i
            break

    if id_index < 0:
        return defined_ids

    try:
        with open(filename, encoding=encoding, newline="") as f:
            reader = csv.reader(f, delimiter=separator)
            next(reader)  # Skip header

            for row in reader:
                if id_index < len(row):
                    value = row[id_index].strip()
                    if value:
                        defined_ids.add(value)
    except Exception as e:
        log.warning(f"Error extracting IDs from CSV: {e}")

    return defined_ids


def _extract_references_from_csv(  # noqa: C901
    filename: str,
    header: list[str],
    odoo_fields: dict[str, Any],
    separator: str = ";",
    encoding: str = "utf-8",
    ignore: Optional[list[str]] = None,
) -> dict[str, dict[str, set[str]]]:
    """Extract all unique references from relational columns in CSV.

    Returns dict mapping model name to dict of column name to set of references.
    """
    ignore = ignore or []
    references: dict[str, dict[str, set[str]]] = {}

    # Identify relational columns
    relational_cols: dict[int, tuple[str, str, str]] = {}  # index -> (col, model, type)
    for i, col in enumerate(header):
        if col in ignore or not col:
            continue
        base_field = col.split("/")[0]
        field_info = odoo_fields.get(base_field, {})
        field_type = field_info.get("type", "")
        relation = field_info.get("relation", "")

        if field_type in ("many2one", "many2many") and relation:
            relational_cols[i] = (col, relation, field_type)
            if relation not in references:
                references[relation] = {}
            if col not in references[relation]:
                references[relation][col] = set()

    if not relational_cols:
        return references

    # Scan CSV and collect all references
    try:
        with open(filename, encoding=encoding, newline="") as f:
            reader = csv.reader(f, delimiter=separator)
            next(reader)  # Skip header

            for row in reader:
                for idx, (col, relation, field_type) in relational_cols.items():
                    if idx >= len(row):
                        continue
                    value = row[idx].strip()
                    if not value:
                        continue

                    # Handle multiple values for m2m
                    if field_type == "many2many":
                        values = [v.strip() for v in value.split(",") if v.strip()]
                    else:
                        values = [value]

                    for ref in values:
                        references[relation][col].add(ref)
    except Exception as e:
        log.warning(f"Error scanning CSV for references: {e}")

    return references


def _check_references_exist(  # noqa: C901
    connection: Any,
    references: dict[str, dict[str, set[str]]],
) -> dict[str, dict[str, set[str]]]:
    """Check which references exist in Odoo.

    Returns dict of missing references: model -> column -> set of missing refs.
    """
    missing: dict[str, dict[str, set[str]]] = {}

    for model, columns in references.items():
        # Collect all unique refs for this model
        all_refs: set[str] = set()
        for refs in columns.values():
            all_refs.update(refs)

        if not all_refs:
            continue

        # Separate external IDs from database IDs
        external_ids: set[str] = set()
        db_ids: set[int] = set()
        invalid_refs: set[str] = set()

        for ref in all_refs:
            if "." in ref:
                external_ids.add(ref)
            else:
                try:
                    db_ids.add(int(ref))
                except ValueError:
                    invalid_refs.add(ref)

        # Check external IDs in batch
        existing_external: set[str] = set()
        if external_ids:
            try:
                ir_model_data = connection.get_model("ir.model.data")
                # Build domain for batch lookup
                domain_parts = []
                for ext_id in external_ids:
                    if "." in ext_id:
                        module, name = ext_id.split(".", 1)
                        domain_parts.append(
                            [
                                "&",
                                "&",
                                ("module", "=", module),
                                ("name", "=", name),
                                ("model", "=", model),
                            ]
                        )

                # Combine with OR
                if domain_parts:
                    if len(domain_parts) == 1:
                        domain = domain_parts[0]
                    else:
                        domain = ["|"] * (len(domain_parts) - 1)
                        for part in domain_parts:
                            domain.extend(part)

                    results = ir_model_data.search_read(
                        domain, ["module", "name"], limit=len(external_ids)
                    )
                    for r in results:
                        existing_external.add(f"{r['module']}.{r['name']}")
            except Exception as e:
                log.debug(f"Error checking external IDs for {model}: {e}")

        # Check database IDs in batch
        existing_db: set[int] = set()
        if db_ids:
            try:
                model_obj = connection.get_model(model)
                results = model_obj.search([("id", "in", list(db_ids))])
                existing_db = set(results)
            except Exception as e:
                log.debug(f"Error checking database IDs for {model}: {e}")

        # Find missing refs for each column
        missing_external = external_ids - existing_external
        missing_db = db_ids - existing_db
        all_missing = missing_external | {str(i) for i in missing_db} | invalid_refs

        if all_missing:
            for col, refs in columns.items():
                col_missing = refs & all_missing
                if col_missing:
                    if model not in missing:
                        missing[model] = {}
                    if col not in missing[model]:
                        missing[model][col] = set()
                    missing[model][col].update(col_missing)

    return missing


def _display_missing_references(
    missing: dict[str, dict[str, set[str]]],
) -> None:
    """Display missing references in a formatted panel."""
    console = Console()
    lines = []

    total_missing = sum(
        len(refs) for cols in missing.values() for refs in cols.values()
    )
    lines.append(f"[red]✗[/red] Found {total_missing} missing references\n")

    for model, columns in missing.items():
        lines.append(f"[bold]Model: {model}[/bold]")
        for col, refs in columns.items():
            lines.append(f"  • Column '{col}': {len(refs)} missing")
            # Show first few examples
            examples = sorted(refs)[:5]
            lines.append(f"    Examples: {', '.join(examples)}")
            if len(refs) > 5:
                lines.append(f"    ... and {len(refs) - 5} more")
        lines.append("")

    console.print(
        Panel(
            "\n".join(lines),
            title="[bold red]Missing References Detected[/bold red]",
            expand=False,
        )
    )


@register_check
def reference_check(  # noqa: C901
    preflight_mode: "PreflightMode",
    model: str,
    filename: str,
    config: Union[str, dict[str, Any]],
    **kwargs: Any,
) -> bool:
    """Pre-flight check to verify all relational references exist."""
    check_refs = kwargs.get("check_refs", "warn")
    if check_refs == "skip":
        log.debug("Skipping reference pre-flight check (--check-refs=skip).")
        return True

    if preflight_mode == PreflightMode.FAIL_MODE:
        log.debug("Skipping reference pre-flight check in fail mode.")
        return True

    log.info("Running pre-flight check: Verifying relational references...")

    separator = kwargs.get("separator", ";")
    encoding = kwargs.get("encoding", "utf-8")
    ignore = kwargs.get("ignore", [])

    # Get CSV header
    csv_header = _get_csv_header(filename, separator)
    if not csv_header:
        return bool(check_refs != "fail")

    # Get Odoo fields
    odoo_fields = _get_odoo_fields(config, model)
    if not odoo_fields:
        return bool(check_refs != "fail")

    # Extract all references from CSV
    references = _extract_references_from_csv(
        filename, csv_header, odoo_fields, separator, encoding, ignore
    )

    if not any(refs for cols in references.values() for refs in cols.values()):
        log.info("No relational references found to check.")
        return True

    # Extract IDs defined in this file (records being created)
    # These should not be flagged as missing for self-referencing fields
    defined_ids = _extract_ids_from_csv(filename, csv_header, separator, encoding)

    # Get connection for checking
    try:
        if isinstance(config, dict):
            connection = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config)
    except Exception as e:
        log.warning(f"Could not connect to check references: {e}")
        return bool(check_refs != "fail")

    # Check which references exist
    missing = _check_references_exist(connection, references)

    # Exclude self-references (IDs defined in this same file)
    # This applies to the model being imported (e.g., parent_id on res.partner)
    if model in missing and defined_ids:
        for col in list(missing[model].keys()):
            # Remove references that are defined in this file
            missing[model][col] -= defined_ids
            # If no missing refs left for this column, remove it
            if not missing[model][col]:
                del missing[model][col]
        # If no missing columns left for this model, remove it
        if not missing[model]:
            del missing[model]

    if not missing:
        total_refs = sum(
            len(refs) for cols in references.values() for refs in cols.values()
        )
        log.info(f"All {total_refs} relational references verified successfully.")
        return True

    # Handle missing references
    _display_missing_references(missing)

    if check_refs == "fail":
        _show_error_panel(
            "Reference Check Failed",
            "Import aborted due to missing references. "
            "Use --check-refs=warn to continue anyway.",
        )
        return False

    # check_refs == "warn"
    log.warning(
        "Continuing with import despite missing references. "
        "Some records may fail to import."
    )
    return True
