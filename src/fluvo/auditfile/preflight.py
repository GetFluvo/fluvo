"""Canonical-level preflight checks for an audit-file import.

Before anything is staged for Odoo, these checks run over the canonical ledger
and the transform's decisions and emit a structured report: a list of findings,
each with a severity, a machine code, a human message and a bounded sample of
offending keys. The report serialises to JSON (for a UI or a log) and renders a
short human summary (for the terminal).

The checks are deliberately conservative and name what they find rather than
guess a fix:

* **unbalanced_move** (error) — a move whose debits and credits differ beyond
  tolerance; Odoo will reject it.
* **unknown_account** (error) — a line or opening balance references an account
  id that is not in the chart; its external id would not resolve.
* **unknown_journal** (error) — a move references a journal not in the file.
* **unknown_partner** (warning) — a line references a customer/supplier id not
  in the party list; the partner link is left empty.
* **non_eur_currency** (warning) — the file is not in EUR, so amounts need an FX
  review before they mean anything in a EUR company.
* **proposed_account_type** (warning) — accounts whose Odoo type came from the
  numbering heuristic or the fallback, not an override or RGS; review
  ``account_map.csv`` before loading.
* **open_item_tie_out** (warning) — in cutover, the per-partner open items on a
  control account do not sum to that account's opening balance.

Severity drives staging: by default any finding blocks (fail-fast); with the
caller's ``allow_warnings`` opt-in, only errors block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

import polars as pl

from .canonical import BALANCE_TOLERANCE, CanonicalLedger, unbalanced_moves
from .transform import TransformResult

ERROR: Final = "error"
WARNING: Final = "warning"

#: How many offending keys to keep per finding (the rest are counted only).
_SAMPLE = 10


@dataclass(frozen=True)
class Finding:
    """One preflight finding.

    Attributes:
        code: A stable machine code, e.g. ``"unknown_account"``.
        severity: :data:`ERROR` or :data:`WARNING`.
        message: A human-readable one-line summary.
        count: How many items the finding covers.
        sample: Up to :data:`_SAMPLE` offending keys, for the report.
    """

    code: str
    severity: str
    message: str
    count: int
    sample: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Render the finding as a JSON-ready dict.

        Returns:
            dict[str, Any]: The finding's fields.
        """
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "count": self.count,
            "sample": list(self.sample),
        }


@dataclass
class PreflightReport:
    """The outcome of the preflight checks.

    Attributes:
        mode: The transform mode the stage was built for.
        findings: Every finding, most severe first.
    """

    mode: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        """The error-severity findings.

        Returns:
            list[Finding]: Findings whose severity is :data:`ERROR`.
        """
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        """The warning-severity findings.

        Returns:
            list[Finding]: Findings whose severity is :data:`WARNING`.
        """
        return [f for f in self.findings if f.severity == WARNING]

    def is_blocking(self, *, allow_warnings: bool) -> bool:
        """Whether this report should stop staging.

        Args:
            allow_warnings: When True, only errors block; when False, any
                finding blocks (fail-fast).

        Returns:
            bool: True if staging must not proceed.
        """
        if self.errors:
            return True
        return bool(self.warnings) and not allow_warnings

    def to_dict(self) -> dict[str, Any]:
        """Render the report as a JSON-ready dict.

        Returns:
            dict[str, Any]: Mode, counts and the findings.
        """
        return {
            "mode": self.mode,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "findings": [f.to_dict() for f in self.findings],
        }

    def summary_lines(self) -> list[str]:
        """Render a short human summary.

        Returns:
            list[str]: One line per finding, plus a header, for the terminal.
        """
        if not self.findings:
            return ["Preflight: no findings."]
        lines = [
            f"Preflight: {len(self.errors)} error(s), {len(self.warnings)} warning(s)."
        ]
        for finding in self.findings:
            mark = "x" if finding.severity == ERROR else "!"
            suffix = ""
            if finding.sample:
                shown = ", ".join(finding.sample)
                more = finding.count - len(finding.sample)
                suffix = f" [{shown}{f', +{more} more' if more > 0 else ''}]"
            lines.append(f"  {mark} {finding.message}{suffix}")
        return lines


def _column_set(frame: pl.DataFrame, column: str) -> set[str]:
    """Collect the non-null, non-empty string values of a column into a set.

    Args:
        frame: The frame to read.
        column: The column name.

    Returns:
        set[str]: The distinct non-empty values.
    """
    if frame.height == 0:
        return set()
    values = frame.get_column(column).drop_nulls().to_list()
    return {str(v) for v in values if str(v) != ""}


def _finding(code: str, severity: str, message: str, keys: list[str]) -> Finding:
    """Build a finding from a list of offending keys.

    Args:
        code: The finding code.
        severity: The severity.
        message: The human message (``{n}`` is replaced with the count).
        keys: The offending keys (sorted and sampled).

    Returns:
        Finding: The finding, with a bounded sample.
    """
    ordered = sorted(set(keys))
    return Finding(
        code=code,
        severity=severity,
        message=message.replace("{n}", str(len(ordered))),
        count=len(ordered),
        sample=tuple(ordered[:_SAMPLE]),
    )


def _check_balance(ledger: CanonicalLedger) -> list[Finding]:
    """Flag moves whose debit and credit do not balance.

    Args:
        ledger: The canonical ledger.

    Returns:
        list[Finding]: A single error finding, or empty.
    """
    bad = unbalanced_moves(ledger.lines)
    if bad.height == 0:
        return []
    keys = [str(k) for k in bad.get_column("move_key").to_list()]
    return [
        _finding(
            "unbalanced_move",
            ERROR,
            "{n} transaction(s) do not balance "
            f"(debit != credit beyond {BALANCE_TOLERANCE}).",
            keys,
        )
    ]


def _check_account_refs(ledger: CanonicalLedger) -> list[Finding]:
    """Flag account references not present in the chart of accounts.

    Args:
        ledger: The canonical ledger.

    Returns:
        list[Finding]: A single error finding, or empty.
    """
    known = _column_set(ledger.accounts, "account_id")
    used = _column_set(ledger.lines, "account_id") | _column_set(
        ledger.opening_lines, "account_id"
    )
    missing = used - known
    if not missing:
        return []
    return [
        _finding(
            "unknown_account",
            ERROR,
            "{n} account id(s) are referenced but not in the chart of accounts.",
            list(missing),
        )
    ]


def _check_journal_refs(ledger: CanonicalLedger) -> list[Finding]:
    """Flag moves referencing journals not present in the file.

    Args:
        ledger: The canonical ledger.

    Returns:
        list[Finding]: A single error finding, or empty.
    """
    known = _column_set(ledger.journals, "journal_id")
    used = _column_set(ledger.moves, "journal_id")
    missing = used - known
    if not missing:
        return []
    return [
        _finding(
            "unknown_journal",
            ERROR,
            "{n} journal id(s) are referenced by moves but not defined.",
            list(missing),
        )
    ]


def _check_partner_refs(ledger: CanonicalLedger) -> list[Finding]:
    """Flag customer/supplier references not present in the party list.

    Args:
        ledger: The canonical ledger.

    Returns:
        list[Finding]: A single warning finding, or empty.
    """
    known = _column_set(ledger.partners, "source_id")
    used = _column_set(ledger.lines, "source_partner_id") | _column_set(
        ledger.opening_lines, "source_partner_id"
    )
    missing = used - known
    if not missing:
        return []
    return [
        _finding(
            "unknown_partner",
            WARNING,
            "{n} customer/supplier id(s) are referenced but not defined; "
            "the partner link is left empty.",
            list(missing),
        )
    ]


def _check_currency(ledger: CanonicalLedger) -> list[Finding]:
    """Flag a non-EUR file currency.

    Args:
        ledger: The canonical ledger.

    Returns:
        list[Finding]: A single warning finding, or empty.
    """
    currency = ledger.header.get_column("currency").item(0)
    if currency in (None, "", "EUR"):
        return []
    return [
        Finding(
            "non_eur_currency",
            WARNING,
            f"File currency is {currency!r}, not EUR; amounts need an FX review.",
            1,
            (str(currency),),
        )
    ]


def _check_proposed_types(result: TransformResult) -> list[Finding]:
    """Flag accounts typed by the heuristic/fallback, needing review.

    Args:
        result: The transform result carrying the account decisions.

    Returns:
        list[Finding]: A single warning finding, or empty.
    """
    if not result.heuristic_accounts:
        return []
    return [
        _finding(
            "proposed_account_type",
            WARNING,
            "{n} account type(s) are proposals (numbering heuristic); "
            "review account_map.csv before loading.",
            list(result.heuristic_accounts),
        )
    ]


def _check_open_item_tie_out(
    ledger: CanonicalLedger, result: TransformResult
) -> list[Finding]:
    """Flag control accounts where open items do not sum to the opening balance.

    Only meaningful in cutover, where the opening balance of a reconcilable
    account is expected to equal the sum of its open items.

    Args:
        ledger: The canonical ledger.
        result: The transform result carrying the open items.

    Returns:
        list[Finding]: A single warning finding, or empty.
    """
    if result.mode != "cutover" or result.open_items.height == 0:
        return []
    opening = ledger.opening_lines.filter(pl.col("source_partner_id").is_null())
    opening_net: dict[str, float] = {}
    for row in opening.to_dicts():
        account_id = str(row.get("account_id") or "")
        opening_net[account_id] = opening_net.get(account_id, 0.0) + (
            float(row.get("debit") or 0.0) - float(row.get("credit") or 0.0)
        )
    item_net: dict[str, float] = {}
    for row in result.open_items.to_dicts():
        account_id = str(row["account_id"])
        item_net[account_id] = item_net.get(account_id, 0.0) + (
            float(row.get("debit") or 0.0) - float(row.get("credit") or 0.0)
        )
    mismatched = [
        account_id
        for account_id, net in item_net.items()
        if abs(net - opening_net.get(account_id, 0.0)) > BALANCE_TOLERANCE
    ]
    if not mismatched:
        return []
    return [
        _finding(
            "open_item_tie_out",
            WARNING,
            "{n} control account(s): open items do not tie out to the "
            "opening balance; review before posting.",
            mismatched,
        )
    ]


def run_preflight(ledger: CanonicalLedger, result: TransformResult) -> PreflightReport:
    """Run every preflight check over a ledger and its transform result.

    Args:
        ledger: The canonical ledger.
        result: The transform result built from it.

    Returns:
        PreflightReport: The findings, errors first then warnings.
    """
    findings: list[Finding] = []
    findings += _check_balance(ledger)
    findings += _check_account_refs(ledger)
    findings += _check_journal_refs(ledger)
    findings += _check_partner_refs(ledger)
    findings += _check_currency(ledger)
    findings += _check_proposed_types(result)
    findings += _check_open_item_tie_out(ledger, result)
    findings.sort(key=lambda f: 0 if f.severity == ERROR else 1)
    return PreflightReport(mode=result.mode, findings=findings)
