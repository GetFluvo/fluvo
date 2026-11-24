"""Canonical-level preflight checks for an audit-file import.

Before anything is staged for Odoo, these checks run over the canonical ledger
and the transform's decisions and emit a structured report: a list of findings,
each with a severity, a machine code, a human message and a bounded sample of
offending keys. The report serialises to JSON (for a UI or a log) and renders a
short human summary (for the terminal).

The checks are deliberately conservative and name what they find rather than
guess a fix:

* **unbalanced_move** (error) — a *source* transaction whose debits and credits
  differ beyond tolerance; Odoo will reject it.
* **unbalanced_staged_move** (error) — a *generated* move (opening or history)
  that does not balance, caught on the staged output itself.
* **duplicate_move_key / duplicate_line_id** (error) — two records collapsed
  onto one external id, which would overwrite or merge silently.
* **parse_anomaly** (error) — a source cell (amount or date) the reader could
  not parse; it was recorded, not silently zeroed.
* **unknown_account** (error) — a line or opening balance references an account
  id that is not in the chart; its external id would not resolve.
* **unknown_journal** (error) — a move references a journal not in the file.
* **unknown_partner** (warning) — a line references a customer/supplier id not
  in the party list; the partner link is left empty.
* **non_eur_currency** (warning) — the file is not in EUR, so amounts need an FX
  review before they mean anything in a EUR company.
* **foreign_currency_line** (warning) — a line carries its own currency; only
  the base amount is staged.
* **control_total_mismatch** (warning) — the file's declared control totals do
  not match what was parsed, so the parse may be incomplete.
* **proposed_account_type** (warning) — accounts whose Odoo type came from the
  numbering heuristic or the fallback, not an override or RGS; review
  ``account_map.csv`` before loading.
* **account_kind_conflict** (warning) — an account's Odoo type contradicts the
  source's balance/P&L kind.
* **open_item_tie_out** (warning) — in cutover, a control account's staged lines
  do not sum to its closing balance.

Severity drives staging: by default any finding blocks (fail-fast); with the
caller's ``allow_warnings`` opt-in, only errors block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

import polars as pl

from .canonical import BALANCE_TOLERANCE, CanonicalLedger, unbalanced_moves
from .transform import MODE_CUTOVER, TransformResult, _net_by_account

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


def _iter_staged_moves(
    moves: pl.DataFrame,
) -> list[tuple[str, float, float, list[str]]]:
    """Reassemble the o2m move rows into per-move totals and line ids.

    A new move starts on each row carrying a non-empty ``id``; the following
    blank-id rows are its continuation lines.

    Args:
        moves: The staged ``account.move`` o2m frame.

    Returns:
        list[tuple[str, float, float, list[str]]]: One tuple per move —
        ``(move id, debit total, credit total, line ids)``.
    """
    out: list[tuple[str, float, float, list[str]]] = []
    for row in moves.to_dicts():
        move_id = str(row.get("id") or "")
        if move_id:
            out.append((move_id, 0.0, 0.0, []))
        if not out:
            continue
        current_id, debit, credit, lines = out[-1]
        debit += float(row.get("line_ids/debit") or 0.0)
        credit += float(row.get("line_ids/credit") or 0.0)
        line_id = str(row.get("line_ids/id") or "")
        if line_id:
            lines.append(line_id)  # same list object as in the tuple
        out[-1] = (current_id, debit, credit, lines)
    return out


def _check_moves_balance(result: TransformResult) -> list[Finding]:
    """Flag any *generated* move whose debits and credits do not balance.

    This checks the staged output itself, not just the source lines, so a
    transform that builds an unbalanced opening or history move is caught before
    it reaches Odoo.

    Args:
        result: The transform result.

    Returns:
        list[Finding]: A single error finding, or empty.
    """
    bad = [
        move_id
        for move_id, debit, credit, _lines in _iter_staged_moves(result.moves)
        if abs(debit - credit) > BALANCE_TOLERANCE
    ]
    if not bad:
        return []
    return [
        _finding(
            "unbalanced_staged_move",
            ERROR,
            "{n} generated move(s) do not balance; Odoo would reject them.",
            bad,
        )
    ]


def _check_move_integrity(result: TransformResult) -> list[Finding]:
    """Flag duplicate move keys or duplicate move-line external ids.

    Two source transactions that collapsed onto one external id, or a repeated
    line id, would make the import overwrite or merge records silently.

    Args:
        result: The transform result.

    Returns:
        list[Finding]: Error findings for any duplicates, or empty.
    """
    moves = _iter_staged_moves(result.moves)
    move_ids = [m[0] for m in moves]
    line_ids = [lid for m in moves for lid in m[3]]
    findings: list[Finding] = []
    dup_moves = sorted({m for m in move_ids if move_ids.count(m) > 1})
    if dup_moves:
        findings.append(
            _finding(
                "duplicate_move_key",
                ERROR,
                "{n} move external id(s) occur more than once.",
                dup_moves,
            )
        )
    dup_lines = sorted({x for x in line_ids if line_ids.count(x) > 1})
    if dup_lines:
        findings.append(
            _finding(
                "duplicate_line_id",
                ERROR,
                "{n} move-line external id(s) occur more than once.",
                dup_lines,
            )
        )
    return findings


def _check_parse_anomalies(ledger: CanonicalLedger) -> list[Finding]:
    """Flag source cells the reader could not parse.

    Args:
        ledger: The canonical ledger.

    Returns:
        list[Finding]: A single error finding, or empty.
    """
    if ledger.anomalies.height == 0:
        return []
    samples = tuple(
        f"{r['location']}={r['raw_value']!r}"
        for r in ledger.anomalies.head(_SAMPLE).to_dicts()
    )
    return [
        Finding(
            "parse_anomaly",
            ERROR,
            f"{ledger.anomalies.height} source cell(s) could not be parsed "
            "(a malformed amount or date); fix the file or the export.",
            count=int(ledger.anomalies.height),
            sample=samples,
        )
    ]


def _check_control_totals(ledger: CanonicalLedger) -> list[Finding]:
    """Flag the file's declared control totals not matching the parse.

    Args:
        ledger: The canonical ledger.

    Returns:
        list[Finding]: A single warning finding, or empty.
    """
    header = ledger.header.to_dicts()[0]
    gl = ledger.opening_lines.filter(pl.col("source_partner_id").is_null())
    lines = ledger.lines
    checks = (
        ("opening lines", "declared_opening_count", float(gl.height)),
        ("opening debit", "declared_opening_debit", _sum(gl, "debit")),
        ("opening credit", "declared_opening_credit", _sum(gl, "credit")),
        ("transaction lines", "declared_tx_count", float(lines.height)),
        ("transaction debit", "declared_tx_debit", _sum(lines, "debit")),
        ("transaction credit", "declared_tx_credit", _sum(lines, "credit")),
    )
    mismatches: list[str] = []
    for label, key, actual in checks:
        declared = header.get(key)
        if declared is None:
            continue
        if abs(float(declared) - actual) > BALANCE_TOLERANCE:
            mismatches.append(f"{label}: file {float(declared):g} vs parsed {actual:g}")
    if not mismatches:
        return []
    return [
        _finding(
            "control_total_mismatch",
            WARNING,
            "{n} declared control total(s) do not match the parse; the file may "
            "be incomplete or non-standard.",
            mismatches,
        )
    ]


def _sum(frame: pl.DataFrame, column: str) -> float:
    """Sum a numeric column, treating an empty frame as zero.

    Args:
        frame: The frame to read.
        column: The column to sum.

    Returns:
        float: The column total.
    """
    if frame.height == 0:
        return 0.0
    return float(frame.get_column(column).sum() or 0.0)


def _check_kind_conflicts(result: TransformResult) -> list[Finding]:
    """Flag accounts whose Odoo type contradicts the source's account kind.

    Args:
        result: The transform result.

    Returns:
        list[Finding]: A single warning finding, or empty.
    """
    if not result.conflicting_accounts:
        return []
    return [
        _finding(
            "account_kind_conflict",
            WARNING,
            "{n} account(s) are typed against the source's balance/P&L kind; "
            "review account_map.csv.",
            list(result.conflicting_accounts),
        )
    ]


def _check_foreign_currency(ledger: CanonicalLedger) -> list[Finding]:
    """Flag transaction lines carrying their own (foreign) currency.

    Args:
        ledger: The canonical ledger.

    Returns:
        list[Finding]: A single warning finding, or empty.
    """
    file_currency = ledger.header.get_column("currency").item(0)
    if ledger.lines.height == 0:
        return []
    foreign = ledger.lines.filter(
        pl.col("currency").is_not_null() & (pl.col("currency") != file_currency)
    )
    if foreign.height == 0:
        return []
    currencies = sorted({str(c) for c in foreign.get_column("currency").to_list()})
    return [
        _finding(
            "foreign_currency_line",
            WARNING,
            f"{foreign.height} line(s) are in a currency other than "
            f"{file_currency!r}; only the base amount is staged, review the FX.",
            currencies,
        )
    ]


def _check_open_item_tie_out(
    ledger: CanonicalLedger, result: TransformResult
) -> list[Finding]:
    """Flag a control account whose opening subledger does not tie to its GL.

    In cutover, each control account's opening subledger items (the carried-over
    open invoices) should sum to that account's opening GL balance. A mismatch
    — including a subledger item whose control account did not resolve — means
    the carried-over open items are inconsistent with the balance sheet, so the
    per-item cutover cannot be trusted without review.

    Args:
        ledger: The canonical ledger.
        result: The transform result (its mode selects the check).

    Returns:
        list[Finding]: A single warning finding, or empty.
    """
    if result.mode != MODE_CUTOVER:
        return []
    sub = ledger.opening_lines.filter(pl.col("source_partner_id").is_not_null())
    if sub.height == 0:
        return []
    opening_gl = _net_by_account(
        ledger.opening_lines.filter(pl.col("source_partner_id").is_null())
    )
    sub_net = _net_by_account(sub)
    mismatched = [
        (account_id or "(unresolved)")
        for account_id, net in sub_net.items()
        if abs(net - opening_gl.get(account_id, 0.0)) > BALANCE_TOLERANCE
    ]
    if not mismatched:
        return []
    return [
        _finding(
            "open_item_tie_out",
            WARNING,
            "{n} control account(s): opening subledger items do not tie out to "
            "the GL opening balance; review before posting.",
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
    findings += _check_moves_balance(result)
    findings += _check_move_integrity(result)
    findings += _check_parse_anomalies(ledger)
    findings += _check_account_refs(ledger)
    findings += _check_journal_refs(ledger)
    findings += _check_partner_refs(ledger)
    findings += _check_currency(ledger)
    findings += _check_foreign_currency(ledger)
    findings += _check_control_totals(ledger)
    findings += _check_proposed_types(result)
    findings += _check_kind_conflicts(result)
    findings += _check_open_item_tie_out(ledger, result)
    findings.sort(key=lambda f: 0 if f.severity == ERROR else 1)
    return PreflightReport(mode=result.mode, findings=findings)
