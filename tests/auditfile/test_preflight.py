"""Tests for the canonical-level preflight checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from fluvo.auditfile import read_auditfile
from fluvo.auditfile.canonical import SCHEMAS, CanonicalLedger
from fluvo.auditfile.preflight import ERROR, WARNING, run_preflight
from fluvo.auditfile.transform import MODE_CUTOVER, transform

from .xaf_fixtures import generate_xaf


def _frame(name: str, rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Build a canonical frame from partial row dicts, filling absent columns."""
    schema = SCHEMAS[name]
    full = [{col: row.get(col) for col in schema} for row in rows]
    return pl.DataFrame(full, schema=schema)


def _ledger(
    *,
    accounts: list[dict[str, Any]] | None = None,
    partners: list[dict[str, Any]] | None = None,
    journals: list[dict[str, Any]] | None = None,
    moves: list[dict[str, Any]] | None = None,
    lines: list[dict[str, Any]] | None = None,
    currency: str = "EUR",
) -> CanonicalLedger:
    """Build a small canonical ledger for a targeted preflight check."""
    header = _frame(
        "header",
        [{"source_format": "xaf", "source_version": "4.0", "currency": currency}],
    )
    return CanonicalLedger(
        header=header,
        accounts=_frame("accounts", accounts or []),
        partners=_frame("partners", partners or []),
        journals=_frame("journals", journals or []),
        moves=_frame("moves", moves or []),
        lines=_frame("lines", lines or []),
        opening_lines=_frame("opening_lines", []),
        vat_codes=_frame("vat_codes", []),
    )


def _report(ledger: CanonicalLedger, mode: str = "full_history"):  # type: ignore[no-untyped-def]
    """Transform a ledger and run preflight over the pair."""
    return run_preflight(ledger, transform(ledger, mode=mode))


def _codes(report: Any) -> set[str]:
    """The set of finding codes in a report."""
    return {f.code for f in report.findings}


def test_clean_ledger_balanced_has_no_errors() -> None:
    """A balanced ledger with resolvable refs produces no errors."""
    ledger = _ledger(
        accounts=[
            {"account_id": "1300", "description": "Deb", "kind": "balance"},
            {"account_id": "8000", "description": "Omzet", "kind": "profit_loss"},
        ],
        journals=[{"journal_id": "S", "description": "Sales", "kind": "sale"}],
        moves=[{"move_key": "S/1", "journal_id": "S", "number": "1"}],
        lines=[
            {
                "move_key": "S/1",
                "line_number": 1,
                "account_id": "1300",
                "debit": 100.0,
                "credit": 0.0,
            },
            {
                "move_key": "S/1",
                "line_number": 2,
                "account_id": "8000",
                "debit": 0.0,
                "credit": 100.0,
            },
        ],
    )
    assert not _report(ledger).errors


def test_unbalanced_move_is_error() -> None:
    """A move whose debit and credit differ is an error."""
    ledger = _ledger(
        accounts=[{"account_id": "1300", "kind": "balance"}],
        moves=[{"move_key": "S/1", "journal_id": "S", "number": "1"}],
        journals=[{"journal_id": "S", "kind": "sale"}],
        lines=[
            {
                "move_key": "S/1",
                "line_number": 1,
                "account_id": "1300",
                "debit": 100.0,
                "credit": 0.0,
            },
        ],
    )
    report = _report(ledger)
    assert "unbalanced_move" in _codes(report)
    assert any(f.severity == ERROR for f in report.findings)


def test_unknown_account_ref_is_error() -> None:
    """A line referencing an account not in the chart is an error."""
    ledger = _ledger(
        accounts=[{"account_id": "1300", "kind": "balance"}],
        journals=[{"journal_id": "S", "kind": "sale"}],
        moves=[{"move_key": "S/1", "journal_id": "S", "number": "1"}],
        lines=[
            {
                "move_key": "S/1",
                "line_number": 1,
                "account_id": "1300",
                "debit": 50.0,
                "credit": 0.0,
            },
            {
                "move_key": "S/1",
                "line_number": 2,
                "account_id": "9999",
                "debit": 0.0,
                "credit": 50.0,
            },
        ],
    )
    assert "unknown_account" in _codes(_report(ledger))


def test_unknown_journal_ref_is_error() -> None:
    """A move referencing an undefined journal is an error."""
    ledger = _ledger(
        accounts=[{"account_id": "1300", "kind": "balance"}],
        moves=[{"move_key": "X/1", "journal_id": "X", "number": "1"}],
        lines=[
            {
                "move_key": "X/1",
                "line_number": 1,
                "account_id": "1300",
                "debit": 1.0,
                "credit": 0.0,
            },
            {
                "move_key": "X/1",
                "line_number": 2,
                "account_id": "1300",
                "debit": 0.0,
                "credit": 1.0,
            },
        ],
    )
    assert "unknown_journal" in _codes(_report(ledger))


def test_unknown_partner_ref_is_warning() -> None:
    """A line referencing an undefined partner is a warning."""
    ledger = _ledger(
        accounts=[{"account_id": "1300", "kind": "balance"}],
        journals=[{"journal_id": "S", "kind": "sale"}],
        moves=[{"move_key": "S/1", "journal_id": "S", "number": "1"}],
        lines=[
            {
                "move_key": "S/1",
                "line_number": 1,
                "account_id": "1300",
                "debit": 1.0,
                "credit": 0.0,
                "source_partner_id": "GHOST",
            },
            {
                "move_key": "S/1",
                "line_number": 2,
                "account_id": "1300",
                "debit": 0.0,
                "credit": 1.0,
            },
        ],
    )
    report = _report(ledger)
    assert "unknown_partner" in _codes(report)
    assert all(
        f.severity == WARNING for f in report.findings if f.code == "unknown_partner"
    )


def test_non_eur_currency_is_warning() -> None:
    """A non-EUR file currency is flagged for FX review."""
    ledger = _ledger(currency="USD")
    assert "non_eur_currency" in _codes(_report(ledger))


def test_proposed_types_are_warning(tmp_path: Path) -> None:
    """Heuristically-typed accounts raise a review warning."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    ledger = read_auditfile(str(path))
    assert "proposed_account_type" in _codes(_report(ledger))


def test_is_blocking_semantics() -> None:
    """Errors always block; warnings block only without allow_warnings."""
    ledger = _ledger(currency="USD")  # one warning, no error
    report = _report(ledger)
    assert report.is_blocking(allow_warnings=False) is True
    assert report.is_blocking(allow_warnings=True) is False


def test_report_serialises(tmp_path: Path) -> None:
    """to_dict and summary_lines render without error."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="3.2")
    report = _report(read_auditfile(str(path)))
    data = report.to_dict()
    assert data["mode"] == "full_history"
    assert isinstance(data["findings"], list)
    assert report.summary_lines()[0].startswith("Preflight:")


def test_open_item_tie_out_warns_on_mismatch(tmp_path: Path) -> None:
    """Cutover flags open items that do not tie out to the opening balance."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    ledger = read_auditfile(str(path))
    report = run_preflight(ledger, transform(ledger, mode=MODE_CUTOVER))
    # The fixture's opening balance carries no AR control line, so the open
    # items (from transactions) cannot tie out -> a warning.
    assert "open_item_tie_out" in _codes(report)


def test_empty_summary() -> None:
    """A report with no findings summarises as 'no findings'."""
    ledger = _ledger(
        accounts=[
            {"account_id": "1300", "kind": "balance", "lead_code": "BVorHanDeb"},
            {"account_id": "8000", "kind": "profit_loss", "lead_code": "WOmzNomGrp"},
        ],
        journals=[{"journal_id": "S", "kind": "sale"}],
        moves=[{"move_key": "S/1", "journal_id": "S", "number": "1"}],
        lines=[
            {
                "move_key": "S/1",
                "line_number": 1,
                "account_id": "1300",
                "debit": 1.0,
                "credit": 0.0,
            },
            {
                "move_key": "S/1",
                "line_number": 2,
                "account_id": "8000",
                "debit": 0.0,
                "credit": 1.0,
            },
        ],
    )
    report = _report(ledger)
    assert report.summary_lines() == ["Preflight: no findings."]
