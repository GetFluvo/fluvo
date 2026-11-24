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


_NS32 = "http://www.auditfiles.nl/XAF/3.2"


def _read_xaf(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
    """Write and read a minimal 3.2 XAF whose company holds ``body``."""
    path = tmp_path / "c.xaf"
    path.write_text(
        f'<?xml version="1.0"?><auditfile xmlns="{_NS32}">'
        "<header><startDate>2025-01-01</startDate><endDate>2025-12-31</endDate>"
        "<curCode>EUR</curCode></header>"
        f"<company><companyName>X</companyName>{body}</company></auditfile>",
        encoding="utf-8",
    )
    return read_auditfile(str(path))


def _move_net(result, account_xmlid: str) -> float:  # type: ignore[no-untyped-def]
    """Net (debit - credit) of the opening move's lines for one account."""
    rows = result.moves.to_dicts()
    net = 0.0
    for row in rows:
        if row["line_ids/account_id/id"] == account_xmlid:
            net += float(row["line_ids/debit"]) - float(row["line_ids/credit"])
    return net


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
    # The opening move (3 GL lines) plus 6 two-line transactions.
    assert result.moves.height == 3 + 12


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


def test_cutover_is_closing_position_and_balances(tmp_path: Path) -> None:
    """Cutover emits the closing position, balanced, carrying prior open items."""
    result = transform(_ledger(tmp_path, "3.2"), prefix="xaf", mode=MODE_CUTOVER)
    debit, credit = _move_balance(result)
    assert debit == pytest.approx(credit)  # closing TB is balanced by construction
    refs = result.open_items.get_column("doc_ref").to_list()
    assert "2023-900" in refs  # the prior-year opening invoice is carried over
    # 1300 closing = opening (1210) + the year's debits on it (101..106 = 621).
    assert _move_net(result, "xaf.account_1300") == pytest.approx(1210.0 + 621.0)


_RECV_BANK_BODY = (
    "<generalLedger>"
    "<ledgerAccount><accID>1300</accID><accDesc>Deb</accDesc><accTp>B</accTp>"
    "</ledgerAccount>"
    "<ledgerAccount><accID>1100</accID><accDesc>Bank</accDesc><accTp>B</accTp>"
    "</ledgerAccount>"
    "<ledgerAccount><accID>8000</accID><accDesc>Omzet</accDesc><accTp>P</accTp>"
    "</ledgerAccount></generalLedger>"
    "<customersSuppliers><customerSupplier><custSupID>C1</custSupID>"
    "<custSupTp>C</custSupTp><custSupName>Klant</custSupName></customerSupplier>"
    "</customersSuppliers>"
    "<transactions>"
    "<journal><jrnID>VK</jrnID><jrnTp>S</jrnTp>"
    "<transaction><nr>1</nr><trDt>2025-02-01</trDt>"
    "<trLine><nr>1</nr><accID>1300</accID><amnt>100.00</amnt><amntTp>D</amntTp>"
    "<invRef>INV1</invRef><custSupID>C1</custSupID></trLine>"
    "<trLine><nr>2</nr><accID>8000</accID><amnt>100.00</amnt><amntTp>C</amntTp>"
    "</trLine></transaction>"
    "<transaction><nr>2</nr><trDt>2025-03-01</trDt>"
    "<trLine><nr>1</nr><accID>1300</accID><amnt>50.00</amnt><amntTp>D</amntTp>"
    "<invRef>INV2</invRef><custSupID>C1</custSupID></trLine>"
    "<trLine><nr>2</nr><accID>8000</accID><amnt>50.00</amnt><amntTp>C</amntTp>"
    "</trLine></transaction></journal>"
    "<journal><jrnID>BNK</jrnID><jrnTp>B</jrnTp>"
    "<transaction><nr>1</nr><trDt>2025-04-01</trDt>"
    "<trLine><nr>1</nr><accID>1100</accID><amnt>100.00</amnt><amntTp>D</amntTp>"
    "<docRef>BANK-7</docRef></trLine>"
    "<trLine><nr>2</nr><accID>1300</accID><amnt>100.00</amnt><amntTp>C</amntTp>"
    "<docRef>BANK-7</docRef><invRef>INV1</invRef><custSupID>C1</custSupID></trLine>"
    "</transaction></journal></transactions>"
)


def test_cutover_nets_payment_against_invoice(tmp_path: Path) -> None:
    """A payment matched by invRef settles its invoice; only INV2 stays open."""
    result = transform(_read_xaf(tmp_path, _RECV_BANK_BODY), mode=MODE_CUTOVER)
    refs = set(result.open_items.get_column("doc_ref").to_list())
    assert "INV2" in refs  # unpaid invoice is open
    assert "INV1" not in refs  # invoice + its payment net to zero
    assert "BANK-7" not in refs  # no phantom open item for the payment
    debit, credit = _move_balance(result)
    assert debit == pytest.approx(credit)


def test_cutover_result_account_closes_pl(tmp_path: Path) -> None:
    """With a result account, P&L balances fold into it, off their own lines."""
    ledger = _read_xaf(tmp_path, _RECV_BANK_BODY)
    carried = transform(ledger, mode=MODE_CUTOVER)
    closed = transform(ledger, mode=MODE_CUTOVER, result_account="1100")
    # Carried: income account has its own closing line. Closed: it does not.
    assert _move_net(carried, "xaf.account_8000") == pytest.approx(-150.0)
    assert _move_net(closed, "xaf.account_8000") == pytest.approx(0.0)
    # Both still balance.
    for result in (carried, closed):
        debit, credit = _move_balance(result)
        assert debit == pytest.approx(credit)


def test_dedup_is_transitive_across_kvk_and_vat(tmp_path: Path) -> None:
    """Records linked by a shared KvK or VAT merge, even transitively."""
    body = (
        "<customersSuppliers>"
        "<customerSupplier><custSupID>A</custSupID><custSupTp>C</custSupTp>"
        "<custSupName>A</custSupName><commerceNr>12345678</commerceNr>"
        "</customerSupplier>"
        "<customerSupplier><custSupID>B</custSupID><custSupTp>C</custSupTp>"
        "<custSupName>B</custSupName><commerceNr>12345678</commerceNr>"
        "<taxRegIdent>NL9</taxRegIdent></customerSupplier>"
        "<customerSupplier><custSupID>C</custSupID><custSupTp>S</custSupTp>"
        "<custSupName>C</custSupName><taxRegIdent>NL9</taxRegIdent>"
        "</customerSupplier></customersSuppliers>"
    )
    ledger = _read_xaf(tmp_path, body)
    merged = transform(ledger, prefix="xaf")
    # A shares KvK with B, B shares VAT with C -> one partner, both ranks.
    assert merged.partners.height == 1
    row = merged.partners.row(0, named=True)
    assert row["customer_rank"] == "1" and row["supplier_rank"] == "1"
    # With dedup off, all three survive.
    assert transform(ledger, prefix="xaf", dedup=False).partners.height == 3


def test_partner_country_is_staged(tmp_path: Path) -> None:
    """A partner country resolves to its Odoo external id rather than dropping."""
    body = (
        "<customersSuppliers><customerSupplier><custSupID>C1</custSupID>"
        "<custSupTp>C</custSupTp><custSupName>K</custSupName>"
        "<streetAddress><country>NL</country></streetAddress>"
        "</customerSupplier></customersSuppliers>"
    )
    result = transform(_read_xaf(tmp_path, body), prefix="xaf")
    assert result.partners.row(0, named=True)["country_id/id"] == "base.nl"


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
