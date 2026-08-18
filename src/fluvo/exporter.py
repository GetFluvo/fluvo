"""This module contains the high-level logic for exporting data from Odoo."""

import ast
from typing import Any

import polars as pl
from rich.console import Console
from rich.panel import Panel

from . import export_threaded
from .lib import preflight
from .logging_config import log


class _ExportTranslationError(Exception):
    """A user-facing problem with a translation (``field@lang``) export request."""


def _show_error_panel(title: str, message: str) -> None:
    """Displays a formatted error panel to the console."""
    console = Console(stderr=True, style="bold red")
    console.print(Panel(message, title=title, border_style="red"))


def _show_success_panel(message: str) -> None:
    """Displays a formatted success panel to the console."""
    console = Console()
    console.print(
        Panel(
            message,
            title="[bold green]Export Complete[/bold green]",
            border_style="green",
        )
    )


def _plan_translation_export(  # noqa: C901
    config: str | dict[str, Any],
    model: str,
    fields_list: list[str],
    languages_str: str | None,
) -> dict[str, Any] | None:
    """Plan a multi-language export from the requested fields (#282).

    Detects ``field@lang`` tokens in ``fields_list`` and, when ``languages_str`` is
    given, auto-expands every translatable plain field into one ``field@lang``
    column per language. Validates that each base field is translatable, each
    language is installed, and a join key (``id``/``.id``) is present.

    Args:
        config: Connection config path or dict.
        model: The target Odoo model.
        fields_list: The requested export fields, in order.
        languages_str: Comma-separated language codes, or None.

    Returns:
        dict[str, Any] | None: The plan (``base_fields``, ``translations`` mapping
        lang -> base fields, ``key``, ``output_columns``), or None if no
        translations were requested.

    Raises:
        _ExportTranslationError: On an invalid field, uninstalled language, or a
            missing join key.
    """
    languages = (
        [lang.strip() for lang in languages_str.split(",") if lang.strip()]
        if languages_str
        else []
    )
    has_explicit = any("@" in f for f in fields_list)
    if not has_explicit and not languages:
        return None

    odoo_fields = preflight._get_odoo_fields(config, model)
    if not odoo_fields:
        raise _ExportTranslationError(
            "Could not fetch the model's fields from Odoo to validate the "
            "translation columns."
        )

    def _translatable(base: str) -> bool:
        return bool(odoo_fields.get(base, {}).get("translate"))

    base_fields = [f for f in fields_list if "@" not in f]
    if "id" in base_fields:
        key = "id"
    elif ".id" in base_fields:
        key = ".id"
    else:
        raise _ExportTranslationError(
            "Exporting translations needs an 'id' (or '.id') column in --fields to "
            "align the languages and keep the file re-importable. Add 'id' to "
            "--fields."
        )

    translations: dict[str, list[str]] = {}
    output_columns: list[str] = []
    for tok in fields_list:
        output_columns.append(tok)
        if "@" in tok:
            base = preflight._base_field_name(tok)
            lang = tok.split("@", 1)[1]
            if not lang:
                raise _ExportTranslationError(
                    f"Column '{tok}' is missing a language after '@' "
                    f"(expected e.g. '{base}@nl_NL')."
                )
            if not _translatable(base):
                raise _ExportTranslationError(
                    f"Column '{tok}': '{base}' is not a translatable field."
                )
            translations.setdefault(lang, []).append(base)
        elif languages and _translatable(tok):
            for lang in languages:
                autotok = f"{tok}@{lang}"
                if autotok not in fields_list:
                    output_columns.append(autotok)
                    translations.setdefault(lang, []).append(tok)

    if not translations:
        return None

    installed = preflight._get_installed_languages(config)
    if installed is not None:
        missing = sorted(lang for lang in translations if lang not in installed)
        if missing:
            raise _ExportTranslationError(
                f"These languages are used in the export but are not installed on "
                f"the database: {missing}. Install them first, then re-run."
            )

    return {
        "base_fields": base_fields,
        "translations": {lang: sorted(set(fs)) for lang, fs in translations.items()},
        "key": key,
        "output_columns": output_columns,
    }


def _run_translation_export(
    config: str | dict[str, Any],
    model: str,
    plan: dict[str, Any],
    domain: list[Any],
    output: str,
    context: dict[str, Any],
    worker: int,
    batch_size: int,
    separator: str,
    encoding: str,
    technical_names: bool,
    sanitize_newlines: str | None,
) -> tuple[bool, int]:
    """Execute a multi-language export: base pass + one pass per language (#282).

    The base (non-``@``) fields are exported once with the user domain. Each
    language's translated fields are then exported with ``context={'lang': ...}``
    but scoped to the *exact* database ids from the base pass — not by re-running
    the domain, which under a language context could select a different record set
    (a domain that filters a translated field matches different rows per language).
    The passes are joined on the database id (``.id``, always unique and
    language-neutral) and the merged frame is written to ``output``. The internal
    ``.id`` helper column is dropped unless the caller requested it.

    Args:
        config: Connection config path or dict.
        model: The target Odoo model.
        plan: The plan from :func:`_plan_translation_export`.
        domain: Parsed Odoo domain.
        output: Destination CSV path.
        context: Base export context (the language is merged onto a copy per pass).
        worker: Worker connection count.
        batch_size: Export batch size.
        separator: CSV delimiter.
        encoding: CSV encoding.
        technical_names: Force the raw read export mode.
        sanitize_newlines: Optional newline replacement.

    Returns:
        tuple[bool, int]: ``(success, record_count)``.

    Raises:
        _ExportTranslationError: If a language pass fails, so no partial file is
            written.
    """
    join_key = ".id"  # db id: always present, unique, and language-neutral

    def _export(
        header: list[str], ctx: dict[str, Any], dom: list[Any]
    ) -> pl.DataFrame | None:
        ok, _sid, _count, df = export_threaded.export_data(
            config=config,
            model=model,
            domain=dom,
            header=header,
            output=None,
            context=ctx,
            max_connection=worker,
            batch_size=batch_size,
            separator=separator,
            encoding=encoding,
            technical_names=technical_names,
            streaming=False,
            sanitize_newlines=sanitize_newlines,
        )
        return df if ok else None

    # Base pass: apply the user domain, and make sure the db id is fetched so the
    # language passes can be scoped to exactly this record set.
    base_header = list(plan["base_fields"])
    if join_key not in base_header:
        base_header.append(join_key)
    df_base = _export(base_header, context, domain)
    if df_base is None:
        return False, 0
    if not df_base.height:
        df_base.select(
            [c for c in plan["output_columns"] if c in df_base.columns]
        ).write_csv(output, separator=separator)
        return True, 0

    db_ids = [int(v) for v in df_base.get_column(join_key).to_list()]
    id_domain: list[Any] = [["id", "in", db_ids]]

    merged = df_base
    for lang in sorted(plan["translations"]):
        fields = plan["translations"][lang]
        df_lang = _export([join_key, *fields], {**context, "lang": lang}, id_domain)
        if df_lang is None:
            raise _ExportTranslationError(
                f"The '{lang}' translation pass failed; aborting so no partial file "
                f"is written."
            )
        rename = {f: f"{f}@{lang}" for f in fields}
        df_lang = df_lang.select([join_key, *fields]).rename(rename)
        merged = merged.join(df_lang, on=join_key, how="left")

    ordered = [c for c in plan["output_columns"] if c in merged.columns]
    merged = merged.select(ordered)
    merged.write_csv(output, separator=separator)
    return True, merged.height


def run_export(  # noqa: C901, D417
    config: str | dict[str, Any],
    model: str,
    fields: str,
    output: str | None,
    domain: str = "[]",
    worker: int = 1,
    batch_size: int = 1000,
    context: str | dict[str, Any] = "{}",
    separator: str = ";",
    encoding: str = "utf-8",
    technical_names: bool = False,
    streaming: bool = False,
    resume_session: str | None = None,
    sanitize_newlines: str | None = None,
    languages: str | None = None,
) -> None:
    """Orchestrates the data export process.

    Args:
        sanitize_newlines: If provided, replace embedded newlines in text fields
            with this string (e.g., " | "). Prevents CSV corruption.
        languages: If provided, comma-separated language codes to export
            translations for as ``field@lang`` columns (#282).
    """
    log.info(f"Starting export for model '{model}'...")

    try:
        parsed_domain = ast.literal_eval(domain)
    except (ValueError, SyntaxError):
        _show_error_panel(
            "Invalid Domain",
            f"The provided domain string is not a valid Python literal: {domain}",
        )
        return

    # Handle context as either string or dict
    if isinstance(context, dict):
        parsed_context = context
    else:
        try:
            parsed_context = ast.literal_eval(context)
            if not isinstance(parsed_context, dict):
                raise TypeError("Context must be a dictionary.")
        except Exception:
            _show_error_panel(
                "Invalid Context",
                f"The --context argument must be a valid Python dictionary string: "
                f"{context}",
            )
            return

    fields_list = fields.split(",")

    # --- Multi-language export (#282): field@lang columns / --languages ---
    try:
        translation_plan = _plan_translation_export(
            config, model, fields_list, languages
        )
    except _ExportTranslationError as exc:
        _show_error_panel("Invalid translation export", str(exc))
        raise SystemExit(1) from exc

    if translation_plan is not None:
        if streaming:
            _show_error_panel(
                "Unsupported combination",
                "--streaming cannot be combined with translation columns / "
                "--languages: the per-language columns are merged in memory before "
                "writing. Drop --streaming for a translated export.",
            )
            raise SystemExit(1)
        if resume_session:
            _show_error_panel(
                "Unsupported combination",
                "--resume-session cannot be combined with translation columns / "
                "--languages (a translated export runs several passes).",
            )
            raise SystemExit(1)
        if not output:
            _show_error_panel(
                "Missing output", "A translated export requires an --output path."
            )
            raise SystemExit(1)
        try:
            ok, count = _run_translation_export(
                config=config,
                model=model,
                plan=translation_plan,
                domain=parsed_domain,
                output=output,
                context=parsed_context,
                worker=int(worker),
                batch_size=int(batch_size),
                separator=separator,
                encoding=encoding,
                technical_names=technical_names,
                sanitize_newlines=sanitize_newlines,
            )
        except _ExportTranslationError as exc:
            _show_error_panel("Translation export failed", str(exc))
            raise SystemExit(1) from exc
        if not ok:
            _show_error_panel(
                "Export Failed",
                "The export process failed. Please check the logs for details.",
            )
            raise SystemExit(1)
        langs = ", ".join(sorted(translation_plan["translations"]))
        _show_success_panel(
            f"Exported {count} records to [bold cyan]{output}[/bold cyan] "
            f"with translations for [bold]{langs}[/bold].\n"
            "[green]Record count verified.[/green]"
        )
        return

    success, session_id, record_count, _ = export_threaded.export_data(
        config=config,
        model=model,
        domain=parsed_domain,
        header=fields_list,
        context=parsed_context,
        output=output,
        max_connection=int(worker),
        batch_size=int(batch_size),
        encoding=encoding,
        separator=separator,
        technical_names=technical_names,
        streaming=streaming,
        resume_session=resume_session,
        sanitize_newlines=sanitize_newlines,
    )

    if success:
        base_message = (
            f"Successfully streamed {record_count} records to "
            f"[bold cyan]{output}[/bold cyan]."
            if streaming
            else f"Successfully exported {record_count} records to "
            f"[bold cyan]{output}[/bold cyan]."
        )

        # --- Record Count Validation ---
        if output:
            try:
                actual_count = len(pl.read_csv(output, separator=separator))
                if actual_count == record_count:
                    final_message = (
                        f"{base_message}\n[green]Record count verified.[/green]"
                    )
                    _show_success_panel(final_message)
                else:
                    warning_message = (
                        f"{base_message}\n\n"
                        "[bold yellow]Warning:[/bold yellow] "
                        "Record count mismatch detected.\n"
                        f" - Expected: {record_count} records\n"
                        f" - Found:    {actual_count} records in the output file."
                    )
                    _show_error_panel("Count Validation Warning", warning_message)
            except Exception as e:
                log.warning(f"Could not validate record count in {output}: {e}")
                _show_success_panel(
                    base_message
                )  # Show original message if validation fails
        else:
            _show_success_panel(base_message)
    else:
        error_message = "The export process failed. Please check the logs for details."
        if session_id:
            error_message += (
                f"\n\nThis export was running under session ID: "
                f"[bold]{session_id}[/bold]"
                "\nTo resume this job, add the following flag to your command:"
                f"\n[bold cyan]--resume-session {session_id}[/bold cyan]"
            )
        _show_error_panel(
            "Export Failed",
            error_message,
        )
        # A failed export must not report success to automation (#253): the panel
        # above is rendered, and we exit non-zero. This covers the fail-fast abort
        # on non-existent fields, which would otherwise return quietly.
        raise SystemExit(1)


def run_export_for_migration(
    config: str | dict[str, Any],
    model: str,
    fields: list[str],
    domain: str = "[]",
    worker: int = 1,
    batch_size: int = 10,
    context: str = "{'tracking_disable' : True}",
    encoding: str = "utf-8",
    technical_names: bool = False,
) -> tuple[list[str] | None, list[list[Any]] | None]:
    """Migration exporter.

    Orchestrates the data export process, returning the data in memory.
    This function is designed to be called by the migration tool.
    """
    log.info(f"Starting in-memory export from model '{model}' for migration...")

    try:
        parsed_domain = ast.literal_eval(domain)
    except Exception:
        log.warning(
            "Invalid domain string for migration export,"
            "defaulting to empty domain '[]'."
        )
        parsed_domain = []

    try:
        parsed_context = ast.literal_eval(context)
    except Exception:
        parsed_context = {}

    success, _, _, result_df = export_threaded.export_data(
        config=config,
        model=model,
        domain=parsed_domain,
        header=fields,
        context=parsed_context,
        output=None,  # This signals the function to return data
        max_connection=int(worker),
        batch_size=int(batch_size),
        encoding=encoding,
        separator=";",
        technical_names=technical_names,
    )

    if not success or result_df is None:
        return fields, None

    header = result_df.columns
    # Corrected: Use a list comprehension to convert tuples to lists.
    data = [list(row) for row in result_df.iter_rows()]
    return header, data
