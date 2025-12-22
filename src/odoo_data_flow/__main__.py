"""Command-line interface for odoo-data-flow."""

import ast
from importlib.metadata import version as get_version
from pathlib import Path
from typing import Any, Optional

import click

from .converter import run_path_to_image, run_url_to_image
from .exporter import run_export
from .importer import run_import
from .lib.actions.language_installer import run_language_installation
from .lib.actions.module_manager import (
    run_module_installation,
    run_module_uninstallation,
    run_update_module_list,
)
from .logging_config import log, setup_logging
from .migrator import run_migration
from .workflow_runner import run_invoice_v9_workflow
from .writer import run_write


def run_project_flow(flow_file: str, flow_name: Optional[str]) -> None:
    """Placeholder for running a project flow."""
    log.info(f"Running project flow from '{flow_file}'")
    if flow_name:
        log.info(f"Executing specific flow: '{flow_name}'")
    else:
        log.info("Executing all flows defined in the file.")


@click.group(
    context_settings=dict(help_option_names=["-h", "--help"]),
    invoke_without_command=True,
)
@click.version_option(version=get_version("odoo-data-flow"))
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
    """Odoo Data Flow: A tool for importing, exporting, and processing data."""
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


# --- Import Command ---
@cli.command(name="import")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
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
    "--worker", default=1, type=int, help="Number of simultaneous connections."
)
@click.option(
    "--size",
    "batch_size",
    default=500,
    type=int,
    help="Number of lines to import per connection.",
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
    type=int,
    help="Company ID for multicompany imports. Sets allowed_company_ids context "
    "to enable cross-company field references. Use when importing records that "
    "reference users/data from different companies.",
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
    "--defer-parent-store",
    is_flag=True,
    default=False,
    help="Defer parent_left/parent_right computation for hierarchical models. "
    "Improves performance for large imports of nested structures.",
)
@click.option("--encoding", default="utf-8", help="Encoding of the data file.")
def import_cmd(connection_file: str, **kwargs: Any) -> None:  # noqa: C901
    """Runs the data import process."""
    kwargs["config"] = connection_file
    try:
        kwargs["context"] = ast.literal_eval(kwargs.get("context", "{}"))
    except (ValueError, SyntaxError) as e:
        log.error(f"Invalid --context dictionary provided: {e}")
        return

    context = kwargs.get("context", {})

    # Handle multicompany context
    company_id = kwargs.pop("company_id", None)
    if company_id is not None:
        # Set allowed_company_ids to enable cross-company access
        context["allowed_company_ids"] = [company_id]
        # Also set force_company for compatibility with older Odoo versions
        context["force_company"] = company_id
        log.info(f"Multicompany mode enabled for company ID: {company_id}")

    # Handle tracking_disable option
    tracking_disable = kwargs.pop("tracking_disable", True)
    context["tracking_disable"] = tracking_disable
    if not tracking_disable:
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
                log.warning(f"Unknown action '{action}' for field '{field}'. "
                           "Use 'create', 'skip', or 'empty'")

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

    run_import(**kwargs)


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
    run_write(**kwargs)


# --- Export Command ---
@cli.command(name="export")
@click.option(
    "--connection-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the Odoo connection file.",
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
def export_cmd(connection_file: str, **kwargs: Any) -> None:
    """Runs the data export process."""
    kwargs["config"] = connection_file
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
    run_migration(**kwargs)


if __name__ == "__main__":
    cli()
