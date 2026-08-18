"""Command-line interface for fluvo."""

import ast
from importlib.metadata import version as get_version
from pathlib import Path
from typing import Any, Optional

import click

from .converter import run_path_to_image, run_url_to_image
from .exporter import run_export
from .importer import _infer_model_from_filename, run_import
from .lib import cache
from .lib.actions.language_installer import run_language_installation
from .lib.actions.module_manager import (
    run_module_installation,
    run_module_uninstallation,
    run_update_module_list,
)
from .lib.actions.variant_manager import run_create_missing_variants
from .lib.actions.vies_manager import (
    disable_vat_validation,
    get_vat_validation_settings,
    restore_vat_validation_settings,
    run_vies_validation,
)
from .lib.validation import display_validation_results, validate_csv_data
from .logging_config import log, setup_logging
from .migrator import run_migration
from .workflow_runner import run_invoice_v9_workflow
from .writer import run_write


def _parse_resolve_relation_specs(specs: tuple[str, ...]) -> list[dict[str, Any]]:
    """Parse --resolve-relation strings into resolve_relations specs.

    Each string is ``source_column:model:key_field:relation_field[:to]`` where
    ``to`` is ``xmlid`` (default) or ``dbid``.

    Args:
        specs: Raw --resolve-relation option values.

    Returns:
        list[dict[str, Any]]: Spec dicts for run_import's resolve_relations.

    Raises:
        click.BadParameter: If a spec string is malformed.
    """
    parsed: list[dict[str, Any]] = []
    for raw in specs:
        parts = [p.strip() for p in raw.split(":")]
        if len(parts) not in (4, 5) or not all(parts[:4]):
            raise click.BadParameter(
                f"--resolve-relation {raw!r}: expected "
                "'source_column:model:key_field:relation_field[:xmlid|dbid]' "
                "with non-empty fields."
            )
        spec: dict[str, Any] = {
            "source_column": parts[0],
            "model": parts[1],
            "key_field": parts[2],
            "relation_field": parts[3],
        }
        if len(parts) == 5:
            if parts[4] not in ("xmlid", "dbid"):
                raise click.BadParameter(
                    f"--resolve-relation {raw!r}: 'to' must be 'xmlid' or 'dbid'."
                )
            spec["to"] = parts[4]
        parsed.append(spec)
    return parsed


def _run_dry_run_validation(connection_file: str, **kwargs: Any) -> None:
    """Run dry-run validation mode without importing."""
    from .lib.conf_lib import get_connection_from_config, get_connection_from_dict
    from .lib.internal.ui import _show_error_panel

    filename = kwargs.get("filename")
    model = kwargs.get("model")
    separator = kwargs.get("separator", ";")
    encoding = kwargs.get("encoding", "utf-8")
    ignore = kwargs.get("ignore")
    protocol = kwargs.get("protocol")

    if not filename:
        _show_error_panel("Dry Run Error", "No file specified for validation.")
        return

    # Infer model if not provided
    if not model:
        model = _infer_model_from_filename(filename)
        if not model:
            _show_error_panel(
                "Model Not Found",
                "Could not infer model from filename. Please use the --model option.",
            )
            return

    # Parse ignore list
    ignore_list: list[str] = []
    if ignore:
        ignore_list = [col.strip() for col in ignore.split(",") if col.strip()]

    log.info(f"Starting dry-run validation for {model}...")

    try:
        # Get connection
        if protocol:
            config: Any = {"_config_file": connection_file, "protocol": protocol}
            conn = get_connection_from_dict(config)
        else:
            conn = get_connection_from_config(connection_file)

        # Get model fields info
        model_obj = conn.get_model(model)
        fields_info = model_obj.fields_get()

        # Run validation
        result = validate_csv_data(
            file_path=filename,
            model=model,
            fields_info=fields_info,
            connection=conn,
            separator=separator,
            encoding=encoding,
            ignore=ignore_list,
        )

        # Display results
        display_validation_results(result, model)

    except Exception as e:
        _show_error_panel("Validation Error", f"Failed to validate data: {e}")


def _execute_post_action(
    config: Any,
    model: Optional[str],
    action_name: str,
    id_map: dict[str, int],
    context: dict[str, Any],
    timeout: int = 600,
) -> bool:
    """Execute a method on all successfully imported records.

    Args:
        config: Connection configuration (file path or dict).
        model: The Odoo model name.
        action_name: The method name to call on the records.
        id_map: Mapping of external IDs to database IDs.
        context: Odoo context to use for the method call.
        timeout: Timeout in seconds for the RPC call (default: 600 = 10 minutes).

    Returns:
        True if the action completed successfully or timed out (server may have
        completed), False if it definitively failed.
    """
    import socket

    from .lib.conf_lib import get_connection_from_config, get_connection_from_dict

    if not model:
        log.error("Cannot execute post-action: model name is required.")
        return False

    if not id_map:
        log.warning("No records were imported, skipping post-action.")
        return False

    # Get all database IDs from the id_map
    db_ids = list(id_map.values())
    if not db_ids:
        log.warning("No record IDs available for post-action.")
        return False

    log.info(
        f"Executing post-action '{action_name}' on {len(db_ids)} "
        f"records of model '{model}' (timeout: {timeout}s)..."
    )

    try:
        # Get connection
        if isinstance(config, dict):
            conn = get_connection_from_dict(config)
        else:
            conn = get_connection_from_config(config)

        # Set a longer timeout for the post-action
        # This helps with large inventory adjustments
        original_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)

        try:
            # Get the model and call the method
            model_obj = conn.get_model(model)

            # Check if the method exists
            if not hasattr(model_obj, action_name):
                log.error(
                    f"Method '{action_name}' not found on model '{model}'. "
                    f"Make sure the method exists and is accessible via RPC."
                )
                return False

            # Call the method with the record IDs
            # Most Odoo methods accept a list of IDs as the first argument
            method = getattr(model_obj, action_name)
            result = method(db_ids, context=context)

            log.info(
                f"Post-action '{action_name}' completed successfully on "
                f"{len(db_ids)} records."
            )
            if result:
                log.debug(f"Post-action result: {result}")
            return True

        finally:
            # Restore original timeout
            socket.setdefaulttimeout(original_timeout)

    except (socket.timeout, TimeoutError, ConnectionError) as e:
        log.warning(f"Post-action '{action_name}' timed out or connection lost: {e}")
        log.warning(
            "The operation may have completed on the server. "
            "Proceeding with subsequent steps..."
        )
        # Return True to allow move date update to proceed
        # The server likely completed the operation
        return True

    except Exception as e:
        log.error(f"Failed to execute post-action '{action_name}': {e}")
        log.error(
            "The import was successful, but the post-action failed. "
            "You may need to run the action manually."
        )
        return False


def _get_product_ids_from_quants(
    config: Any,
    quant_ids: list[int],
) -> list[int]:
    """Extract product IDs from a list of quant IDs.

    Args:
        config: Connection configuration (file path or dict).
        quant_ids: List of stock.quant database IDs.

    Returns:
        List of unique product IDs from the quants.
    """
    from .lib.conf_lib import get_connection_from_config, get_connection_from_dict

    if not quant_ids:
        return []

    try:
        if isinstance(config, dict):
            conn = get_connection_from_dict(config)
        else:
            conn = get_connection_from_config(config)

        quant_model = conn.get_model("stock.quant")
        quant_data = quant_model.read(quant_ids, ["product_id"])
        product_ids = list(
            set(q["product_id"][0] for q in quant_data if q.get("product_id"))
        )
        log.debug(
            f"Extracted {len(product_ids)} product IDs from {len(quant_ids)} quants"
        )
        return product_ids

    except Exception as e:
        log.error(f"Failed to extract product IDs from quants: {e}")
        return []


def _update_inventory_move_dates(
    config: Any,
    move_date: str,
    context: dict[str, Any],
    product_ids: list[int],
    time_window_hours: float = 2.0,
) -> None:
    """Update stock move dates for inventory adjustment moves.

    After action_apply_inventory creates stock moves with today's date,
    this function updates them to the specified date.

    Args:
        config: Connection configuration (file path or dict).
        move_date: Target date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS format.
        context: Odoo context to use.
        product_ids: List of product IDs to filter moves by.
        time_window_hours: How far back to look for moves (default: 2 hours).
            This handles cases where the post-action timed out but completed
            on the server.
    """
    from datetime import datetime, timedelta, timezone

    from .lib.conf_lib import get_connection_from_config, get_connection_from_dict

    # Parse the move_date
    try:
        if " " in move_date:
            # Full datetime format
            dt = datetime.strptime(move_date, "%Y-%m-%d %H:%M:%S")
        else:
            # Date only - set to start of day
            dt = datetime.strptime(move_date, "%Y-%m-%d")
        move_date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError as e:
        log.error(f"Invalid --move-date format: {e}")
        log.error("Expected format: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")
        return

    if not product_ids:
        log.warning("No product IDs available for move date update.")
        return

    log.info(
        f"Updating inventory move dates to {move_date_str} "
        f"for {len(product_ids)} product(s)..."
    )

    # Get connection
    try:
        if isinstance(config, dict):
            conn = get_connection_from_dict(config)
        else:
            conn = get_connection_from_config(config)

        # Find inventory adjustment location
        location_model = conn.get_model("stock.location")
        inv_adj_locs = location_model.search([("usage", "=", "inventory")])

        if not inv_adj_locs:
            log.error("Could not find inventory adjustment location.")
            return

        # Calculate the time window cutoff
        # Use a generous window to handle timeout scenarios where the server
        # completed the operation but we lost the connection
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=time_window_hours)
        cutoff_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S")

        log.debug(
            f"Searching for moves created after {cutoff_str} "
            f"(time window: {time_window_hours} hours)"
        )

        # Build the search domain
        # - From or to inventory adjustment location
        # - For the products we imported
        # - State = done (action_apply_inventory completes them)
        # - Created within the time window
        domain: list[Any] = [
            "|",
            ("location_id", "in", inv_adj_locs),
            ("location_dest_id", "in", inv_adj_locs),
            ("product_id", "in", product_ids),
            ("state", "=", "done"),
            ("create_date", ">=", cutoff_str),
        ]

        # Find stock moves
        move_model = conn.get_model("stock.move")
        move_ids = move_model.search(domain)

        if not move_ids:
            log.warning(
                "No stock moves found to update. "
                "The inventory adjustment may not have created any moves yet, "
                "or the moves may be older than the time window."
            )
            return

        # Update the date on these moves
        move_model.write(move_ids, {"date": move_date_str}, context=context)

        log.info(f"Updated date to {move_date_str} on {len(move_ids)} stock move(s).")

    except Exception as e:
        log.error(f"Failed to update stock move dates: {e}")
        log.error(
            "The import and inventory adjustment succeeded, but move dates "
            "could not be updated. You may need to update them manually."
        )


def run_project_flow(flow_file: str, flow_name: Optional[str]) -> None:
    """Abort: the declarative flow runner is not implemented yet (see #251).

    ``--flow-file`` is still advertised on the CLI, but the runner that would
    execute a ``flows.yml`` does not exist. The previous placeholder validated the
    file, printed two log lines, and returned normally — a run that exits 0 having
    done nothing, which is the worst failure mode for a migration tool and
    contradicts the reconciliation-first contract. Until the real runner lands,
    fail loudly with a non-zero exit so automation never mistakes it for success.

    Args:
        flow_file: Path to the flow file the user asked to run (reported, not run).
        flow_name: Specific flow name from ``--run``, if any (reported, not run).

    Raises:
        SystemExit: always, with code 1 — the flow runner is not implemented.
    """
    from .lib.internal.ui import _show_error_panel

    target = f"flow '{flow_name}' from {flow_file}" if flow_name else flow_file
    _show_error_panel(
        "Flow runner not implemented",
        f"Cannot run {target}: the declarative flow runner (flows.yml) is not "
        "implemented yet, so [bold]nothing was executed[/bold].\n\n"
        "Run your steps individually for now with [bold]fluvo import[/bold], "
        "[bold]export[/bold], [bold]write[/bold], or [bold]migrate[/bold].\n\n"
        "Track the flow runner at "
        "https://github.com/GetFluvo/fluvo/issues/251.",
    )
    raise SystemExit(1)


@click.group(
    context_settings=dict(help_option_names=["-h", "--help"]),
    invoke_without_command=True,
)
@click.version_option(version=get_version("fluvo"))
@click.option(
    "-v", "--verbose", is_flag=True, help="Enable verbose, debug-level logging."
)
@click.option(
    "--log-file",
    default=None,
    type=click.Path(),
    help="Path to a file to write logs to, in addition to the console.",
)
@click.option(
    "--flow-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the YAML flow file. Defaults to 'flows.yml' in current directory.",
)
@click.option(
    "--run",
    "flow_name",
    help="Name of a specific flow to run from the flow file.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    verbose: bool,
    log_file: Optional[str],
    flow_file: Optional[str],
    flow_name: Optional[str],
) -> None:
    """Fluvo: A tool for importing, exporting, and processing data."""
    setup_logging(verbose, log_file)

    # If a subcommand is invoked, it's Single-Action mode. Let it proceed.
    if ctx.invoked_subcommand is not None:
        return

    # --- Project Mode Logic ---
    effective_flow_file = flow_file
    if not effective_flow_file:
        default_flow_file = Path("flows.yml")
        if default_flow_file.exists():
            log.info("No --flow-file specified, using default 'flows.yml'.")
            effective_flow_file = str(default_flow_file)
        else:
            # No subcommand, no --flow-file, and no default flows.yml -> show help.
            click.echo(ctx.get_help())
            return

    run_project_flow(effective_flow_file, flow_name)


# --- Module Management Command Group ---
@cli.group(name="module")
def module_group() -> None:
    """Commands for managing Odoo modules."""
    pass


@module_group.command(name="update-list")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
def update_module_list_cmd(connection_file: str) -> None:
    """Scans the addons path and updates the list of available modules."""
    run_update_module_list(config=connection_file)


@module_group.command(name="install")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "-m",
    "--modules",
    "modules_str",
    required=True,
    help="A comma-separated list of module names to install or upgrade.",
)
def install_modules_cmd(connection_file: str, modules_str: str) -> None:
    """Installs or upgrades a list of Odoo modules."""
    modules_list = [mod.strip() for mod in modules_str.split(",") if mod.strip()]
    run_module_installation(config=connection_file, modules=modules_list)


@module_group.command(name="uninstall")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "-m",
    "--modules",
    "modules_str",
    required=True,
    help="A comma-separated list of module names to uninstall.",
)
def uninstall_modules_cmd(connection_file: str, modules_str: str) -> None:
    """Uninstalls a list of Odoo modules."""
    modules_list = [mod.strip() for mod in modules_str.split(",")]
    run_module_uninstallation(config=connection_file, modules=modules_list)


@module_group.command(name="install-languages")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "-l",
    "--languages",
    "languages_str",
    required=True,
    help="A comma-separated list of language codes to install (e.g., 'nl_BE,fr_FR').",
)
def install_languages_cmd(connection_file: str, languages_str: str) -> None:
    """Installs one or more languages in the Odoo database."""
    languages_list = [lang.strip() for lang in languages_str.split(",")]
    run_language_installation(config=connection_file, languages=languages_list)


# --- Workflow Command Group ---
@cli.group(name="workflow")
def workflow_group() -> None:
    """Run legacy or complex post-import processing workflows."""
    pass


# --- Create Missing Variants Sub-command (#188) ---
@workflow_group.command(name="create-missing-variants")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "--domain",
    default=None,
    help="Optional Odoo domain (Python list literal) to scope which product "
    "templates are checked, e.g. \"[('categ_id', '=', 5)]\".",
)
@click.option(
    "--batch-size",
    default=200,
    show_default=True,
    type=int,
    help="Number of variants to create per RPC call.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Only report how many templates lack variants; create nothing.",
)
def create_missing_variants_cmd(
    connection_file: str,
    domain: Optional[str],
    batch_size: int,
    dry_run: bool,
) -> None:
    """Create default variants for product.template records that have none.

    Odoo auto-creates a default variant on ORM create, but the ``load()`` import
    API does not, so templates can import with no variants (#188). This finds
    them and creates the missing default variant.
    """
    parsed_domain: Optional[list[Any]] = None
    if domain:
        try:
            parsed_domain = ast.literal_eval(domain)
        except (ValueError, SyntaxError) as exc:
            raise click.BadParameter(f"Invalid --domain: {exc}") from exc
        if not isinstance(parsed_domain, list):
            raise click.BadParameter(
                "--domain must be a list, e.g. \"[('categ_id', '=', 5)]\"."
            )
    success = run_create_missing_variants(
        config=connection_file,
        domain=parsed_domain,
        batch_size=batch_size,
        dry_run=dry_run,
    )
    if not success:
        raise SystemExit(1)


# --- Invoice v9 Workflow Sub-command ---
@workflow_group.command(name="invoice-v9")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "--action",
    "actions",
    multiple=True,
    type=click.Choice(
        ["tax", "validate", "pay", "proforma", "rename", "all"],
        case_sensitive=False,
    ),
    default=["all"],
    help="Workflow action to run. Can be specified multiple times. Defaults to 'all'.",
)
@click.option(
    "--field",
    required=True,
    help="The source field containing the legacy invoice status.",
)
@click.option(
    "--status-map",
    "status_map_str",
    required=True,
    help="Dictionary string mapping Odoo states to legacy states. "
    "e.g., \"{'open': ['OP']}\"",
)
@click.option(
    "--paid-date-field",
    required=True,
    help="The source field containing the payment date.",
)
@click.option(
    "--payment-journal",
    required=True,
    type=int,
    help="The database ID of the payment journal.",
)
@click.option(
    "--max-connection", default=4, type=int, help="Number of parallel threads."
)
def invoice_v9_cmd(connection_file: str, **kwargs: Any) -> None:
    """Runs the legacy Odoo v9 invoice processing workflow."""
    kwargs["config"] = connection_file
    run_invoice_v9_workflow(**kwargs)


# --- VAT Validation Command Group ---
@cli.group(name="vat")
def vat_group() -> None:
    """Commands for managing VAT/VIES validation settings."""
    pass


@vat_group.command(name="get-settings")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "--company-ids",
    default=None,
    help="Comma-separated list of company IDs to check. If not specified, checks all.",
)
@click.option(
    "--include-stdnum/--no-stdnum",
    default=True,
    help="Include stdnum validation settings. Default: True.",
)
def vat_get_settings_cmd(
    connection_file: str,
    company_ids: Optional[str],
    include_stdnum: bool,
) -> None:
    """Get current VAT validation settings for all companies."""
    from rich.console import Console
    from rich.table import Table

    company_id_list: Optional[list[int]] = None
    if company_ids:
        company_id_list = [int(c.strip()) for c in company_ids.split(",") if c.strip()]

    settings = get_vat_validation_settings(
        config=connection_file,
        company_ids=company_id_list,
        include_stdnum=include_stdnum,
    )

    if not settings:
        Console().print("[red]Failed to retrieve VAT settings.[/red]")
        return

    console = Console()
    table = Table(title="VAT Validation Settings")
    table.add_column("Company ID", style="cyan")
    table.add_column("VIES Check", style="green")

    for company_id, vies_enabled in sorted(settings.vies_settings.items()):
        table.add_row(str(company_id), "✓ Enabled" if vies_enabled else "✗ Disabled")

    console.print(table)

    if include_stdnum and settings.stdnum_settings:
        console.print("\n[bold]stdnum Settings (ir.config_parameter):[/bold]")
        for key, value in settings.stdnum_settings.items():
            console.print(f"  {key}: {value}")


@vat_group.command(name="disable")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "--company-ids",
    default=None,
    help="Comma-separated list of company IDs. If not specified, disables for all.",
)
@click.option(
    "--vies/--no-vies",
    default=True,
    help="Disable VIES online check. Default: True.",
)
@click.option(
    "--stdnum/--no-stdnum",
    default=True,
    help="Disable stdnum format validation. Default: True.",
)
@click.option(
    "--save-settings",
    is_flag=True,
    default=True,
    help="Save current settings for later restoration. Default: True.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False),
    help="Save settings to a JSON file for later restoration.",
)
def vat_disable_cmd(
    connection_file: str,
    company_ids: Optional[str],
    vies: bool,
    stdnum: bool,
    save_settings: bool,
    output: Optional[str],
) -> None:
    """Disable VAT validation (VIES and/or stdnum) for companies."""
    import json

    from rich.console import Console

    console = Console()

    company_id_list: Optional[list[int]] = None
    if company_ids:
        company_id_list = [int(c.strip()) for c in company_ids.split(",") if c.strip()]

    settings = disable_vat_validation(
        config=connection_file,
        company_ids=company_id_list,
        disable_vies=vies,
        disable_stdnum=stdnum,
        save_settings=save_settings,
    )

    if not settings:
        console.print("[red]Failed to disable VAT validation.[/red]")
        return

    console.print("[green]VAT validation disabled successfully.[/green]")

    if output:
        settings_dict = {
            "vies_settings": settings.vies_settings,
            "stdnum_settings": settings.stdnum_settings,
            "timestamp": settings.timestamp,
        }
        with open(output, "w") as f:
            json.dump(settings_dict, f, indent=2)
        console.print(f"Settings saved to: {output}")
    elif save_settings:
        console.print(
            "[dim]Settings stored in memory. Use 'vat restore' to restore them.[/dim]"
        )


@vat_group.command(name="restore")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "--input",
    "input_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Restore settings from a JSON file saved by 'vat disable --output'.",
)
def vat_restore_cmd(
    connection_file: str,
    input_file: Optional[str],
) -> None:
    """Restore VAT validation settings to their original state."""
    import json

    from rich.console import Console

    from .lib.actions.vies_manager import VatValidationSettings

    console = Console()

    if input_file:
        with open(input_file) as f:
            data = json.load(f)
        # Convert string keys back to int for company IDs
        vies_settings = {int(k): v for k, v in data.get("vies_settings", {}).items()}
        settings = VatValidationSettings(
            vies_settings=vies_settings,
            stdnum_settings=data.get("stdnum_settings", {}),
            timestamp=data.get("timestamp", 0),
        )
    else:
        console.print(
            "[red]No settings file provided. "
            "Use --input to specify a settings file.[/red]"
        )
        console.print(
            "[dim]Tip: Use 'vat disable --output settings.json' to save settings.[/dim]"
        )
        return

    success = restore_vat_validation_settings(
        config=connection_file,
        settings=settings,
    )

    if success:
        console.print("[green]VAT validation settings restored successfully.[/green]")
    else:
        console.print("[red]Failed to restore VAT validation settings.[/red]")


@vat_group.command(name="validate")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "--batch-size",
    default=50,
    type=int,
    help="Number of records to validate per batch. Default: 50.",
)
@click.option(
    "--delay",
    default=1.0,
    type=float,
    help="Delay between batches in seconds. Default: 1.0.",
)
@click.option(
    "--notify-users",
    default=None,
    help="Comma-separated list of user IDs to notify on failures.",
)
@click.option(
    "--domain",
    default=None,
    help="Odoo domain filter as a list string. "
    "Example: \"[('is_company', '=', True)]\"",
)
@click.option(
    "--max-records",
    default=None,
    type=int,
    help="Maximum number of records to validate.",
)
def vat_validate_cmd(
    connection_file: str,
    batch_size: int,
    delay: float,
    notify_users: Optional[str],
    domain: Optional[str],
    max_records: Optional[int],
) -> None:
    """Validate VAT numbers against VIES in batches with optional notifications."""
    import ast

    from rich.console import Console
    from rich.table import Table

    console = Console()

    notify_user_ids: Optional[list[int]] = None
    if notify_users:
        notify_user_ids = [int(u.strip()) for u in notify_users.split(",") if u.strip()]

    parsed_domain: Optional[list[Any]] = None
    if domain:
        try:
            parsed_domain = ast.literal_eval(domain)
        except (ValueError, SyntaxError) as e:
            console.print(f"[red]Invalid domain format: {e}[/red]")
            return

    console.print(f"Starting VIES validation (batch size: {batch_size})...")

    result = run_vies_validation(
        config=connection_file,
        batch_size=batch_size,
        delay_between_batches=delay,
        notify_user_ids=notify_user_ids,
        domain=parsed_domain,
        max_records=max_records,
    )

    # Display results
    table = Table(title="VIES Validation Results")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total Checked", str(result.total_checked))
    table.add_row("Valid", str(result.valid_count))
    table.add_row("Invalid", str(result.invalid_count))
    table.add_row("Errors", str(result.error_count))

    console.print(table)

    if result.invalid_partners:
        console.print("\n[bold red]Invalid VAT Numbers:[/bold red]")
        for partner in result.invalid_partners[:20]:
            console.print(
                f"  Partner {partner['id']}: {partner['vat']} - {partner['name']}"
            )
        if len(result.invalid_partners) > 20:
            console.print(f"  ... and {len(result.invalid_partners) - 20} more")

    if result.error_partners:
        console.print("\n[bold yellow]Errors:[/bold yellow]")
        for partner in result.error_partners[:10]:
            console.print(
                f"  Partner {partner['id']}: {partner['vat']} - {partner['error']}"
            )
        if len(result.error_partners) > 10:
            console.print(f"  ... and {len(result.error_partners) - 10} more")


# --- Import Command ---
@cli.command(name="import")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "--protocol",
    type=click.Choice(
        ["xmlrpc", "xmlrpcs", "jsonrpc", "jsonrpcs", "json2", "json2s"],
        case_sensitive=False,
    ),
    default=None,
    help="RPC protocol to use. Options: xmlrpc (default for Odoo 8-9), "
    "jsonrpc (recommended for Odoo 10-18, ~30%% faster), "
    "json2 (Odoo 19+, requires API key). "
    "If not specified, uses protocol from config file or defaults to xmlrpc.",
)
@click.option("--file", "filename", required=True, help="File to import.")
@click.option(
    "--model",
    default=None,
    help="Odoo model to import into. If not provided, it's inferred from the filename.",
)
# --- ADDED: New options for the deferred import strategy ---
@click.option(
    "--deferred-fields",
    default=None,
    help="Comma-separated list of fields to defer to a second pass "
    "(enables two-pass import).",
)
@click.option(
    "--auto-defer",
    is_flag=True,
    default=False,
    help="Automatically defer all non-required many2one fields. "
    "Enables progressive import where records are created first, "
    "then relational fields are populated in Pass 2.",
)
@click.option(
    "--unique-id-field",
    default=None,
    help="The column that uniquely identifies records (e.g., 'xml_id'). "
    "Required for deferred imports.",
)
# --- END ADDED ---
@click.option(
    "--no-preflight-checks",
    is_flag=True,
    default=False,
    help="Skip all pre-flight checks before starting the import.",
)
@click.option(
    "--check-refs",
    type=click.Choice(["fail", "warn", "skip"], case_sensitive=False),
    default="warn",
    help="Action for pre-import reference check: "
    "fail (abort if missing), warn (continue with warning), skip (no check). "
    "Default: warn.",
)
@click.option(
    "--worker", default=1, type=int, help="Number of simultaneous connections."
)
@click.option(
    "--size",
    "batch_size",
    default=500,
    type=int,
    help="Number of lines to import per connection.",
)
@click.option(
    "--delay",
    "batch_delay",
    default=0.0,
    type=float,
    help="Delay in seconds between batches to reduce server load. "
    "Use 0.5-2.0 for busy servers. Default: 0 (no delay).",
)
@click.option(
    "--max-batch-bytes",
    default=5 * 1024 * 1024,
    type=int,
    help="Maximum estimated payload size per batch in bytes. "
    "When a batch exceeds this size, it is split regardless of record count. "
    "Useful for imports with large binary fields like images. "
    "Default: 5242880 (5MB). Set to 0 to disable size-based batching.",
)
@click.option("--skip", default=0, type=int, help="Number of initial lines to skip.")
@click.option(
    "--fail",
    is_flag=True,
    default=False,
    help="Run in fail mode, retrying records from the _fail.csv file.",
)
@click.option(
    "--headless",
    is_flag=True,
    default=False,
    help="Run in headless mode, auto-confirming any prompts "
    "(e.g., installing languages).",
)
@click.option("-s", "--sep", "separator", default=";", help="CSV separator character.")
@click.option(
    "--groupby",
    default=None,
    help="Comma-separated list of columns to group data by to prevent deadlocks."
    "Records with empty values for the first column are processed first, then grouped "
    "by that column. This process repeats for subsequent columns.",
)
@click.option(
    "--auto-groupby",
    is_flag=True,
    default=False,
    help="Automatically pick a many2one column to group by (deadlock avoidance) "
    "when --groupby is not given. Chooses the relation with the most shared "
    "targets. Off by default.",
)
@click.option(
    "--ignore", default=None, help="Comma-separated list of columns to ignore."
)
@click.option(
    "--context",
    default="{'tracking_disable': True}",
    help="Odoo context as a JSON string e.g., '{\"key\": true}'.",
)
@click.option(
    "--company-id",
    default=None,
    type=str,
    help="Company ID or external ID for multicompany imports. Accepts database ID "
    "(e.g., '1') or XML ID (e.g., 'base.main_company'). Sets allowed_company_ids "
    "context to enable cross-company field references.",
)
@click.option(
    "--all-companies",
    is_flag=True,
    default=False,
    help="Automatically set allowed_company_ids to all companies the user has "
    "access to. This mimics the behavior of the Odoo web interface and enables "
    "importing records that reference data across multiple companies.",
)
@click.option(
    "--allow-default-company",
    is_flag=True,
    default=False,
    help="For company-specific models on a multi-company database, proceed under "
    "the connecting user's default company instead of aborting. By default such "
    "an import aborts unless --company-id or --all-companies is given, to prevent "
    "the most common silent migration error (wrong-company assignment). No effect "
    "on single-company databases.",
)
@click.option(
    "--o2m",
    is_flag=True,
    default=False,
    help="Special handling for one-to-many imports.",
)
# --- Import behavior options ---
@click.option(
    "--on-missing-ref",
    default=None,
    help="Action for missing references: field:action pairs. "
    "Actions: create (auto-create), skip (skip row), empty (set to False). "
    "Example: 'country_id:create,user_id:skip,category_id:empty'",
)
@click.option(
    "--auto-create-refs",
    is_flag=True,
    default=False,
    help="Automatically create missing related records for all many2one fields. "
    "Uses Odoo's name_create to create records with just the name.",
)
@click.option(
    "--set-empty-on-missing",
    is_flag=True,
    default=False,
    help="Set relational fields to empty (False) when reference not found, "
    "instead of failing the row. Useful for capturing incomplete data.",
)
@click.option(
    "--fallback-values",
    default=None,
    help="Default values for invalid selection/boolean fields: field:value pairs. "
    "Example: 'state:draft,active:true'",
)
@click.option(
    "--tracking-disable/--tracking-enable",
    default=True,
    help="Disable/enable mail tracking during import. Disabled by default.",
)
@click.option(
    "--auto-clean",
    is_flag=True,
    default=False,
    help="Apply safe, type-aware coercions before load (strip whitespace, "
    "normalize null tokens, canonicalize booleans). Off by default.",
)
@click.option(
    "--resolve-relation",
    "resolve_relation_specs",
    multiple=True,
    help="Pre-resolve a relation column in Polars before load, so Odoo performs "
    "no name_search for it. Format "
    "'source_column:model:key_field:relation_field[:xmlid|dbid]'. Repeatable. "
    "Example: --resolve-relation country:res.country:code:country_id",
)
@click.option(
    "--fix-missing-variants",
    is_flag=True,
    default=False,
    help="For product.template imports: create default variants for any imported "
    "templates that end up with none (Odoo's load() does not auto-create them). "
    "Without this flag, fluvo only warns about them.",
)
@click.option(
    "--allow-xmlid-collisions",
    is_flag=True,
    default=False,
    help="Proceed even when distinct source ids sanitize to the same Odoo external "
    "id (e.g. 'a b' and 'a,b' both become 'a_b'). By default fluvo aborts, because "
    "Odoo's load() would silently merge such rows into one record.",
)
@click.option(
    "--m2m-mode",
    type=click.Choice(["replace", "add"]),
    default="replace",
    show_default=True,
    help="How many2many fields are written in Pass 2. 'replace' makes each record's "
    "set exactly the values in the file (single source of truth); 'add' only links "
    "the file's values, leaving any pre-existing links intact.",
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Disable the on-disk id-map cache for this run (read and write). Use it "
    "after restoring/rebuilding the target database, when cached natural-key -> id "
    "mappings from a previous run may no longer be valid.",
)
@click.option(
    "--defer-parent-store",
    is_flag=True,
    default=False,
    help="Defer parent_left/parent_right computation for hierarchical models. "
    "Improves performance for large imports of nested structures.",
)
@click.option("--encoding", default="utf-8", help="Encoding of the data file.")
@click.option(
    "--stream",
    is_flag=True,
    default=False,
    help="Stream CSV data without loading entire file into memory. "
    "Ideal for very large files. Not compatible with --o2m, --groupby, "
    "--defer, or --fail options.",
)
@click.option(
    "--resume/--no-resume",
    default=True,
    help="Resume from checkpoint if available. Enabled by default. "
    "When enabled, imports can be resumed after crashes or interruptions.",
)
@click.option(
    "--no-checkpoint",
    is_flag=True,
    default=False,
    help="Disable checkpoint saving during import. Use for small imports "
    "where checkpointing overhead is not needed.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate data without importing. Checks required fields, "
    "selection values, and reference existence.",
)
@click.option(
    "--skip-unchanged",
    is_flag=True,
    default=False,
    help="Skip records that already exist with identical values. "
    "Makes imports idempotent by comparing field values before importing.",
)
@click.option(
    "--skip-existing",
    is_flag=True,
    default=False,
    help="Skip records whose external ID already exists in Odoo. "
    "Makes imports safely re-runnable without update errors. "
    "Ideal for stock.quant and other models with update restrictions.",
)
@click.option(
    "--adaptive-throttle/--no-adaptive-throttle",
    default=True,
    help="Health-aware throttling that automatically adjusts batch sizes "
    "and delays based on server response times. Enabled by default to prevent "
    "server overload. Use --no-adaptive-throttle to disable for maximum speed.",
)
@click.option(
    "--sudo",
    is_flag=True,
    default=False,
    help="Temporarily disable record rules for the model during import. "
    "Requires admin rights. Use with --all-companies to import all records "
    "across companies regardless of restrictive record rules.",
)
@click.option(
    "--post-action",
    default=None,
    help="Method to call on imported records after successful import. "
    "Example: 'action_apply_inventory' for stock.quant to apply stock adjustments. "
    "The method is called with all successfully imported record IDs.",
)
@click.option(
    "--move-date",
    default=None,
    help="Set the date on stock moves created by inventory adjustment. "
    "Use with --post-action action_apply_inventory for opening inventory imports. "
    "Format: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS. "
    "Example: --move-date 2026-01-01",
)
def import_cmd(connection_file: str, **kwargs: Any) -> None:  # noqa: C901
    """Runs the data import process."""
    # Handle dry-run mode early
    dry_run = kwargs.pop("dry_run", False)
    if dry_run:
        _run_dry_run_validation(connection_file, **kwargs)
        return

    # Handle protocol option - create config dict if protocol specified
    # --no-cache disables the on-disk id-map cache process-wide for this run.
    cache.set_cache_enabled(not kwargs.pop("no_cache", False))

    protocol = kwargs.pop("protocol", None)
    if protocol:
        # Pass config as dict with protocol instead of file path
        # conf_lib will merge this with file contents
        kwargs["config"] = {"_config_file": connection_file, "protocol": protocol}
        log.info(f"Using {protocol} protocol for RPC communication")
    else:
        kwargs["config"] = connection_file

    try:
        kwargs["context"] = ast.literal_eval(kwargs.get("context", "{}"))
    except (ValueError, SyntaxError) as e:
        log.error(f"Invalid --context dictionary provided: {e}")
        return

    context = kwargs.get("context", {})

    # Handle multicompany context
    company_id = kwargs.pop("company_id", None)
    all_companies = kwargs.pop("all_companies", False)

    if all_companies:
        # Fetch all companies the user has access to
        from .lib.conf_lib import get_connection_from_config, get_connection_from_dict

        try:
            if isinstance(kwargs["config"], dict):
                conn = get_connection_from_dict(kwargs["config"])
            else:
                conn = get_connection_from_config(kwargs["config"])

            user_model = conn.get_model("res.users")
            user_data = user_model.read(conn.user_id, ["company_ids"])
            user_company_ids = user_data.get("company_ids", [])

            if user_company_ids:
                context["allowed_company_ids"] = user_company_ids
                log.info(
                    f"All-companies mode: enabled access to {len(user_company_ids)} "
                    f"companies: {user_company_ids}"
                )
            else:
                log.warning(
                    "No company access found for user. "
                    "Continuing without setting allowed_company_ids."
                )
        except Exception as e:
            log.error(f"Failed to fetch user companies: {e}")
            log.warning("Continuing without setting allowed_company_ids.")

    elif company_id is not None:
        # Resolve company_id (can be database ID or XML ID)
        resolved_company_id: Optional[int] = None

        if company_id.isdigit():
            # It's a database ID
            resolved_company_id = int(company_id)
        else:
            # It's an XML ID - resolve it
            from .lib.conf_lib import (
                get_connection_from_config,
                get_connection_from_dict,
            )

            try:
                if isinstance(kwargs["config"], dict):
                    conn = get_connection_from_dict(kwargs["config"])
                else:
                    conn = get_connection_from_config(kwargs["config"])

                ir_model_data = conn.get_model("ir.model.data")

                # Parse the XML ID (module.name format)
                if "." in company_id:
                    module, name = company_id.split(".", 1)
                else:
                    module, name = "base", company_id

                found = ir_model_data.search(
                    [
                        ("module", "=", module),
                        ("name", "=", name),
                        ("model", "=", "res.company"),
                    ]
                )

                if found:
                    data = ir_model_data.read(found[0], ["res_id"])
                    resolved_company_id = data["res_id"]
                    log.info(
                        f"Resolved company XML ID '{company_id}' "
                        f"to database ID {resolved_company_id}"
                    )
                else:
                    log.error(
                        f"Company XML ID '{company_id}' not found. "
                        "Make sure the external ID exists for a res.company record."
                    )
                    return
            except Exception as e:
                log.error(f"Failed to resolve company XML ID '{company_id}': {e}")
                return

        if resolved_company_id is not None:
            # Set allowed_company_ids to enable cross-company access
            # Note: force_company is deprecated in Odoo 18+ and causes warnings
            context["allowed_company_ids"] = [resolved_company_id]
            log.info(f"Multicompany mode enabled for company ID: {resolved_company_id}")

    # Handle tracking_disable option
    tracking_disable = kwargs.pop("tracking_disable", True)
    context["tracking_disable"] = tracking_disable
    if tracking_disable:
        # Additional context keys to fully suppress mail/chatter messages
        # These prevent tracking on related records (e.g., res.partner when
        # importing res.partner.bank)
        context["mail_create_nolog"] = True  # Don't log record creation
        context["mail_notrack"] = True  # Don't track field changes
        context["mail_activity_automation_skip"] = True  # Skip activity automation
    else:
        log.info("Mail tracking enabled for this import")

    # Handle defer_parent_store option
    defer_parent_store = kwargs.pop("defer_parent_store", False)
    if defer_parent_store:
        context["defer_parent_store_computation"] = True
        log.info("Parent store computation will be deferred")

    # Handle --on-missing-ref option: parse field:action pairs
    on_missing_ref = kwargs.pop("on_missing_ref", None)
    name_create_enabled_fields: dict[str, bool] = {}
    import_set_empty_fields: list[str] = []

    if on_missing_ref:
        for pair in on_missing_ref.split(","):
            if ":" not in pair:
                log.warning(
                    f"Invalid --on-missing-ref format: '{pair}'. "
                    "Expected 'field:action'"
                )
                continue
            field, action = pair.split(":", 1)
            field = field.strip()
            action = action.strip().lower()
            if action == "create":
                name_create_enabled_fields[field] = True
                log.info(f"Field '{field}': will auto-create missing references")
            elif action == "empty":
                import_set_empty_fields.append(field)
                log.info(f"Field '{field}': will set to empty if reference not found")
            elif action == "skip":
                # Skip is the default behavior (row goes to fail file)
                log.info(f"Field '{field}': will skip row if reference not found")
            else:
                log.warning(
                    f"Unknown action '{action}' for field '{field}'. "
                    "Use 'create', 'skip', or 'empty'"
                )

    # Handle --auto-create-refs option
    auto_create_refs = kwargs.pop("auto_create_refs", False)
    if auto_create_refs:
        # This will be handled in the importer to enable name_create for all m2o fields
        kwargs["auto_create_refs"] = True
        log.info("Auto-create enabled for all many2one fields")

    # Handle --set-empty-on-missing option
    set_empty_on_missing = kwargs.pop("set_empty_on_missing", False)
    if set_empty_on_missing:
        kwargs["set_empty_on_missing"] = True
        log.info("Fields will be set to empty when references not found")

    # Handle --fallback-values option: parse field:value pairs
    fallback_values_str = kwargs.pop("fallback_values", None)
    fallback_values: dict[str, str] = {}
    if fallback_values_str:
        for pair in fallback_values_str.split(","):
            if ":" not in pair:
                log.warning(
                    f"Invalid --fallback-values format: '{pair}'. "
                    "Expected 'field:value'"
                )
                continue
            field, value = pair.split(":", 1)
            fallback_values[field.strip()] = value.strip()
            log.info(f"Fallback value for '{field.strip()}': '{value.strip()}'")

    # Add import options to context
    if name_create_enabled_fields:
        context["name_create_enabled_fields"] = name_create_enabled_fields
    if import_set_empty_fields:
        context["import_set_empty_fields"] = import_set_empty_fields
    if fallback_values:
        context["fallback_values"] = fallback_values

    kwargs["context"] = context
    resolve_relation_specs = kwargs.pop("resolve_relation_specs", ())
    if resolve_relation_specs:
        kwargs["resolve_relations"] = _parse_resolve_relation_specs(
            resolve_relation_specs
        )

    # Handle groupby option
    groupby = kwargs.get("groupby")
    if groupby is not None:
        kwargs["groupby"] = [col.strip() for col in groupby.split(",") if col.strip()]

    # Convert deferred_fields from comma-separated string to list
    deferred = kwargs.get("deferred_fields")
    if deferred is not None:
        kwargs["deferred_fields"] = [
            f.strip() for f in deferred.split(",") if f.strip()
        ]

    # Convert ignore from comma-separated string to list
    ignore = kwargs.get("ignore")
    if ignore is not None:
        kwargs["ignore"] = [col.strip() for col in ignore.split(",") if col.strip()]

    # Handle --post-action flag
    post_action = kwargs.pop("post_action", None)

    # Handle --move-date flag (for opening inventory)
    move_date = kwargs.pop("move_date", None)
    if move_date and not post_action:
        log.warning(
            "--move-date is only useful with --post-action action_apply_inventory. "
            "The option will be ignored."
        )
        move_date = None

    # Handle --sudo flag: temporarily disable record rules for the model
    sudo = kwargs.pop("sudo", False)
    import_result: Optional[dict[str, int]] = None
    if sudo:
        from .lib.conf_lib import get_connection_from_config, get_connection_from_dict

        model = kwargs.get("model")
        disabled_rule_ids: list[int] = []
        ir_rule = None

        try:
            # Get connection
            if isinstance(kwargs["config"], dict):
                conn = get_connection_from_dict(kwargs["config"])
            else:
                conn = get_connection_from_config(kwargs["config"])

            # Find and disable record rules for this model
            ir_model = conn.get_model("ir.model")
            ir_rule = conn.get_model("ir.rule")

            model_ids = ir_model.search([("model", "=", model)])
            if model_ids:
                # Find active record rules for this model
                rule_ids = ir_rule.search(
                    [
                        ("model_id", "=", model_ids[0]),
                        ("active", "=", True),
                    ]
                )
                if rule_ids:
                    # Disable the rules
                    ir_rule.write(rule_ids, {"active": False})
                    disabled_rule_ids = rule_ids
                    log.info(
                        f"Sudo mode: temporarily disabled {len(rule_ids)} "
                        f"record rule(s) for model '{model}'"
                    )

            # Run import with rules disabled
            import_result = run_import(**kwargs)

            # Execute post-action if specified and any records were imported
            if post_action and import_result is not None:
                # Extract product IDs BEFORE post-action while connection is reliable
                # This is needed for --move-date to find the correct moves
                product_ids_for_move_update: list[int] = []
                if move_date and import_result:
                    quant_ids = list(import_result.values())
                    log.info(
                        f"Extracting product IDs from {len(quant_ids)} imported quants "
                        f"for --move-date update..."
                    )
                    product_ids_for_move_update = _get_product_ids_from_quants(
                        kwargs["config"], quant_ids
                    )
                    num_products = len(product_ids_for_move_update)
                    log.info(f"Extracted {num_products} unique product IDs")

                # Execute the post-action (with longer timeout)
                post_action_ok = _execute_post_action(
                    kwargs["config"], model, post_action, import_result, context
                )

                # Update move dates if requested (for opening inventory)
                # Proceed even if post-action timed out (server may have completed)
                if move_date:
                    if not product_ids_for_move_update:
                        log.warning(
                            "--move-date: No product IDs extracted from quants. "
                            "Move date update will be skipped."
                        )
                    elif not post_action_ok:
                        log.warning(
                            "--move-date: Post-action failed. "
                            "Move date update will be skipped."
                        )
                    else:
                        log.info(
                            f"--move-date: Updating move dates to {move_date} "
                            f"for {len(product_ids_for_move_update)} products..."
                        )
                        _update_inventory_move_dates(
                            kwargs["config"],
                            move_date,
                            context,
                            product_ids_for_move_update,
                        )

        finally:
            # Re-enable the rules
            if disabled_rule_ids and ir_rule:
                try:
                    ir_rule.write(disabled_rule_ids, {"active": True})
                    log.info(
                        f"Sudo mode: re-enabled {len(disabled_rule_ids)} "
                        f"record rule(s) for model '{model}'"
                    )
                except Exception as e:
                    log.error(f"Failed to re-enable record rules: {e}")
                    log.error(
                        f"IMPORTANT: Record rules {disabled_rule_ids} for model "
                        f"'{model}' may still be disabled! Please re-enable them "
                        "manually in Odoo."
                    )
    else:
        import_result = run_import(**kwargs)

        # Execute post-action if specified and any records were imported
        if post_action and import_result is not None:
            # Extract product IDs BEFORE post-action while connection is reliable
            # This is needed for --move-date to find the correct moves
            product_ids_for_move_update = []
            if move_date and import_result:
                quant_ids = list(import_result.values())
                log.info(
                    f"Extracting product IDs from {len(quant_ids)} imported quants "
                    f"for --move-date update..."
                )
                product_ids_for_move_update = _get_product_ids_from_quants(
                    kwargs["config"], quant_ids
                )
                log.info(
                    f"Extracted {len(product_ids_for_move_update)} unique product IDs"
                )

            # Execute the post-action (with longer timeout)
            post_action_ok = _execute_post_action(
                kwargs["config"],
                kwargs.get("model"),
                post_action,
                import_result,
                context,
            )

            # Update move dates if requested (for opening inventory)
            # Proceed even if post-action timed out (server may have completed)
            if move_date:
                if not product_ids_for_move_update:
                    log.warning(
                        "--move-date: No product IDs extracted from quants. "
                        "Move date update will be skipped."
                    )
                elif not post_action_ok:
                    log.warning(
                        "--move-date: Post-action failed. "
                        "Move date update will be skipped."
                    )
                else:
                    log.info(
                        f"--move-date: Updating move dates to {move_date} "
                        f"for {len(product_ids_for_move_update)} products..."
                    )
                    _update_inventory_move_dates(
                        kwargs["config"],
                        move_date,
                        context,
                        product_ids_for_move_update,
                    )

    # #247: a None return means the run aborted fatally (bad credentials,
    # unreachable model, invalid input) and imported nothing — the error panel is
    # already rendered above. Propagate a non-zero exit so automation (set -e / $?)
    # sees the failure instead of a false success. A successful import — including a
    # partial one (fail file written) or a --fail run with nothing to retry — returns
    # a dict (possibly empty) and keeps exit 0.
    if import_result is None:
        raise SystemExit(1)


# --- Write Command (New) ---
@cli.command(name="write")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option("--file", "filename", required=True, help="File with records to update.")
@click.option("--model", required=True, help="Odoo model to write to.")
@click.option(
    "--worker", default=1, type=int, help="Number of simultaneous connections."
)
@click.option(
    "--size",
    "batch_size",
    default=1000,
    type=int,
    help="Number of records to process per batch.",
)
@click.option(
    "--fail",
    is_flag=True,
    default=False,
    help="Run in fail mode, retrying records from the _write_fail.csv file.",
)
@click.option("-s", "--sep", "separator", default=";", help="CSV separator character.")
@click.option(
    "--context",
    default="{'tracking_disable': True}",
    help="Odoo context as a dictionary string.",
)
@click.option("--encoding", default="utf-8", help="Encoding of the data file.")
def write_cmd(connection_file: str, **kwargs: Any) -> None:
    """Runs the batch update (write) process."""
    kwargs["config"] = connection_file
    try:
        kwargs["context"] = ast.literal_eval(kwargs.get("context", "{}"))
    except (ValueError, SyntaxError) as e:
        log.error(f"Invalid --context dictionary provided: {e}")
        return

    # Add extra mail tracking suppression flags if tracking_disable is set
    context = kwargs.get("context", {})
    if context.get("tracking_disable", False):
        context["mail_create_nolog"] = True
        context["mail_notrack"] = True
        context["mail_activity_automation_skip"] = True
        kwargs["context"] = context

    run_write(**kwargs)


# --- Export Command ---
@cli.command(name="export")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
)
@click.option(
    "--protocol",
    type=click.Choice(
        ["xmlrpc", "xmlrpcs", "jsonrpc", "jsonrpcs", "json2", "json2s"],
        case_sensitive=False,
    ),
    default=None,
    help="RPC protocol to use. Options: xmlrpc (default for Odoo 8-9), "
    "jsonrpc (recommended for Odoo 10-18, ~30%% faster), "
    "json2 (Odoo 19+, requires API key). "
    "If not specified, uses protocol from config file or defaults to xmlrpc.",
)
@click.option("--output", required=True, help="Output file path.")
@click.option("--model", required=True, help="Odoo model to export from.")
@click.option(
    "--fields",
    required=True,
    help="""Comma-separated list of fields to export.
    Special specifiers are available for IDs:
    '.id' for raw database ID; 'field/.id' for related raw ID;
    'id' for XML ID; 'field/id' for related XML ID.
    The tool automatically uses the best export method based on the fields requested.
    """,
)
@click.option("--domain", default="[]", help="Odoo domain filter as a list string.")
@click.option(
    "--worker", default=1, type=int, help="Number of simultaneous connections."
)
@click.option(
    "--size",
    "batch_size",
    default=4000,
    type=int,
    help="Number of records to process per batch.",
)
@click.option(
    "--streaming",
    is_flag=True,
    help="""Enable streaming to write data batch-by-batch.
    Use for very large datasets.""",
)
@click.option(
    "--resume-session",
    default=None,
    help="Resume a previously failed export session using its ID.",
)
@click.option("-s", "--sep", "separator", default=";", help="CSV separator character.")
@click.option(
    "--context",
    default="{'tracking_disable': True}",
    help="Odoo context as a dictionary string.",
)
@click.option("--encoding", default="utf-8", help="Encoding of the data file.")
@click.option(
    "--technical-names",
    is_flag=True,
    default=False,
    help="""Force the use of the high-performance raw export mode.
    This is often enabled automatically if you request raw IDs or technical field types
    like 'selection' or 'binary'.
    """,
)
@click.option(
    "--all-companies",
    is_flag=True,
    default=False,
    help="Automatically set allowed_company_ids to all companies the user has "
    "access to. This enables exporting records across multiple companies.",
)
@click.option(
    "--sudo",
    is_flag=True,
    default=False,
    help="Temporarily disable record rules for the model during export. "
    "Requires admin rights. Use with --all-companies to export all records "
    "across companies regardless of restrictive record rules.",
)
@click.option(
    "--sanitize-newlines",
    default=None,
    help="Replace embedded newlines in text fields with this string. "
    'Default: None (no sanitization). Recommended: " | " to prevent '
    "CSV corruption from embedded newlines in text/char/html fields.",
)
def export_cmd(connection_file: str, **kwargs: Any) -> None:  # noqa: C901
    """Runs the data export process."""
    # Handle protocol option - create config dict if protocol specified
    protocol = kwargs.pop("protocol", None)
    if protocol:
        kwargs["config"] = {"_config_file": connection_file, "protocol": protocol}
        log.info(f"Using {protocol} protocol for RPC communication")
    else:
        kwargs["config"] = connection_file

    # Handle --all-companies flag
    all_companies = kwargs.pop("all_companies", False)
    if all_companies:
        import ast

        from .lib.conf_lib import get_connection_from_config, get_connection_from_dict

        # Parse the existing context string
        context_str = kwargs.get("context", "{}")
        try:
            context = ast.literal_eval(context_str)
            if not isinstance(context, dict):
                context = {}
        except (ValueError, SyntaxError):
            context = {}

        # Parse the existing domain string
        domain_str = kwargs.get("domain", "[]")
        try:
            domain = ast.literal_eval(domain_str)
            if not isinstance(domain, list):
                domain = []
        except (ValueError, SyntaxError):
            domain = []

        try:
            if isinstance(kwargs["config"], dict):
                conn = get_connection_from_dict(kwargs["config"])
            else:
                conn = get_connection_from_config(kwargs["config"])

            user_model = conn.get_model("res.users")
            user_data = user_model.read(conn.user_id, ["company_ids"])
            user_company_ids = user_data.get("company_ids", [])

            if user_company_ids:
                context["allowed_company_ids"] = user_company_ids
                log.info(
                    f"All-companies mode: enabled access to {len(user_company_ids)} "
                    f"companies: {user_company_ids}"
                )

                # Check if model has company_id field before adding domain filter
                model = kwargs.get("model")
                model_obj = conn.get_model(model)
                fields = model_obj.fields_get(["company_id"])
                if "company_id" in fields:
                    # Add domain filter to include records from all companies
                    # This handles models where company_id can be False (shared)
                    company_domain = [
                        "|",
                        ("company_id", "=", False),
                        ("company_id", "in", user_company_ids),
                    ]
                    # Combine with existing domain
                    if domain:
                        domain = company_domain + domain
                    else:
                        domain = company_domain
                    kwargs["domain"] = str(domain)
                    log.info(f"Added company_id domain filter for model '{model}'")
                else:
                    log.info(
                        f"Model '{model}' has no company_id field, "
                        "skipping domain filter"
                    )
            else:
                log.warning(
                    "No company access found for user. "
                    "Continuing without setting allowed_company_ids."
                )
        except Exception as e:
            log.error(f"Failed to fetch user companies: {e}")
            log.warning("Continuing without setting allowed_company_ids.")

        # Pass context as dict (run_export will handle both str and dict)
        kwargs["context"] = context

    # Handle --sudo flag: temporarily disable record rules for the model
    sudo = kwargs.pop("sudo", False)
    if sudo:
        from .lib.conf_lib import get_connection_from_config, get_connection_from_dict

        model = kwargs.get("model")
        if model is None:
            raise click.BadParameter("--model is required when using --sudo")
        fields = kwargs.get("fields", "")
        disabled_rule_ids: list[int] = []
        ir_rule = None

        try:
            # Get connection
            if isinstance(kwargs["config"], dict):
                conn = get_connection_from_dict(kwargs["config"])
            else:
                conn = get_connection_from_config(kwargs["config"])

            ir_model = conn.get_model("ir.model")
            ir_rule = conn.get_model("ir.rule")

            # Collect all models to disable rules for (main + related)
            models_to_disable: set[str] = {model}

            # Find related models from the fields being exported
            model_obj = conn.get_model(model)
            field_names = [
                f.split("/")[0].replace(".id", "") for f in fields.split(",")
            ]
            field_names = [f for f in field_names if f and f != "id"]
            if field_names:
                fields_meta = model_obj.fields_get(field_names)
                for _field_name, meta in fields_meta.items():
                    if meta.get("relation"):
                        models_to_disable.add(meta["relation"])

            # Find and disable record rules for all models
            for model_name in models_to_disable:
                model_ids = ir_model.search([("model", "=", model_name)])
                if model_ids:
                    rule_ids = ir_rule.search(
                        [
                            ("model_id", "=", model_ids[0]),
                            ("active", "=", True),
                        ]
                    )
                    if rule_ids:
                        ir_rule.write(rule_ids, {"active": False})
                        disabled_rule_ids.extend(rule_ids)
                        log.info(
                            f"Sudo mode: disabled {len(rule_ids)} rule(s) "
                            f"for '{model_name}'"
                        )

            if disabled_rule_ids:
                log.info(
                    f"Sudo mode: temporarily disabled {len(disabled_rule_ids)} "
                    f"record rule(s) total across {len(models_to_disable)} model(s)"
                )

            # Run export with rules disabled
            run_export(**kwargs)

        finally:
            # Re-enable the rules
            if disabled_rule_ids and ir_rule:
                try:
                    ir_rule.write(disabled_rule_ids, {"active": True})
                    log.info(
                        f"Sudo mode: re-enabled {len(disabled_rule_ids)} record rule(s)"
                    )
                except Exception as e:
                    log.error(f"Failed to re-enable record rules: {e}")
                    log.error(
                        f"IMPORTANT: Record rules {disabled_rule_ids} may still "
                        "be disabled! Please re-enable them manually in Odoo."
                    )
    else:
        run_export(**kwargs)


# --- Path-to-Image Command ---
@cli.command(name="path-to-image")
@click.argument("file")
@click.option(
    "-f",
    "--fields",
    required=True,
    help="""Comma-separated list of fields to export.
        Special specifiers are available for IDs:
        '.id' for the raw database ID of the record.
        'field/.id' for the raw database ID of a related record.
        'id' for the XML/External ID of the record.
        'field/id' for the XML/External ID of a related record.
        Using '.id' or '/.id' will automatically enable a faster, raw export mode.
        """,
)
@click.option(
    "--path",
    default=None,
    help="Image path prefix. Defaults to the current working directory.",
)
@click.option("--out", default="out.csv", help="Name of the resulting output file.")
def path_to_image_cmd(**kwargs: Any) -> None:
    """Converts columns with local file paths into base64 strings."""
    run_path_to_image(**kwargs)


# --- URL-to-Image Command ---
@cli.command(name="url-to-image")
@click.argument("file")
@click.option(
    "-f",
    "--fields",
    required=True,
    help="Comma-separated list of fields with URLs to convert to base64.",
)
@click.option("--out", default="out.csv", help="Name of the resulting output file.")
def url_to_image_cmd(**kwargs: Any) -> None:
    """Downloads content from URLs in columns and converts to base64."""
    run_url_to_image(**kwargs)


# --- Migrate Command ---
@cli.command(name="migrate")
@click.option(
    "--config-export",
    required=True,
    help="Path to the source Odoo connection config.",
)
@click.option(
    "--config-import",
    required=True,
    help="Path to the destination Odoo connection config.",
)
@click.option("--model", required=True, help="The Odoo model to migrate.")
@click.option(
    "--domain", default="[]", help="Domain filter to select records for export."
)
@click.option(
    "--fields", required=True, help="Comma-separated list of fields to migrate."
)
@click.option(
    "--mapping",
    default=None,
    help="A dictionary string defining the transformation mapping.",
)
@click.option(
    "--export-worker",
    default=1,
    type=int,
    help="Number of workers for the export phase.",
)
@click.option(
    "--export-batch-size",
    default=2000,
    type=int,
    help="Batch size for the export phase.",
)
@click.option(
    "--import-worker",
    default=1,
    type=int,
    help="Number of workers for the import phase.",
)
@click.option(
    "--import-batch-size",
    default=200,
    type=int,
    help="Batch size for the import phase.",
)
def migrate_cmd(**kwargs: Any) -> None:
    """Performs a direct server-to-server data migration."""
    if kwargs.get("mapping"):
        try:
            parsed_mapping = ast.literal_eval(kwargs["mapping"])
            if not isinstance(parsed_mapping, dict):
                raise TypeError("Mapping must be a dictionary.")
            kwargs["mapping"] = parsed_mapping
        except (ValueError, TypeError, SyntaxError) as e:
            print(
                "Error: Invalid mapping provided. "
                f"Must be a valid Python dictionary string. Error: {e}"
            )
            return
    migration_ok = run_migration(**kwargs)
    # Exit non-zero when any record failed so the migration failure is visible to
    # callers/CI instead of being masked by a success exit code.
    if not migration_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
