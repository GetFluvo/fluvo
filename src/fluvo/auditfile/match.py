"""Resolve audit-file account codes onto an existing Odoo chart, over RPC.

This is the one deliberately *online* helper in the connector. ``stage`` stays a
pure, offline compiler; when ``--match-accounts`` is given, the CLI uses this to
read the target company's chart of accounts and hand ``stage`` the match
metadata. Matched accounts are then *referenced* in the staged data, never
created and never modified — so an import into a company that already has an
``l10n_nl`` (or any) chart does not collide with, or overwrite, the accounts
already there.

The read is **company-scoped** (an account code is only unique within a company,
so a cross-company read would make every code look ambiguous) and captures not
just the external id but the target account's ``name``, ``account_type`` and
``reconcile`` flag, so the transform can check that a matched pair actually
agrees — a company's own numbering booked blindly onto an ``l10n_nl`` code of a
different type is a silent misposting the preflight now catches.

Only an account whose **code is unique** in the target company and which already
carries an external id is matched. A code that is ambiguous (several accounts) or
whose target account has no external id is reported as *present but unmatchable*:
it must not be re-created (the code already exists → a duplicate-code load
failure), so the transform turns it into a preflight error instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AccountMatch:
    """A source account code matched onto an existing target account.

    Attributes:
        code: The account code (shared by the source and the target account).
        xmlid: The existing target account's external id.
        name: The target account's name.
        account_type: The target account's Odoo ``account_type``.
        reconcile: Whether the target account is reconcilable.
    """

    code: str
    xmlid: str
    name: str
    account_type: str
    reconcile: bool


@dataclass(frozen=True)
class MatchResult:
    """The outcome of reading the target chart for a match.

    Attributes:
        matches: Account code → its :class:`AccountMatch` (unique code with an
            external id).
        unmatched_present: Account code → the reason it is present in the target
            but could not be matched (``"ambiguous"`` or ``"no external id"``).
            A source account with one of these codes must not be re-created.
    """

    matches: dict[str, AccountMatch] = field(default_factory=dict)
    unmatched_present: dict[str, str] = field(default_factory=dict)


def _resolve_company_id(connection: Any, company: str | None) -> int | None:
    """Resolve a ``--company`` value (database id or external id) to an id.

    Args:
        connection: A live Odoo connection.
        company: A company database id (digits) or external id
            (``module.name``), or None.

    Returns:
        int | None: The company's database id, or None when no company was
        given.

    Raises:
        ValueError: If an external id does not resolve to a company.
    """
    if not company:
        return None
    value = str(company).strip()
    if value.isdigit():
        return int(value)
    module, _, name = value.partition(".")
    rows = connection.get_model("ir.model.data").search_read(
        [["module", "=", module], ["name", "=", name], ["model", "=", "res.company"]],
        ["res_id"],
    )
    if not rows:
        raise ValueError(f"company external id {company!r} did not resolve.")
    return int(rows[0]["res_id"])


def _company_field(connection: Any) -> str | None:
    """Return the ``account.account`` company field present in this Odoo.

    Odoo 18 moved accounts to a shared ``company_ids`` many2many; earlier
    versions use a ``company_id`` many2one. Detect whichever exists.

    Args:
        connection: A live Odoo connection.

    Returns:
        str | None: ``"company_ids"``, ``"company_id"``, or None if neither is
        present (a single-company build).
    """
    fields = connection.get_model("account.account").fields_get(
        ["company_id", "company_ids"], ["type"]
    )
    if "company_ids" in fields:
        return "company_ids"
    if "company_id" in fields:
        return "company_id"
    return None


def resolve_account_matches(connection: Any, company: str | None = None) -> MatchResult:
    """Read the target chart and match each account code onto an existing one.

    The read is scoped to ``company`` (when given), because a code is only
    unique within a company. For each code it captures the target account's
    external id, name, type and reconcile flag; a code that is ambiguous or whose
    account has no external id is recorded as present-but-unmatchable rather than
    silently re-created.

    Args:
        connection: A live Odoo connection (as returned by
            :func:`fluvo.lib.conf_lib.get_connection_from_config`).
        company: The target company (database id or external id) to scope the
            read to; None reads every company (only safe on a single-company
            build).

    Returns:
        MatchResult: The matches and the present-but-unmatchable codes.
    """
    company_id = _resolve_company_id(connection, company)
    domain: list[Any] = []
    if company_id is not None:
        field_name = _company_field(connection)
        if field_name is not None:
            domain = [[field_name, "in", [company_id]]]
    accounts = connection.get_model("account.account").search_read(
        domain, ["code", "name", "account_type", "reconcile"]
    )
    rows_by_code: dict[str, list[dict[str, Any]]] = {}
    for account in accounts:
        code = str(account.get("code") or "").strip()
        if code:
            rows_by_code.setdefault(code, []).append(account)

    all_ids = [int(a["id"]) for rows in rows_by_code.values() for a in rows]
    xmlid_by_res: dict[int, str] = {}
    if all_ids:
        imd = connection.get_model("ir.model.data").search_read(
            [["model", "=", "account.account"], ["res_id", "in", all_ids]],
            ["res_id", "module", "name"],
        )
        xmlid_by_res = {
            int(row["res_id"]): f"{row['module']}.{row['name']}" for row in imd
        }

    matches: dict[str, AccountMatch] = {}
    unmatched: dict[str, str] = {}
    for code, rows in rows_by_code.items():
        if len(rows) != 1:
            unmatched[code] = "ambiguous"  # same code on several accounts
            continue
        account = rows[0]
        xmlid = xmlid_by_res.get(int(account["id"]))
        if not xmlid:
            unmatched[code] = "no external id"
            continue
        matches[code] = AccountMatch(
            code=code,
            xmlid=xmlid,
            name=str(account.get("name") or ""),
            account_type=str(account.get("account_type") or ""),
            reconcile=bool(account.get("reconcile")),
        )
    return MatchResult(matches=matches, unmatched_present=unmatched)
