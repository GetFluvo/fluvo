"""The format-neutral canonical ledger for audit-file imports.

An audit-file connector is a *compiler*: it turns a tax-authority audit file
into a canonical ledger, and a later transform turns that ledger into the
Odoo-shaped CSVs the existing flow runner loads. This module is the waist of
that pipeline — the single representation every format reader targets and every
transform consumes.

The model is **columnar**, a set of named :class:`polars.DataFrame` frames, not
a tree of per-record Python dicts. Audit files for active companies routinely
exceed 100 MB; materialising one as dicts would blow the memory budget, so the
canonical shape is deliberately frame-shaped from the start.

The frames are modelled on the OECD **SAF-T** concepts, of which the Dutch XAF
is the direct ancestor. Keeping the canonical vocabulary SAF-T-shaped is what
makes a second reader (FEC, FAIA, Norwegian SAF-T, DATEV) thin: a reader only
has to map its own element names onto these columns, never invent a structure.

Each frame has a fixed, documented column schema (the ``*_SCHEMA`` constants).
A reader builds frames to these schemas; :func:`CanonicalLedger.validate`
checks that it did, so a drift between a reader and the transform fails loudly
at the seam rather than silently downstream.

Canonical vocabularies (the normalised code sets a reader maps source codes
onto, so the transform never sees a format-specific letter):

* partner ``role`` — :data:`PARTNER_CUSTOMER`, :data:`PARTNER_SUPPLIER`,
  :data:`PARTNER_BOTH`.
* account ``kind`` — :data:`ACCOUNT_BALANCE`, :data:`ACCOUNT_PROFIT_LOSS`, or
  :data:`UNKNOWN` when the source does not say.
* journal ``kind`` — :data:`JOURNAL_SALE`, :data:`JOURNAL_PURCHASE`,
  :data:`JOURNAL_BANK`, :data:`JOURNAL_CASH`, :data:`JOURNAL_GENERAL`,
  :data:`JOURNAL_OPENING`, or :data:`UNKNOWN`.

Money lives in split ``debit`` / ``credit`` columns (:class:`polars.Float64`),
never a single signed amount, so a balance check is a plain column sum and the
``debit == credit`` tolerance the rest of Fluvo uses applies directly.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Final

import polars as pl

from ..lib.core import InputError

# --- Canonical code vocabularies -------------------------------------------

#: Used wherever the source does not classify a record.
UNKNOWN: Final = ""

PARTNER_CUSTOMER: Final = "customer"
PARTNER_SUPPLIER: Final = "supplier"
PARTNER_BOTH: Final = "both"

ACCOUNT_BALANCE: Final = "balance"
ACCOUNT_PROFIT_LOSS: Final = "profit_loss"

JOURNAL_SALE: Final = "sale"
JOURNAL_PURCHASE: Final = "purchase"
JOURNAL_BANK: Final = "bank"
JOURNAL_CASH: Final = "cash"
JOURNAL_GENERAL: Final = "general"
JOURNAL_OPENING: Final = "opening"

#: Debit / credit equality tolerance, in the file's currency unit. Matches the
#: 0.01 rounding tolerance the import engine and ADR 0001 already use.
BALANCE_TOLERANCE: Final = 0.01

# --- Frame schemas ----------------------------------------------------------
# Every reader builds exactly these columns, in these types. A nullable column
# is still present (filled with nulls) when the source omits it: the canonical
# shape is fixed, the data within it may be sparse.

#: One row. The file's identity and reporting period.
HEADER_SCHEMA: Final[dict[str, type[pl.DataType]]] = {
    "source_format": pl.String,  # e.g. "xaf"
    "source_version": pl.String,  # e.g. "3.2", "4.0"
    "company_name": pl.String,
    "company_id": pl.String,  # Chamber of Commerce / KvK number
    "tax_registration": pl.String,  # company VAT, nullable
    "fiscal_year": pl.String,
    "date_start": pl.Date,
    "date_end": pl.Date,
    "currency": pl.String,
    "software": pl.String,  # producing software, nullable
    "software_version": pl.String,  # nullable
}

#: General-ledger accounts (the chart of accounts).
ACCOUNTS_SCHEMA: Final[dict[str, type[pl.DataType]]] = {
    "account_id": pl.String,  # source account code
    "description": pl.String,
    "kind": pl.String,  # ACCOUNT_BALANCE / ACCOUNT_PROFIT_LOSS / UNKNOWN
    "lead_code": pl.String,  # RGS reference code, nullable
}

#: Customers and suppliers, pre-deduplication (the transform merges them).
PARTNERS_SCHEMA: Final[dict[str, type[pl.DataType]]] = {
    "source_id": pl.String,  # the format's own customer/supplier id
    "role": pl.String,  # PARTNER_CUSTOMER / PARTNER_SUPPLIER / PARTNER_BOTH
    "name": pl.String,
    "vat": pl.String,  # nullable
    "kvk": pl.String,  # Chamber of Commerce number, nullable
    "street": pl.String,  # nullable
    "city": pl.String,  # nullable
    "postal_code": pl.String,  # nullable
    "country": pl.String,  # ISO code, nullable
    "bank_account": pl.String,  # IBAN, nullable
}

#: Journals (books): sales, purchase, bank, cash, memorial.
JOURNALS_SCHEMA: Final[dict[str, type[pl.DataType]]] = {
    "journal_id": pl.String,
    "description": pl.String,
    "kind": pl.String,  # JOURNAL_* / UNKNOWN
}

#: One row per transaction (header level). Lines live in :data:`LINES_SCHEMA`.
MOVES_SCHEMA: Final[dict[str, type[pl.DataType]]] = {
    "move_key": pl.String,  # stable within the file: journal_id + "/" + number
    "journal_id": pl.String,
    "number": pl.String,  # the source transaction number
    "description": pl.String,  # nullable
    "date": pl.Date,
    "doc_ref": pl.String,  # invoice/document reference, nullable
    "source_partner_id": pl.String,  # nullable; may instead sit on the line
    "period": pl.String,  # nullable
}

#: Transaction lines. ``move_key`` joins back to :data:`MOVES_SCHEMA`.
LINES_SCHEMA: Final[dict[str, type[pl.DataType]]] = {
    "move_key": pl.String,
    "line_number": pl.Int64,  # 1-based ordinal within the move
    "account_id": pl.String,
    "debit": pl.Float64,
    "credit": pl.Float64,
    "doc_ref": pl.String,  # nullable
    "effective_date": pl.Date,  # nullable
    "source_partner_id": pl.String,  # nullable
    "vat_code": pl.String,  # nullable
    "vat_amount": pl.Float64,  # nullable
    "description": pl.String,  # nullable
}

#: Opening balances, one row per account (and optionally per open document).
OPENING_LINES_SCHEMA: Final[dict[str, type[pl.DataType]]] = {
    "account_id": pl.String,
    "source_partner_id": pl.String,  # nullable
    "debit": pl.Float64,
    "credit": pl.Float64,
    "doc_ref": pl.String,  # nullable; present enables per-invoice open items
    "date": pl.Date,  # nullable
}

#: VAT / tax codes, where the format carries a master table.
VAT_CODES_SCHEMA: Final[dict[str, type[pl.DataType]]] = {
    "code": pl.String,
    "description": pl.String,  # nullable
    "rate": pl.Float64,  # percentage, nullable
    "account_id": pl.String,  # nullable
}

#: Frame name -> schema, in load order (partners/accounts/journals before
#: moves/lines). The field names of :class:`CanonicalLedger` match these keys.
SCHEMAS: Final[dict[str, dict[str, type[pl.DataType]]]] = {
    "header": HEADER_SCHEMA,
    "accounts": ACCOUNTS_SCHEMA,
    "partners": PARTNERS_SCHEMA,
    "journals": JOURNALS_SCHEMA,
    "moves": MOVES_SCHEMA,
    "lines": LINES_SCHEMA,
    "opening_lines": OPENING_LINES_SCHEMA,
    "vat_codes": VAT_CODES_SCHEMA,
}


def empty_frame(name: str) -> pl.DataFrame:
    """Build an empty frame with the declared schema for ``name``.

    ``name`` must be a key of :data:`SCHEMAS`; an unknown name raises
    :class:`KeyError`.

    Args:
        name: A frame name — a key of :data:`SCHEMAS`.

    Returns:
        pl.DataFrame: A zero-row frame with exactly that frame's columns and
        dtypes, so a reader can start from the right shape and append rows.
    """
    return pl.DataFrame(schema=SCHEMAS[name])


@dataclass(frozen=True)
class CanonicalLedger:
    """A whole audit file, format-neutral, as a set of columnar frames.

    Attributes:
        header: One row describing the file (see :data:`HEADER_SCHEMA`).
        accounts: The chart of accounts (:data:`ACCOUNTS_SCHEMA`).
        partners: Customers and suppliers (:data:`PARTNERS_SCHEMA`).
        journals: The journals/books (:data:`JOURNALS_SCHEMA`).
        moves: Transaction headers (:data:`MOVES_SCHEMA`).
        lines: Transaction lines (:data:`LINES_SCHEMA`).
        opening_lines: Opening balances (:data:`OPENING_LINES_SCHEMA`).
        vat_codes: VAT/tax codes (:data:`VAT_CODES_SCHEMA`).
    """

    header: pl.DataFrame
    accounts: pl.DataFrame
    partners: pl.DataFrame
    journals: pl.DataFrame
    moves: pl.DataFrame
    lines: pl.DataFrame
    opening_lines: pl.DataFrame
    vat_codes: pl.DataFrame

    def validate(self) -> None:
        """Check every frame carries exactly its declared columns.

        A reader that drifts from the schema (a renamed, missing or extra
        column) is a bug at the reader/transform seam; failing here turns it
        into a clear error instead of a confusing one downstream.

        Raises:
            InputError: If a frame's columns do not match its schema, or the
                header does not hold exactly one row.
        """
        for spec in fields(self):
            frame: pl.DataFrame = getattr(self, spec.name)
            expected = list(SCHEMAS[spec.name])
            actual = frame.columns
            if actual != expected:
                raise InputError(
                    f"canonical frame '{spec.name}' has columns {actual}, "
                    f"expected {expected}."
                )
        if self.header.height != 1:
            raise InputError(
                f"canonical header must have exactly one row, got {self.header.height}."
            )


def _one(frame: pl.DataFrame, column: str) -> str | None:
    """Read a single string cell from a one-row frame.

    Args:
        frame: A frame expected to hold one row.
        column: The column to read.

    Returns:
        str | None: The value as text, or None when the frame is empty or the
        cell is null.
    """
    if frame.height == 0:
        return None
    value = frame.get_column(column).item(0)
    return None if value is None else str(value)


@dataclass(frozen=True)
class LedgerSummary:
    """The read-only overview behind ``fluvo auditfile inspect``.

    Pure derived facts about a :class:`CanonicalLedger`, so the inspect command
    is a thin renderer over this and the computation is unit-testable without a
    console. No Odoo connection is involved.

    Attributes:
        source_format: The canonical format tag, e.g. ``"xaf"``.
        source_version: The detected schema version, e.g. ``"3.2"``.
        software: The producing software, if the header carried it.
        company_name: The company the file belongs to.
        company_id: The company's Chamber-of-Commerce identifier.
        fiscal_year: The reporting fiscal year.
        date_start: Period start, as text (``None`` if absent).
        date_end: Period end, as text (``None`` if absent).
        currency: The file's currency code.
        counts: Row count per frame, keyed by frame name.
        total_debit: Sum of all line debits.
        total_credit: Sum of all line credits.
        unbalanced_moves: How many moves fail the debit==credit tolerance.
    """

    source_format: str | None
    source_version: str | None
    software: str | None
    company_name: str | None
    company_id: str | None
    fiscal_year: str | None
    date_start: str | None
    date_end: str | None
    currency: str | None
    counts: dict[str, int]
    total_debit: float
    total_credit: float
    unbalanced_moves: int

    @property
    def balanced(self) -> bool:
        """Whether total debits equal total credits within tolerance.

        Returns:
            bool: True when ``abs(total_debit - total_credit)`` is within
            :data:`BALANCE_TOLERANCE`.
        """
        return abs(self.total_debit - self.total_credit) <= BALANCE_TOLERANCE


def unbalanced_moves(lines: pl.DataFrame) -> pl.DataFrame:
    """Return per-move debit/credit totals for moves that do not balance.

    Args:
        lines: A frame shaped like :data:`LINES_SCHEMA`.

    Returns:
        pl.DataFrame: One row per unbalanced move, with ``move_key``,
        ``debit`` and ``credit`` totals and their ``delta``. Empty when every
        move balances within :data:`BALANCE_TOLERANCE`.
    """
    if lines.height == 0:
        return pl.DataFrame(
            schema={
                "move_key": pl.String,
                "debit": pl.Float64,
                "credit": pl.Float64,
                "delta": pl.Float64,
            }
        )
    grouped = (
        lines.group_by("move_key")
        .agg(
            pl.col("debit").sum().alias("debit"),
            pl.col("credit").sum().alias("credit"),
        )
        .with_columns((pl.col("debit") - pl.col("credit")).alias("delta"))
    )
    return grouped.filter(pl.col("delta").abs() > BALANCE_TOLERANCE).sort("move_key")


def summarise(ledger: CanonicalLedger) -> LedgerSummary:
    """Derive the inspect overview from a canonical ledger.

    Args:
        ledger: The ledger to summarise.

    Returns:
        LedgerSummary: Header facts, per-frame row counts and a balance check.
    """
    counts = {
        spec.name: int(getattr(ledger, spec.name).height)
        for spec in fields(ledger)
        if spec.name != "header"
    }
    lines = ledger.lines
    total_debit = float(lines.get_column("debit").sum() or 0.0) if lines.height else 0.0
    total_credit = (
        float(lines.get_column("credit").sum() or 0.0) if lines.height else 0.0
    )
    return LedgerSummary(
        source_format=_one(ledger.header, "source_format"),
        source_version=_one(ledger.header, "source_version"),
        software=_one(ledger.header, "software"),
        company_name=_one(ledger.header, "company_name"),
        company_id=_one(ledger.header, "company_id"),
        fiscal_year=_one(ledger.header, "fiscal_year"),
        date_start=_one(ledger.header, "date_start"),
        date_end=_one(ledger.header, "date_end"),
        currency=_one(ledger.header, "currency"),
        counts=counts,
        total_debit=total_debit,
        total_credit=total_credit,
        unbalanced_moves=int(unbalanced_moves(lines).height),
    )
