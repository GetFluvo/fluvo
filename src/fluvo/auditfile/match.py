"""Resolve audit-file account codes onto an existing Odoo chart, over RPC.

This is the one deliberately *online* helper in the connector. ``stage`` stays a
pure, offline compiler; when ``--match-accounts`` is given, the CLI uses this to
read the target company's chart of accounts and hand ``stage`` an
``account_id → existing external id`` map. Matched accounts are then *referenced*
in the staged data, never created and never modified — so an import into a
company that already has an ``l10n_nl`` (or any) chart does not collide with, or
overwrite, the accounts already there.

Only accounts whose **code is unique** in the target and which already carry an
external id are matched; an ambiguous code (the same code on several accounts,
e.g. per-company) or an account with no external id is left unmatched and
reported, rather than guessed.
"""

from __future__ import annotations

from typing import Any


def resolve_account_matches(connection: Any) -> dict[str, str]:
    """Read the target chart and map each account code to its external id.

    Args:
        connection: A live Odoo connection (as returned by
            :func:`fluvo.lib.conf_lib.get_connection_from_config`).

    Returns:
        dict[str, str]: ``account code`` → existing external id, for codes that
        are unique in the target and have an external id.
    """
    accounts = connection.get_model("account.account").search_read([], ["code"])
    ids_by_code: dict[str, list[int]] = {}
    for account in accounts:
        code = str(account.get("code") or "").strip()
        if code:
            ids_by_code.setdefault(code, []).append(int(account["id"]))

    all_ids = [i for ids in ids_by_code.values() for i in ids]
    if not all_ids:
        return {}
    imd = connection.get_model("ir.model.data").search_read(
        [["model", "=", "account.account"], ["res_id", "in", all_ids]],
        ["res_id", "module", "name"],
    )
    xmlid_by_res = {int(row["res_id"]): f"{row['module']}.{row['name']}" for row in imd}

    matches: dict[str, str] = {}
    for code, ids in ids_by_code.items():
        if len(ids) != 1:
            continue  # ambiguous (same code on several accounts); do not guess
        xmlid = xmlid_by_res.get(ids[0])
        if xmlid:
            matches[code] = xmlid
    return matches
