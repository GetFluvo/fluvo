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


def test_cutover_open_items_become_invoices(tmp_path: Path) -> None:
    """Cutover stages each open item as a draft invoice on the clearing account."""
    result = transform(_ledger(tmp_path, "3.2"), prefix="xaf", mode=MODE_CUTOVER)
    assert result.open_items.height >= 1
    # The moves frame holds only the opening move; AR control is not on it, and
    # it carries no partner lines (the open items are invoices now).
    move_ids = {i for i in result.moves.get_column("id").to_list() if i}
    assert move_ids == {"xaf.move__opening"}
    assert all(not p for p in result.moves["line_ids/partner_id/id"].to_list())
    # One invoice per open item, each on the clearing account, with a partner.
    assert result.open_invoices.height == result.open_items.height
    inv = result.open_invoices.row(0, named=True)
    assert inv["move_type"] in ("out_invoice", "out_refund", "in_invoice", "in_refund")
    assert inv["partner_id/id"].startswith("xaf.partner_")
    assert inv["invoice_line_ids/account_id/id"] == result.clearing_xmlid
    assert inv["id"].startswith("xaf.openinv_")


def test_unknown_mode_raises(tmp_path: Path) -> None:
    """An unsupported mode is an input error."""
    with pytest.raises(InputError, match="unknown mode"):
        transform(_ledger(tmp_path), mode="nope")


def test_cutover_is_closing_position_and_balances(tmp_path: Path) -> None:
    """Cutover emits the closing position, balanced, carrying prior open items."""
    result = transform(_ledger(tmp_path, "3.2"), prefix="xaf", mode=MODE_CUTOVER)
    debit, credit = _move_balance(result)
    assert debit == pytest.approx(credit)  # opening move balances by construction
    refs = result.open_items.get_column("doc_ref").to_list()
    assert "2023-900" in refs  # the prior-year opening invoice is carried over
    # The AR control (1300) is on the open-item invoices, not the opening move;
    # the opening move carries the clearing counter = sum of open-item nets
    # (prior 1210 + the year's debits 101..106 = 621).
    assert _move_net(result, "xaf.account_1300") == pytest.approx(0.0)
    assert _move_net(result, result.clearing_xmlid) == pytest.approx(1210.0 + 621.0)


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


def test_partner_banks_are_staged(tmp_path: Path) -> None:
    """A 3.2 partner's bank account becomes a res.partner.bank row."""
    result = transform(_ledger(tmp_path, "3.2"), prefix="xaf")
    assert result.partner_banks.height >= 1
    row = result.partner_banks.row(0, named=True)
    assert row["partner_id/id"].startswith("xaf.partner_")
    assert row["acc_number"].startswith("NL00BANK")
    assert row["id"].startswith("xaf.partner_bank_")


def test_partner_banks_empty_for_4_0(tmp_path: Path) -> None:
    """4.0 carries no party bank account, so no bank rows are produced."""
    assert transform(_ledger(tmp_path, "4.0"), prefix="xaf").partner_banks.height == 0


def test_account_match_references_existing(tmp_path: Path) -> None:
    """A matched account is referenced, not created, and reported."""
    ledger = _read_xaf(tmp_path, _RECV_BANK_BODY)
    result = transform(
        ledger, prefix="xaf", account_matches={"1300": "l10n_nl.recv_existing"}
    )
    # 1300 is not in the accounts-to-create frame...
    assert "1300" not in result.accounts.get_column("code").to_list()
    # ...and every 1300 line references the existing external id.
    refs = {
        r["line_ids/account_id/id"]
        for r in result.moves.to_dicts()
        if r["line_ids/account_id/id"]
    }
    assert "l10n_nl.recv_existing" in refs
    assert "xaf.account_1300" not in refs
    assert result.matched_accounts == ("1300",)
    # The account map pins the target so the match is reviewable.
    row = next(r for r in result.account_map.to_dicts() if r["account_id"] == "1300")
    assert row["target_xmlid"] == "l10n_nl.recv_existing"


def test_open_item_invoice_fields(tmp_path: Path) -> None:
    """An open item becomes a draft invoice with its number in ref and residual.

    The source number stays in ``ref``; the move ``name`` is left to Odoo's
    sequence so the cutover date cannot clash with a historical number's year.
    """
    result = transform(_ledger(tmp_path, "3.2"), prefix="xaf", mode=MODE_CUTOVER)
    inv = next(r for r in result.open_invoices.to_dicts() if r["ref"] == "2023-900")
    assert inv["move_type"] == "out_invoice"
    assert inv["name"] == ""  # Odoo numbers the move; the source number is in ref
    assert inv["partner_id/id"] == "xaf.partner_C001"
    assert inv["invoice_date"] == "2023-12-15"
    assert inv["invoice_date_due"] == "2024-01-14"  # invoice date + 30 days
    assert inv["invoice_line_ids/price_unit"] == "1210.00"
    assert inv["invoice_line_ids/account_id/id"] == result.clearing_xmlid
    assert inv["invoice_line_ids/tax_ids/id"] == ""  # no tax
    # Customer invoice: the number is also the payment communication, so
    # reminders and bank matching keep recognising it after migration.
    assert inv["payment_reference"] == "2023-900"


def test_due_dates_are_derived_and_counted(tmp_path: Path) -> None:
    """XAF carries no due date, so every invoice's due date is derived."""
    result = transform(
        _ledger(tmp_path, "3.2"), mode=MODE_CUTOVER, default_payment_days=14
    )
    assert result.derived_due_dates == result.open_invoices.height
    inv = next(r for r in result.open_invoices.to_dicts() if r["ref"] == "2023-900")
    assert inv["invoice_date_due"] == "2023-12-29"  # +14 days


def test_partner_receivable_property_is_set(tmp_path: Path) -> None:
    """Cutover pins the partner's receivable property to the source control.

    The property lives in its own update frame, only for partners with a
    receivable open item — never on the main partner frame, so no other
    partner's company default is overwritten.
    """
    result = transform(_ledger(tmp_path, "3.2"), prefix="xaf", mode=MODE_CUTOVER)
    # It is not a column on the main partner frame.
    assert "property_account_receivable_id/id" not in result.partners.columns
    row = next(
        r for r in result.partner_receivable.to_dicts() if r["id"] == "xaf.partner_C001"
    )
    assert row["property_account_receivable_id/id"] == "xaf.account_1300"


def test_partner_property_only_for_open_item_partners(tmp_path: Path) -> None:
    """Only partners carrying an open item appear in the property update frames.

    So a migrated partner with no open item keeps its company default, rather
    than having it cleared by a blank cell.
    """
    result = transform(_ledger(tmp_path, "3.2"), prefix="xaf", mode=MODE_CUTOVER)
    recv_ids = set(result.partner_receivable.get_column("id").to_list())
    # The 3.2 fixture's only open item is C001's; S001/B001 must not be touched.
    assert recv_ids == {"xaf.partner_C001"}
    assert "xaf.partner_S001" not in recv_ids
    # full_history never writes a property at all.
    hist = transform(_ledger(tmp_path, "3.2"), prefix="xaf")
    assert hist.partner_receivable.height == 0
    assert hist.partner_payable.height == 0


def test_matched_control_equal_to_company_default_is_not_staged(
    tmp_path: Path,
) -> None:
    """A matched control equal to the target's default is left to Odoo.

    When ``--match-accounts`` finds the source control account resolves to the
    very account that is already the company's default receivable, forcing the
    partner property is redundant, so the partner drops out of the frame.
    """
    ledger = _ledger(tmp_path, "3.2")
    # Baseline: C001's receivable control resolves to xaf.account_1300.
    baseline = transform(ledger, prefix="xaf", mode=MODE_CUTOVER)
    assert baseline.partner_receivable.height == 1
    # Tell the transform that xaf.account_1300 *is* the company default.
    result = transform(
        ledger,
        prefix="xaf",
        mode=MODE_CUTOVER,
        default_receivable="xaf.account_1300",
    )
    assert result.partner_receivable.height == 0
    # A non-matching default still forces the property.
    other = transform(
        ledger,
        prefix="xaf",
        mode=MODE_CUTOVER,
        default_receivable="xaf.account_9999",
    )
    assert other.partner_receivable.height == 1


def test_clearing_account_is_staged(tmp_path: Path) -> None:
    """The clearing account is created when it is not matched to an existing one."""
    result = transform(_ledger(tmp_path, "3.2"), prefix="xaf", mode=MODE_CUTOVER)
    assert result.clearing_account == "OPENCL"
    assert "OPENCL" in result.accounts.get_column("code").to_list()


_UNALLOC_BODY = (
    "<generalLedger>"
    "<ledgerAccount><accID>1300</accID><accDesc>Deb</accDesc><accTp>B</accTp>"
    "</ledgerAccount>"
    "<ledgerAccount><accID>1100</accID><accDesc>Bank</accDesc><accTp>B</accTp>"
    "</ledgerAccount></generalLedger>"
    "<customersSuppliers><customerSupplier><custSupID>C1</custSupID>"
    "<custSupTp>C</custSupTp><custSupName>Klant</custSupName></customerSupplier>"
    "</customersSuppliers>"
    "<transactions><journal><jrnID>BNK</jrnID><jrnTp>B</jrnTp>"
    "<transaction><nr>1</nr><trDt>2025-04-01</trDt>"
    "<trLine><nr>1</nr><accID>1100</accID><amnt>100.00</amnt><amntTp>D</amntTp>"
    "<effDate>2025-04-01</effDate><docRef>PMT1</docRef></trLine>"
    "<trLine><nr>2</nr><accID>1300</accID><amnt>100.00</amnt><amntTp>C</amntTp>"
    "<effDate>2025-04-01</effDate><invRef>UNALLOC</invRef><custSupID>C1</custSupID>"
    "</trLine></transaction></journal></transactions>"
)


def test_reversed_open_item_is_a_refund(tmp_path: Path) -> None:
    """An unallocated receipt (AR credit) is staged as a refund and flagged."""
    result = transform(_read_xaf(tmp_path, _UNALLOC_BODY), mode=MODE_CUTOVER)
    inv = next(r for r in result.open_invoices.to_dicts() if r["ref"] == "UNALLOC")
    assert inv["move_type"] == "out_refund"
    assert inv["invoice_line_ids/price_unit"] == "100.00"
    assert inv["payment_reference"] == "UNALLOC"  # customer refund carries it too
    assert result.refund_items  # flagged for review


def test_open_invoice_has_accounting_date_and_journal(tmp_path: Path) -> None:
    """An open-item invoice carries the cutover date and a dedicated journal."""
    result = transform(_ledger(tmp_path, "3.2"), prefix="xaf", mode=MODE_CUTOVER)
    inv = next(r for r in result.open_invoices.to_dicts() if r["ref"] == "2023-900")
    # The accounting date is the period end (the cutover), not the invoice date.
    assert inv["date"] == "2024-12-31"
    assert inv["invoice_date"] == "2023-12-15"
    # Sale invoices post to the staged sale journal.
    assert inv["journal_id/id"] == "xaf.journal__opening_sale"
    journal_ids = result.journals.get_column("id").to_list()
    assert "xaf.journal__opening_sale" in journal_ids
    assert "xaf.journal__opening_purchase" in journal_ids


_PAYABLE_BODY = (
    "<generalLedger>"
    "<ledgerAccount><accID>1600</accID><accDesc>Cred</accDesc><accTp>B</accTp>"
    "</ledgerAccount>"
    "<ledgerAccount><accID>4000</accID><accDesc>Kosten</accDesc><accTp>P</accTp>"
    "</ledgerAccount></generalLedger>"
    "<customersSuppliers><customerSupplier><custSupID>S1</custSupID>"
    "<custSupTp>S</custSupTp><custSupName>Lev</custSupName></customerSupplier>"
    "</customersSuppliers>"
    "<transactions><journal><jrnID>INK</jrnID><jrnTp>P</jrnTp>"
    "<transaction><nr>1</nr><trDt>2025-03-01</trDt>"
    "<trLine><nr>1</nr><accID>4000</accID><amnt>200.00</amnt><amntTp>D</amntTp>"
    "<effDate>2025-03-01</effDate><docRef>BILL-77</docRef></trLine>"
    "<trLine><nr>2</nr><accID>1600</accID><amnt>200.00</amnt><amntTp>C</amntTp>"
    "<effDate>2025-03-01</effDate><invRef>BILL-77</invRef><custSupID>S1</custSupID>"
    "</trLine></transaction></journal></transactions>"
)


def test_vendor_bill_name_blank_supplier_ref_kept(tmp_path: Path) -> None:
    """A vendor bill keeps the supplier doc in ref and lets Odoo number it."""
    result = transform(_read_xaf(tmp_path, _PAYABLE_BODY), mode=MODE_CUTOVER)
    inv = next(r for r in result.open_invoices.to_dicts() if r["ref"] == "BILL-77")
    assert inv["move_type"] == "in_invoice"
    assert inv["name"] == ""  # Odoo numbers the bill; the supplier ref is not its name
    assert inv["ref"] == "BILL-77"
    # A bill's payment_reference is our outgoing payment's, not the supplier's
    # number, so it is left empty — the supplier doc stays in ref only.
    assert inv["payment_reference"] == ""
    assert inv["journal_id/id"] == "xaf.journal__opening_purchase"
    # The payable property is set, not the receivable one.
    assert result.partner_payable.height == 1
    assert result.partner_receivable.height == 0


def test_matched_control_type_is_checked(tmp_path: Path) -> None:
    """A receivable matched onto a non-reconcilable target is a control error."""
    from fluvo.auditfile.match import AccountMatch

    ledger = _read_xaf(tmp_path, _RECV_BANK_BODY)
    bad = AccountMatch(
        code="1300",
        xmlid="l10n_nl.a_recv",
        name="Some current asset",
        account_type="asset_current",  # wrong type, and...
        reconcile=False,  # ...not reconcilable
    )
    result = transform(
        ledger,
        prefix="xaf",
        account_matches={"1300": "l10n_nl.a_recv"},
        match_meta={"1300": bad},
    )
    assert result.match_control_errors == ("1300",)
    row = next(
        r for r in result.account_matches.to_dicts() if r["account_id"] == "1300"
    )
    assert row["status"] == "control_mismatch"
    assert row["target_account_type"] == "asset_current"


def test_matched_type_mismatch_is_flagged(tmp_path: Path) -> None:
    """A non-control account matched onto a different target type warns."""
    from fluvo.auditfile.match import AccountMatch

    ledger = _read_xaf(tmp_path, _RECV_BANK_BODY)
    meta = {
        "8000": AccountMatch("8000", "l10n_nl.sales", "Sales", "expense", False),
    }
    result = transform(
        ledger,
        prefix="xaf",
        account_matches={"8000": "l10n_nl.sales"},
        match_meta=meta,
    )
    assert result.match_type_mismatches == ("8000",)
    assert result.match_control_errors == ()


def test_match_collision_is_flagged(tmp_path: Path) -> None:
    """A code present in the target but unmatchable must not be re-created."""
    ledger = _read_xaf(tmp_path, _RECV_BANK_BODY)
    result = transform(
        ledger,
        prefix="xaf",
        match_collisions={"1300": "ambiguous"},
    )
    assert result.collision_accounts == ("1300",)


def test_open_sale_journal_override(tmp_path: Path) -> None:
    """A source sale journal can be reused instead of staging a new one."""
    result = transform(
        _ledger(tmp_path, "3.2"),
        prefix="xaf",
        mode=MODE_CUTOVER,
        open_sale_journal="VRK",
    )
    inv = next(r for r in result.open_invoices.to_dicts() if r["ref"] == "2023-900")
    assert inv["journal_id/id"] == "xaf.journal_VRK"
    # The dedicated sale journal is not staged when overridden...
    assert "xaf.journal__opening_sale" not in result.journals.get_column("id").to_list()
    # ...but the purchase one still is (not overridden).
    assert "xaf.journal__opening_purchase" in result.journals.get_column("id").to_list()


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
