"""Transform a canonical ledger into Odoo-shaped frames.

Pure functions: a :class:`~fluvo.auditfile.canonical.CanonicalLedger` in, a
:class:`TransformResult` of Odoo-shaped :class:`polars.DataFrame` frames out.
No I/O and no Odoo connection — :mod:`fluvo.auditfile.stage` writes the result
and :mod:`fluvo.auditfile.preflight` checks it.

What the transform decides:

* **Deterministic external ids** for every record, so a re-run upserts instead
  of duplicating (``<prefix>.partner_<custSupID>``, ``…account_<accID>``, …).
  Move lines get ``<move_xmlid>_l<n>`` exactly as ADR 0001 requires. The same
  file always produces the same ids, hence an identical stage.
* **Partner dedup** on a normalised Chamber-of-Commerce (KvK) number or VAT:
  Dutch systems often hold one company as both a customer and a supplier, so
  matching records merge into one ``res.partner`` carrying both ranks. Every
  source id is recorded in an **alias map** (source id → surviving xml id) so
  move lines resolve to the survivor.
* **Account typing** via a chart-mapper chain: a user override (always wins) →
  a small curated RGS table (4.0 ``RGScode``) → the Dutch numbering heuristic.
  Heuristic/fallback results are *proposals*: they are written to an editable
  ``account_map.csv`` and flagged in preflight, never applied silently.
  Receivable/payable accounts get ``reconcile = True``.
* **Journal type** mapping, and a synthetic opening journal for the opening
  balance move.

Moves always land as **draft** (no ``state`` column): posting is a separate,
explicit step (ADR 0001) and is out of scope here. Two modes:

* ``full_history`` — every transaction as a draft ``account.move`` plus one
  opening-balance move carrying the GL opening balances as-is.
* ``cutover`` — the **closing position at the end of the period**: each
  account's opening balance plus the year's movement. A single opening move
  carries the balance-sheet (and, by default, profit-and-loss) closing balances;
  set ``result_account`` to close profit-and-loss into one result account for a
  clean balance sheet. Each open AR/AP item — the prior-year opening subledger
  plus this year's lines, matched on an explicit key then invoice then document
  reference, settled items dropped — becomes its **own draft invoice/bill** on a
  dedicated clearing account (see :func:`_open_invoice_rows`), with the control
  line forced onto the source account via the partner's staged property. The
  opening move then carries the clearing counter-amount (which nets to zero
  against those invoices) plus a lumped remainder for any control opening a
  subledger did not itemise. Every move balances by construction and preflight
  re-checks it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

import polars as pl

from ..lib.core import InputError
from .canonical import (
    ACCOUNT_BALANCE,
    JOURNAL_BANK,
    JOURNAL_CASH,
    JOURNAL_PURCHASE,
    JOURNAL_SALE,
    CanonicalLedger,
)
from .match import AccountMatch

MODE_CUTOVER: Final = "cutover"
MODE_FULL_HISTORY: Final = "full_history"
MODES: Final = (MODE_CUTOVER, MODE_FULL_HISTORY)

#: Odoo account types that must be reconcilable.
_RECONCILE_TYPES: Final = frozenset({"asset_receivable", "liability_payable"})

#: Odoo profit-and-loss account types (the rest are balance-sheet types). Used
#: both to cross-check the canonical ``kind`` and to decide which accounts a
#: cutover closes to the result account.
_PL_TYPES: Final = frozenset(
    {
        "income",
        "income_other",
        "expense",
        "expense_depreciation",
        "expense_direct_cost",
    }
)

#: Net debit/credit below this (in currency units) is treated as settled/zero.
_ZERO_TOLERANCE: Final = 0.005

#: The external id of the always-present opening journal and move.
_OPENING_JOURNAL = "journal__opening"
_OPENING_MOVE = "move__opening"

#: External ids of the dedicated sale/purchase journals the open-item invoices
#: post to in cutover. A real ``move_type`` needs a journal of the matching
#: type; these are staged so the invoices never fall onto whatever default
#: journal Odoo would otherwise pick.
_OPENING_SALE_JOURNAL = "journal__opening_sale"
_OPENING_PURCHASE_JOURNAL = "journal__opening_purchase"

#: Default code/name of the opening open-item clearing account, and the default
#: payment term (days) used to derive an invoice due date the file does not
#: carry. The clearing account absorbs the invoice side of every open-item
#: invoice and nets to zero against the opening move.
_CLEARING_DEFAULT_CODE: Final = "OPENCL"
_CLEARING_NAME: Final = "Opening open-item clearing"
_DEFAULT_PAYMENT_DAYS: Final = 30

#: Odoo ``move_type`` per control-account kind and whether the item's sign is
#: reversed (a credit note / unallocated item) relative to the normal side.
_MOVE_TYPE = {
    ("receivable", False): "out_invoice",
    ("receivable", True): "out_refund",
    ("payable", False): "in_invoice",
    ("payable", True): "in_refund",
}

#: Column shape of the staged ``open_invoices.csv`` (one invoice per open item).
#: ``journal_id/id`` pins a dedicated sale/purchase journal and ``date`` the
#: accounting date (the cutover date) so the entry lands in the open period, not
#: the original invoice's (likely locked) period.
_OPEN_INVOICE_COLUMNS: Final = [
    "id",
    "move_type",
    "journal_id/id",
    "partner_id/id",
    "date",
    "invoice_date",
    "invoice_date_due",
    "ref",
    "payment_reference",
    "name",
    "narration",
    "invoice_line_ids/id",
    "invoice_line_ids/name",
    "invoice_line_ids/account_id/id",
    "invoice_line_ids/quantity",
    "invoice_line_ids/price_unit",
    "invoice_line_ids/tax_ids/id",
]

#: Column shape of the staged account-match review file (``account_matches.csv``).
_MATCH_REPORT_COLUMNS: Final = [
    "account_id",
    "source_name",
    "source_account_type",
    "source_reconcile",
    "target_xmlid",
    "target_name",
    "target_account_type",
    "target_reconcile",
    "status",
]

#: Source of an account-type decision, most to least authoritative.
SRC_OVERRIDE: Final = "override"
SRC_RGS: Final = "rgs"
SRC_HEURISTIC: Final = "heuristic"
SRC_FALLBACK: Final = "fallback"

#: A small, curated RGS reference-code → Odoo account type table (4.0). RGS has
#: thousands of codes; this covers the control/liquidity accounts worth typing
#: with confidence. Everything else falls to the numbering heuristic, which is
#: a proposal for review — so this table is intentionally conservative.
_RGS_TYPE: Final[dict[str, str]] = {
    "BVorHanDeb": "asset_receivable",
    "BSchCreHac": "liability_payable",
    "BLimBanRba": "asset_cash",
    "BLimKasKas": "asset_cash",
    "BEivKap": "equity",
    "WOmzNomGrp": "income",
    "WBedKanInd": "expense",
}

#: Canonical journal kind → Odoo journal type (``general`` is the catch-all).
_JOURNAL_TYPE: Final = {
    JOURNAL_SALE: "sale",
    JOURNAL_PURCHASE: "purchase",
    JOURNAL_BANK: "bank",
    JOURNAL_CASH: "cash",
}


def _san(value: str) -> str:
    """Sanitise a source key into the name part of an Odoo external id.

    Args:
        value: The raw source identifier (may contain ``/``, ``-``, spaces).

    Returns:
        str: ``value`` with every character outside ``[A-Za-z0-9_]`` replaced
        by ``_``.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _xmlid(prefix: str, kind: str, value: str) -> str:
    """Build a deterministic external id ``<prefix>.<kind>_<sanitised>``.

    Args:
        prefix: The import prefix (the external-id module), e.g. ``"xaf"``.
        kind: The record kind, e.g. ``"partner"``, ``"account"``, ``"move"``.
        value: The source key.

    Returns:
        str: The external id, e.g. ``"xaf.account_1300"``.
    """
    return f"{prefix}.{kind}_{_san(value)}"


def _norm_kvk(value: str | None) -> str | None:
    """Normalise a Chamber-of-Commerce number to its digits.

    Args:
        value: The raw KvK/commerce number.

    Returns:
        str | None: The digit-only form (zero-padded to 8 where shorter), or
        None when there are no digits.
    """
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    return digits.zfill(8) if len(digits) < 8 else digits


def _norm_vat(value: str | None) -> str | None:
    """Normalise a VAT number (upper-case, strip non-alphanumerics).

    Args:
        value: The raw VAT/tax registration number.

    Returns:
        str | None: The normalised form, or None when empty.
    """
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return cleaned or None


def _numbering_type(code: str) -> str | None:  # noqa: C901 - a flat lookup chain
    """Propose an Odoo account type from the Dutch grootboek numbering.

    Args:
        code: The account code (``accID``).

    Returns:
        str | None: A proposed account type, or None when the code gives no
        signal.
    """
    code = code.strip()
    if not code:
        return None
    two = code[:2]
    if two == "13":
        return "asset_receivable"
    if two == "16":
        return "liability_payable"
    if two in ("10", "11", "12"):
        return "asset_cash"
    if two in ("05", "06", "07", "08", "09"):
        return "equity"
    first = code[0]
    if first == "0":
        return "asset_fixed"
    if first == "1":
        return "asset_current"
    if first == "2":
        return "liability_current"
    if first == "3":
        return "asset_current"
    if first in ("4", "5", "6", "7"):
        return "expense"
    if first == "8":
        return "income"
    if first == "9":
        return "income_other"
    return None


@dataclass(frozen=True)
class AccountDecision:
    """One account's proposed Odoo typing and where it came from.

    Attributes:
        account_id: The source account code.
        code: The Odoo account code (same as ``account_id``).
        name: The account description.
        account_type: The proposed Odoo ``account_type``.
        reconcile: Whether the account must be reconcilable.
        source: How the type was chosen — one of :data:`SRC_OVERRIDE`,
            :data:`SRC_RGS`, :data:`SRC_HEURISTIC`, :data:`SRC_FALLBACK`.
        kind: The canonical account kind from the source (balance / profit_loss
            / unknown), kept to cross-check the chosen Odoo type.
    """

    account_id: str
    code: str
    name: str
    account_type: str
    reconcile: bool
    source: str
    kind: str = ""

    @property
    def kind_conflict(self) -> bool:
        """Whether the chosen Odoo type contradicts the source's account kind.

        Returns:
            bool: True when the source says profit-and-loss but the type is a
            balance-sheet type, or vice versa. Unknown kinds never conflict.
        """
        from .canonical import ACCOUNT_BALANCE, ACCOUNT_PROFIT_LOSS

        is_pl_type = self.account_type in _PL_TYPES
        if self.kind == ACCOUNT_PROFIT_LOSS:
            return not is_pl_type
        if self.kind == ACCOUNT_BALANCE:
            return is_pl_type
        return False


def classify_account(
    account_id: str,
    lead_code: str | None,
    kind: str,
    overrides: dict[str, str],
) -> tuple[str, str]:
    """Decide an account's Odoo type via the chart-mapper chain.

    The chain, most authoritative first: a user override, then the curated RGS
    table (4.0), then the numbering heuristic, then a kind-based fallback.

    Args:
        account_id: The source account code.
        lead_code: The RGS reference code (4.0 only), if any.
        kind: The canonical account kind (balance / profit_loss / unknown).
        overrides: A user ``account_id`` → ``account_type`` override map.

    Returns:
        tuple[str, str]: ``(account_type, source)``.
    """
    if account_id in overrides:
        return overrides[account_id], SRC_OVERRIDE
    if lead_code and lead_code in _RGS_TYPE:
        return _RGS_TYPE[lead_code], SRC_RGS
    proposed = _numbering_type(account_id)
    if proposed is not None:
        return proposed, SRC_HEURISTIC
    return ("asset_current" if kind == ACCOUNT_BALANCE else "expense"), SRC_FALLBACK


def _decide_accounts(
    ledger: CanonicalLedger,
    overrides: dict[str, str],
    reconcile_overrides: dict[str, bool] | None = None,
) -> list[AccountDecision]:
    """Classify every ledger account, deterministically by code.

    Args:
        ledger: The canonical ledger.
        overrides: A user ``account_id`` → ``account_type`` override map.
        reconcile_overrides: A user ``account_id`` → reconcile override; absent
            accounts fall back to reconcilable-by-type.

    Returns:
        list[AccountDecision]: One decision per account, sorted by code.
    """
    reconcile_overrides = reconcile_overrides or {}
    rows = ledger.accounts.sort("account_id").to_dicts()
    decisions: list[AccountDecision] = []
    for row in rows:
        account_id = str(row["account_id"])
        kind = str(row.get("kind") or "")
        account_type, source = classify_account(
            account_id, row.get("lead_code"), kind, overrides
        )
        decisions.append(
            AccountDecision(
                account_id=account_id,
                code=account_id,
                name=str(row.get("description") or account_id),
                account_type=account_type,
                reconcile=reconcile_overrides.get(
                    account_id, account_type in _RECONCILE_TYPES
                ),
                source=source,
                kind=kind,
            )
        )
    return decisions


@dataclass(frozen=True)
class TransformResult:
    """The Odoo-shaped output of a transform.

    Attributes:
        partners: ``res.partner`` rows (deduplicated).
        accounts: ``account.account`` rows.
        journals: ``account.journal`` rows (incl. the opening journal).
        moves: ``account.move`` rows in the ``line_ids/...`` o2m shape.
        alias_map: Source partner id → surviving partner external id.
        account_map: The editable account-typing proposals (account_id, code,
            name, account_type, reconcile, source).
        open_items: Per-open-item AR/AP breakdown used by cutover, for the
            preflight tie-out and an audit report.
        open_invoices: Cutover open-item invoices/bills (one per open item), in
            the ``invoice_line_ids/...`` shape.
        partner_banks: ``res.partner.bank`` rows (one per survivor with a bank).
        mode: The mode the result was built for.
        prefix: The external-id prefix used.
        partner_receivable: ``res.partner`` rows carrying only the receivable
            property to force (cutover; one per partner with a receivable open
            item), kept off the main partner frame so other partners' company
            default is not overwritten.
        partner_payable: The same for the payable property.
        account_matches: The reviewable account-match report
            (:data:`_MATCH_REPORT_COLUMNS`).
        clearing_account: The open-item clearing account id (cutover).
        clearing_xmlid: The clearing account's resolved external id (cutover).
        heuristic_accounts: Account ids whose type came from the numbering
            heuristic or the fallback (not an override or RGS) — the proposals
            preflight flags for review.
        conflicting_accounts: Account ids whose chosen Odoo type contradicts the
            source's balance/profit-loss kind — preflight flags these too.
        matched_accounts: Account ids referenced onto an existing Odoo account
            (not created) — reported so the match is reviewable.
        match_type_mismatches: Matched account ids whose target ``account_type``
            differs from the source typing — preflight warns.
        match_control_errors: Matched receivable/payable account ids whose target
            is not reconcilable or not the right type — preflight errors (an
            open-item control line would misbook).
        collision_accounts: Source account ids that share a code with a target
            account that could not be matched (ambiguous or no external id), so
            re-creating them would fail on the unique code — preflight errors.
        refund_items: External ids of reversed-sign open items (credit notes /
            unallocated payments) staged as refunds — preflight flags these.
        ambiguous_partners: Source partner ids whose open items span more than
            one control account of a kind — preflight flags these.
        derived_due_dates: How many invoice due dates were derived (the file
            carries none), for a preflight warning.
    """

    partners: pl.DataFrame
    accounts: pl.DataFrame
    journals: pl.DataFrame
    moves: pl.DataFrame
    alias_map: pl.DataFrame
    account_map: pl.DataFrame
    open_items: pl.DataFrame
    open_invoices: pl.DataFrame
    partner_banks: pl.DataFrame
    mode: str
    prefix: str
    partner_receivable: pl.DataFrame = field(
        default_factory=lambda: pl.DataFrame(
            schema={"id": pl.String, "property_account_receivable_id/id": pl.String}
        )
    )
    partner_payable: pl.DataFrame = field(
        default_factory=lambda: pl.DataFrame(
            schema={"id": pl.String, "property_account_payable_id/id": pl.String}
        )
    )
    account_matches: pl.DataFrame = field(
        default_factory=lambda: pl.DataFrame(
            schema={c: pl.String for c in _MATCH_REPORT_COLUMNS}
        )
    )
    clearing_account: str = ""
    clearing_xmlid: str = ""
    heuristic_accounts: tuple[str, ...] = field(default_factory=tuple)
    conflicting_accounts: tuple[str, ...] = field(default_factory=tuple)
    matched_accounts: tuple[str, ...] = field(default_factory=tuple)
    match_type_mismatches: tuple[str, ...] = field(default_factory=tuple)
    match_control_errors: tuple[str, ...] = field(default_factory=tuple)
    collision_accounts: tuple[str, ...] = field(default_factory=tuple)
    refund_items: tuple[str, ...] = field(default_factory=tuple)
    ambiguous_partners: tuple[str, ...] = field(default_factory=tuple)
    derived_due_dates: int = 0


# --- partners ---------------------------------------------------------------


class _UnionFind:
    """A tiny union-find over string tokens, for transitive partner dedup."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, token: str) -> None:
        """Register a token as its own set if unseen.

        Args:
            token: The token to register.
        """
        self._parent.setdefault(token, token)

    def find(self, token: str) -> str:
        """Return the representative of ``token``'s set, compressing the path.

        Args:
            token: The token to resolve.

        Returns:
            str: The set representative.
        """
        root = token
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[token] != root:
            self._parent[token], token = root, self._parent[token]
        return root

    def union(self, a: str, b: str) -> None:
        """Merge the sets containing ``a`` and ``b``.

        Args:
            a: A token.
            b: Another token.
        """
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Keep the lexicographically smaller root, for determinism.
            lo, hi = sorted((ra, rb))
            self._parent[hi] = lo


def _partner_groups(
    partners: list[dict[str, object]], *, dedup: bool
) -> dict[str, list[dict[str, object]]]:
    """Group partner records into merged entities (KvK/VAT transitively).

    With ``dedup`` off, every record is its own group. Otherwise records are
    linked through any shared normalised KvK *or* VAT number, so a record known
    only by KvK and another known only by VAT still merge when a third ties them
    together. Each group is keyed by its surviving (lowest) source id.

    Args:
        partners: Partner rows, sorted by source id.
        dedup: Whether to merge matching records at all.

    Returns:
        dict[str, list[dict[str, object]]]: Survivor source id → its members.
    """
    uf = _UnionFind()
    id_token: dict[str, str] = {}
    for row in partners:
        source_id = str(row["source_id"])
        tokens = [f"id:{source_id}"]
        kvk = _norm_kvk(row.get("kvk"))  # type: ignore[arg-type]
        vat = _norm_vat(row.get("vat"))  # type: ignore[arg-type]
        if dedup and kvk:
            tokens.append(f"kvk:{kvk}")
        if dedup and vat:
            tokens.append(f"vat:{vat}")
        for token in tokens:
            uf.add(token)
        for token in tokens[1:]:
            uf.union(tokens[0], token)
        id_token[source_id] = tokens[0]

    members: dict[str, list[dict[str, object]]] = {}
    for row in partners:
        members.setdefault(uf.find(id_token[str(row["source_id"])]), []).append(row)
    # Re-key each group by its lowest (surviving) source id.
    groups: dict[str, list[dict[str, object]]] = {}
    for group in members.values():
        survivor = min(str(m["source_id"]) for m in group)
        groups[survivor] = group
    return groups


def _country_xmlid(country: str) -> str:
    """Map an ISO-3166 alpha-2 country code to its Odoo external id.

    Args:
        country: A country code, e.g. ``"NL"``.

    Returns:
        str: ``"base.<lower>"`` for a 2-letter code, else ``""``.
    """
    code = (country or "").strip()
    return f"base.{code.lower()}" if len(code) == 2 and code.isalpha() else ""


#: Columns of the staged ``res.partner.bank`` frame.
_BANK_COLUMNS: Final = ["id", "partner_id/id", "acc_number"]


def _dedup_partners(
    ledger: CanonicalLedger, prefix: str, *, dedup: bool = True
) -> tuple[pl.DataFrame, dict[str, str], pl.DataFrame]:
    """Merge customer/supplier records and build the alias and bank maps.

    Within a merged group the surviving external id is the lowest source id,
    both ranks are set if any member carries that role, and the first non-empty
    field wins. The country resolves to its Odoo external id, and a bank account
    becomes a ``res.partner.bank`` row, so neither is silently dropped.

    Args:
        ledger: The canonical ledger.
        prefix: The external-id prefix.
        dedup: Whether to merge records sharing a KvK/VAT (off = one per source).

    Returns:
        tuple[pl.DataFrame, dict[str, str], pl.DataFrame]: the ``res.partner``
        frame, the ``source_id`` → surviving external-id alias map, and the
        ``res.partner.bank`` frame (one row per survivor that carries a bank).
    """
    partners = ledger.partners.sort("source_id").to_dicts()
    groups = _partner_groups(partners, dedup=dedup)
    alias: dict[str, str] = {}
    for survivor, members in groups.items():
        survivor_xmlid = _xmlid(prefix, "partner", survivor)
        for member in members:
            alias[str(member["source_id"])] = survivor_xmlid

    out_rows: list[dict[str, object]] = []
    bank_rows: list[dict[str, object]] = []
    for survivor in sorted(groups):
        members = groups[survivor]
        roles = {str(m["role"]) for m in members}
        is_customer = bool(roles & {"customer", "both"})
        is_supplier = bool(roles & {"supplier", "both"})

        def _first(column: str) -> str:
            for member in members:  # noqa: B023 - evaluated eagerly per survivor
                value = member.get(column)
                if value:
                    return str(value)
            return ""

        partner_xmlid = _xmlid(prefix, "partner", survivor)
        out_rows.append(
            {
                "id": partner_xmlid,
                "name": _first("name") or survivor,
                "is_company": "True",
                "customer_rank": "1" if is_customer else "0",
                "supplier_rank": "1" if is_supplier else "0",
                "vat": _first("vat"),
                "company_registry": _first("kvk"),
                "street": _first("street"),
                "city": _first("city"),
                "zip": _first("postal_code"),
                "country_id/id": _country_xmlid(_first("country")),
            }
        )
        bank = _first("bank_account")
        if bank:
            bank_rows.append(
                {
                    "id": _xmlid(prefix, "partner_bank", survivor),
                    "partner_id/id": partner_xmlid,
                    "acc_number": bank,
                }
            )
    columns = [
        "id",
        "name",
        "is_company",
        "customer_rank",
        "supplier_rank",
        "vat",
        "company_registry",
        "street",
        "city",
        "zip",
        "country_id/id",
    ]
    frame = pl.DataFrame(out_rows, schema={c: pl.String for c in columns})
    banks = pl.DataFrame(bank_rows, schema={c: pl.String for c in _BANK_COLUMNS})
    return frame, alias, banks


# --- accounts & journals ----------------------------------------------------


def _accounts_frame(
    decisions: list[AccountDecision], prefix: str, matches: dict[str, str]
) -> pl.DataFrame:
    """Build the ``account.account`` frame of accounts to *create*.

    Accounts matched onto an existing record are omitted: they already exist in
    the target chart and are referenced, not re-created.

    Args:
        decisions: The per-account typing decisions.
        prefix: The external-id prefix.
        matches: Source account id → existing external id (matched accounts).

    Returns:
        pl.DataFrame: The ``account.account`` rows to create.
    """
    rows = [
        {
            "id": _xmlid(prefix, "account", d.account_id),
            "code": d.code,
            "name": d.name,
            "account_type": d.account_type,
            "reconcile": "True" if d.reconcile else "False",
        }
        for d in decisions
        if d.account_id not in matches
    ]
    columns = ["id", "code", "name", "account_type", "reconcile"]
    return pl.DataFrame(rows, schema={c: pl.String for c in columns})


def _account_map_frame(
    decisions: list[AccountDecision], matches: dict[str, str]
) -> pl.DataFrame:
    """Build the editable ``account_map.csv`` proposal frame.

    The ``target_xmlid`` column pins an account onto an existing Odoo record:
    fill it (by hand, or via ``--match-accounts``) and the account is referenced
    instead of created.

    Args:
        decisions: The per-account typing decisions.
        matches: Source account id → existing external id (matched accounts).

    Returns:
        pl.DataFrame: One row per account with its proposed type and source.
    """
    rows = [
        {
            "account_id": d.account_id,
            "code": d.code,
            "name": d.name,
            "account_type": d.account_type,
            "reconcile": "True" if d.reconcile else "False",
            "source": d.source,
            "target_xmlid": matches.get(d.account_id, ""),
        }
        for d in decisions
    ]
    columns = [
        "account_id",
        "code",
        "name",
        "account_type",
        "reconcile",
        "source",
        "target_xmlid",
    ]
    return pl.DataFrame(rows, schema={c: pl.String for c in columns})


def _classify_matches(
    decisions: list[AccountDecision],
    matches: dict[str, str],
    match_meta: dict[str, AccountMatch],
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """Check each RPC-matched pair's target type/reconcile against the source.

    An offline ``target_xmlid`` pin has no metadata (no RPC read), so it is not
    checked here — the user asserted it by hand.

    Args:
        decisions: The per-account typing decisions (the source side).
        matches: Source account id → referenced external id (all matches).
        match_meta: Source account id → target metadata (RPC matches only).

    Returns:
        tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]: the type
        mismatches (warn), the control errors (matched AR/AP whose target is not
        reconcilable or the wrong type — error), and ``account_id → status`` for
        the review file.
    """
    mismatches: list[str] = []
    control_errors: list[str] = []
    status: dict[str, str] = {}
    for decision in decisions:
        if decision.account_id not in matches:
            continue
        meta = match_meta.get(decision.account_id)
        if meta is None:
            status[decision.account_id] = "pinned"  # offline, not checked
            continue
        type_differs = meta.account_type != decision.account_type
        is_control = decision.account_type in _RECONCILE_TYPES
        if is_control and (not meta.reconcile or type_differs):
            control_errors.append(decision.account_id)
            status[decision.account_id] = "control_mismatch"
        elif type_differs:
            mismatches.append(decision.account_id)
            status[decision.account_id] = "type_mismatch"
        else:
            status[decision.account_id] = "ok"
    return tuple(sorted(mismatches)), tuple(sorted(control_errors)), status


def _account_matches_frame(
    decisions: list[AccountDecision],
    matches: dict[str, str],
    match_meta: dict[str, AccountMatch],
    status: dict[str, str],
) -> pl.DataFrame:
    """Build the reviewable account-match report (``account_matches.csv``).

    One row per matched account, pairing the source typing with the target
    account it was referenced onto and a status, so the match can be reviewed
    before anything loads.

    Args:
        decisions: The per-account typing decisions.
        matches: Source account id → referenced external id (all matches).
        match_meta: Source account id → target metadata (RPC matches only).
        status: Source account id → review status from :func:`_classify_matches`.

    Returns:
        pl.DataFrame: The report rows (:data:`_MATCH_REPORT_COLUMNS`).
    """
    rows: list[dict[str, str]] = []
    for decision in decisions:
        if decision.account_id not in matches:
            continue
        meta = match_meta.get(decision.account_id)
        rows.append(
            {
                "account_id": decision.account_id,
                "source_name": decision.name,
                "source_account_type": decision.account_type,
                "source_reconcile": "True" if decision.reconcile else "False",
                "target_xmlid": matches[decision.account_id],
                "target_name": meta.name if meta else "",
                "target_account_type": meta.account_type if meta else "",
                "target_reconcile": ("True" if meta.reconcile else "False")
                if meta
                else "",
                "status": status.get(decision.account_id, ""),
            }
        )
    return pl.DataFrame(rows, schema={c: pl.String for c in _MATCH_REPORT_COLUMNS})


def _journals_frame(
    ledger: CanonicalLedger,
    prefix: str,
    *,
    stage_sale_journal: bool = False,
    stage_purchase_journal: bool = False,
) -> pl.DataFrame:
    """Build the ``account.journal`` frame, including the opening journal.

    Args:
        ledger: The canonical ledger.
        prefix: The external-id prefix.
        stage_sale_journal: Also stage a dedicated sale journal for the cutover
            open-item invoices.
        stage_purchase_journal: Also stage a dedicated purchase journal for the
            cutover open-item bills.

    Returns:
        pl.DataFrame: The ``account.journal`` rows, codes de-duplicated.
    """
    rows: list[dict[str, str]] = []
    used_codes: set[str] = set()
    for row in ledger.journals.sort("journal_id").to_dicts():
        journal_id = str(row["journal_id"])
        rows.append(
            {
                "id": _xmlid(prefix, "journal", journal_id),
                "name": str(row.get("description") or journal_id),
                "code": _unique_code(journal_id, used_codes),
                "type": _JOURNAL_TYPE.get(str(row.get("kind") or ""), "general"),
            }
        )
    rows.append(
        {
            "id": f"{prefix}.{_OPENING_JOURNAL}",
            "name": "Opening balance",
            "code": _unique_code("OPEN", used_codes),
            "type": "general",
        }
    )
    if stage_sale_journal:
        rows.append(
            {
                "id": f"{prefix}.{_OPENING_SALE_JOURNAL}",
                "name": "Opening open items (sales)",
                "code": _unique_code("OPINS", used_codes),
                "type": "sale",
            }
        )
    if stage_purchase_journal:
        rows.append(
            {
                "id": f"{prefix}.{_OPENING_PURCHASE_JOURNAL}",
                "name": "Opening open items (purchases)",
                "code": _unique_code("OPINP", used_codes),
                "type": "purchase",
            }
        )
    columns = ["id", "name", "code", "type"]
    return pl.DataFrame(rows, schema={c: pl.String for c in columns})


def _unique_code(source: str, used: set[str]) -> str:
    """Derive a short, unique Odoo journal code from a source id.

    Args:
        source: The source journal id (or a seed like ``"OPEN"``).
        used: Codes already taken (mutated to record the new one).

    Returns:
        str: An uppercase code of at most 5 characters, unique within ``used``.
    """
    base = re.sub(r"[^A-Za-z0-9]", "", source).upper()[:5] or "JRN"
    code = base
    suffix = 1
    while code in used:
        tail = str(suffix)
        code = base[: 5 - len(tail)] + tail
        suffix += 1
    used.add(code)
    return code


# --- moves ------------------------------------------------------------------

_MOVE_COLUMNS: Final = [
    "id",
    "name",
    "journal_id/id",
    "date",
    "ref",
    "line_ids/id",
    "line_ids/account_id/id",
    "line_ids/partner_id/id",
    "line_ids/name",
    "line_ids/debit",
    "line_ids/credit",
]


def _money_str(value: Any) -> str:
    """Format a money cell as a fixed 2-decimal string.

    Args:
        value: A float/int/None amount (from a polars row dict).

    Returns:
        str: The amount with two decimals, e.g. ``"101.00"``; ``"0.00"`` for None.
    """
    return f"{float(value or 0.0):.2f}"


def _account_ref_map(
    decisions: list[AccountDecision], account_matches: dict[str, str], prefix: str
) -> dict[str, str]:
    """Map each source account id to the external id a line should reference.

    A matched account references the *existing* record's external id; every
    other account references the one this stage will create.

    Args:
        decisions: The per-account typing decisions.
        account_matches: Source account id → existing external id (matched).
        prefix: The external-id prefix.

    Returns:
        dict[str, str]: Source account id → external id to reference.
    """
    return {
        d.account_id: account_matches.get(d.account_id)
        or _xmlid(prefix, "account", d.account_id)
        for d in decisions
    }


def _resolve_account(account_ref: dict[str, str], prefix: str, account_id: str) -> str:
    """Resolve a line's account reference, falling back to a created id.

    Args:
        account_ref: Source account id → external id, from :func:`_account_ref_map`.
        prefix: The external-id prefix.
        account_id: The source account id, possibly ``""`` or off-chart.

    Returns:
        str: The external id to reference, or ``""`` when the line has no
        account. An account referenced but absent from the chart falls back to
        its would-be created id, which preflight flags as ``unknown_account``.
    """
    if not account_id:
        return ""
    return account_ref.get(account_id) or _xmlid(prefix, "account", account_id)


def _history_move_rows(
    ledger: CanonicalLedger,
    prefix: str,
    alias: dict[str, str],
    account_ref: dict[str, str],
) -> list[dict[str, str]]:
    """Build o2m rows for every transaction (full-history moves).

    Args:
        ledger: The canonical ledger.
        prefix: The external-id prefix.
        alias: The source-partner → external-id alias map.
        account_ref: Source account id → external id to reference.

    Returns:
        list[dict[str, str]]: Rows in the ``line_ids/...`` o2m shape.
    """
    moves = {str(m["move_key"]): m for m in ledger.moves.to_dicts()}
    rows: list[dict[str, str]] = []
    current: str | None = None
    for line in ledger.lines.sort(["move_key", "line_number"]).to_dicts():
        move_key = str(line["move_key"])
        header = move_key != current
        current = move_key
        move = moves.get(move_key, {})
        move_xmlid = _xmlid(prefix, "move", move_key)
        rows.append(
            _line_row(
                move_xmlid=move_xmlid,
                header=header,
                name=str(move.get("number") or move_key) if header else "",
                journal=(
                    _xmlid(prefix, "journal", str(move.get("journal_id") or ""))
                    if header and move.get("journal_id")
                    else ""
                ),
                date=_date_str(move.get("date")) if header else "",
                ref=str(move.get("doc_ref") or "") if header else "",
                line_number=int(line["line_number"]),
                account_xmlid=_resolve_account(
                    account_ref, prefix, str(line.get("account_id") or "")
                ),
                partner=alias.get(str(line.get("source_partner_id") or ""), ""),
                line_name=str(line.get("description") or ""),
                debit=_money_str(line.get("debit")),
                credit=_money_str(line.get("credit")),
            )
        )
    return rows


def _date_str(value: object) -> str:
    """Render a date cell as an ISO string, or empty.

    Args:
        value: A date or None.

    Returns:
        str: ISO ``YYYY-MM-DD``, or ``""`` when None.
    """
    return "" if value is None else str(value)


def _line_row(
    *,
    move_xmlid: str,
    header: bool,
    name: str,
    journal: str,
    date: str,
    ref: str,
    line_number: int,
    account_xmlid: str,
    partner: str,
    line_name: str,
    debit: str,
    credit: str,
) -> dict[str, str]:
    """Assemble one o2m move row.

    Args:
        move_xmlid: The move's external id (repeated on every line).
        header: Whether this is the move's first line (carries header fields).
        name: The move ``name`` (header only).
        journal: The journal external id (header only).
        date: The move date (header only).
        ref: The move reference (header only).
        line_number: The 1-based line ordinal within the move.
        account_xmlid: The account's external id reference (created or matched),
            or ``""``.
        partner: The partner external id, or ``""``.
        line_name: The line label.
        debit: The debit amount string.
        credit: The credit amount string.

    Returns:
        dict[str, str]: The row keyed by :data:`_MOVE_COLUMNS`.
    """
    return {
        # fluvo's --o2m groups a parent row (non-empty id) with the following
        # continuation rows (blank id); only the first line carries the move id.
        "id": move_xmlid if header else "",
        "name": name,
        "journal_id/id": journal,
        "date": date,
        "ref": ref,
        "line_ids/id": f"{move_xmlid}_l{line_number}",
        "line_ids/account_id/id": account_xmlid,
        "line_ids/partner_id/id": partner,
        "line_ids/name": line_name,
        "line_ids/debit": debit,
        "line_ids/credit": credit,
    }


def _effective_ref() -> pl.Expr:
    """Expression for a line's matching key: match_ref > invoice_ref > doc_ref.

    Returns:
        pl.Expr: The coalesced reference, empty string when none is present.
    """
    return pl.coalesce(
        [pl.col("match_ref"), pl.col("invoice_ref"), pl.col("doc_ref")]
    ).fill_null("")


#: The columns an open-item source frame is reduced to before netting.
_OPEN_ITEM_COLS: Final = [
    "account_id",
    "source_partner_id",
    "match_ref",
    "invoice_ref",
    "doc_ref",
    "debit",
    "credit",
]


def _open_items(ledger: CanonicalLedger, reconcile_accounts: set[str]) -> pl.DataFrame:
    """Net open AR/AP items per partner and matching key, at the cutover date.

    An open item is an invoice still unsettled at the end of the period. Its
    components come from two places: the prior-year invoices carried in the
    opening subledger, and this year's invoices and payments on the control
    accounts. They are matched on an explicit key, else the invoice reference,
    else the document reference — so a payment nets against its invoice and a
    fully-paid invoice drops out rather than leaving a phantom pair.

    Args:
        ledger: The canonical ledger.
        reconcile_accounts: Source account ids typed receivable/payable.

    Returns:
        pl.DataFrame: One row per still-open item (account_id, partner, doc_ref
        = the matching key, invoice_date, debit, credit), sorted
        deterministically.
    """
    schema = {
        "account_id": pl.String,
        "source_partner_id": pl.String,
        "doc_ref": pl.String,
        "invoice_date": pl.Date,
        "debit": pl.Float64,
        "credit": pl.Float64,
    }
    if not reconcile_accounts:
        return pl.DataFrame(schema=schema)
    accounts = list(reconcile_accounts)
    sub = ledger.opening_lines.filter(
        pl.col("source_partner_id").is_not_null() & pl.col("account_id").is_in(accounts)
    ).with_columns(pl.col("date").alias("item_date"))
    year = ledger.lines.filter(pl.col("account_id").is_in(accounts)).with_columns(
        pl.col("effective_date").alias("item_date")
    )
    cols = [*_OPEN_ITEM_COLS, "item_date"]
    sources = [f.select(cols) for f in (sub, year) if f.height]
    if not sources:
        return pl.DataFrame(schema=schema)
    combined = pl.concat(sources).with_columns(_effective_ref().alias("ref"))
    grouped = (
        combined.group_by(["account_id", "source_partner_id", "ref"])
        .agg(
            pl.col("debit").sum().alias("debit_sum"),
            pl.col("credit").sum().alias("credit_sum"),
            pl.col("item_date").min().alias("invoice_date"),
        )
        .with_columns((pl.col("debit_sum") - pl.col("credit_sum")).alias("net"))
        .filter(pl.col("net").abs() > _ZERO_TOLERANCE)
    )
    return (
        grouped.with_columns(
            pl.when(pl.col("net") > 0)
            .then(pl.col("net"))
            .otherwise(0.0)
            .alias("debit"),
            pl.when(pl.col("net") < 0)
            .then(-pl.col("net"))
            .otherwise(0.0)
            .alias("credit"),
            pl.col("ref").alias("doc_ref"),
        )
        .select(
            [
                "account_id",
                "source_partner_id",
                "doc_ref",
                "invoice_date",
                "debit",
                "credit",
            ]
        )
        .sort(["account_id", "source_partner_id", "doc_ref"])
    )


def _net_by_account(frame: pl.DataFrame) -> dict[str, float]:
    """Return the signed (debit - credit) net per account id.

    Args:
        frame: A frame with ``account_id``, ``debit`` and ``credit`` columns.

    Returns:
        dict[str, float]: Account id → net; absent accounts net to zero.
    """
    if frame.height == 0:
        return {}
    grouped = frame.group_by("account_id").agg(
        (pl.col("debit").sum() - pl.col("credit").sum()).alias("net")
    )
    return {str(r["account_id"]): float(r["net"]) for r in grouped.to_dicts()}


def _spec(account_id: str, partner: str, net: float) -> tuple[str, str, str, str]:
    """Turn a signed net into an ``(account, partner, debit, credit)`` spec.

    Args:
        account_id: The source account id.
        partner: The partner external id, or ``""``.
        net: The signed amount (positive = debit).

    Returns:
        tuple[str, str, str, str]: The opening-line spec.
    """
    debit = net if net > 0 else 0.0
    credit = -net if net < 0 else 0.0
    return (account_id, partner, _money_str(debit), _money_str(credit))


def _cutover_specs(  # noqa: C901 - a flat per-account classification chain
    ledger: CanonicalLedger,
    account_types: dict[str, str],
    reconcile_accounts: set[str],
    open_items: pl.DataFrame,
    result_account: str | None,
    clearing_account: str,
) -> list[tuple[str, str, str, str]]:
    """Build the cutover opening lines: the closing position at the period end.

    Every account's closing balance is its opening balance plus the year's
    movement. Balance-sheet and (by default) profit-and-loss accounts carry
    their closing balance directly. The receivable/payable control accounts are
    **not** on the opening move: each open item is a separate invoice (see
    :func:`_open_invoice_rows`), so the opening move instead carries, on the
    **clearing account**, the counter-amount of every open item (which nets to
    zero against those invoices), plus a lumped remainder on each control account
    for any opening balance a subledger did not itemise. With ``result_account``
    set, profit-and-loss accounts close into that one account. The whole set is
    debit/credit balanced by construction.

    Args:
        ledger: The canonical ledger.
        account_types: Source account id → chosen Odoo account type.
        reconcile_accounts: Source account ids typed receivable/payable.
        open_items: The netted open items from :func:`_open_items`.
        result_account: An account id to close profit-and-loss into, or None to
            carry each profit-and-loss account's closing balance.
        clearing_account: The open-item clearing account id.

    Returns:
        list[tuple[str, str, str, str]]: ``(account, partner, debit, credit)``
        specs for the opening move.
    """
    movement = _net_by_account(ledger.lines)
    gl_opening = ledger.opening_lines.filter(pl.col("source_partner_id").is_null())
    sub_opening = ledger.opening_lines.filter(pl.col("source_partner_id").is_not_null())
    opening_gl = _net_by_account(gl_opening)
    subledger = _net_by_account(sub_opening)

    specs: list[tuple[str, str, str, str]] = []
    pl_net = 0.0
    for account_id in sorted(set(opening_gl) | set(movement)):
        if account_id in reconcile_accounts:
            continue  # on the open-item invoices, not here
        net = opening_gl.get(account_id, 0.0) + movement.get(account_id, 0.0)
        is_pl = account_types.get(account_id, "") in _PL_TYPES
        if result_account and account_id == result_account:
            continue  # folded in with pl_net below
        if result_account and is_pl:
            pl_net += net
            continue
        if abs(net) > _ZERO_TOLERANCE:
            specs.append(_spec(account_id, "", net))
    if result_account:
        own = opening_gl.get(result_account, 0.0) + movement.get(result_account, 0.0)
        if abs(own + pl_net) > _ZERO_TOLERANCE:
            specs.append(_spec(result_account, "", own + pl_net))

    # The clearing counter: + the net of every open item (the open-item invoices
    # post the opposite side on the clearing account, so the two cancel).
    items = open_items.to_dicts()
    clearing_net = sum(
        float(r.get("debit") or 0.0) - float(r.get("credit") or 0.0) for r in items
    )
    if abs(clearing_net) > _ZERO_TOLERANCE:
        specs.append(_spec(clearing_account, "", clearing_net))
    # A lumped remainder on each control account for opening not itemised by a
    # subledger (e.g. 4.0, which has no subledger at all).
    for account_id in sorted(reconcile_accounts):
        remainder = opening_gl.get(account_id, 0.0) - subledger.get(account_id, 0.0)
        if abs(remainder) > _ZERO_TOLERANCE:
            specs.append(_spec(account_id, "", remainder))
    return specs


def _open_invoice_rows(
    open_items: pl.DataFrame,
    account_types: dict[str, str],
    alias: dict[str, str],
    prefix: str,
    clearing_xmlid: str,
    default_payment_days: int,
    cutover_date: str,
    sale_journal_xmlid: str,
    purchase_journal_xmlid: str,
) -> tuple[list[dict[str, str]], int, tuple[str, ...]]:
    """Turn each open item into a draft invoice/bill row on the clearing account.

    Each item becomes its own ``account.move`` with a real ``move_type`` and one
    tax-free line carrying the residual open amount on the clearing account; the
    receivable/payable line lands on the source control account via the partner's
    staged property. The source document number is kept in ``ref`` (the move
    ``name`` is left to Odoo's sequence, so the cutover date cannot clash with a
    historical number's year), and on customer invoices/refunds it is also set as
    ``payment_reference`` so reminders and bank matching keep recognising it. The
    invoice date, due date and partner are
    kept. The **accounting date** is the cutover date so the entry lands in the
    open period, and the move posts to a dedicated sale/purchase journal rather
    than whatever default Odoo would pick. Because the file carries no due date,
    it is derived from the invoice date plus ``default_payment_days`` and the
    count is reported.

    Args:
        open_items: The netted open items from :func:`_open_items`.
        account_types: Source account id → chosen Odoo account type.
        alias: The source-partner → external-id alias map.
        prefix: The external-id prefix.
        clearing_xmlid: The clearing account's external id.
        default_payment_days: Days added to the invoice date for the due date.
        cutover_date: The accounting date (period end) for every open-item move.
        sale_journal_xmlid: The journal external id for sale invoices/refunds.
        purchase_journal_xmlid: The journal external id for bills/refunds.

    Returns:
        tuple[list[dict[str, str]], int, tuple[str, ...]]: the invoice rows, how
        many due dates were derived, and the external ids of reversed-sign
        (refund / unallocated) items for preflight to flag.
    """
    rows: list[dict[str, str]] = []
    derived_due = 0
    refunds: list[str] = []
    for item in open_items.to_dicts():
        account_id = str(item["account_id"])
        is_sale = account_types.get(account_id) == "asset_receivable"
        kind = "receivable" if is_sale else "payable"
        net = float(item.get("debit") or 0.0) - float(item.get("credit") or 0.0)
        # Normal side: receivable is a debit, payable a credit. A reversed sign
        # is a credit note / unallocated item -> a refund document.
        reversed_sign = net < 0 if kind == "receivable" else net > 0
        move_type = _MOVE_TYPE[(kind, reversed_sign)]
        if reversed_sign:
            partner_src = str(item.get("source_partner_id") or "")
            refunds.append(_open_invoice_xmlid(prefix, account_id, partner_src, item))
        inv_date = item.get("invoice_date")
        invoice_date = _date_str(inv_date)
        due = _due_date_str(inv_date, default_payment_days)
        derived_due += 1 if due else 0
        ref = str(item.get("doc_ref") or "")
        # The source document number (both invoices and bills) stays in ``ref``;
        # the move ``name`` is left blank so Odoo numbers the entry from the
        # opening journal's own sequence. Forcing the historical number as the
        # name would clash with the cutover accounting date — Odoo ties a move's
        # sequence to its date and rejects e.g. a "2023-…" number dated at a 2024
        # period end (strictly so on Odoo 16).
        name = ""
        # On customer invoices/refunds the number also goes to
        # ``payment_reference`` — the communication a customer pays with — so
        # follow-up reminders and bank-statement matching still recognise the
        # migrated document. Bills use ``ref`` only (the payment reference of a
        # bill is our outgoing payment's, which we are not migrating here).
        payment_reference = ref if is_sale else ""
        inv_xmlid = _open_invoice_xmlid(
            prefix, account_id, str(item.get("source_partner_id") or ""), item
        )
        rows.append(
            {
                "id": inv_xmlid,
                "move_type": move_type,
                "journal_id/id": sale_journal_xmlid
                if is_sale
                else purchase_journal_xmlid,
                "partner_id/id": alias.get(
                    str(item.get("source_partner_id") or ""), ""
                ),
                "date": cutover_date,
                "invoice_date": invoice_date,
                "invoice_date_due": due,
                "ref": ref,
                "payment_reference": payment_reference,
                "name": name,
                "narration": "Opening open-item residual (migrated).",
                # ADR 0001 deterministic line id, so a re-run updates the line
                # rather than appending a duplicate (idempotent invoice).
                "invoice_line_ids/id": f"{inv_xmlid}_l1",
                "invoice_line_ids/name": ref or "Opening balance",
                "invoice_line_ids/account_id/id": clearing_xmlid,
                "invoice_line_ids/quantity": "1",
                "invoice_line_ids/price_unit": _money_str(abs(net)),
                "invoice_line_ids/tax_ids/id": "",
            }
        )
    return rows, derived_due, tuple(refunds)


def _open_invoice_xmlid(
    prefix: str, account_id: str, partner_src: str, item: dict[str, Any]
) -> str:
    """Deterministic external id for an open-item invoice.

    Args:
        prefix: The external-id prefix.
        account_id: The control account id.
        partner_src: The source partner id.
        item: The open-item row (for its document reference).

    Returns:
        str: ``<prefix>.openinv_<account>_<partner>_<ref>`` sanitised.
    """
    ref = str(item.get("doc_ref") or "")
    return _xmlid(prefix, "openinv", f"{account_id}_{partner_src}_{ref}")


def _due_date_str(invoice_date: object, payment_days: int) -> str:
    """Derive a due date ``payment_days`` after the invoice date.

    Args:
        invoice_date: A ``datetime.date`` or None.
        payment_days: Days to add.

    Returns:
        str: ISO due date, or ``""`` when there is no invoice date.
    """
    if not isinstance(invoice_date, date):
        return ""
    return (invoice_date + timedelta(days=payment_days)).isoformat()


def _partner_control_accounts(
    open_items: pl.DataFrame, account_types: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], tuple[str, ...]]:
    """Map each partner to the control account its open items sit on.

    Args:
        open_items: The netted open items.
        account_types: Source account id → chosen Odoo account type.

    Returns:
        tuple[dict[str, str], dict[str, str], tuple[str, ...]]: receivable and
        payable ``source_partner_id → account_id`` maps, and the partner ids
        whose items span more than one control account of a kind (ambiguous).
    """
    recv: dict[str, set[str]] = {}
    pay: dict[str, set[str]] = {}
    for item in open_items.to_dicts():
        partner = str(item.get("source_partner_id") or "")
        if not partner:
            continue
        account_id = str(item["account_id"])
        bucket = recv if account_types.get(account_id) == "asset_receivable" else pay
        bucket.setdefault(partner, set()).add(account_id)
    ambiguous = sorted(
        {p for m in (recv, pay) for p, accts in m.items() if len(accts) > 1}
    )
    recv_one = {p: sorted(accts)[0] for p, accts in recv.items()}
    pay_one = {p: sorted(accts)[0] for p, accts in pay.items()}
    return recv_one, pay_one, tuple(ambiguous)


def _history_opening_specs(ledger: CanonicalLedger) -> list[tuple[str, str, str, str]]:
    """Build the full-history opening lines: the GL opening balances, as-is.

    Args:
        ledger: The canonical ledger.

    Returns:
        list[tuple[str, str, str, str]]: ``(account, partner, debit, credit)``
        specs, one per GL opening line (subledger detail stays in the history).
    """
    gl = ledger.opening_lines.filter(pl.col("source_partner_id").is_null())
    return [
        (
            str(row.get("account_id") or ""),
            "",
            _money_str(row.get("debit")),
            _money_str(row.get("credit")),
        )
        for row in gl.sort("account_id").to_dicts()
    ]


def _opening_move_rows(
    prefix: str,
    specs: list[tuple[str, str, str, str]],
    date: str,
    account_ref: dict[str, str],
) -> list[dict[str, str]]:
    """Render opening-line specs into the o2m move rows.

    Args:
        prefix: The external-id prefix.
        specs: ``(account, partner, debit, credit)`` lines for the opening move.
        date: The move date (ISO string), on the header line only.
        account_ref: Source account id → external id to reference.

    Returns:
        list[dict[str, str]]: The opening move's rows, or ``[]`` when empty.
    """
    if not specs:
        return []
    move_xmlid = f"{prefix}.{_OPENING_MOVE}"
    rows: list[dict[str, str]] = []
    for index, (account_id, partner, debit, credit) in enumerate(specs, start=1):
        rows.append(
            _line_row(
                move_xmlid=move_xmlid,
                header=index == 1,
                name="Opening balance" if index == 1 else "",
                journal=f"{prefix}.{_OPENING_JOURNAL}" if index == 1 else "",
                date=date if index == 1 else "",
                ref="opening" if index == 1 else "",
                line_number=index,
                account_xmlid=_resolve_account(account_ref, prefix, account_id),
                partner=partner,
                line_name="Opening balance",
                debit=debit,
                credit=credit,
            )
        )
    return rows


def transform(
    ledger: CanonicalLedger,
    *,
    prefix: str = "xaf",
    mode: str = MODE_FULL_HISTORY,
    account_overrides: dict[str, str] | None = None,
    reconcile_overrides: dict[str, bool] | None = None,
    account_matches: dict[str, str] | None = None,
    match_meta: dict[str, AccountMatch] | None = None,
    match_collisions: dict[str, str] | None = None,
    dedup: bool = True,
    result_account: str | None = None,
    clearing_account: str | None = None,
    default_payment_days: int = _DEFAULT_PAYMENT_DAYS,
    open_sale_journal: str | None = None,
    open_purchase_journal: str | None = None,
    default_receivable: str | None = None,
    default_payable: str | None = None,
) -> TransformResult:
    """Transform a canonical ledger into Odoo-shaped frames.

    Args:
        ledger: The canonical ledger to transform.
        prefix: The external-id prefix (the import module name).
        mode: ``full_history`` or ``cutover`` (see the module docstring).
        account_overrides: A user ``account_id`` → ``account_type`` map, from an
            edited ``account_map.csv``; always wins over the mapper chain.
        reconcile_overrides: A user ``account_id`` → reconcile flag from the same
            edited map; absent accounts stay reconcilable-by-type.
        account_matches: A user ``account_id`` → existing Odoo external id map,
            for accounts that already exist in the target chart. Matched
            accounts are referenced, never created and never modified.
        match_meta: Target metadata (name, type, reconcile) for the RPC-resolved
            matches, used to check each matched pair agrees and to build the
            review file; offline ``target_xmlid`` pins have no metadata.
        match_collisions: Account code → reason it is present in the target but
            could not be matched (ambiguous / no external id); a source account
            with such a code must not be re-created, so it becomes an error.
        dedup: Merge customer/supplier records sharing a KvK/VAT (default on).
        result_account: In cutover, a source account id to close the
            profit-and-loss accounts into; None carries each P&L balance.
        clearing_account: In cutover, the account id of the open-item clearing
            account; a synthetic default is used (and staged) when omitted.
        default_payment_days: Days after the invoice date used to derive an
            open-item invoice's due date (the file carries none).
        open_sale_journal: In cutover, a source journal id to post sale open-item
            invoices to; a dedicated sale journal is staged when omitted.
        open_purchase_journal: In cutover, a source journal id for the bills; a
            dedicated purchase journal is staged when omitted.
        default_receivable: The target company's default partner receivable
            account external id (from a ``--match-accounts`` read). A matched
            source control equal to it is left to Odoo's default rather than
            force-staged on the partner; None always stages the property.
        default_payable: The same for the default partner payable account.

    Returns:
        TransformResult: The Odoo-shaped frames, alias map, account proposals,
        the open-item breakdown and the open-item invoices.

    Raises:
        InputError: If ``mode`` is not a supported mode.
    """
    if mode not in MODES:
        raise InputError(f"unknown mode '{mode}'; expected one of {list(MODES)}.")
    overrides = account_overrides or {}
    matches = account_matches or {}
    meta = match_meta or {}
    collisions = match_collisions or {}
    decisions = _decide_accounts(ledger, overrides, reconcile_overrides)
    reconcile_accounts = {d.account_id for d in decisions if d.reconcile}
    account_types = {d.account_id: d.account_type for d in decisions}
    heuristic = tuple(
        d.account_id for d in decisions if d.source in (SRC_HEURISTIC, SRC_FALLBACK)
    )
    conflicting = tuple(d.account_id for d in decisions if d.kind_conflict)
    matched = tuple(d.account_id for d in decisions if d.account_id in matches)
    type_mismatches, control_errors, match_status = _classify_matches(
        decisions, matches, meta
    )
    # A source account whose code exists in the target but could not be matched
    # would be re-created with a duplicate code: name it as an error, not a crash.
    collision_accounts = tuple(
        sorted(
            d.account_id
            for d in decisions
            if d.account_id in collisions and d.account_id not in matches
        )
    )
    matches_report = _account_matches_frame(decisions, matches, meta, match_status)
    account_ref = _account_ref_map(decisions, matches, prefix)

    partners, alias, partner_banks = _dedup_partners(ledger, prefix, dedup=dedup)
    accounts = _accounts_frame(decisions, prefix, matches)
    account_map = _account_map_frame(decisions, matches)
    open_items = _open_items(ledger, reconcile_accounts)

    open_invoices = _empty_open_invoices()
    clearing_id = clearing_account or _CLEARING_DEFAULT_CODE
    refund_items: tuple[str, ...] = ()
    ambiguous: tuple[str, ...] = ()
    derived_due = 0
    partner_receivable, partner_payable = _partner_property_frames({}, {}, {}, {})
    stage_sale = mode == MODE_CUTOVER and not open_sale_journal
    stage_purchase = mode == MODE_CUTOVER and not open_purchase_journal
    journals = _journals_frame(
        ledger,
        prefix,
        stage_sale_journal=stage_sale,
        stage_purchase_journal=stage_purchase,
    )

    if mode == MODE_CUTOVER:
        # Register (and, unless it already exists, create) the clearing account.
        account_ref[clearing_id] = matches.get(clearing_id) or _xmlid(
            prefix, "account", clearing_id
        )
        if clearing_id not in matches and clearing_id not in account_types:
            accounts = _with_clearing_account(accounts, prefix, clearing_id)
        sale_journal_xmlid = (
            _xmlid(prefix, "journal", open_sale_journal)
            if open_sale_journal
            else f"{prefix}.{_OPENING_SALE_JOURNAL}"
        )
        purchase_journal_xmlid = (
            _xmlid(prefix, "journal", open_purchase_journal)
            if open_purchase_journal
            else f"{prefix}.{_OPENING_PURCHASE_JOURNAL}"
        )
        opening_date = _date_str(ledger.header.get_column("date_end").item(0)) or (
            _date_str(ledger.header.get_column("date_start").item(0))
        )
        open_rows, derived_due, refund_items = _open_invoice_rows(
            open_items,
            account_types,
            alias,
            prefix,
            account_ref[clearing_id],
            default_payment_days,
            opening_date,
            sale_journal_xmlid,
            purchase_journal_xmlid,
        )
        open_invoices = pl.DataFrame(
            open_rows, schema={c: pl.String for c in _OPEN_INVOICE_COLUMNS}
        )
        recv, pay, ambiguous = _partner_control_accounts(open_items, account_types)
        partner_receivable, partner_payable = _partner_property_frames(
            alias, account_ref, recv, pay, default_receivable, default_payable
        )

        specs = _cutover_specs(
            ledger,
            account_types,
            reconcile_accounts,
            open_items,
            result_account,
            clearing_id,
        )
        rows = _opening_move_rows(prefix, specs, opening_date, account_ref)
    else:
        rows = _opening_move_rows(
            prefix,
            _history_opening_specs(ledger),
            _date_str(ledger.header.get_column("date_start").item(0)),
            account_ref,
        )
        rows = rows + _history_move_rows(ledger, prefix, alias, account_ref)
    moves = pl.DataFrame(rows, schema={c: pl.String for c in _MOVE_COLUMNS})

    alias_frame = pl.DataFrame(
        {
            "source_id": list(alias.keys()),
            "partner_xmlid": list(alias.values()),
        },
        schema={"source_id": pl.String, "partner_xmlid": pl.String},
    ).sort("source_id")

    return TransformResult(
        partners=partners,
        accounts=accounts,
        journals=journals,
        moves=moves,
        alias_map=alias_frame,
        account_map=account_map,
        open_items=open_items,
        open_invoices=open_invoices,
        partner_banks=partner_banks,
        mode=mode,
        prefix=prefix,
        partner_receivable=partner_receivable,
        partner_payable=partner_payable,
        account_matches=matches_report,
        clearing_account=clearing_id if mode == MODE_CUTOVER else "",
        clearing_xmlid=account_ref.get(clearing_id, "") if mode == MODE_CUTOVER else "",
        heuristic_accounts=heuristic,
        conflicting_accounts=conflicting,
        matched_accounts=matched,
        match_type_mismatches=type_mismatches,
        match_control_errors=control_errors,
        collision_accounts=collision_accounts,
        refund_items=refund_items,
        ambiguous_partners=ambiguous,
        derived_due_dates=derived_due,
    )


def _empty_open_invoices() -> pl.DataFrame:
    """An empty open-invoices frame with the declared columns.

    Returns:
        pl.DataFrame: Zero rows, the :data:`_OPEN_INVOICE_COLUMNS` schema.
    """
    return pl.DataFrame(schema={c: pl.String for c in _OPEN_INVOICE_COLUMNS})


def _with_clearing_account(
    accounts: pl.DataFrame, prefix: str, clearing_id: str
) -> pl.DataFrame:
    """Append the synthetic clearing account to the accounts-to-create frame.

    Args:
        accounts: The ``account.account`` frame.
        prefix: The external-id prefix.
        clearing_id: The clearing account code/id.

    Returns:
        pl.DataFrame: ``accounts`` with the clearing account appended.
    """
    row = pl.DataFrame(
        [
            {
                "id": _xmlid(prefix, "account", clearing_id),
                "code": clearing_id,
                "name": _CLEARING_NAME,
                "account_type": "asset_current",
                "reconcile": "False",
            }
        ],
        schema={c: pl.String for c in accounts.columns},
    )
    return pl.concat([accounts, row])


def _partner_property_frames(
    alias: dict[str, str],
    account_ref: dict[str, str],
    recv: dict[str, str],
    pay: dict[str, str],
    default_receivable: str | None = None,
    default_payable: str | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Build the receivable/payable property-override frames (cutover).

    An open-item invoice's control line must land on the **source** control
    account, which Odoo takes from the partner's company-dependent property. So
    the property is forced — but only for the partners that actually carry an
    open item of that kind, each in its own one-column update frame. Partners
    without an open item never appear, so their company default is left intact
    (writing a blank property cell would otherwise *clear* it). The update loads
    under the target company, so the company-dependent property is written there.

    When the matched source control already equals the target company's default
    receivable/payable account (``default_receivable``/``default_payable``, read
    over RPC in a ``--match-accounts`` run), forcing the property is redundant —
    Odoo would pick that account anyway — so that partner is dropped from the
    frame and left to the company default.

    Args:
        alias: Source partner id → surviving partner external id.
        account_ref: Source account id → external id to reference.
        recv: Source partner id → receivable control account id.
        pay: Source partner id → payable control account id.
        default_receivable: Target company default receivable account external
            id; a matched control equal to it is left to the default (None
            forces every open-item partner's property).
        default_payable: The same for the default payable account.

    Returns:
        tuple[pl.DataFrame, pl.DataFrame]: the receivable and payable property
        frames (``id`` plus the one property column), one row per overridden
        partner, sorted by id.
    """

    def _frame(
        mapping: dict[str, str], column: str, default: str | None
    ) -> pl.DataFrame:
        seen: dict[str, str] = {}
        for source_id, account_id in mapping.items():
            xmlid = alias.get(source_id, "")
            ref = account_ref.get(account_id, "")
            if xmlid and ref and ref != default:
                seen[xmlid] = ref
        rows = [{"id": xmlid, column: ref} for xmlid, ref in sorted(seen.items())]
        return pl.DataFrame(rows, schema={"id": pl.String, column: pl.String})

    return (
        _frame(recv, "property_account_receivable_id/id", default_receivable),
        _frame(pay, "property_account_payable_id/id", default_payable),
    )
