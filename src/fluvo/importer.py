"""Main importer module.

This module contains the high-level logic for orchestrating the import process.
It handles file I/O, pre-flight checks, and the delegation of the core
import tasks to the multi-threaded `import_threaded` module.
"""

import csv
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Union, cast

import polars as pl
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress

from . import import_threaded
from .enums import PreflightMode
from .lib import cache, preflight, relational_import, sort
from .lib.internal.ui import _show_error_panel
from .logging_config import log, suppress_console_handler


def _count_lines(filepath: str) -> int:
    """Counts the number of lines in a file, returning 0 if it doesn't exist."""
    try:
        with open(filepath, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def _infer_model_from_filename(filename: str) -> Optional[str]:
    """Tries to guess the Odoo model from a CSV filename."""
    basename = Path(filename).stem
    # Remove common suffixes like _fail, _transformed, etc.
    clean_name = re.sub(r"(_fail|_transformed|_\d+)$", "", basename)
    # Convert underscores to dots
    model_name = clean_name.replace("_", ".")
    if "." in model_name:
        return model_name
    return None


def _get_fail_filename(model: str, is_fail_run: bool = False) -> str:
    """Generates a standardized filename for failed records.

    Args:
        model (str): The Odoo model name being imported.
        is_fail_run (bool): Unused, kept for API compatibility.
            The fail file is always the same name so it gets overwritten
            instead of accumulating timestamped copies (#182).

    Returns:
        str: The generated filename for the fail file.
    """
    model_filename = model.replace(".", "_")
    return f"{model_filename}_fail.csv"


def _get_env_from_config(
    config: Optional[Union[str, dict[str, Any]]],
) -> Optional[str]:
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


def _run_preflight_checks(
    preflight_mode: PreflightMode, import_plan: dict[str, Any], **kwargs: Any
) -> bool:
    """Iterates through and runs all registered pre-flight checks.

    Args:
        preflight_mode (PreflightMode): The current mode (NORMAL or FAIL_MODE).
        import_plan (dict[str, Any]): A dictionary that checks can populate
            with strategy details (e.g., detected deferred fields).
        **kwargs (Any): A dictionary of arguments to pass to each check.

    Returns:
        bool: True if all checks pass, False otherwise.
    """
    for check_func in preflight.PREFLIGHT_CHECKS:
        if not check_func(
            preflight_mode=preflight_mode, import_plan=import_plan, **kwargs
        ):
            return False
    return True


def run_import(  # noqa: C901
    config: Union[str, dict[str, Any]],
    filename: str,
    model: Optional[str],
    deferred_fields: Optional[list[str]],
    auto_defer: bool,
    unique_id_field: Optional[str],
    no_preflight_checks: bool,
    headless: bool,
    worker: int,
    batch_size: int,
    skip: int,
    fail: bool,
    separator: str,
    ignore: Optional[list[str]],
    context: Any,  # Accept Any type for robustness
    encoding: str,
    o2m: bool,
    groupby: Optional[list[str]],
    auto_create_refs: bool = False,
    auto_groupby: bool = False,
    set_empty_on_missing: bool = False,
    batch_delay: float = 0.0,
    stream: bool = False,
    resume: bool = True,
    no_checkpoint: bool = False,
    check_refs: str = "warn",
    skip_unchanged: bool = False,
    skip_existing: bool = False,
    adaptive_throttle: bool = True,
    max_batch_bytes: int = 5 * 1024 * 1024,
    resolve_relations: Optional[list[dict[str, Any]]] = None,
    auto_clean: bool = False,
    fix_missing_variants: bool = False,
    allow_xmlid_collisions: bool = False,
    m2m_mode: str = "replace",
    allow_default_company: bool = False,
) -> Optional[dict[str, int]]:
    """Main entry point for the import command, handling all orchestration.

    Returns:
        dict[str, int]: Mapping of external IDs to database IDs for all
            successfully imported records, or None if the import failed.
    """
    log.info("Starting data import process from file...")

    parsed_context: dict[str, Any]
    if isinstance(context, str):
        try:
            parsed_context_raw: Any = json.loads(context)
            if not isinstance(parsed_context_raw, dict):
                raise TypeError
            parsed_context = parsed_context_raw
        except (json.JSONDecodeError, TypeError):
            _show_error_panel(
                "Invalid Context",
                "The --context argument must be a valid JSON dictionary string.",
            )
            return None
    elif isinstance(context, dict):
        parsed_context = context
    else:
        _show_error_panel(
            "Invalid Context", "The context must be a dictionary or a JSON string."
        )
        return None

    if not model:
        model = _infer_model_from_filename(filename)
        if not model:
            _show_error_panel(
                "Model Not Found",
                "Could not infer model from filename. Please use the --model option.",
            )
            return None

    file_to_process = filename
    # Determine environment-specific output directory from config file name
    env_name = _get_env_from_config(config)
    input_file_dir = Path(filename).resolve().parent
    if env_name:
        # Avoid nested directories if input is already in env directory
        # e.g., data/prod/file.csv with env="prod" -> data/prod/ not data/prod/prod/
        if input_file_dir.name == env_name:
            env_output_dir = input_file_dir
        else:
            env_output_dir = input_file_dir / env_name
    else:
        env_output_dir = input_file_dir

    if fail:
        # Look for fail file in environment-specific directory
        fail_path = env_output_dir / _get_fail_filename(model, False)
        line_count = _count_lines(str(fail_path))
        if line_count <= 1:
            Console().print(
                Panel(
                    f"No records to retry in '{fail_path}'.",
                    title="[bold green]No Recovery Needed[/bold green]",
                )
            )
            # An empty id_map, NOT None: a --fail run with nothing to retry
            # *completed successfully* (there was nothing to do). The CLI treats a
            # None return as a fatal abort and exits non-zero (#247), so this
            # benign no-op must stay distinguishable from a real failure.
            return {}
        log.info(
            f"Running in --fail mode. Retrying {line_count - 1} records from: "
            f"{fail_path}"
        )
        file_to_process = str(fail_path)
        if ignore is None:
            ignore = []
        if "_ERROR_REASON" not in ignore:
            log.info("Ignoring the internal '_ERROR_REASON' column for re-import.")
            ignore.append("_ERROR_REASON")

    import_plan: dict[str, Any] = {}
    if not no_preflight_checks:
        validation_filename = filename if fail else file_to_process
        if not _run_preflight_checks(
            preflight_mode=PreflightMode.FAIL_MODE if fail else PreflightMode.NORMAL,
            import_plan=import_plan,
            model=model,
            filename=file_to_process,
            validation_filename=validation_filename,
            config=config,
            headless=headless,
            separator=separator,
            unique_id_field=unique_id_field,
            ignore=ignore or [],
            o2m=o2m,
            auto_defer=auto_defer,
            auto_groupby=auto_groupby,
            groupby=groupby,
            check_refs=check_refs,
            encoding=encoding,
            allow_xmlid_collisions=allow_xmlid_collisions,
            context=parsed_context,
            allow_default_company=allow_default_company,
        ):
            return None

    # Apply an auto-detected groupby column when the user enabled --auto-groupby
    # and did not pass an explicit --groupby (deadlock avoidance).
    if auto_groupby and not groupby:
        groupby = import_plan.get("groupby") or None
        if groupby:
            log.info(f"Auto-groupby active: grouping by {', '.join(groupby)}.")

    # --- Strategy Execution ---
    sorted_temp_file = None
    if import_plan.get("strategy") == "sort_and_one_pass_load":
        log.info("Executing 'Sort & One-Pass Load' strategy.")
        sorted_temp_file = sort.sort_for_self_referencing(
            file_to_process,
            id_column=import_plan["id_column"],
            parent_column=import_plan["parent_column"],
            encoding=encoding,
            separator=separator,
        )
        if isinstance(sorted_temp_file, str):
            file_to_process = sorted_temp_file
            # Disable deferred fields for this strategy
            deferred_fields = []

    # Only use auto-detected deferred fields if:
    # 1. User explicitly specified deferred_fields, OR
    # 2. User enabled auto_defer flag
    # This prevents automatic deferral of m2m/o2m fields without user consent
    if deferred_fields:
        final_deferred = deferred_fields
    elif auto_defer:
        final_deferred = import_plan.get("deferred_fields", [])
    else:
        # Check for self-referencing fields only (like parent_id)
        # These are the only fields that MUST be deferred for correctness
        detected = import_plan.get("deferred_fields", [])
        # Filter to only include self-referencing fields detected by preflight
        # For now, we'll only auto-defer if explicitly requested
        final_deferred = []
        if detected:
            log.debug(
                f"Deferrable fields detected but not applied (use --auto-defer "
                f"or --deferred-fields to enable): {detected}"
            )
    # Safeguard: never defer a required relational field. Deferring it would run
    # the Pass-1 create without the value and fail with 'Missing required value'.
    required_relational = import_plan.get("required_relational_fields", [])
    if final_deferred and required_relational:
        blocked = [f for f in final_deferred if f in required_relational]
        if blocked:
            log.warning(
                f"Ignoring deferral of required relational field(s) {blocked}: "
                f"required fields must be set in Pass 1. They will be imported "
                f"normally to avoid 'Missing required value' errors."
            )
            final_deferred = [f for f in final_deferred if f not in blocked]

    # Guard against --groupby on a deferred field (#185/#186). A deferred field is
    # not written in Pass 1, so partitioning Pass-1 batches by it is meaningless and
    # silently breaks Pass-2 relation resolution. Drop the conflicting column(s)
    # from groupby with a warning rather than producing undefined behavior.
    if groupby and final_deferred:

        def _base(field: str) -> str:
            return field.replace("/.id", "").replace("/id", "")

        deferred_bases = {_base(f) for f in final_deferred}
        conflicting = [g for g in groupby if _base(g) in deferred_bases]
        if conflicting:
            log.warning(
                f"Field(s) {conflicting} are in both --groupby and the deferred set. "
                f"A deferred field is not imported in Pass 1, so it cannot be grouped "
                f"on; removing it from --groupby. It is still resolved in Pass 2."
            )
            groupby = [g for g in groupby if g not in conflicting] or None

    final_uid_field = unique_id_field or import_plan.get("unique_id_field") or "id"
    # Create environment-specific directory if it doesn't exist
    if env_name and not env_output_dir.exists():
        env_output_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"Created environment directory: {env_output_dir}")
    fail_output_file = str(env_output_dir / _get_fail_filename(model, fail))

    if fail:
        log.info("Single-record batching enabled for this import strategy.")
        max_conn = 1
        batch_size_run = 1
        force_create = True
    else:
        max_conn = worker
        batch_size_run = batch_size
        force_create = False

    start_time = time.time()
    try:
        success, stats = import_threaded.import_data(
            config=config,
            model=model,
            unique_id_field=final_uid_field,
            file_csv=file_to_process,
            deferred_fields=final_deferred,
            context=parsed_context,
            fail_file=fail_output_file,
            encoding=encoding,
            separator=separator,
            ignore=ignore or [],
            max_connection=max_conn,
            batch_size=batch_size_run,
            batch_delay=batch_delay,
            skip=skip,
            force_create=force_create,
            o2m=o2m,
            split_by_cols=groupby,
            stream=stream,
            resume=resume,
            enable_checkpoint=not no_checkpoint,
            skip_unchanged=skip_unchanged,
            skip_existing=skip_existing,
            adaptive_throttle=adaptive_throttle,
            max_batch_bytes=max_batch_bytes,
            resolve_relations=resolve_relations,
            auto_clean=auto_clean,
        )
    finally:
        if (
            sorted_temp_file
            and sorted_temp_file is not True
            and os.path.exists(sorted_temp_file)
        ):
            os.remove(sorted_temp_file)

    elapsed = time.time() - start_time

    fail_file_was_created = _count_lines(fail_output_file) > 1
    is_truly_successful = success and not fail_file_was_created

    id_map = cast(dict[str, int], stats.get("id_map", {}))

    if not success:
        # Critical failure - the import process itself failed
        _show_error_panel(
            "Import Failed",
            "The import process failed. Check logs for details.",
        )
        return None

    if id_map:
        if isinstance(config, str):
            cache.save_id_map(config, model, id_map)

    # --- Pass 2: Relational Strategies ---
    # Run whenever records were imported and relational strategies were planned —
    # including a *partially* successful import and a --fail retry. Each strategy
    # looks every row up in id_map and skips anything not imported, so this only
    # ever writes relations for records that actually exist. Previously this
    # required a fully-clean import AND non-fail mode, so a single failed row left
    # every *other* record's m2m/o2m fields unpopulated, and the recommended
    # "run, then retry the fail file" flow never populated relations at all (#8).
    # source_df is read from the original ``filename``; id_map scopes the writes to
    # the imported subset.
    source_df: Optional[pl.DataFrame] = None
    if id_map and import_plan.get("strategies"):
        try:
            source_df = pl.read_csv(
                filename,
                separator=separator,
                truncate_ragged_lines=True,
                infer_schema_length=0,  # Read all columns as strings
            )
        except Exception as e:
            # Never crash Pass 2 (Pass 1 already committed): if the source can't be
            # re-read (e.g. an empty or missing original file on a --fail retry),
            # log and skip relational population rather than aborting.
            log.warning(
                f"Could not read '{filename}' for relational Pass 2; "
                f"skipping relational fields. Error: {e}"
            )

    if source_df is not None and import_plan.get("strategies"):
        with suppress_console_handler(), Progress() as progress:
            task_id = progress.add_task(
                "Pass 2/2: Relational fields",
                total=len(import_plan["strategies"]),
            )
            for field, strategy_info in import_plan["strategies"].items():
                if strategy_info["strategy"] == "direct_relational_import":
                    import_details = relational_import.run_direct_relational_import(
                        config,
                        model,
                        field,
                        strategy_info,
                        source_df,
                        id_map,
                        max_conn,
                        batch_size_run,
                        progress,
                        task_id,
                        filename,
                    )
                    if import_details:
                        import_threaded.import_data(
                            config=config,
                            model=import_details["model"],
                            unique_id_field=import_details["unique_id_field"],
                            file_csv=import_details["file_csv"],
                            max_connection=max_conn,
                            batch_size=batch_size_run,
                        )
                        Path(import_details["file_csv"]).unlink()
                elif strategy_info["strategy"] == "write_tuple":
                    result = relational_import.run_write_tuple_import(
                        config,
                        model,
                        field,
                        strategy_info,
                        source_df,
                        id_map,
                        max_conn,
                        batch_size_run,
                        progress,
                        task_id,
                        filename,
                        m2m_mode,
                    )
                    if not result:
                        log.warning(
                            f"Write tuple import failed for field '{field}'. "
                            "Check logs for details."
                        )
                elif strategy_info["strategy"] == "write_o2m_tuple":
                    result = relational_import.run_write_o2m_tuple_import(
                        config,
                        model,
                        field,
                        strategy_info,
                        source_df,
                        id_map,
                        max_conn,
                        batch_size_run,
                        progress,
                        task_id,
                        filename,
                    )
                    if not result:
                        log.warning(
                            f"Write O2M tuple import failed for field '{field}'. "
                            "Check logs for details."
                        )
                progress.update(task_id, advance=1)

    log.info(
        f"{stats.get('total_records', 0)} records processed. "
        f"Total time: {elapsed:.2f}s."
    )

    # Check for unaccounted records and warn the user
    unaccounted = stats.get("unaccounted_records", 0)
    if unaccounted > 0:
        Console(stderr=True).print(
            Panel(
                f"[yellow]Warning:[/yellow] {unaccounted} records were not accounted "
                f"for in the import results.\n"
                f"This may indicate records with duplicate IDs (expected) or "
                f"records dropped due to malformed data or transient errors.\n"
                f"Total: {stats.get('total_records', 0)}, "
                f"Created: {stats.get('created_records', 0)}, "
                f"Failed: {stats.get('failed_records', 0)}",
                title="[bold yellow]Record Reconciliation Warning[/bold yellow]",
                border_style="yellow",
            )
        )

    # On-the-record echo of which company the records landed in (#255). Set by the
    # company_context_check preflight for company-specific models; blank otherwise.
    company_ctx = import_plan.get("company_context") or {}
    company_suffix = f"\n{company_ctx['line']}" if company_ctx.get("line") else ""

    if is_truly_successful:
        if final_deferred:  # It was a two-pass import
            summary = (
                f"Records: {stats.get('total_records', 0)}, "
                f"Created: {stats.get('created_records', 0)}, "
                f"Updated: {stats.get('updated_relations', 0)}"
            )
            title = f"[bold green]Import Complete for [cyan]{model}[/cyan][/bold green]"
            Console().print(
                Panel(
                    summary + company_suffix,
                    title=title,
                    expand=False,
                )
            )
        else:  # Single pass
            Console().print(
                Panel(
                    f"Import for [cyan]{model}[/cyan] finished successfully."
                    + company_suffix,
                    title="[bold green]Import Complete[/bold green]",
                )
            )
    else:
        num_imported = len(id_map)
        num_failed = _count_lines(fail_output_file) - 1  # Subtract header
        Console().print(
            Panel(
                f"Partial import for [cyan]{model}[/cyan]: "
                f"[green]{num_imported}[/green] succeeded, "
                f"[red]{num_failed}[/red] failed. "
                f"See {fail_output_file} for failed records." + company_suffix,
                title="[bold yellow]Import Partially Complete[/bold yellow]",
            )
        )

    # Guardrail (#188): Odoo's load() does not auto-create default variants, so a
    # product.template import can silently leave templates unusable. Warn (or fix
    # with --fix-missing-variants) for the just-imported templates. Runs whenever
    # any records were imported (id_map), including partially-successful imports.
    if model == "product.template" and id_map:
        from .lib.actions.variant_manager import check_missing_variants_after_import

        check_missing_variants_after_import(
            config, model, id_map, fix=fix_missing_variants
        )

    return id_map


def run_import_for_migration(
    config: Union[str, dict[str, Any]],
    model: str,
    header: list[str],
    data: list[list[Any]],
    worker: int = 1,
    batch_size: int = 10,
    fail_file: Optional[str] = None,
) -> tuple[bool, dict[str, int]]:
    """Orchestrates the data import process from in-memory data.

    This function adapts in-memory data to the file-based import engine by
    writing the data to a temporary file. This allows it to leverage all the
    robust features of the main importer.

    Args:
        config (str): Path to the connection configuration file.
        model (str): The Odoo model to import data into.
        header (list[str]): A list of strings representing the column headers.
        data (list[list[Any]]): A list of lists representing the data rows.
        worker (int): The number of simultaneous connections to use.
        batch_size (int): The number of records to process in each batch.
        fail_file (Optional[str]): Path to write rows that fail to import, so a
            migration never silently drops data. When None, no fail file is written.

    Returns:
        tuple[bool, dict[str, int]]: ``(overall_success, stats)`` from the import
        engine, so the caller can detect (and report) partial failures.
    """
    log.info("Starting data import from in-memory data...")
    tmp_path = ""
    success: bool = False
    stats: dict[str, int] = {}
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+", delete=False, suffix=".csv", newline=""
        ) as tmp:
            writer = csv.writer(tmp)
            writer.writerow(header)
            writer.writerows(data)
            tmp_path = tmp.name
        log.info(f"In-memory data written to temporary file: {tmp_path}")
        success, stats = import_threaded.import_data(
            config=config,
            model=model,
            unique_id_field="id",  # Migration import assumes 'id'
            file_csv=tmp_path,
            fail_file=fail_file,
            # The temp file is written with csv.writer (comma-delimited); import_data
            # defaults to ';', so the separator must be set explicitly or the header
            # parses as a single column (#192).
            separator=",",
            context={
                "tracking_disable": True,
                "mail_create_nolog": True,
                "mail_notrack": True,
                "mail_activity_automation_skip": True,
            },
            max_connection=int(worker),
            batch_size=int(batch_size),
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    log.info("In-memory import process finished.")
    return success, stats
