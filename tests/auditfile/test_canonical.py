"""Tests for the format-neutral canonical ledger and its summary."""

from __future__ import annotations

import polars as pl
import pytest

from fluvo.auditfile import canonical
from fluvo.auditfile.canonical import (
    SCHEMAS,
    CanonicalLedger,
    empty_frame,
    summarise,
    unbalanced_moves,
)
from fluvo.lib.core import InputError


def _empty_ledger() -> CanonicalLedger:
    """Build a ledger of empty frames with a one-row header."""
    frames = {name: empty_frame(name) for name in SCHEMAS if name != "header"}
    header = pl.DataFrame(
        {name: [None] for name in SCHEMAS["header"]}, schema=SCHEMAS["header"]
    )
    return CanonicalLedger(header=header, **frames)


def test_empty_frame_has_declared_schema() -> None:
    """empty_frame returns a zero-row frame with exactly the schema columns."""
    frame = empty_frame("lines")
    assert frame.height == 0
    assert frame.columns == list(SCHEMAS["lines"])


def test_validate_accepts_well_formed_ledger() -> None:
    """A ledger built to the schemas validates cleanly."""
    _empty_ledger().validate()


def test_validate_rejects_wrong_columns() -> None:
    """A frame missing a column is rejected at the reader/transform seam."""
    ledger = _empty_ledger()
    broken = CanonicalLedger(
        header=ledger.header,
        accounts=pl.DataFrame({"account_id": []}, schema={"account_id": pl.String}),
        partners=ledger.partners,
        journals=ledger.journals,
        moves=ledger.moves,
        lines=ledger.lines,
        opening_lines=ledger.opening_lines,
        vat_codes=ledger.vat_codes,
        anomalies=ledger.anomalies,
    )
    with pytest.raises(InputError, match="accounts"):
        broken.validate()


def test_validate_rejects_multi_row_header() -> None:
    """The header frame must carry exactly one row."""
    ledger = _empty_ledger()
    two = pl.concat([ledger.header, ledger.header])
    broken = CanonicalLedger(
        header=two,
        accounts=ledger.accounts,
        partners=ledger.partners,
        journals=ledger.journals,
        moves=ledger.moves,
        lines=ledger.lines,
        opening_lines=ledger.opening_lines,
        vat_codes=ledger.vat_codes,
        anomalies=ledger.anomalies,
    )
    with pytest.raises(InputError, match="one row"):
        broken.validate()


def _lines(rows: list[dict[str, object]]) -> pl.DataFrame:
    """Build a lines frame from partial row dicts."""
    full = [{col: row.get(col) for col in SCHEMAS["lines"]} for row in rows]
    return pl.DataFrame(full, schema=SCHEMAS["lines"])


def test_unbalanced_moves_flags_only_imbalanced() -> None:
    """A move whose debits and credits differ beyond tolerance is flagged."""
    lines = _lines(
        [
            {"move_key": "a", "debit": 100.0, "credit": 0.0},
            {"move_key": "a", "debit": 0.0, "credit": 100.0},
            {"move_key": "b", "debit": 50.0, "credit": 0.0},
            {"move_key": "b", "debit": 0.0, "credit": 40.0},
        ]
    )
    flagged = unbalanced_moves(lines)
    assert flagged.get_column("move_key").to_list() == ["b"]


def test_unbalanced_moves_tolerates_rounding() -> None:
    """A one-cent difference is within tolerance and not flagged."""
    lines = _lines(
        [
            {"move_key": "a", "debit": 100.00, "credit": 0.0},
            {"move_key": "a", "debit": 0.0, "credit": 100.005},
        ]
    )
    assert unbalanced_moves(lines).height == 0


def test_unbalanced_moves_empty_is_empty() -> None:
    """With no lines there are no unbalanced moves."""
    assert unbalanced_moves(empty_frame("lines")).height == 0


def test_summarise_counts_and_balance() -> None:
    """Summary reports per-frame counts and a balance verdict."""
    ledger = _empty_ledger()
    ledger = CanonicalLedger(
        header=pl.DataFrame(
            {
                **{name: [None] for name in SCHEMAS["header"]},
                "company_name": ["ACME"],
                "currency": ["EUR"],
                "source_version": ["4.0"],
            },
            schema=SCHEMAS["header"],
        ),
        accounts=ledger.accounts,
        partners=ledger.partners,
        journals=ledger.journals,
        moves=ledger.moves,
        lines=_lines(
            [
                {"move_key": "a", "debit": 100.0, "credit": 0.0},
                {"move_key": "a", "debit": 0.0, "credit": 100.0},
            ]
        ),
        opening_lines=ledger.opening_lines,
        vat_codes=ledger.vat_codes,
        anomalies=ledger.anomalies,
    )
    summary = summarise(ledger)
    assert summary.company_name == "ACME"
    assert summary.currency == "EUR"
    assert summary.source_version == "4.0"
    assert summary.counts["lines"] == 2
    assert summary.total_debit == pytest.approx(100.0)
    assert summary.total_credit == pytest.approx(100.0)
    assert summary.balanced is True
    assert summary.unbalanced_moves == 0


def test_summarise_detects_imbalance() -> None:
    """An imbalanced ledger reports balanced=False and a count."""
    ledger = _empty_ledger()
    ledger = CanonicalLedger(
        header=ledger.header,
        accounts=ledger.accounts,
        partners=ledger.partners,
        journals=ledger.journals,
        moves=ledger.moves,
        lines=_lines([{"move_key": "a", "debit": 100.0, "credit": 0.0}]),
        opening_lines=ledger.opening_lines,
        vat_codes=ledger.vat_codes,
        anomalies=ledger.anomalies,
    )
    summary = summarise(ledger)
    assert summary.balanced is False
    assert summary.unbalanced_moves == 1


def test_vocabularies_are_distinct() -> None:
    """The canonical code vocabularies do not collide."""
    roles = {
        canonical.PARTNER_CUSTOMER,
        canonical.PARTNER_SUPPLIER,
        canonical.PARTNER_BOTH,
    }
    assert len(roles) == 3
    kinds = {
        canonical.ACCOUNT_BALANCE,
        canonical.ACCOUNT_PROFIT_LOSS,
        canonical.UNKNOWN,
    }
    assert len(kinds) == 3
