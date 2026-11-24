"""Tests for the canonical -> Odoo-shaped transform."""

from __future__ import annotations

from pathlib import Path

import pytest

from fluvo.auditfile import read_auditfile
from fluvo.auditfile.transform import (
    MODE_CUTOVER,
    MODE_FULL_HISTORY,
    SRC_FALLBACK,
    SRC_HEURISTIC,
    SRC_OVERRIDE,
    SRC_RGS,
    _norm_kvk,
    _norm_vat,
    _san,
    _xmlid,
    classify_account,
    transform,
)
from fluvo.lib.core import InputError

from .xaf_fixtures import generate_xaf


def _ledger(tmp_path: Path, version: str = "4.0"):  # type: ignore[no-untyped-def]
    """Read a freshly generated fixture ledger of the given version."""
    path = tmp_path / f"f{version}.xaf"
    generate_xaf(path, version=version)
    return read_auditfile(str(path))


def test_san_and_xmlid() -> None:
    """External ids sanitise unsafe characters and keep the prefix dot."""
    assert _san("JRN000/000-0001") == "JRN000_000_0001"
    assert _xmlid("xaf", "account", "1300") == "xaf.account_1300"
    assert _xmlid("xaf", "move", "A/B") == "xaf.move_A_B"


def test_norm_kvk_and_vat() -> None:
    """KvK normalises to padded digits; VAT upper-cases and strips."""
    assert _norm_kvk("1234567") == "01234567"
    assert _norm_kvk("8123 4567") == "81234567"
    assert _norm_kvk("x") is None
    assert _norm_vat("nl 0012.3B01") == "NL00123B01"
    assert _norm_vat("") is None


def test_classify_account_chain() -> None:
    """The mapper chain honours override > RGS > heuristic > fallback."""
    assert classify_account("1300", None, "balance", {"1300": "asset_cash"}) == (
        "asset_cash",
        SRC_OVERRIDE,
    )
    assert classify_account("1300", "BVorHanDeb", "balance", {}) == (
        "asset_receivable",
        SRC_RGS,
    )
    assert classify_account("1300", None, "balance", {}) == (
        "asset_receivable",
        SRC_HEURISTIC,
    )
    assert classify_account("ZZZ", None, "balance", {}) == (
        "asset_current",
        SRC_FALLBACK,
    )


def test_partner_dedup_and_alias(tmp_path: Path) -> None:
    """Two records sharing a KvK merge into one partner with both ranks."""
    result = transform(_ledger(tmp_path), prefix="xaf")
    # C001 and C002 share a KvK -> one survivor, three partners total.
    assert result.partners.height == 3
    alias = dict(
        zip(
            result.alias_map.get_column("source_id").to_list(),
            result.alias_map.get_column("partner_xmlid").to_list(),
            strict=True,
        )
    )
    assert alias["C001"] == alias["C002"] == "xaf.partner_C001"
    survivor = result.partners.filter(result.partners["id"] == "xaf.partner_C001")
    assert survivor["customer_rank"].item(0) == "1"


def test_both_role_sets_both_ranks(tmp_path: Path) -> None:
    """A 'both' partner gets customer_rank and supplier_rank set."""
    result = transform(_ledger(tmp_path), prefix="xaf")
    both = result.partners.filter(result.partners["id"] == "xaf.partner_B001")
    assert both["customer_rank"].item(0) == "1"
    assert both["supplier_rank"].item(0) == "1"


def test_accounts_reconcile_and_source(tmp_path: Path) -> None:
    """Receivable/payable accounts are reconcilable; filler types are proposals."""
    result = transform(_ledger(tmp_path, "4.0"), prefix="xaf")
    rows = {r["code"]: r for r in result.account_map.to_dicts()}
    assert rows["1300"]["account_type"] == "asset_receivable"
    assert rows["1300"]["reconcile"] == "True"
    assert rows["1300"]["source"] == SRC_RGS
    assert set(result.heuristic_accounts)  # the 9xxx fillers are proposals


def test_override_applied(tmp_path: Path) -> None:
    """An override changes the account type and its source."""
    result = transform(
        _ledger(tmp_path), prefix="xaf", account_overrides={"8000": "income_other"}
    )
    row = next(r for r in result.account_map.to_dicts() if r["code"] == "8000")
    assert row["account_type"] == "income_other"
    assert row["source"] == SRC_OVERRIDE


def test_journals_include_opening_and_unique_codes(tmp_path: Path) -> None:
    """Journals carry a synthetic opening journal and unique codes."""
    result = transform(_ledger(tmp_path), prefix="xaf")
    ids = result.journals.get_column("id").to_list()
    assert "xaf.journal__opening" in ids
    codes = result.journals.get_column("code").to_list()
    assert len(codes) == len(set(codes))


def _move_balance(result) -> tuple[float, float]:  # type: ignore[no-untyped-def]
    """Sum debit and credit across all staged move rows."""
    debit = sum(float(v) for v in result.moves.get_column("line_ids/debit").to_list())
    credit = sum(float(v) for v in result.moves.get_column("line_ids/credit").to_list())
    return debit, credit


def test_full_history_is_balanced(tmp_path: Path) -> None:
    """Full-history staging keeps every move balanced in aggregate."""
    result = transform(_ledger(tmp_path), prefix="xaf", mode=MODE_FULL_HISTORY)
    debit, credit = _move_balance(result)
    assert debit == pytest.approx(credit)
    # The opening move plus 6 two-line transactions.
    assert result.moves.height == 2 + 12


def test_move_line_external_ids(tmp_path: Path) -> None:
    """Move lines use the ADR 0001 deterministic <move_xmlid>_l<n> id."""
    result = transform(_ledger(tmp_path), prefix="xaf")
    first = result.moves.row(0, named=True)
    assert first["id"] == "xaf.move__opening"
    assert first["line_ids/id"] == "xaf.move__opening_l1"
    # The opening move's second line is a continuation row: blank id (so fluvo's
    # --o2m groups it with the parent), own line_ids/id, no repeated header.
    second = result.moves.row(1, named=True)
    assert second["id"] == ""
    assert second["name"] == ""
    assert second["line_ids/id"] == "xaf.move__opening_l2"


def test_cutover_breaks_out_open_items(tmp_path: Path) -> None:
    """Cutover emits an opening move with per-partner AR lines and open items."""
    result = transform(_ledger(tmp_path), prefix="xaf", mode=MODE_CUTOVER)
    assert result.open_items.height >= 1
    # Only the opening move is produced in cutover (no transaction history).
    # Continuation lines carry a blank id (fluvo --o2m grouping), so filter them.
    move_ids = {i for i in result.moves.get_column("id").to_list() if i}
    assert move_ids == {"xaf.move__opening"}
    partners = [p for p in result.moves["line_ids/partner_id/id"].to_list() if p]
    assert any(p.startswith("xaf.partner_") for p in partners)


def test_unknown_mode_raises(tmp_path: Path) -> None:
    """An unsupported mode is an input error."""
    with pytest.raises(InputError, match="unknown mode"):
        transform(_ledger(tmp_path), mode="nope")


def test_transform_is_deterministic(tmp_path: Path) -> None:
    """The same ledger transforms to identical frames (idempotent stage)."""
    ledger = _ledger(tmp_path)
    a = transform(ledger, prefix="xaf", mode=MODE_FULL_HISTORY)
    b = transform(ledger, prefix="xaf", mode=MODE_FULL_HISTORY)
    assert a.partners.equals(b.partners)
    assert a.accounts.equals(b.accounts)
    assert a.journals.equals(b.journals)
    assert a.moves.equals(b.moves)
    assert a.alias_map.equals(b.alias_map)
