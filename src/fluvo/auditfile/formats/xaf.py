"""Streaming reader for the Dutch XAF audit file (versions 3.2 and 4.0).

XAF (*XML Auditfile Financieel*) is the Dutch tax authority's financial audit
file and the direct ancestor of the OECD SAF-T standard. This reader turns a
3.2 or 4.0 file into a :class:`~fluvo.auditfile.canonical.CanonicalLedger`.

**Memory safety.** Active-company files routinely exceed 100 MB, so the reader
streams with :func:`lxml.etree.iterparse`, asks it (via the ``tag`` filter) to
fire only for the record elements it cares about — skipping every leaf event —
and clears each transaction and journal subtree as it goes. Accumulated rows are
flushed to compact columnar frames in chunks, so Python never holds the whole
file as objects. Peak memory is a chunk plus the final frames, not the file.

**Version detection.** The schema version comes from the root element's
namespace, verified against the official Belastingdienst XSDs:

* 3.2 — ``http://www.auditfiles.nl/XAF/3.2`` (note the plural *auditfiles*).
* 4.0 — ``http://www.odb.belastingdienst.nl/Belastingdienst/BCPP/1.1/structures/XmlauditfileXAF_4.0``.

Element names are matched by *local* name (namespace stripped), so one code path
serves both versions; the genuine 3.2/4.0 differences (``leadCode`` vs
``RGScode``, amounts that moved off ``transaction`` onto ``trLine``, the 3.2-only
opening-balance subledger) are handled explicitly below.

Several points where a widely-circulated draft spec is wrong, confirmed against
the XSDs: the amounts live on ``trLine`` children, never reliably on the
``transaction``; the journal-type letters are S=sales, P=purchase, B=bank,
C=cash, M=memorial (the draft's "Z=sale" is wrong — Z is "other"); and XAF
carries no fixed-asset register in either version.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl
from lxml import etree

from ..canonical import (
    ACCOUNT_BALANCE,
    ACCOUNT_PROFIT_LOSS,
    JOURNAL_BANK,
    JOURNAL_CASH,
    JOURNAL_GENERAL,
    JOURNAL_OPENING,
    JOURNAL_PURCHASE,
    JOURNAL_SALE,
    PARTNER_BOTH,
    PARTNER_CUSTOMER,
    PARTNER_SUPPLIER,
    SCHEMAS,
    UNKNOWN,
    CanonicalLedger,
)

#: Namespace fragments that identify an XAF file and its version.
_NS_32 = "auditfiles.nl/XAF/3.2"
_NS_40 = "XmlauditfileXAF_4.0"

#: ``accTp`` (account type) -> canonical account kind. 3.2 also has ``M``
#: (mixed); it is left UNKNOWN so the transform's heuristic decides.
_ACCOUNT_KIND = {"B": ACCOUNT_BALANCE, "P": ACCOUNT_PROFIT_LOSS}

#: ``custSupTp`` -> canonical partner role. 3.2's ``O`` (other) is left UNKNOWN.
_PARTNER_ROLE = {"C": PARTNER_CUSTOMER, "S": PARTNER_SUPPLIER, "B": PARTNER_BOTH}

#: ``jrnTp`` (journal type) -> canonical journal kind. Verified against the XSD
#: enum: S=sales, P=purchase, B=bank, C=cash, M=memorial, O=opening, and
#: G/T/Y/Z (goods/production/payroll/other) fold into the general ledger.
_JOURNAL_KIND = {
    "S": JOURNAL_SALE,
    "P": JOURNAL_PURCHASE,
    "B": JOURNAL_BANK,
    "C": JOURNAL_CASH,
    "M": JOURNAL_GENERAL,
    "O": JOURNAL_OPENING,
    "G": JOURNAL_GENERAL,
    "T": JOURNAL_GENERAL,
    "Y": JOURNAL_GENERAL,
    "Z": JOURNAL_GENERAL,
}

#: Direct children of ``streetAddress`` combined into the ``street`` column.
_ADDRESS_STREET = ("streetname", "houseNumber", "houseNumberAdd")

#: Company-level identity fields (read from ``company``'s direct children, so a
#: customer's ``taxRegIdent`` is never mistaken for the company's).
_COMPANY_IDENT = frozenset({"companyName", "companyIdent", "Commercenr", "taxRegIdent"})

#: Record elements the parser asks ``iterparse`` to fire for; everything else
#: (every leaf) is skipped. ``{*}`` matches the element in any namespace.
_WANTED_TAGS = tuple(
    f"{{*}}{name}"
    for name in (
        "header",
        "ledgerAccount",
        "customerSupplier",
        "vatCode",
        "obLine",
        "obSbLine",
        "journal",
        "transaction",
    )
)

#: Rows buffered per frame before flushing to a columnar chunk.
_CHUNK_ROWS = 50_000


def _local(tag: object) -> str:
    """Return an element tag's local name (namespace stripped).

    Args:
        tag: An ``lxml`` element tag; non-string for comments/PIs.

    Returns:
        str: The local name, or ``""`` for a non-element node.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: object) -> str:
    """Return an element tag's namespace URI, or ``""`` when unqualified.

    Args:
        tag: An ``lxml`` element tag.

    Returns:
        str: The namespace URI portion of a Clark-notation tag.
    """
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def _version_of(tag: object) -> str | None:
    """Map a root-element tag to an XAF version, or None if not XAF.

    Args:
        tag: The root element's tag.

    Returns:
        str | None: ``"3.2"``, ``"4.0"``, or None when the element is not an
        XAF ``auditfile`` root.
    """
    if _local(tag) != "auditfile":
        return None
    ns = _namespace(tag)
    if _NS_32 in ns:
        return "3.2"
    if _NS_40 in ns or "belastingdienst" in ns.lower():
        return "4.0"
    # An auditfile root in an unexpected namespace is still almost certainly XAF
    # (the local structure is identical); default to the current version.
    return "4.0"


def _txt(elem: etree._Element) -> str | None:
    """Return an element's stripped text, or None when empty.

    Args:
        elem: The element to read.

    Returns:
        str | None: The stripped text, or None when blank.
    """
    text = elem.text
    if text is not None:
        stripped = str(text).strip()
        if stripped:
            return stripped
    return None


def _text(elem: etree._Element | None, name: str) -> str | None:
    """Return the stripped text of the first direct child named ``name``.

    Args:
        elem: The parent element, or None.
        name: The child's local name.

    Returns:
        str | None: The child's text, or None when absent or blank.
    """
    if elem is None:
        return None
    for child in elem:
        if _local(child.tag) == name:
            return _txt(child)
    return None


def _child(elem: etree._Element, name: str) -> etree._Element | None:
    """Return the first direct child element with local name ``name``.

    Args:
        elem: The parent element.
        name: The child's local name.

    Returns:
        etree._Element | None: The child element, or None when absent.
    """
    for child in elem:
        if _local(child.tag) == name:
            return child
    return None


def _money(text: str | None) -> float | None:
    """Parse an XAF amount into a float.

    Args:
        text: The raw amount text, e.g. ``"1234.56"``.

    Returns:
        float | None: The value, or None when ``text`` is missing or unparseable.
    """
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _date(text: str | None) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` date, leniently.

    Args:
        text: The raw date text.

    Returns:
        date | None: The parsed date, or None when missing or malformed (a bad
        cell is surfaced by preflight, not by crashing the whole read).
    """
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _split_amount(amnt: str | None, amnt_tp: str | None) -> tuple[float, float]:
    """Split an amount + D/C type into (debit, credit).

    Args:
        amnt: The amount text.
        amnt_tp: The amount type, ``"D"`` (debit) or ``"C"`` (credit).

    Returns:
        tuple[float, float]: ``(debit, credit)``; the unused side is 0.0.
    """
    value = _money(amnt) or 0.0
    if (amnt_tp or "").upper() == "C":
        return 0.0, value
    return value, 0.0


class _Acc:
    """A chunk-flushing, column-oriented row accumulator for one frame.

    Buffers rows as parallel per-column Python lists and flushes them to a
    compact :class:`polars.DataFrame` chunk every :data:`_CHUNK_ROWS` rows, so
    peak Python-object memory stays bounded by one chunk regardless of file
    size. :meth:`build` concatenates the chunks into the final typed frame.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = schema
        self._cols: dict[str, list[Any]] = {name: [] for name in schema}
        self._chunks: list[pl.DataFrame] = []
        self._buffered = 0

    def add(self, **values: Any) -> None:
        """Append one row; columns absent from ``values`` get None.

        Args:
            **values: Column values for this row.
        """
        for name, column in self._cols.items():
            column.append(values.get(name))
        self._buffered += 1
        if self._buffered >= _CHUNK_ROWS:
            self._flush()

    def _flush(self) -> None:
        """Materialise the buffered rows into a chunk and reset the buffers."""
        if self._buffered == 0:
            return
        self._chunks.append(pl.DataFrame(self._cols, schema=self._schema))
        self._cols = {name: [] for name in self._schema}
        self._buffered = 0

    def build(self) -> pl.DataFrame:
        """Concatenate the chunks into the final typed frame.

        Returns:
            pl.DataFrame: The frame with exactly its schema's columns.
        """
        self._flush()
        if not self._chunks:
            return pl.DataFrame(schema=self._schema)
        if len(self._chunks) == 1:
            return self._chunks[0]
        return pl.concat(self._chunks)


#: ``trLine`` child field names read on the hot path.
_LINE_FIELDS = ("accID", "amnt", "amntTp", "docRef", "effDate", "custSupID", "desc")

#: ``transaction`` header field names read on the hot path.
_TX_FIELDS = ("nr", "desc", "periodNumber", "trDt")


def _qualify(ns: str, name: str) -> str:
    """Return a Clark-notation tag for ``name`` in namespace ``ns``.

    Args:
        ns: The namespace URI (``""`` for no namespace).
        name: The local element name.

    Returns:
        str: ``"{ns}name"`` when ``ns`` is set, else the bare name.
    """
    return f"{{{ns}}}{name}" if ns else name


class _State:
    """Mutable accumulators and parse context for one read.

    Holds, besides the accumulators, the namespace-qualified tag lookups used on
    the hot transaction/line path (so a child is matched by a dict lookup on its
    full tag, never a per-element ``rsplit``) and a one-per-journal cache of the
    current journal id.
    """

    def __init__(self) -> None:
        self.header: dict[str, Any] = {
            "source_format": "xaf",
            "source_version": None,
        }
        self.company_captured = False
        self.accounts = _Acc(SCHEMAS["accounts"])
        self.partners = _Acc(SCHEMAS["partners"])
        self.journals = _Acc(SCHEMAS["journals"])
        self.moves = _Acc(SCHEMAS["moves"])
        self.lines = _Acc(SCHEMAS["lines"])
        self.opening = _Acc(SCHEMAS["opening_lines"])
        self.vat_codes = _Acc(SCHEMAS["vat_codes"])
        self.move_seq = 0
        self.q_trline = "trLine"
        self.q_vat = "vat"
        self.q_jrnid = "jrnID"
        self.tx_fields: dict[str, str] = {name: name for name in _TX_FIELDS}
        self.line_fields: dict[str, str] = {name: name for name in _LINE_FIELDS}
        self._journal_el: etree._Element | None = None
        self._journal_id: str | None = None

    def configure(self, ns: str) -> None:
        """Precompute namespace-qualified tag lookups for the hot path.

        Args:
            ns: The document's (default) namespace URI.
        """
        self.q_trline = _qualify(ns, "trLine")
        self.q_vat = _qualify(ns, "vat")
        self.q_jrnid = _qualify(ns, "jrnID")
        self.tx_fields = {_qualify(ns, name): name for name in _TX_FIELDS}
        self.line_fields = {_qualify(ns, name): name for name in _LINE_FIELDS}

    def journal_id(self, journal: etree._Element | None) -> str | None:
        """Return the id of ``journal``, caching it per journal element.

        A journal holds many transactions; resolving its id once per journal
        (rather than per transaction) removes that scan from the hot path.

        Args:
            journal: The ``journal`` element a transaction sits in.

        Returns:
            str | None: The journal's ``jrnID``, or None.
        """
        if journal is None:
            return None
        if journal is not self._journal_el:
            self._journal_el = journal
            self._journal_id = None
            for child in journal:
                if child.tag == self.q_jrnid:
                    self._journal_id = _txt(child)
                    break
        return self._journal_id


def _sniff_root(path: str) -> tuple[str | None, str]:
    """Read just the root element for the XAF version and namespace.

    Args:
        path: The file to sniff.

    Returns:
        tuple[str | None, str]: ``(version, namespace)``; version is None when
        the root is not an XAF file.

    Raises:
        SourceReadError: If the file cannot be parsed as XML.
    """
    from ...lib.core import SourceReadError

    try:
        for _event, elem in etree.iterparse(path, events=("start",)):
            return _version_of(elem.tag), _namespace(elem.tag)
    except etree.XMLSyntaxError as exc:
        raise SourceReadError(f"malformed XAF file '{path}': {exc}") from exc
    except OSError as exc:
        raise SourceReadError(f"could not read XAF file '{path}': {exc}") from exc
    return None, ""


class XafReader:
    """Reads Dutch XAF 3.2 / 4.0 files into a canonical ledger."""

    format_name = "xaf"

    def detect(self, path: str) -> bool:
        """Report whether ``path`` is an XAF file, by its root namespace.

        Args:
            path: The candidate file.

        Returns:
            bool: True if the root element is an XAF ``auditfile``.
        """
        try:
            for _event, elem in etree.iterparse(path, events=("start",)):
                return _version_of(elem.tag) is not None
        except (etree.XMLSyntaxError, OSError):
            return False
        return False

    def read(self, path: str) -> CanonicalLedger:
        """Read an XAF file into a canonical ledger via streaming parse.

        Args:
            path: The XAF file to read.

        Returns:
            CanonicalLedger: The file as format-neutral frames.

        Raises:
            SourceReadError: If the file is not well-formed XML or cannot be read.
        """
        from ...lib.core import SourceReadError

        state = _State()
        version, ns = _sniff_root(path)
        state.header["source_version"] = version
        state.configure(ns)
        try:
            context = etree.iterparse(path, events=("end",), tag=_WANTED_TAGS)
            for _event, elem in context:
                _handle(state, elem)
        except etree.XMLSyntaxError as exc:
            raise SourceReadError(f"malformed XAF file '{path}': {exc}") from exc
        except OSError as exc:
            raise SourceReadError(f"could not read XAF file '{path}': {exc}") from exc
        return _build_ledger(state)


def _handle(state: _State, elem: etree._Element) -> None:
    """Dispatch one filtered record element to its reader.

    Args:
        state: The running parse state.
        elem: A record element (one of :data:`_WANTED_TAGS`).
    """
    name = _local(elem.tag)
    if not state.company_captured and name != "header":
        _capture_company_identity(state.header, elem)
        state.company_captured = True
    if name == "header":
        _read_header(state.header, elem)
        elem.clear()
    elif name == "ledgerAccount":
        _read_account(state.accounts, elem)
        elem.clear()
    elif name == "customerSupplier":
        _read_partner(state.partners, elem)
        elem.clear()
    elif name == "vatCode":
        _read_vat(state.vat_codes, elem)
        elem.clear()
    elif name == "obLine":
        _read_opening_gl(state.opening, elem)
        elem.clear()
    elif name == "obSbLine":
        _read_opening_subledger(state.opening, elem)
        elem.clear()
    elif name == "transaction":
        state.move_seq += 1
        _read_transaction(state, elem)
        elem.clear()  # free the lines; the empty shell is dropped with its journal
    elif name == "journal":
        _read_journal(state.journals, elem)
        _drop_with_siblings(elem)  # whole journal (and prior journals) are done


def _capture_company_identity(header: dict[str, Any], elem: etree._Element) -> None:
    """Fill company-level identity fields from the enclosing ``company``.

    Called once, on the first record seen; reads ``company``'s own direct
    children (which precede the big list containers), so it is cheap.

    Args:
        header: The header accumulator dict.
        elem: Any element inside ``company``.
    """
    for ancestor in elem.iterancestors():
        if _local(ancestor.tag) != "company":
            continue
        for child in ancestor:
            cname = _local(child.tag)
            if cname not in _COMPANY_IDENT:
                continue
            value = _txt(child)
            if cname == "companyName":
                header["company_name"] = value
            elif cname in ("companyIdent", "Commercenr"):
                header["company_id"] = value
            elif cname == "taxRegIdent":
                header["tax_registration"] = value
        return


def _read_header(header: dict[str, Any], elem: etree._Element) -> None:
    """Read the ``header`` block's reporting-period and software fields.

    Args:
        header: The header accumulator dict.
        elem: The ``header`` element.
    """
    header["fiscal_year"] = _text(elem, "fiscalYear")
    header["date_start"] = _date(_text(elem, "startDate"))
    header["date_end"] = _date(_text(elem, "endDate"))
    header["currency"] = _text(elem, "curCode")
    header["software"] = _text(elem, "softwareDesc")
    header["software_version"] = _text(elem, "softwareVersion")


def _read_account(acc: _Acc, elem: etree._Element) -> None:
    """Append one ledger account row.

    ``leadCode`` (3.2) is deliberately not treated as an RGS code — the XSD doc
    says it must not be used for RGS — so only 4.0's ``RGScode`` populates
    ``lead_code``; a 3.2 account falls to the numbering heuristic downstream.

    Args:
        acc: The accounts accumulator.
        elem: A ``ledgerAccount`` element.
    """
    acc.add(
        account_id=_text(elem, "accID"),
        description=_text(elem, "accDesc"),
        kind=_ACCOUNT_KIND.get(_text(elem, "accTp") or "", UNKNOWN),
        lead_code=_text(elem, "RGScode"),
    )


def _read_partner(acc: _Acc, elem: etree._Element) -> None:
    """Append one customer/supplier row.

    Args:
        acc: The partners accumulator.
        elem: A ``customerSupplier`` element.
    """
    street_elem = _child(elem, "streetAddress")
    street = city = postal = country = None
    if street_elem is not None:
        parts = [_text(street_elem, tag) for tag in _ADDRESS_STREET]
        street = " ".join(p for p in parts if p) or None
        city = _text(street_elem, "city")
        postal = _text(street_elem, "postalCode")
        country = _text(street_elem, "country")
    bank_elem = _child(elem, "bankAccount")
    bank = _text(bank_elem, "bankAccNr")
    acc.add(
        source_id=_text(elem, "custSupID"),
        role=_PARTNER_ROLE.get(_text(elem, "custSupTp") or "", UNKNOWN),
        name=_text(elem, "custSupName"),
        vat=_text(elem, "taxRegIdent"),
        kvk=_text(elem, "commerceNr"),
        street=street,
        city=city,
        postal_code=postal,
        country=country,
        bank_account=bank,
    )


def _read_vat(acc: _Acc, elem: etree._Element) -> None:
    """Append one VAT-code row.

    Args:
        acc: The vat_codes accumulator.
        elem: A ``vatCode`` element.
    """
    acc.add(
        code=_text(elem, "vatID"),
        description=_text(elem, "vatDesc"),
        rate=None,
        account_id=_text(elem, "vatToPayAccID"),
    )


def _read_opening_gl(acc: _Acc, elem: etree._Element) -> None:
    """Append a GL-account opening balance (``obLine``).

    Args:
        acc: The opening_lines accumulator.
        elem: An ``obLine`` element.
    """
    debit, credit = _split_amount(_text(elem, "amnt"), _text(elem, "amntTp"))
    acc.add(
        account_id=_text(elem, "accID"),
        source_partner_id=None,
        debit=debit,
        credit=credit,
        doc_ref=None,
        date=None,
    )


def _read_opening_subledger(acc: _Acc, elem: etree._Element) -> None:
    """Append a per-debtor/creditor opening item (3.2 ``obSbLine``).

    These carry the open invoices at cutover: a ``custSupID`` and an invoice
    reference, but no GL account (the control account sits on the enclosing
    subledger), so ``account_id`` is left blank for the transform to resolve.

    Args:
        acc: The opening_lines accumulator.
        elem: An ``obSbLine`` element.
    """
    debit, credit = _split_amount(_text(elem, "amnt"), _text(elem, "amntTp"))
    acc.add(
        account_id="",
        source_partner_id=_text(elem, "custSupID"),
        debit=debit,
        credit=credit,
        doc_ref=_text(elem, "invRef") or _text(elem, "docRef"),
        date=_date(_text(elem, "invDt")),
    )


def _read_journal(acc: _Acc, elem: etree._Element) -> None:
    """Append one journal row.

    Args:
        acc: The journals accumulator.
        elem: A ``journal`` element (fired after its transactions).
    """
    acc.add(
        journal_id=_text(elem, "jrnID"),
        description=_text(elem, "desc"),
        kind=_JOURNAL_KIND.get(_text(elem, "jrnTp") or "", UNKNOWN),
    )


def _read_transaction(state: _State, elem: etree._Element) -> None:
    """Append a transaction header and all its ``trLine`` rows (single pass).

    Args:
        state: The running parse state (for the move/line accumulators).
        elem: A ``transaction`` element.
    """
    journal_id = state.journal_id(elem.getparent())
    tx_fields = state.tx_fields
    q_trline = state.q_trline
    header: dict[str, str | None] = {}
    trlines: list[etree._Element] = []
    for child in elem:
        tag = child.tag
        if tag == q_trline:
            trlines.append(child)
        else:
            field = tx_fields.get(tag)
            if field is not None:
                header[field] = _txt(child)
    number = header.get("nr") or f"t{state.move_seq}"
    move_key = f"{journal_id or 'J'}/{number}"
    state.moves.add(
        move_key=move_key,
        journal_id=journal_id,
        number=number,
        description=header.get("desc"),
        date=_date(header.get("trDt")),
        doc_ref=None,
        source_partner_id=None,
        period=header.get("periodNumber"),
    )
    for index, child in enumerate(trlines, start=1):
        _read_trline(state, child, move_key, index)


def _read_trline(
    state: _State, elem: etree._Element, move_key: str, line_number: int
) -> None:
    """Append one transaction line, reading its children in a single pass.

    Args:
        state: The running parse state (accumulators and tag lookups).
        elem: A ``trLine`` element.
        move_key: The enclosing move's key.
        line_number: The 1-based ordinal of this line within the move.
    """
    line_fields = state.line_fields
    q_vat = state.q_vat
    values: dict[str, str | None] = {}
    vat_elem: etree._Element | None = None
    for child in elem:
        tag = child.tag
        if tag == q_vat:
            vat_elem = child
        else:
            field = line_fields.get(tag)
            if field is not None:
                values[field] = _txt(child)
    debit, credit = _split_amount(values.get("amnt"), values.get("amntTp"))
    state.lines.add(
        move_key=move_key,
        line_number=line_number,
        account_id=values.get("accID"),
        debit=debit,
        credit=credit,
        doc_ref=values.get("docRef"),
        effective_date=_date(values.get("effDate")),
        source_partner_id=values.get("custSupID"),
        vat_code=_text(vat_elem, "vatID"),
        vat_amount=_money(_text(vat_elem, "vatAmnt")),
        description=values.get("desc"),
    )


def _drop_with_siblings(elem: etree._Element) -> None:
    """Free an element and all its already-processed preceding siblings.

    Used for ``journal`` elements: once a journal is read, it and every earlier
    journal can be dropped from the ``transactions`` container, keeping the tree
    small across the whole file.

    Args:
        elem: The element to free.
    """
    elem.clear()
    parent = elem.getparent()
    if parent is None:
        return
    while elem.getprevious() is not None:
        del parent[0]


def _build_ledger(state: _State) -> CanonicalLedger:
    """Assemble the accumulated state into a canonical ledger.

    Args:
        state: The populated parse state.

    Returns:
        CanonicalLedger: The populated, unvalidated ledger.
    """
    header_row = {name: [state.header.get(name)] for name in SCHEMAS["header"]}
    return CanonicalLedger(
        header=pl.DataFrame(header_row, schema=SCHEMAS["header"]),
        accounts=state.accounts.build(),
        partners=state.partners.build(),
        journals=state.journals.build(),
        moves=state.moves.build(),
        lines=state.lines.build(),
        opening_lines=state.opening.build(),
        vat_codes=state.vat_codes.build(),
    )


#: The singleton reader the registry registers.
READER = XafReader()
