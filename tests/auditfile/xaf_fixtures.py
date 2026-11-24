"""Synthetic XAF (3.2 / 4.0) fixture generator for the test suite.

Everything here is **synthetic** — invented company, accounts, partners and
amounts. No client data ever enters the repository. The generator streams the
file to disk one transaction at a time, so it can emit a 50 MB+ file for the
performance test without itself using much memory.

The structure and namespaces match the official Belastingdienst XSDs closely
enough to exercise the reader: correct root namespaces per version, amounts on
``trLine`` children, ``leadCode`` in 3.2 vs ``RGScode`` in 4.0, and the 3.2-only
opening-balance subledger. Every generated transaction has two balancing lines,
so a generated file always reconciles.

:func:`generate_xaf` returns a :class:`XafFixtureSpec` describing exactly what it
wrote, so a test asserts the reader's output against known counts and totals.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from xml.sax.saxutils import escape

#: Root namespaces per version (verified against the official XSDs).
_NAMESPACE = {
    "3.2": "http://www.auditfiles.nl/XAF/3.2",
    "4.0": (
        "http://www.odb.belastingdienst.nl/Belastingdienst/BCPP/1.1/"
        "structures/XmlauditfileXAF_4.0"
    ),
}

#: The well-known accounts every fixture includes, as
#: ``(accID, accDesc, accTp, rgs_code)``. The receivable/payable pair lets the
#: transform exercise control-account handling.
_CORE_ACCOUNTS = (
    ("1100", "Bank", "B", "BLimBanRba"),
    ("1300", "Debiteuren", "B", "BVorHanDeb"),
    ("1600", "Crediteuren", "B", "BSchCreHac"),
    ("4000", "Kosten", "P", "WBedKanInd"),
    ("8000", "Omzet", "P", "WOmzNomGrp"),
)


@dataclass(frozen=True)
class XafFixtureSpec:
    """A description of a generated fixture, for assertions.

    Attributes:
        version: The XAF version written, ``"3.2"`` or ``"4.0"``.
        company_name: The synthetic company name.
        company_id: The synthetic Chamber-of-Commerce number.
        currency: The currency code (always ``"EUR"``).
        fiscal_year: The fiscal year written.
        n_accounts: Ledger accounts written.
        n_partners: Customer/supplier records written.
        n_journals: Journals written.
        n_moves: Transactions written.
        n_lines: Transaction lines written (``2 * n_moves``).
        n_opening_gl_lines: GL-level opening-balance lines.
        n_opening_subledger_lines: Per-partner opening items (3.2 only).
        n_vat_codes: VAT codes written.
        total_debit: Sum of all transaction-line debits.
        total_credit: Sum of all transaction-line credits.
        duplicate_kvk: The KvK number shared by the two dedup-test partners.
    """

    version: str
    company_name: str
    company_id: str
    currency: str
    fiscal_year: str
    n_accounts: int
    n_partners: int
    n_journals: int
    n_moves: int
    n_lines: int
    n_opening_gl_lines: int
    n_opening_subledger_lines: int
    n_vat_codes: int
    total_debit: float
    total_credit: float
    duplicate_kvk: str

    @property
    def n_opening_lines(self) -> int:
        """Total opening-balance lines (GL plus subledger).

        Returns:
            int: ``n_opening_gl_lines + n_opening_subledger_lines``.
        """
        return self.n_opening_gl_lines + self.n_opening_subledger_lines


def _tag(handle: TextIO, name: str, value: str) -> None:
    """Write a simple ``<name>value</name>`` element.

    Args:
        handle: The open text file to write to.
        name: The element name.
        value: The text content (escaped).
    """
    handle.write(f"<{name}>{escape(value)}</{name}>")


def _write_accounts(handle: TextIO, version: str, n_accounts: int) -> int:
    """Write the ``generalLedger`` block and return the account count.

    Args:
        handle: The open file.
        version: The XAF version.
        n_accounts: The desired number of accounts (at least the core set).

    Returns:
        int: The number of accounts written.
    """
    handle.write("<generalLedger>")
    written = 0
    for acc_id, desc, acc_tp, rgs in _CORE_ACCOUNTS:
        handle.write("<ledgerAccount>")
        _tag(handle, "accID", acc_id)
        _tag(handle, "accDesc", desc)
        _tag(handle, "accTp", acc_tp)
        if version == "4.0":
            _tag(handle, "RGScode", rgs)
        else:
            _tag(handle, "leadCode", rgs)  # 3.2: NOT an RGS code; reader ignores it
        handle.write("</ledgerAccount>")
        written += 1
    for i in range(len(_CORE_ACCOUNTS), max(n_accounts, len(_CORE_ACCOUNTS))):
        handle.write("<ledgerAccount>")
        _tag(handle, "accID", f"9{i:03d}")
        _tag(handle, "accDesc", f"Tussenrekening {i}")
        _tag(handle, "accTp", "B")
        handle.write("</ledgerAccount>")
        written += 1
    handle.write("</generalLedger>")
    return written


def _write_partners(handle: TextIO, version: str, kvk: str) -> int:
    """Write the ``customersSuppliers`` block and return the partner count.

    Includes two records sharing ``kvk`` (for the dedup test) plus a supplier
    and a both-roles partner.

    Args:
        handle: The open file.
        version: The XAF version.
        kvk: The KvK number shared by the first two partners.

    Returns:
        int: The number of partners written.
    """
    records = [
        ("C001", "C", "Jansen Handel BV", kvk, "NL001111111B01"),
        ("C002", "C", "Jansen Handel B.V.", kvk, "NL001111111B01"),
        ("S001", "S", "Leverancier Pietersen", "87654321", "NL002222222B01"),
        ("B001", "B", "De Vries Diensten", "11223344", "NL003333333B01"),
    ]
    handle.write("<customersSuppliers>")
    for cust_id, tp, name, commerce, vat in records:
        handle.write("<customerSupplier>")
        _tag(handle, "custSupID", cust_id)
        _tag(handle, "custSupTp", tp)
        _tag(handle, "custSupName", name)
        _tag(handle, "taxRegistrationCountry", "NL")
        _tag(handle, "taxRegIdent", vat)
        _tag(handle, "commerceNr", commerce)
        handle.write("<streetAddress>")
        _tag(handle, "streetname", "Dorpsstraat")
        _tag(handle, "houseNumber", "1")
        _tag(handle, "postalCode", "1000 AA")
        _tag(handle, "city", "Amsterdam")
        _tag(handle, "country", "NL")
        handle.write("</streetAddress>")
        if version == "3.2":
            handle.write("<bankAccount>")
            _tag(handle, "bankAccNr", "NL00BANK0123456789")
            handle.write("</bankAccount>")
        handle.write("</customerSupplier>")
    handle.write("</customersSuppliers>")
    return len(records)


def _write_vat_codes(handle: TextIO) -> int:
    """Write the ``vatCodes`` block and return the code count.

    Args:
        handle: The open file.

    Returns:
        int: The number of VAT codes written.
    """
    handle.write("<vatCodes>")
    for vat_id, desc, acc in (("HOOG", "BTW 21%", "1520"), ("LAAG", "BTW 9%", "1521")):
        handle.write("<vatCode>")
        _tag(handle, "vatID", vat_id)
        _tag(handle, "vatDesc", desc)
        _tag(handle, "vatToPayAccID", acc)
        handle.write("</vatCode>")
    handle.write("</vatCodes>")
    return 2


def _write_opening(handle: TextIO, version: str) -> tuple[int, int]:
    """Write the ``openingBalance`` block.

    Args:
        handle: The open file.
        version: The XAF version.

    Returns:
        tuple[int, int]: ``(gl_lines, subledger_lines)`` written.
    """
    handle.write("<openingBalance>")
    if version == "3.2":
        _tag(handle, "opBalDate", "2024-01-01")
    _tag(handle, "linesCount", "2")
    _tag(handle, "totalDebit", "5000.00")
    _tag(handle, "totalCredit", "5000.00")
    gl = (("1100", "5000.00", "D"), ("0500", "5000.00", "C"))
    for acc, amnt, tp in gl:
        handle.write("<obLine>")
        _tag(handle, "nr", acc)
        _tag(handle, "accID", acc)
        _tag(handle, "amnt", amnt)
        _tag(handle, "amntTp", tp)
        handle.write("</obLine>")
    sub_lines = 0
    if version == "3.2":
        handle.write("<obSubledgers><obSubledger>")
        _tag(handle, "sbType", "CU")
        for cust, amnt, inv in (("C001", "1210.00", "2023-900"),):
            handle.write("<obSbLine>")
            _tag(handle, "custSupID", cust)
            _tag(handle, "amnt", amnt)
            _tag(handle, "amntTp", "D")
            _tag(handle, "invRef", inv)
            _tag(handle, "invDt", "2023-12-15")
            handle.write("</obSbLine>")
            sub_lines += 1
        handle.write("</obSubledger></obSubledgers>")
    handle.write("</openingBalance>")
    return len(gl), sub_lines


def _write_transactions(
    handle: TextIO, n_journals: int, tx_per_journal: int
) -> tuple[int, int, float]:
    """Write the ``transactions`` block of balancing two-line moves.

    Args:
        handle: The open file.
        n_journals: Number of journals to write.
        tx_per_journal: Transactions per journal.

    Returns:
        tuple[int, int, float]: ``(n_moves, n_lines, total_each_side)`` — the
        move count, line count, and the per-side total (debit == credit).
    """
    journal_types = ("S", "P", "B", "C", "M")
    n_moves = 0
    total = 0.0
    handle.write("<transactions>")
    for j in range(n_journals):
        jrn_tp = journal_types[j % len(journal_types)]
        handle.write("<journal>")
        _tag(handle, "jrnID", f"JRN{j:03d}")
        _tag(handle, "desc", f"Dagboek {j}")
        _tag(handle, "jrnTp", jrn_tp)
        for t in range(tx_per_journal):
            n_moves += 1
            amount = 100.0 + n_moves
            total += amount
            handle.write("<transaction>")
            _tag(handle, "nr", f"{j:03d}-{t:04d}")
            _tag(handle, "desc", f"Boeking {n_moves}")
            _tag(handle, "periodNumber", str((t % 12) + 1))
            _tag(handle, "trDt", "2024-06-30")
            _write_trline(handle, 1, "1300", f"{amount:.2f}", "D", "C001")
            _write_trline(handle, 2, "8000", f"{amount:.2f}", "C", None)
            handle.write("</transaction>")
        handle.write("</journal>")
    handle.write("</transactions>")
    return n_moves, n_moves * 2, total


def _write_trline(
    handle: TextIO,
    nr: int,
    acc_id: str,
    amnt: str,
    amnt_tp: str,
    cust_sup_id: str | None,
) -> None:
    """Write one ``trLine`` with the amount on the line (per the XSD).

    Args:
        handle: The open file.
        nr: The line number.
        acc_id: The GL account id.
        amnt: The amount, as a formatted string.
        amnt_tp: The amount type, ``"D"`` or ``"C"``.
        cust_sup_id: A partner id to attach, or None.
    """
    handle.write("<trLine>")
    _tag(handle, "nr", str(nr))
    _tag(handle, "accID", acc_id)
    _tag(handle, "docRef", f"DOC{nr}")
    _tag(handle, "effDate", "2024-06-30")
    _tag(handle, "desc", "Regel")
    _tag(handle, "amnt", amnt)
    _tag(handle, "amntTp", amnt_tp)
    if cust_sup_id is not None:
        _tag(handle, "custSupID", cust_sup_id)
    handle.write("<vat>")
    _tag(handle, "vatID", "HOOG")
    _tag(handle, "vatPerc", "21.00")
    _tag(handle, "vatAmnt", "0.00")
    _tag(handle, "vatAmntTp", amnt_tp)
    handle.write("</vat>")
    handle.write("</trLine>")


def generate_xaf(
    path: Path | str,
    *,
    version: str = "4.0",
    n_accounts: int = 8,
    n_journals: int = 2,
    tx_per_journal: int = 3,
) -> XafFixtureSpec:
    """Write a synthetic XAF file and return a spec describing it.

    Args:
        path: Where to write the file.
        version: The XAF version to emit, ``"3.2"`` or ``"4.0"``.
        n_accounts: How many ledger accounts to write (at least the core set).
        n_journals: How many journals to write.
        tx_per_journal: Transactions per journal (each is two balancing lines).

    Returns:
        XafFixtureSpec: Exactly what was written, for assertions.

    Raises:
        ValueError: If ``version`` is not a supported XAF version.
    """
    if version not in _NAMESPACE:
        raise ValueError(f"unsupported XAF version {version!r}")
    company_name = "Testbedrijf Synthetisch BV"
    company_id = "80012345"
    duplicate_kvk = "81234567"
    with Path(path).open("w", encoding="utf-8") as handle:
        handle.write('<?xml version="1.0" encoding="UTF-8"?>')
        handle.write(f'<auditfile xmlns="{_NAMESPACE[version]}">')
        handle.write("<header>")
        _tag(handle, "fiscalYear", "2024")
        _tag(handle, "startDate", "2024-01-01")
        _tag(handle, "endDate", "2024-12-31")
        _tag(handle, "curCode", "EUR")
        _tag(handle, "dateCreated", "2025-01-15")
        _tag(handle, "softwareDesc", "Fluvo Synthetic")
        _tag(handle, "softwareVersion", "1.0")
        handle.write("</header>")
        handle.write("<company>")
        _tag(handle, "companyName", company_name)
        _tag(handle, "Commercenr" if version == "4.0" else "companyIdent", company_id)
        _tag(handle, "taxRegistrationCountry", "NL")
        _tag(handle, "taxRegIdent", "NL800123450B01")
        n_partners = _write_partners(handle, version, duplicate_kvk)
        accounts = _write_accounts(handle, version, n_accounts)
        n_vat = _write_vat_codes(handle)
        gl_lines, sub_lines = _write_opening(handle, version)
        n_moves, n_lines, total = _write_transactions(
            handle, n_journals, tx_per_journal
        )
        handle.write("</company>")
        handle.write("</auditfile>")
    return XafFixtureSpec(
        version=version,
        company_name=company_name,
        company_id=company_id,
        currency="EUR",
        fiscal_year="2024",
        n_accounts=accounts,
        n_partners=n_partners,
        n_journals=n_journals,
        n_moves=n_moves,
        n_lines=n_lines,
        n_opening_gl_lines=gl_lines,
        n_opening_subledger_lines=sub_lines,
        n_vat_codes=n_vat,
        total_debit=total,
        total_credit=total,
        duplicate_kvk=duplicate_kvk,
    )
