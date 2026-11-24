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
    opening_lines: list[dict[str, Any]] | None = None,
    anomalies: list[dict[str, Any]] | None = None,
    currency: str = "EUR",
    header_extra: dict[str, Any] | None = None,
) -> CanonicalLedger:
    """Build a small canonical ledger for a targeted preflight check."""
    header = _frame(
        "header",
        [
            {
                "source_format": "xaf",
                "source_version": "4.0",
                "currency": currency,
                **(header_extra or {}),
            }
        ],
    )
    return CanonicalLedger(
        header=header,
        accounts=_frame("accounts", accounts or []),
        partners=_frame("partners", partners or []),
        journals=_frame("journals", journals or []),
        moves=_frame("moves", moves or []),
        lines=_frame("lines", lines or []),
        opening_lines=_frame("opening_lines", opening_lines or []),
        vat_codes=_frame("vat_codes", []),
        anomalies=_frame("anomalies", anomalies or []),
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


def test_open_item_tie_out_warns_on_mismatch() -> None:
    """Cutover flags a subledger that does not tie to its GL opening balance."""
    ledger = _ledger(
        accounts=[{"account_id": "1300", "kind": "balance", "lead_code": "BVorHanDeb"}],
        opening_lines=[
            {"account_id": "1300", "debit": 500.0, "credit": 0.0},  # GL control
            {  # subledger item short of the control balance by 200
                "account_id": "1300",
                "source_partner_id": "C1",
                "debit": 300.0,
                "credit": 0.0,
                "invoice_ref": "INV1",
            },
        ],
    )
    report = run_preflight(ledger, transform(ledger, mode=MODE_CUTOVER))
    assert "open_item_tie_out" in _codes(report)


def test_open_item_tie_out_passes_when_consistent(tmp_path: Path) -> None:
    """A valid 3.2 file whose subledger ties to its GL control does not warn."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="3.2")
    ledger = read_auditfile(str(path))
    report = run_preflight(ledger, transform(ledger, mode=MODE_CUTOVER))
    assert "open_item_tie_out" not in _codes(report)


def test_staged_move_balance_is_checked() -> None:
    """An unbalanced source move is flagged on the staged output too."""
    ledger = _ledger(
        accounts=[{"account_id": "1300", "kind": "balance"}],
        journals=[{"journal_id": "S", "kind": "sale"}],
        moves=[{"move_key": "S/1", "journal_id": "S", "number": "1"}],
        lines=[
            {"move_key": "S/1", "line_number": 1, "account_id": "1300", "debit": 100.0},
            {"move_key": "S/1", "line_number": 2, "account_id": "1300", "credit": 90.0},
        ],
    )
    codes = _codes(_report(ledger))
    assert "unbalanced_staged_move" in codes


def test_duplicate_line_ids_are_error() -> None:
    """Two transactions collapsed onto one move key yield duplicate line ids."""
    ledger = _ledger(
        accounts=[{"account_id": "1300", "kind": "balance"}],
        journals=[{"journal_id": "S", "kind": "sale"}],
        moves=[
            {"move_key": "S/1", "journal_id": "S", "number": "1"},
            {"move_key": "S/1", "journal_id": "S", "number": "1"},
        ],
        lines=[
            {"move_key": "S/1", "line_number": 1, "account_id": "1300", "debit": 100.0},
            {
                "move_key": "S/1",
                "line_number": 2,
                "account_id": "1300",
                "credit": 100.0,
            },
            {"move_key": "S/1", "line_number": 1, "account_id": "1300", "debit": 50.0},
            {"move_key": "S/1", "line_number": 2, "account_id": "1300", "credit": 50.0},
        ],
    )
    report = _report(ledger)
    assert "duplicate_line_id" in _codes(report)
    assert report.is_blocking(allow_warnings=True) is True  # it is an error


def test_parse_anomaly_is_error() -> None:
    """A recorded parse anomaly blocks with an error finding."""
    ledger = _ledger(
        anomalies=[
            {"kind": "amount", "location": "trLine X line 1 amnt", "raw_value": "1,5"}
        ]
    )
    report = _report(ledger)
    assert "parse_anomaly" in _codes(report)
    assert report.is_blocking(allow_warnings=True) is True


def test_control_total_mismatch_is_warning() -> None:
    """A declared transactions total not matching the parse warns."""
    ledger = _ledger(
        accounts=[{"account_id": "1300", "kind": "balance"}],
        journals=[{"journal_id": "S", "kind": "sale"}],
        moves=[{"move_key": "S/1", "journal_id": "S", "number": "1"}],
        lines=[
            {"move_key": "S/1", "line_number": 1, "account_id": "1300", "debit": 100.0},
            {
                "move_key": "S/1",
                "line_number": 2,
                "account_id": "1300",
                "credit": 100.0,
            },
        ],
        header_extra={"declared_tx_debit": 999.0, "declared_tx_credit": 999.0},
    )
    assert "control_total_mismatch" in _codes(_report(ledger))


def test_account_kind_conflict_is_warning() -> None:
    """An account typed against its source balance/P&L kind is flagged."""
    # Code 1300 is heuristically receivable (a balance type), but the source
    # marks it profit_loss -> a conflict to review.
    ledger = _ledger(accounts=[{"account_id": "1300", "kind": "profit_loss"}])
    assert "account_kind_conflict" in _codes(_report(ledger))


def test_foreign_currency_line_is_warning() -> None:
    """A line in a currency other than the file's is flagged for FX review."""
    ledger = _ledger(
        accounts=[{"account_id": "1300", "kind": "balance"}],
        journals=[{"journal_id": "S", "kind": "sale"}],
        moves=[{"move_key": "S/1", "journal_id": "S", "number": "1"}],
        lines=[
            {
                "move_key": "S/1",
                "line_number": 1,
                "account_id": "1300",
                "debit": 100.0,
                "currency": "USD",
            },
            {
                "move_key": "S/1",
                "line_number": 2,
                "account_id": "1300",
                "credit": 100.0,
            },
        ],
    )
    assert "foreign_currency_line" in _codes(_report(ledger))


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
