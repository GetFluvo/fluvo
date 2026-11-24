"""``fluvo auditfile inspect`` — read-only overview of an audit file.

The free funnel artifact: point it at an audit file and it reports the format,
version, producing software, reporting period, currency, entity counts and a
balance check — with no Odoo connection and without staging anything. A prospect
can run it against their own export before any engagement, so the output is
meant to stand on its own.

The analysis is :func:`fluvo.auditfile.canonical.summarise` (pure, unit-tested);
this module is the thin renderer over it, to a console and to JSON.
"""

from __future__ import annotations

import dataclasses
import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import read_auditfile
from .canonical import LedgerSummary, summarise

#: Human labels for the per-frame counts, in display order.
_COUNT_LABELS: tuple[tuple[str, str], ...] = (
    ("accounts", "Ledger accounts"),
    ("partners", "Customers / suppliers"),
    ("journals", "Journals"),
    ("moves", "Transactions"),
    ("lines", "Transaction lines"),
    ("opening_lines", "Opening balances"),
    ("vat_codes", "VAT codes"),
)


def summary_to_dict(summary: LedgerSummary) -> dict[str, object]:
    """Render a summary as a JSON-ready dict, including derived fields.

    Args:
        summary: The summary to serialise.

    Returns:
        dict[str, object]: The summary's fields plus the derived ``balanced``
        flag, suitable for :func:`json.dumps`.
    """
    data = dataclasses.asdict(summary)
    data["balanced"] = summary.balanced
    return data


def render(summary: LedgerSummary, console: Console) -> None:
    """Render a summary to a console as a panel and a counts table.

    Args:
        summary: The summary to display.
        console: The rich console to write to.
    """
    fmt = (summary.source_format or "?").upper()
    version = summary.source_version or "?"
    software = summary.software or "unknown software"
    period = f"{summary.date_start or '?'} → {summary.date_end or '?'}"
    header = (
        f"[bold]{summary.company_name or 'Unknown company'}[/bold]"
        f"  (KvK {summary.company_id or '?'})\n"
        f"{fmt} {version}  ·  {software}\n"
        f"Fiscal year {summary.fiscal_year or '?'}  ·  {period}"
        f"  ·  {summary.currency or '?'}"
    )
    console.print(Panel(header, title="Audit file", expand=False))

    table = Table(show_header=True, header_style="bold")
    table.add_column("Entity")
    table.add_column("Count", justify="right")
    for key, label in _COUNT_LABELS:
        table.add_row(label, f"{summary.counts.get(key, 0):,}")
    console.print(table)

    delta = summary.total_debit - summary.total_credit
    if summary.balanced and summary.unbalanced_moves == 0:
        console.print(
            f"[green]✓ Balanced[/green]  "
            f"debit {summary.total_debit:,.2f} = credit {summary.total_credit:,.2f}"
        )
    else:
        parts = [
            f"debit {summary.total_debit:,.2f}",
            f"credit {summary.total_credit:,.2f}",
            f"delta {delta:,.2f}",
        ]
        if summary.unbalanced_moves:
            parts.append(f"{summary.unbalanced_moves:,} unbalanced transaction(s)")
        console.print("[yellow]⚠ Not balanced[/yellow]  " + "  ·  ".join(parts))


def run_inspect(
    path: str,
    *,
    fmt: str | None = None,
    as_json: bool = False,
    console: Console | None = None,
) -> LedgerSummary:
    """Inspect an audit file and report an overview.

    Propagates :class:`~fluvo.lib.core.InputError` from
    :func:`~fluvo.auditfile.read_auditfile` when the file cannot be recognised
    or read; the CLI maps that to an error panel and a non-zero exit.

    Args:
        path: The audit file to inspect.
        fmt: Force a specific format instead of auto-detecting.
        as_json: Emit the summary as JSON instead of the rich rendering.
        console: The console to render to; a default is created when omitted.

    Returns:
        LedgerSummary: The computed summary (also rendered as a side effect).
    """
    out = console or Console()
    ledger = read_auditfile(path, fmt=fmt)
    summary = summarise(ledger)
    if as_json:
        out.print_json(json.dumps(summary_to_dict(summary)))
    else:
        render(summary, out)
    return summary
