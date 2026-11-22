"""Source-side migration assessment — the free ``fluvo assess`` wedge (PLAN 2.2).

Connect to an Odoo, and for each requested model report an entity inventory: row
volume, a field-type breakdown, and the risk flags that drive migration effort
(company-dependent fields, translated fields, relational density, required
fields, non-importable computed fields, binaries). The output is meant to be
handed to a prospect in place of a paid discovery call, so it renders both to the
console and to a machine-readable JSON / Markdown artifact.

With ``--profile`` (issue #349) it additionally reads a bounded sample of rows
and runs :mod:`fluvo.lib.profiling` over the values, which catches the
data-quality problems metadata cannot see: two unit populations sharing one
numeric column, date-format drift, dirty enumerations, and a value field whose
qualifier (unit of measure, currency, language) is left empty. The sample is
capped and the cap is reported, because a fill rate from a sample is an
estimate and saying so is the whole point of an assessment.

The analysis engine (:func:`assess_model`) is pure: it takes ``fields_get``
metadata plus a row count and returns a structured summary, so it is fully
unit-testable without a live Odoo. :func:`run_assess` is the thin connector.
"""

import json
from typing import Any

import polars as pl
from rich.console import Console
from rich.table import Table

from ..logging_config import log
from . import conf_lib
from .core import ConnectError, NothingToDoError, NullReporter, RunReporter
from .mangling import ANGLO, Locale
from .profiling import looks_like_qualifier, profile_frame

# A curated default set of common migration models, used when --models is omitted.
DEFAULT_MODELS = (
    "res.partner",
    "res.users",
    "product.template",
    "product.product",
    "account.move",
    "account.move.line",
    "sale.order",
    "sale.order.line",
    "purchase.order",
    "stock.picking",
    "crm.lead",
)

#: Default ceiling on rows read per model for ``--profile``. Profiling reads
#: values, so it is the one part of ``assess`` that is not cheap on a large
#: database; the cap keeps a free assessment from hammering a prospect's server.
DEFAULT_PROFILE_LIMIT = 10_000

#: Odoo field types profiled as numbers.
_NUMERIC_TYPES = frozenset({"integer", "float", "monetary"})

#: Odoo field types profiled as dates.
_DATE_TYPES = frozenset({"date", "datetime"})

#: Odoo field types profiled as enumerations.
_ENUM_TYPES = frozenset({"selection", "char"})


def assess_model(
    model: str, odoo_fields: dict[str, Any], row_count: int | None
) -> dict[str, Any]:
    """Summarise one model's ``fields_get`` metadata into a readiness report.

    Args:
        model: The model's technical name.
        odoo_fields: The model's ``fields_get()`` metadata (field name -> attrs).
        row_count: Number of records, or None if it could not be counted.

    Returns:
        dict[str, Any]: The per-model assessment (``model``, ``row_count``,
        ``field_total``, ``by_type`` and ``risks``).
    """
    by_type: dict[str, int] = {}
    company_dependent: list[str] = []
    translated: list[str] = []
    required: list[str] = []
    readonly_computed: list[str] = []
    binary: list[str] = []
    relational = {"many2one": 0, "many2many": 0, "one2many": 0}

    for name, info in odoo_fields.items():
        ftype = info.get("type", "unknown")
        by_type[ftype] = by_type.get(ftype, 0) + 1
        if info.get("company_dependent"):
            company_dependent.append(name)
        if info.get("translate"):
            translated.append(name)
        if info.get("required"):
            required.append(name)
        # Readonly and not stored -> computed and non-writable: an import can't set
        # it, so it is a mapping dead-end worth surfacing.
        if info.get("readonly") and not info.get("store", True):
            readonly_computed.append(name)
        if ftype == "binary":
            binary.append(name)
        if ftype in relational:
            relational[ftype] += 1

    return {
        "model": model,
        "row_count": row_count,
        "field_total": len(odoo_fields),
        "by_type": dict(sorted(by_type.items())),
        "risks": {
            "company_dependent": sorted(company_dependent),
            "translated": sorted(translated),
            "required": sorted(required),
            "readonly_computed": sorted(readonly_computed),
            "binary": sorted(binary),
            "relational": relational,
        },
    }


def select_profile_fields(
    odoo_fields: dict[str, Any],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Choose which of a model's fields are worth reading values for.

    Binaries, text/html blobs and one2many/many2many fields are excluded: they
    are expensive to transfer and there is nothing a value profile can say
    about them. Relational many2one fields are read only when their name reads
    as a *qualifier* (unit of measure, currency, language), because that is the
    cross-field check in :func:`fluvo.lib.profiling.profile_qualifier_pairs`.

    Args:
        odoo_fields: The model's ``fields_get()`` metadata.

    Returns:
        tuple[list[str], list[str], list[str], list[str]]: Numeric, date,
        enumeration and qualifier field names, each sorted.
    """
    numeric: list[str] = []
    dates: list[str] = []
    enums: list[str] = []
    qualifiers: list[str] = []
    for name, info in odoo_fields.items():
        ftype = info.get("type", "unknown")
        if ftype in _NUMERIC_TYPES:
            numeric.append(name)
        elif ftype in _DATE_TYPES:
            dates.append(name)
        elif ftype in _ENUM_TYPES:
            enums.append(name)
        if ftype == "many2one" and looks_like_qualifier(name):
            qualifiers.append(name)
    return sorted(numeric), sorted(dates), sorted(enums), sorted(qualifiers)


def _scalar(value: Any) -> str | None:
    """Flatten one Odoo RPC value to a string, or None when it is unset.

    Odoo returns ``False`` for any unset scalar and ``[id, display_name]`` for
    a set many2one, so a raw ``search_read`` result is not a rectangular table
    of scalars until this has run over it.

    Args:
        value: A single value from a ``search_read`` row.

    Returns:
        str | None: The value as a string, or None when unset.
    """
    if value is None or value is False:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        # many2one comes back as [id, display_name]; the name is what a human
        # reads in a report, and it is what makes a qualifier legible.
        return str(value[-1])
    return str(value)


def rows_to_frame(rows: list[dict[str, Any]], columns: list[str]) -> pl.DataFrame:
    """Build a string-typed Polars frame from ``search_read`` output.

    Everything becomes ``pl.String``, including numbers: the profiling engine
    parses numerics itself under an explicit locale, and a uniform dtype avoids
    the mixed ``False`` / number / list columns Odoo returns.

    Args:
        rows: Rows from ``search_read``.
        columns: Field names to include, in order.

    Returns:
        pl.DataFrame: One column per requested field, all ``pl.String``.
    """
    data = {column: [_scalar(row.get(column)) for row in rows] for column in columns}
    return pl.DataFrame(data, schema={column: pl.String for column in columns})


def _profile_model(
    conn: Any,
    model: str,
    odoo_fields: dict[str, Any],
    row_count: int | None,
    limit: int,
    group_by: str | None,
    locale: Locale,
) -> dict[str, Any] | None:
    """Read a bounded sample of one model and profile its values.

    Args:
        conn: An open Odoo connection.
        model: The model's technical name.
        odoo_fields: The model's ``fields_get()`` metadata.
        row_count: Total records, or None when it could not be counted.
        limit: Maximum rows to read.
        group_by: Field to repeat the numeric population test within, when the
            model has it.
        locale: Decimal/thousands convention for numeric parsing.

    Returns:
        dict[str, Any] | None: The profile, with ``sampled``, ``total`` and
        ``sampled_all`` recording what it was computed from; or None when the
        model has no profilable field or could not be read.
    """
    numeric, dates, enums, qualifiers = select_profile_fields(odoo_fields)
    columns = sorted(set(numeric + dates + enums + qualifiers))
    if not columns:
        log.debug(f"Nothing to profile on '{model}': no profilable fields.")
        return None

    group = group_by if group_by and group_by in odoo_fields else None
    if group and group not in columns:
        columns.append(group)

    try:
        rows = conn.get_model(model).search_read([], columns, limit=limit)
    except Exception as e:
        log.warning(f"Could not read values for '{model}' to profile it ({e}).")
        return None
    if not rows:
        return None

    frame = rows_to_frame(rows, columns)
    profile = profile_frame(
        frame,
        numeric=numeric,
        dates=dates,
        enums=enums,
        locale=locale,
        group_by=group,
    )
    profile["sampled"] = frame.height
    profile["total"] = row_count
    profile["sampled_all"] = row_count is not None and frame.height >= row_count
    profile["group_by"] = group
    return profile


def _risk_summary(risks: dict[str, Any]) -> str:
    """Render a one-line risk summary for a model, or 'none' if there are none."""
    rel = risks["relational"]
    rel_total = rel["many2one"] + rel["many2many"] + rel["one2many"]
    parts = []
    if risks["company_dependent"]:
        parts.append(f"{len(risks['company_dependent'])} company-dependent")
    if risks["translated"]:
        parts.append(f"{len(risks['translated'])} translated")
    if rel_total:
        parts.append(
            f"{rel_total} relational "
            f"(m2o {rel['many2one']}/m2m {rel['many2many']}/o2m {rel['one2many']})"
        )
    if risks["required"]:
        parts.append(f"{len(risks['required'])} required")
    if risks["readonly_computed"]:
        parts.append(f"{len(risks['readonly_computed'])} computed (not importable)")
    if risks["binary"]:
        parts.append(f"{len(risks['binary'])} binary")
    return ", ".join(parts) if parts else "none"


def _fmt_count(row_count: int | None) -> str:
    """Format a row count for display (``?`` when it could not be read)."""
    return f"{row_count:,}" if row_count is not None else "?"


def render_table(assessments: list[dict[str, Any]]) -> None:
    """Print the assessment as a rich table to the console.

    Args:
        assessments: Per-model assessments from :func:`assess_model`.
    """
    table = Table(title="Fluvo migration assessment", expand=False)
    table.add_column("Model", style="cyan", no_wrap=True)
    table.add_column("Rows", justify="right")
    table.add_column("Fields", justify="right")
    table.add_column("Risk flags")
    for a in assessments:
        risk = _risk_summary(a["risks"])
        audit = _company_audit_str(a)
        if audit:
            risk = f"{risk}\n{audit}" if risk != "none" else audit
        table.add_row(
            a["model"],
            _fmt_count(a["row_count"]),
            str(a["field_total"]),
            risk,
        )
    Console().print(table)


def to_json(assessments: list[dict[str, Any]]) -> str:
    """Serialise the assessments as pretty JSON.

    Args:
        assessments: Per-model assessments.

    Returns:
        str: The JSON document.
    """
    return json.dumps({"assessment": assessments}, indent=2, sort_keys=True)


def to_markdown(assessments: list[dict[str, Any]]) -> str:
    """Render the assessments as a Markdown report (a prospect handout).

    Args:
        assessments: Per-model assessments.

    Returns:
        str: The Markdown document.
    """
    lines = ["# Fluvo migration assessment", ""]
    lines.append("| Model | Rows | Fields | Risk flags |")
    lines.append("| --- | ---: | ---: | --- |")
    for a in assessments:
        risk = _risk_summary(a["risks"])
        audit = _company_audit_str(a)
        if audit:
            risk = f"{risk}; {audit}" if risk != "none" else audit
        lines.append(
            f"| `{a['model']}` | {_fmt_count(a['row_count'])} | "
            f"{a['field_total']} | {risk} |"
        )
    lines.append("")
    profile_section = profile_markdown(assessments)
    if profile_section:
        lines.append(profile_section)
    return "\n".join(lines)


def _fill_pct(fill: dict[str, Any]) -> str:
    """Format a fill rate as a percentage string."""
    return f"{fill['fill_rate'] * 100:.0f}%"


def _decades_str(decades: dict[str, Any]) -> str:
    """Render a decade histogram as ``1eN:share`` pairs, most populous first."""
    total = sum(int(v) for v in decades.values()) or 1
    parts = [
        f"1e{d}:{100 * int(n) / total:.0f}%"
        for d, n in sorted(decades.items(), key=lambda kv: int(kv[0]))
    ]
    return " ".join(parts)


def profile_findings(assessments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten every model's profile into one list of flagged findings.

    Only flagged columns are returned. A report that lists every clean column
    buries the finding, and the per-model counts in the summary line already
    say how much was looked at.

    Args:
        assessments: Per-model assessments, some carrying a ``profile`` key.

    Returns:
        list[dict[str, Any]]: One entry per flagged column or qualifier pair,
        with ``model``, ``column``, ``kind``, ``fill``, ``flags`` and ``detail``.
    """
    findings: list[dict[str, Any]] = []
    for assessment in assessments:
        profile = assessment.get("profile")
        if not profile:
            continue
        model = assessment["model"]
        for report in profile["columns"]:
            if not report["flags"]:
                continue
            findings.append(
                {
                    "model": model,
                    "column": report["column"],
                    "kind": report["kind"],
                    "fill": _fill_pct(report["fill"]),
                    "flags": list(report["flags"]),
                    "detail": _finding_detail(report),
                }
            )
        for pair in profile["qualifiers"]:
            findings.append(
                {
                    "model": model,
                    "column": pair["value_column"],
                    "kind": "qualifier",
                    "fill": f"{pair['value_fill_rate'] * 100:.0f}%",
                    "flags": list(pair["flags"]),
                    "detail": (
                        f"`{pair['qualifier_column']}` is only "
                        f"{pair['qualifier_fill_rate'] * 100:.0f}% filled"
                    ),
                }
            )
    return findings


def _finding_detail(report: dict[str, Any]) -> str:
    """Build the one-line evidence string for a flagged column.

    Args:
        report: A per-column profile from :mod:`fluvo.lib.profiling`.

    Returns:
        str: Human-readable evidence for why the column was flagged.
    """
    kind = report["kind"]
    if kind == "numeric":
        bits = [f"decades {_decades_str(report['decades'])}"]
        whole, decimal = report["whole"], report["decimal"]
        if "whole-vs-decimal" in report["flags"]:
            bits.append(
                f"whole n={whole['count']} at 1e{whole['median_decade']} vs "
                f"decimal n={decimal['count']} at 1e{decimal['median_decade']}"
            )
        groups = report.get("groups") or []
        if groups:
            bits.append(f"{len(groups)} group(s) also split")
        return "; ".join(bits)
    if kind == "date":
        formats = ", ".join(f"{k}={v}" for k, v in report["formats"].items())
        detail = f"formats {formats}"
        if report["ambiguous"]:
            detail += f"; {report['ambiguous']} day/month-ambiguous"
        return detail
    if kind == "enumeration":
        bits = [f"{report['distinct']} distinct"]
        if report["collisions"]:
            shown = "; ".join(" / ".join(g) for g in report["collisions"][:3])
            bits.append(f"collisions: {shown}")
        if report["tail"]:
            bits.append(f"{len(report['tail'])} tail value(s)")
        return "; ".join(bits)
    return ""


def render_profile_table(assessments: list[dict[str, Any]]) -> None:
    """Print the value-profiling findings as a second rich table.

    Args:
        assessments: Per-model assessments, some carrying a ``profile`` key.
    """
    console = Console()
    findings = profile_findings(assessments)
    profiled = [a for a in assessments if a.get("profile")]
    if not profiled:
        return

    for assessment in profiled:
        profile = assessment["profile"]
        scope = (
            "all rows"
            if profile["sampled_all"]
            else f"{profile['sampled']:,} of {_fmt_count(profile['total'])} rows"
        )
        console.print(
            f"[dim]{assessment['model']}: profiled "
            f"{len(profile['columns'])} column(s) over {scope}[/dim]"
        )

    if not findings:
        console.print("[green]Value profiling found nothing to flag.[/green]")
        return

    table = Table(title="Value profiling findings", expand=False)
    table.add_column("Model", style="cyan", no_wrap=True)
    table.add_column("Column", style="yellow", no_wrap=True)
    table.add_column("Kind")
    table.add_column("Fill", justify="right")
    table.add_column("Flags", style="red")
    table.add_column("Evidence")
    for finding in findings:
        table.add_row(
            finding["model"],
            finding["column"],
            finding["kind"],
            finding["fill"],
            ", ".join(finding["flags"]),
            finding["detail"],
        )
    console.print(table)


def profile_markdown(assessments: list[dict[str, Any]]) -> str:
    """Render the value-profiling findings as a Markdown section.

    Args:
        assessments: Per-model assessments, some carrying a ``profile`` key.

    Returns:
        str: The Markdown section, or an empty string when nothing was profiled.
    """
    profiled = [a for a in assessments if a.get("profile")]
    if not profiled:
        return ""

    lines = ["## Value profiling", ""]
    for assessment in profiled:
        profile = assessment["profile"]
        scope = (
            "all rows"
            if profile["sampled_all"]
            else f"a sample of {profile['sampled']:,} of "
            f"{_fmt_count(profile['total'])} rows"
        )
        lines.append(
            f"- `{assessment['model']}`: {len(profile['columns'])} column(s) "
            f"profiled over {scope}."
        )
    lines.append("")

    findings = profile_findings(assessments)
    if not findings:
        lines.append("Nothing was flagged.")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        "Fill rates computed from a sample are estimates. A flag is a question "
        "to ask, not a defect: it says the column holds more than one kind of "
        "value, and someone has to say which kind is correct."
    )
    lines.append("")
    lines.append("| Model | Column | Kind | Fill | Flags | Evidence |")
    lines.append("| --- | --- | --- | ---: | --- | --- |")
    for finding in findings:
        lines.append(
            f"| `{finding['model']}` | `{finding['column']}` | {finding['kind']} "
            f"| {finding['fill']} | {', '.join(finding['flags'])} "
            f"| {finding['detail']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _connect(config: str | dict[str, Any]) -> Any:
    """Open an Odoo connection from a config path or dict."""
    if isinstance(config, dict):
        return conf_lib.get_connection_from_dict(config)
    return conf_lib.get_connection_from_config(config_file=config)


def _discover_models(conn: Any) -> list[str]:
    """Discover a sensible default set of models to assess (those that exist)."""
    try:
        rows = conn.get_model("ir.model").search_read(
            [("model", "in", list(DEFAULT_MODELS)), ("transient", "=", False)],
            ["model"],
        )
        present = {r["model"] for r in rows}
        return [m for m in DEFAULT_MODELS if m in present]
    except Exception as e:  # pragma: no cover - best-effort discovery
        log.warning(f"Could not discover models: {e}")
        return []


def _company_distribution(conn: Any, model: str) -> list[dict[str, Any]] | None:
    """Count a company-aware model's records grouped by ``company_id``.

    Surfaces the *distribution* of existing records across companies — the cheapest
    way to spot the "everything landed under one company" migration error (#291):
    a company with a suspicious 0, or all records under a single company on a
    multi-company database, stands out at a glance.

    Args:
        conn: An open Odoo connection.
        model: A model that has a ``company_id`` field.

    Returns:
        list[dict[str, Any]] | None: One entry per company (``company_id``,
        ``company_name``, ``count``; a ``None`` id means company-less/shared
        records), or None if the grouping could not be read.
    """
    try:
        groups = conn.get_model(model).read_group([], ["company_id"], ["company_id"])
    except Exception as e:  # pragma: no cover - environment-dependent
        log.debug(f"Could not read company distribution for '{model}': {e}")
        return None
    dist: list[dict[str, Any]] = []
    for g in groups:
        ref = g.get("company_id")
        count = int(g.get("company_id_count", 0))
        if isinstance(ref, (list, tuple)) and ref:
            dist.append(
                {
                    "company_id": int(ref[0]),
                    "company_name": str(ref[1]) if len(ref) > 1 else None,
                    "count": count,
                }
            )
        else:
            dist.append(
                {"company_id": None, "company_name": "(no company)", "count": count}
            )
    return dist


def _company_audit_str(assessment: dict[str, Any]) -> str:
    """Render a company-distribution audit line, or '' if not company-aware."""
    dist = assessment.get("company_distribution")
    if not dist:
        return ""
    parts = [
        f"{d['company_id'] if d['company_id'] is not None else '-'}:{d['count']}"
        for d in dist
    ]
    return "by company: " + ", ".join(parts)


def run_assess(  # noqa: C901
    config: str | dict[str, Any],
    models: list[str] | None = None,
    output: str | None = None,
    fmt: str = "table",
    profile: bool = False,
    group_by: str | None = None,
    profile_limit: int = DEFAULT_PROFILE_LIMIT,
    locale: Locale = ANGLO,
    reporter: RunReporter | None = None,
) -> list[dict[str, Any]]:
    """Assess the requested models on the source Odoo and emit a report (PLAN 2.2).

    Args:
        config: Connection config path or dict.
        models: Models to assess; when None, a curated default set that exists on
            the database is discovered.
        output: If given, also write the report (JSON or Markdown per ``fmt``,
            defaulting to JSON) to this path.
        fmt: Console/report format: ``table`` (default), ``json`` or ``markdown``.
        profile: Also read a bounded sample of rows and profile the values
            (issue #349). Off by default: it is the only part of ``assess``
            that transfers data rather than metadata.
        group_by: Field to repeat the numeric population test within, where the
            model has it — a unit mix invisible across a whole table is often
            obvious inside one product category.
        profile_limit: Maximum rows read per model when profiling.
        locale: Decimal/thousands convention used to parse numeric values.
            Never inferred, because an inverted guess changes every finding.
        reporter: Optional progress sink (issue #363). Receives phase and
            per-model progress; defaults to a no-op, so the console-free core
            never has to check for None.

    Returns:
        list[dict[str, Any]]: The per-model assessments (also for programmatic use).

    Raises:
        ConnectError: If the source Odoo cannot be reached or authenticated.
        NothingToDoError: If no models are selected, or none can be read.
    """
    reporter = reporter or NullReporter()
    reporter.on_phase("connecting")
    try:
        conn = _connect(config)
    except Exception as e:
        raise ConnectError(
            f"Could not connect to the source Odoo to assess it.\nError: {e}"
        ) from e

    reporter.on_phase("discovering models")
    target_models = models or _discover_models(conn)
    if not target_models:
        raise NothingToDoError(
            "No models to assess. Pass --models model.a,model.b, or check that the "
            "common models exist on this database."
        )

    reporter.on_phase("assessing")
    assessments: list[dict[str, Any]] = []
    total = len(target_models)
    for index, model in enumerate(target_models, start=1):
        reporter.on_progress(index, total)
        try:
            odoo_fields = conn.get_model(model).fields_get()
        except Exception as e:
            log.warning(f"Skipping '{model}': could not read its fields ({e}).")
            continue
        try:
            row_count: int | None = int(conn.get_model(model).search_count([]))
        except Exception as e:  # counting can fail on access rules; report unknown
            log.debug(f"Could not count '{model}': {e}")
            row_count = None
        assessment = assess_model(model, odoo_fields, row_count)
        # Company audit: for company-aware models, record how existing records are
        # spread across companies, so a wrong-company migration shows up (#291).
        if "company_id" in odoo_fields:
            dist = _company_distribution(conn, model)
            if dist is not None:
                assessment["company_distribution"] = dist
        if profile:
            model_profile = _profile_model(
                conn,
                model,
                odoo_fields,
                row_count,
                profile_limit,
                group_by,
                locale,
            )
            if model_profile is not None:
                assessment["profile"] = model_profile
        assessments.append(assessment)

    if not assessments:
        raise NothingToDoError(
            "None of the requested models could be read on this database."
        )

    # Console output.
    if fmt == "json":
        Console().print_json(to_json(assessments))
    elif fmt == "markdown":
        Console().print(to_markdown(assessments))
    else:
        render_table(assessments)
        if profile:
            render_profile_table(assessments)

    if output:
        document = (
            to_markdown(assessments) if fmt == "markdown" else to_json(assessments)
        )
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(document)
        log.info(f"Assessment written to '{output}'.")

    return assessments
