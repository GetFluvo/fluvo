"""Tests for the streaming XAF reader (3.2 and 4.0) and the format registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from fluvo.auditfile import canonical, formats, read_auditfile
from fluvo.auditfile.formats import xaf
from fluvo.lib.core import InputError, SourceReadError

from .xaf_fixtures import generate_xaf

VERSIONS = ("3.1", "3.2", "4.0")


@pytest.mark.parametrize("version", VERSIONS)
def test_reads_counts_and_identity(version: str, tmp_path: Path) -> None:
    """The reader returns the exact entity counts and company identity."""
    path = tmp_path / "f.xaf"
    spec = generate_xaf(path, version=version)
    ledger = read_auditfile(str(path))
    ledger.validate()
    assert ledger.header.get_column("source_version").item(0) == version
    assert ledger.header.get_column("company_name").item(0) == spec.company_name
    assert ledger.header.get_column("company_id").item(0) == spec.company_id
    assert ledger.accounts.height == spec.n_accounts
    assert ledger.partners.height == spec.n_partners
    assert ledger.journals.height == spec.n_journals
    assert ledger.moves.height == spec.n_moves
    assert ledger.lines.height == spec.n_lines
    assert ledger.opening_lines.height == spec.n_opening_lines
    assert ledger.vat_codes.height == spec.n_vat_codes


@pytest.mark.parametrize("version", VERSIONS)
def test_reads_balanced_totals(version: str, tmp_path: Path) -> None:
    """Every generated move balances, so the ledger balances."""
    path = tmp_path / "f.xaf"
    spec = generate_xaf(path, version=version)
    summary = canonical.summarise(read_auditfile(str(path)))
    assert summary.balanced is True
    assert summary.unbalanced_moves == 0
    assert summary.total_debit == pytest.approx(spec.total_debit)


@pytest.mark.parametrize("version", VERSIONS)
def test_header_period_and_software(version: str, tmp_path: Path) -> None:
    """Reporting period, currency and producing software are read."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version=version)
    header = read_auditfile(str(path)).header
    assert str(header.get_column("fiscal_year").item(0)) == "2024"
    assert str(header.get_column("currency").item(0)) == "EUR"
    assert header.get_column("software").item(0) == "Fluvo Synthetic"
    assert str(header.get_column("date_start").item(0)) == "2024-01-01"


def test_lead_code_only_populated_for_4_0(tmp_path: Path) -> None:
    """4.0 RGScode fills lead_code; 3.2 leadCode is intentionally ignored."""
    p32 = tmp_path / "v32.xaf"
    p40 = tmp_path / "v40.xaf"
    generate_xaf(p32, version="3.2")
    generate_xaf(p40, version="4.0")
    a32 = read_auditfile(str(p32)).accounts
    a40 = read_auditfile(str(p40)).accounts
    assert a32.get_column("lead_code").null_count() == a32.height
    assert a40.filter(a40["account_id"] == "1300")["lead_code"].item(0) == "BVorHanDeb"


def test_bank_account_present_in_3_2_absent_in_4_0(tmp_path: Path) -> None:
    """XAF 3.2 carries a partner bank account; 4.0 dropped it from the party."""
    p32 = tmp_path / "v32.xaf"
    p40 = tmp_path / "v40.xaf"
    generate_xaf(p32, version="3.2")
    generate_xaf(p40, version="4.0")
    b32 = read_auditfile(str(p32)).partners.get_column("bank_account")
    b40 = read_auditfile(str(p40)).partners.get_column("bank_account")
    assert b32.null_count() == 0
    assert b40.null_count() == b40.len()


def test_partner_roles_mapped(tmp_path: Path) -> None:
    """Map the custSupTp letters to canonical roles (C/S/B)."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    partners = read_auditfile(str(path)).partners
    roles = dict(
        zip(
            partners.get_column("source_id").to_list(),
            partners.get_column("role").to_list(),
            strict=True,
        )
    )
    assert roles["C001"] == canonical.PARTNER_CUSTOMER
    assert roles["S001"] == canonical.PARTNER_SUPPLIER
    assert roles["B001"] == canonical.PARTNER_BOTH


def test_subledger_open_item_only_in_3_2(tmp_path: Path) -> None:
    """3.2's opening subledger yields a per-partner open item with an invoice ref."""
    p32 = tmp_path / "v32.xaf"
    p40 = tmp_path / "v40.xaf"
    generate_xaf(p32, version="3.2")
    generate_xaf(p40, version="4.0")
    o32 = read_auditfile(str(p32)).opening_lines
    o40 = read_auditfile(str(p40)).opening_lines
    sub = o32.filter(o32["source_partner_id"].is_not_null())
    assert sub.height == 1
    assert sub["invoice_ref"].item(0) == "2023-900"
    assert o40.filter(o40["source_partner_id"].is_not_null()).height == 0


def test_line_detail_captured(tmp_path: Path) -> None:
    """Lines carry the per-line account, amount, partner, doc ref and VAT code."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    lines = read_auditfile(str(path)).lines
    first = lines.row(0, named=True)
    assert first["account_id"] == "1300"
    assert first["debit"] == pytest.approx(101.0)
    assert first["source_partner_id"] == "C001"
    assert first["vat_code"] == "HOOG"
    assert first["line_number"] == 1
    assert str(first["effective_date"]) == "2024-06-30"


@pytest.mark.parametrize("version", VERSIONS)
def test_detect_recognises_xaf(version: str, tmp_path: Path) -> None:
    """The reader's detect() accepts an XAF file of either version."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version=version)
    assert xaf.READER.detect(str(path)) is True


def test_detect_rejects_non_xaf(tmp_path: Path) -> None:
    """detect() returns False for XML that is not an XAF audit file."""
    other = tmp_path / "other.xml"
    other.write_text("<root><child/></root>", encoding="utf-8")
    assert xaf.READER.detect(str(other)) is False
    missing = tmp_path / "nope.xml"
    assert xaf.READER.detect(str(missing)) is False


def test_registry_detects_and_names(tmp_path: Path) -> None:
    """The registry finds the XAF reader by sniffing and by name."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    reader = formats.detect(str(path))
    assert reader is not None and reader.format_name == "xaf"
    assert formats.by_name("xaf") is not None
    assert formats.by_name("nope") is None
    assert any(r.format_name == "xaf" for r in formats.readers())


def test_read_auditfile_forced_format(tmp_path: Path) -> None:
    """A forced --format reads without sniffing."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="3.2")
    ledger = read_auditfile(str(path), fmt="xaf")
    assert ledger.moves.height > 0


def test_read_auditfile_unknown_format(tmp_path: Path) -> None:
    """An unknown forced format is an input error."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="3.2")
    with pytest.raises(InputError, match="unknown audit-file format"):
        read_auditfile(str(path), fmt="saft")


def test_read_auditfile_unrecognised_file(tmp_path: Path) -> None:
    """A file no reader recognises is an input error."""
    other = tmp_path / "other.xml"
    other.write_text("<root/>", encoding="utf-8")
    with pytest.raises(InputError, match="could not recognise"):
        read_auditfile(str(other))


def test_malformed_xaf_is_source_read_error(tmp_path: Path) -> None:
    """A truncated XAF (valid root, broken body) raises SourceReadError."""
    path = tmp_path / "broken.xaf"
    path.write_text(
        '<?xml version="1.0"?>'
        '<auditfile xmlns="http://www.auditfiles.nl/XAF/3.2">'
        "<header><fiscalYear>2024",
        encoding="utf-8",
    )
    with pytest.raises(SourceReadError, match="malformed XAF"):
        xaf.READER.read(str(path))


def test_custsup_type_other_is_unknown(tmp_path: Path) -> None:
    """A 3.2 custSupTp of 'O' (other) maps to the UNKNOWN role."""
    path = tmp_path / "o.xaf"
    path.write_text(
        '<?xml version="1.0"?>'
        '<auditfile xmlns="http://www.auditfiles.nl/XAF/3.2"><header/>'
        "<company><companyName>X</companyName><customersSuppliers>"
        "<customerSupplier><custSupID>Z1</custSupID><custSupTp>O</custSupTp>"
        "<custSupName>Other</custSupName></customerSupplier>"
        "</customersSuppliers></company></auditfile>",
        encoding="utf-8",
    )
    partners = read_auditfile(str(path)).partners
    assert partners.get_column("role").item(0) == canonical.UNKNOWN


def test_register_adds_reader() -> None:
    """A reader registered in-process becomes available to the registry."""
    saved = list(formats._READERS)
    try:

        class _Dummy:
            format_name = "dummy"

            def detect(self, path: str) -> bool:
                return False

            def read(self, path: str) -> object:
                raise NotImplementedError

        formats.register(_Dummy())  # type: ignore[arg-type]
        assert formats.by_name("dummy") is not None
    finally:
        formats._READERS[:] = saved


def test_broken_plugin_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A faulty external format plugin is skipped, not fatal to the core."""
    saved = list(formats._READERS)
    saved_loaded = formats._external_loaded

    class _BadEntry:
        name = "broken"

        def load(self) -> object:
            raise RuntimeError("boom")

    try:
        monkeypatch.setattr(formats, "_external_loaded", False)
        monkeypatch.setattr(formats, "entry_points", lambda group: [_BadEntry()])
        names = [r.format_name for r in formats.readers()]
        assert "xaf" in names  # built-in survives the broken plugin
    finally:
        formats._READERS[:] = saved
        formats._external_loaded = saved_loaded


_NS32 = "http://www.auditfiles.nl/XAF/3.2"


def _write_xaf(path: Path, body: str, *, ns: str = _NS32) -> str:
    """Write a minimal XAF document wrapping ``body`` and return its path.

    Args:
        path: Where to write the file.
        body: The XML placed inside ``<company>…</company>``.
        ns: The root namespace (defaults to 3.2).

    Returns:
        str: The written path as a string.
    """
    path.write_text(
        f'<?xml version="1.0"?><auditfile xmlns="{ns}"><header/>'
        f"<company><companyName>X</companyName>{body}</company></auditfile>",
        encoding="utf-8",
    )
    return str(path)


def test_opening_subledger_resolves_control_account(tmp_path: Path) -> None:
    """A 3.2 obSbLine resolves its control account via obLineNr → obLine."""
    path = _write_xaf(
        tmp_path / "ob.xaf",
        "<generalLedger>"
        "<ledgerAccount><accID>1300</accID><accDesc>Deb</accDesc><accTp>B</accTp>"
        "</ledgerAccount></generalLedger>"
        "<openingBalance><opBalDate>2025-01-01</opBalDate>"
        "<obLine><nr>7</nr><accID>1300</accID><amnt>500.00</amnt><amntTp>D</amntTp>"
        "</obLine>"
        "<obSubledgers><obSubledger><sbType>CU</sbType>"
        "<obSbLine><obLineNr>7</obLineNr><custSupID>C1</custSupID>"
        "<amnt>500.00</amnt><amntTp>D</amntTp><invRef>INV-9</invRef></obSbLine>"
        "</obSubledger></obSubledgers></openingBalance>",
    )
    opening = read_auditfile(path).opening_lines
    sub = opening.filter(opening["source_partner_id"].is_not_null())
    assert sub.height == 1
    assert sub["account_id"].item(0) == "1300"
    assert sub["invoice_ref"].item(0) == "INV-9"


def test_trline_reads_invoice_and_match_refs(tmp_path: Path) -> None:
    """Invoice (invRef) and explicit match key (matchKeyID) read onto the line."""
    path = _write_xaf(
        tmp_path / "m.xaf",
        "<transactions><journal><jrnID>BNK</jrnID><jrnTp>B</jrnTp>"
        "<transaction><nr>1</nr><trDt>2025-03-01</trDt>"
        "<trLine><nr>1</nr><accID>1300</accID><amnt>50.00</amnt><amntTp>C</amntTp>"
        "<docRef>BANK-07</docRef><invRef>INV1</invRef><matchKeyID>M42</matchKeyID>"
        "</trLine></transaction></journal></transactions>",
    )
    line = read_auditfile(path).lines.row(0, named=True)
    assert line["doc_ref"] == "BANK-07"
    assert line["invoice_ref"] == "INV1"
    assert line["match_ref"] == "M42"


def test_duplicate_transaction_numbers_disambiguated(tmp_path: Path) -> None:
    """Two transactions sharing journal+number become two distinct moves."""
    one = (
        "<transaction><nr>1</nr><periodNumber>{p}</periodNumber>"
        "<trDt>2025-0{p}-05</trDt>"
        "<trLine><nr>1</nr><accID>1300</accID><amnt>{a}</amnt><amntTp>D</amntTp>"
        "</trLine>"
        "<trLine><nr>2</nr><accID>8000</accID><amnt>{a}</amnt><amntTp>C</amntTp>"
        "</trLine></transaction>"
    )
    path = _write_xaf(
        tmp_path / "dup.xaf",
        "<transactions><journal><jrnID>VK</jrnID><jrnTp>S</jrnTp>"
        + one.format(p=1, a="100.00")
        + one.format(p=2, a="200.00")
        + "</journal></transactions>",
    )
    ledger = read_auditfile(path)
    keys = ledger.moves.get_column("move_key").to_list()
    assert ledger.moves.height == 2
    assert len(set(keys)) == 2  # not merged into one move
    assert ledger.lines.height == 4


def test_unparseable_amount_recorded_not_dropped(tmp_path: Path) -> None:
    """A present-but-bad amount (e.g. '1,5') is recorded as a parse anomaly."""
    path = _write_xaf(
        tmp_path / "bad.xaf",
        "<transactions><journal><jrnID>BNK</jrnID><jrnTp>B</jrnTp>"
        "<transaction><nr>1</nr><trDt>2025-04-15</trDt>"
        "<trLine><nr>1</nr><accID>1100</accID><amnt>1,5</amnt><amntTp>D</amntTp>"
        "</trLine></transaction></journal></transactions>",
    )
    ledger = read_auditfile(path)
    anomalies = ledger.anomalies
    assert anomalies.height == 1
    assert anomalies["kind"].item(0) == "amount"
    assert anomalies["raw_value"].item(0) == "1,5"
    # The bad amount became 0.0 (not a phantom value), and was not dropped.
    assert ledger.lines["debit"].item(0) == pytest.approx(0.0)


def test_unsupported_xaf_version_rejected(tmp_path: Path) -> None:
    """An auditfile root in an unsupported namespace is refused, not mis-read."""
    path = tmp_path / "old.xaf"
    path.write_text(
        '<?xml version="1.0"?>'
        '<auditfile xmlns="http://www.auditfiles.nl/XAF/3.0"><header/>'
        "<company><companyName>X</companyName></company></auditfile>",
        encoding="utf-8",
    )
    # Detected as XAF (so this reader owns it) but refused with a clear version error.
    assert xaf.READER.detect(str(path)) is True
    with pytest.raises(SourceReadError, match="unsupported XAF version"):
        xaf.READER.read(str(path))


def test_xaf_3_1_is_accepted(tmp_path: Path) -> None:
    """XAF 3.1 shares 3.2's structure and is read, not refused."""
    path = tmp_path / "v31.xaf"
    spec = generate_xaf(path, version="3.1")
    ledger = read_auditfile(str(path))
    assert ledger.header.get_column("source_version").item(0) == "3.1"
    assert ledger.lines.height == spec.n_lines
    debit = float(ledger.lines.get_column("debit").sum())
    credit = float(ledger.lines.get_column("credit").sum())
    assert debit == pytest.approx(credit)


def test_control_totals_captured(tmp_path: Path) -> None:
    """The declared transactions-block control totals are read into the header."""
    path = _write_xaf(
        tmp_path / "ct.xaf",
        "<transactions><linesCount>2</linesCount>"
        "<totalDebit>100.00</totalDebit><totalCredit>100.00</totalCredit>"
        "<journal><jrnID>VK</jrnID><jrnTp>S</jrnTp>"
        "<transaction><nr>1</nr><trDt>2025-03-01</trDt>"
        "<trLine><nr>1</nr><accID>1300</accID><amnt>100.00</amnt><amntTp>D</amntTp>"
        "</trLine>"
        "<trLine><nr>2</nr><accID>8000</accID><amnt>100.00</amnt><amntTp>C</amntTp>"
        "</trLine></transaction></journal></transactions>",
    )
    header = read_auditfile(path).header
    assert header["declared_tx_count"].item(0) == 2
    assert header["declared_tx_debit"].item(0) == pytest.approx(100.0)


def test_per_line_currency_read(tmp_path: Path) -> None:
    """A trLine foreign-currency block's code is captured on the line."""
    path = _write_xaf(
        tmp_path / "fx.xaf",
        "<transactions><journal><jrnID>BNK</jrnID><jrnTp>B</jrnTp>"
        "<transaction><nr>1</nr><trDt>2025-03-01</trDt>"
        "<trLine><nr>1</nr><accID>1100</accID><amnt>100.00</amnt><amntTp>D</amntTp>"
        "<currency><curCode>USD</curCode><curAmnt>90.00</curAmnt></currency>"
        "</trLine></transaction></journal></transactions>",
    )
    line = read_auditfile(path).lines.row(0, named=True)
    assert line["currency"] == "USD"


def test_unknown_account_type_is_unknown(tmp_path: Path) -> None:
    """An account type outside B/P (e.g. 3.2 'M') maps to UNKNOWN."""
    path = tmp_path / "m.xaf"
    path.write_text(
        '<?xml version="1.0"?>'
        '<auditfile xmlns="http://www.auditfiles.nl/XAF/3.2"><header/>'
        "<company><companyName>X</companyName><generalLedger>"
        "<ledgerAccount><accID>3000</accID><accDesc>Mixed</accDesc>"
        "<accTp>M</accTp></ledgerAccount>"
        "</generalLedger></company></auditfile>",
        encoding="utf-8",
    )
    accounts = read_auditfile(str(path)).accounts
    assert accounts.get_column("kind").item(0) == canonical.UNKNOWN
