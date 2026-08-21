"""Source-side migration assessment — the free ``fluvo assess`` wedge (PLAN 2.2).

Connect to an Odoo, and for each requested model report an entity inventory: row
volume, a field-type breakdown, and the risk flags that drive migration effort
(company-dependent fields, translated fields, relational density, required
fields, non-importable computed fields, binaries). The output is meant to be
handed to a prospect in place of a paid discovery call, so it renders both to the
console and to a machine-readable JSON / Markdown artifact.

The analysis engine (:func:`assess_model`) is pure: it takes ``fields_get``
metadata plus a row count and returns a structured summary, so it is fully
unit-testable without a live Odoo. :func:`run_assess` is the thin connector.
"""

import json
from typing import Any

from rich.console import Console
from rich.table import Table

from ..logging_config import log
from . import conf_lib
from .internal.ui import _show_error_panel

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
        table.add_row(
            a["model"],
            _fmt_count(a["row_count"]),
            str(a["field_total"]),
            _risk_summary(a["risks"]),
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
        lines.append(
            f"| `{a['model']}` | {_fmt_count(a['row_count'])} | "
            f"{a['field_total']} | {_risk_summary(a['risks'])} |"
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


def run_assess(
    config: str | dict[str, Any],
    models: list[str] | None = None,
    output: str | None = None,
    fmt: str = "table",
) -> list[dict[str, Any]]:
    """Assess the requested models on the source Odoo and emit a report (PLAN 2.2).

    Args:
        config: Connection config path or dict.
        models: Models to assess; when None, a curated default set that exists on
            the database is discovered.
        output: If given, also write the report (JSON or Markdown per ``fmt``,
            defaulting to JSON) to this path.
        fmt: Console/report format: ``table`` (default), ``json`` or ``markdown``.

    Returns:
        list[dict[str, Any]]: The per-model assessments (also for programmatic use).

    Raises:
        SystemExit: with code 1 if the connection fails or no models can be
            assessed.
    """
    try:
        conn = _connect(config)
    except Exception as e:
        _show_error_panel(
            "Odoo Connection Error",
            f"Could not connect to the source Odoo to assess it.\nError: {e}",
        )
        raise SystemExit(1) from e

    target_models = models or _discover_models(conn)
    if not target_models:
        _show_error_panel(
            "Nothing to assess",
            "No models to assess. Pass --models model.a,model.b, or check that the "
            "common models exist on this database.",
        )
        raise SystemExit(1)

    assessments: list[dict[str, Any]] = []
    for model in target_models:
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
        assessments.append(assess_model(model, odoo_fields, row_count))

    if not assessments:
        _show_error_panel(
            "Nothing to assess",
            "None of the requested models could be read on this database.",
        )
        raise SystemExit(1)

    # Console output.
    if fmt == "json":
        Console().print_json(to_json(assessments))
    elif fmt == "markdown":
        Console().print(to_markdown(assessments))
    else:
        render_table(assessments)

    if output:
        document = (
            to_markdown(assessments) if fmt == "markdown" else to_json(assessments)
        )
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(document)
        log.info(f"Assessment written to '{output}'.")

    return assessments
