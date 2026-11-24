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
* ``cutover`` — a single opening move holding the **closing position at the end
  of the period**: each account's opening balance plus the year's movement.
  Balance-sheet (and, by default, profit-and-loss) accounts carry their closing
  balance directly; set ``result_account`` to close the profit-and-loss accounts
  into one result account instead, for a clean balance sheet. The AR/AP control
  accounts are broken out **per open item** — the prior-year opening subledger
  plus this year's lines, matched on an explicit key then invoice then document
  reference, so a settled invoice drops out — plus a lumped remainder for any
  opening balance a subledger did not itemise. The whole move balances by
  construction, and preflight re-checks it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
        mode: The mode the result was built for.
        prefix: The external-id prefix used.
        heuristic_accounts: Account ids whose type came from the numbering
            heuristic or the fallback (not an override or RGS) — the proposals
            preflight flags for review.
        conflicting_accounts: Account ids whose chosen Odoo type contradicts the
            source's balance/profit-loss kind — preflight flags these too.
    """

    partners: pl.DataFrame
    accounts: pl.DataFrame
    journals: pl.DataFrame
    moves: pl.DataFrame
    alias_map: pl.DataFrame
    account_map: pl.DataFrame
    open_items: pl.DataFrame
    mode: str
    prefix: str
    heuristic_accounts: tuple[str, ...] = field(default_factory=tuple)
    conflicting_accounts: tuple[str, ...] = field(default_factory=tuple)


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


def _dedup_partners(
    ledger: CanonicalLedger, prefix: str, *, dedup: bool = True
) -> tuple[pl.DataFrame, dict[str, str]]:
    """Merge customer/supplier records and build the alias map.

    Within a merged group the surviving external id is the lowest source id,
    both ranks are set if any member carries that role, and the first non-empty
    field wins. The country resolves to its Odoo external id so it is staged
    rather than silently dropped.

    Args:
        ledger: The canonical ledger.
        prefix: The external-id prefix.
        dedup: Whether to merge records sharing a KvK/VAT (off = one per source).

    Returns:
        tuple[pl.DataFrame, dict[str, str]]: The ``res.partner`` frame and the
        ``source_id`` → surviving external-id alias map.
    """
    partners = ledger.partners.sort("source_id").to_dicts()
    groups = _partner_groups(partners, dedup=dedup)
    alias: dict[str, str] = {}
    for survivor, members in groups.items():
        survivor_xmlid = _xmlid(prefix, "partner", survivor)
        for member in members:
            alias[str(member["source_id"])] = survivor_xmlid

    out_rows: list[dict[str, object]] = []
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

        out_rows.append(
            {
                "id": _xmlid(prefix, "partner", survivor),
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
    return frame, alias


# --- accounts & journals ----------------------------------------------------


def _accounts_frame(decisions: list[AccountDecision], prefix: str) -> pl.DataFrame:
    """Build the ``account.account`` frame from account decisions.

    Args:
        decisions: The per-account typing decisions.
        prefix: The external-id prefix.

    Returns:
        pl.DataFrame: The ``account.account`` rows.
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
    ]
    columns = ["id", "code", "name", "account_type", "reconcile"]
    return pl.DataFrame(rows, schema={c: pl.String for c in columns})


def _account_map_frame(decisions: list[AccountDecision]) -> pl.DataFrame:
    """Build the editable ``account_map.csv`` proposal frame.

    Args:
        decisions: The per-account typing decisions.

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
        }
        for d in decisions
    ]
    columns = ["account_id", "code", "name", "account_type", "reconcile", "source"]
    return pl.DataFrame(rows, schema={c: pl.String for c in columns})


def _journals_frame(ledger: CanonicalLedger, prefix: str) -> pl.DataFrame:
    """Build the ``account.journal`` frame, including the opening journal.

    Args:
        ledger: The canonical ledger.
        prefix: The external-id prefix.

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


def _history_move_rows(
    ledger: CanonicalLedger, prefix: str, alias: dict[str, str]
) -> list[dict[str, str]]:
    """Build o2m rows for every transaction (full-history moves).

    Args:
        ledger: The canonical ledger.
        prefix: The external-id prefix.
        alias: The source-partner → external-id alias map.

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
                account_id=str(line.get("account_id") or ""),
                partner=alias.get(str(line.get("source_partner_id") or ""), ""),
                line_name=str(line.get("description") or ""),
                debit=_money_str(line.get("debit")),
                credit=_money_str(line.get("credit")),
                prefix=prefix,
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
    account_id: str,
    partner: str,
    line_name: str,
    debit: str,
    credit: str,
    prefix: str,
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
        account_id: The source account id for this line.
        partner: The partner external id, or ``""``.
        line_name: The line label.
        debit: The debit amount string.
        credit: The credit amount string.
        prefix: The external-id prefix.

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
        "line_ids/account_id/id": (
            _xmlid(prefix, "account", account_id) if account_id else ""
        ),
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
        = the matching key, debit, credit), sorted deterministically.
    """
    schema = {
        "account_id": pl.String,
        "source_partner_id": pl.String,
        "doc_ref": pl.String,
        "debit": pl.Float64,
        "credit": pl.Float64,
    }
    if not reconcile_accounts:
        return pl.DataFrame(schema=schema)
    accounts = list(reconcile_accounts)
    sub = ledger.opening_lines.filter(
        pl.col("source_partner_id").is_not_null() & pl.col("account_id").is_in(accounts)
    )
    year = ledger.lines.filter(pl.col("account_id").is_in(accounts))
    sources = [f.select(_OPEN_ITEM_COLS) for f in (sub, year) if f.height]
    if not sources:
        return pl.DataFrame(schema=schema)
    combined = pl.concat(sources).with_columns(_effective_ref().alias("ref"))
    grouped = (
        combined.group_by(["account_id", "source_partner_id", "ref"])
        .agg(
            pl.col("debit").sum().alias("debit_sum"),
            pl.col("credit").sum().alias("credit_sum"),
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
        .select(["account_id", "source_partner_id", "doc_ref", "debit", "credit"])
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
    alias: dict[str, str],
    account_types: dict[str, str],
    reconcile_accounts: set[str],
    open_items: pl.DataFrame,
    result_account: str | None,
) -> list[tuple[str, str, str, str]]:
    """Build the cutover opening lines: the closing position at the period end.

    Every account's closing balance is its opening balance plus the year's
    movement. Balance-sheet and (by default) profit-and-loss accounts carry
    their closing balance directly; the receivable/payable control accounts are
    broken out per open item plus a lumped remainder for any opening balance not
    itemised by a subledger. With ``result_account`` set, profit-and-loss
    accounts are instead closed into that one account, leaving a clean balance
    sheet. The whole set is debit/credit balanced by construction.

    Args:
        ledger: The canonical ledger.
        alias: The source-partner → external-id alias map.
        account_types: Source account id → chosen Odoo account type.
        reconcile_accounts: Source account ids typed receivable/payable.
        open_items: The netted open items from :func:`_open_items`.
        result_account: An account id to close profit-and-loss into, or None to
            carry each profit-and-loss account's closing balance.

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
            continue  # itemised below
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

    for row in open_items.to_dicts():
        specs.append(
            (
                str(row["account_id"]),
                alias.get(str(row.get("source_partner_id") or ""), ""),
                _money_str(row.get("debit")),
                _money_str(row.get("credit")),
            )
        )
    for account_id in sorted(reconcile_accounts):
        remainder = opening_gl.get(account_id, 0.0) - subledger.get(account_id, 0.0)
        if abs(remainder) > _ZERO_TOLERANCE:
            specs.append(_spec(account_id, "", remainder))
    return specs


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
) -> list[dict[str, str]]:
    """Render opening-line specs into the o2m move rows.

    Args:
        prefix: The external-id prefix.
        specs: ``(account, partner, debit, credit)`` lines for the opening move.
        date: The move date (ISO string), on the header line only.

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
                account_id=account_id,
                partner=partner,
                line_name="Opening balance",
                debit=debit,
                credit=credit,
                prefix=prefix,
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
    dedup: bool = True,
    result_account: str | None = None,
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
        dedup: Merge customer/supplier records sharing a KvK/VAT (default on).
        result_account: In cutover, a source account id to close the
            profit-and-loss accounts into; None carries each P&L balance.

    Returns:
        TransformResult: The Odoo-shaped frames, alias map, account proposals
        and the open-item breakdown.

    Raises:
        InputError: If ``mode`` is not a supported mode.
    """
    if mode not in MODES:
        raise InputError(f"unknown mode '{mode}'; expected one of {list(MODES)}.")
    overrides = account_overrides or {}
    decisions = _decide_accounts(ledger, overrides, reconcile_overrides)
    reconcile_accounts = {d.account_id for d in decisions if d.reconcile}
    account_types = {d.account_id: d.account_type for d in decisions}
    heuristic = tuple(
        d.account_id for d in decisions if d.source in (SRC_HEURISTIC, SRC_FALLBACK)
    )
    conflicting = tuple(d.account_id for d in decisions if d.kind_conflict)

    partners, alias = _dedup_partners(ledger, prefix, dedup=dedup)
    accounts = _accounts_frame(decisions, prefix)
    account_map = _account_map_frame(decisions)
    journals = _journals_frame(ledger, prefix)
    open_items = _open_items(ledger, reconcile_accounts)

    if mode == MODE_CUTOVER:
        specs = _cutover_specs(
            ledger, alias, account_types, reconcile_accounts, open_items, result_account
        )
        opening_date = _date_str(ledger.header.get_column("date_end").item(0)) or (
            _date_str(ledger.header.get_column("date_start").item(0))
        )
        rows = _opening_move_rows(prefix, specs, opening_date)
    else:
        rows = _opening_move_rows(
            prefix,
            _history_opening_specs(ledger),
            _date_str(ledger.header.get_column("date_start").item(0)),
        )
        rows = rows + _history_move_rows(ledger, prefix, alias)
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
        mode=mode,
        prefix=prefix,
        heuristic_accounts=heuristic,
        conflicting_accounts=conflicting,
    )
