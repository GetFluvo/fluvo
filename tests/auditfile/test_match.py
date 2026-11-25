"""Unit tests for the RPC chart-matching helper (``match.py``).

These stand in for a full two-company e2e: a fake connection records the
context each ``search_read`` is called with, so the tests can prove the read is
pinned to the *target* company — which matters because ``account.account.code``
is company-dependent on Odoo 18+, where a bare read would return the connecting
user's company's codes instead.
"""

from __future__ import annotations

from typing import Any

from fluvo.auditfile.match import resolve_account_matches

# Two companies, each with its own account carrying code "1300".
_ACCOUNTS = {
    1: [
        {
            "id": 11,
            "code": "1300",
            "name": "C1 Recv",
            "account_type": "asset_receivable",
            "reconcile": True,
        }
    ],
    2: [
        {
            "id": 21,
            "code": "1300",
            "name": "C2 Recv",
            "account_type": "asset_receivable",
            "reconcile": True,
        }
    ],
}
_ACCOUNT_XMLIDS = {11: ("l10n", "acc_c1"), 21: ("l10n", "acc_c2")}
_COMPANY_XMLIDS = {("base", "company_2"): 2}


class _Model:
    """A per-model fake exposing just the methods ``match.py`` calls."""

    def __init__(self, name: str, log: list[dict[str, Any]]) -> None:
        self._name = name
        self._log = log

    def fields_get(self, names: Any = None, attrs: Any = None) -> dict[str, Any]:
        # Simulate Odoo 18+: accounts live on a shared company_ids m2m.
        return {"company_ids": {"type": "many2many"}}

    def default_get(self, fields: Any = None, context: Any = None) -> dict[str, Any]:
        return {}

    def search_read(
        self,
        domain: Any = None,
        fields: Any = None,
        offset: int = 0,
        limit: Any = None,
        order: Any = None,
        context: Any = None,
    ) -> list[dict[str, Any]]:
        self._log.append({"model": self._name, "context": context or {}})
        if self._name == "ir.model.data":
            return self._imd(domain or [])
        if self._name == "account.account":
            cids = (context or {}).get("allowed_company_ids") or [1]
            return [a for cid in cids for a in _ACCOUNTS.get(cid, [])]
        return []

    @staticmethod
    def _imd(domain: list[Any]) -> list[dict[str, Any]]:
        terms = {
            d[0]: d[2] for d in domain if isinstance(d, (list, tuple)) and len(d) == 3
        }
        model = terms.get("model")
        if model == "res.company":
            key = (str(terms.get("module")), str(terms.get("name")))
            res_id = _COMPANY_XMLIDS.get(key)
            return [{"res_id": res_id}] if res_id else []
        if model == "account.account":
            want = set(terms.get("res_id") or [])
            return [
                {"res_id": rid, "module": mod, "name": nm}
                for rid, (mod, nm) in _ACCOUNT_XMLIDS.items()
                if rid in want
            ]
        return []


class _Connection:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_model(self, name: str) -> _Model:
        return _Model(name, self.calls)


def _account_context(conn: _Connection) -> dict[str, Any]:
    return next(c["context"] for c in conn.calls if c["model"] == "account.account")


def test_account_read_is_pinned_to_the_target_company_context() -> None:
    """The account read carries the target company in context, not just domain.

    On Odoo 18+ ``code`` is company-dependent, so the context must name the
    company or the wrong company's codes come back.
    """
    conn = _Connection()
    result = resolve_account_matches(conn, company="base.company_2")
    ctx = _account_context(conn)
    assert ctx.get("allowed_company_ids") == [2]
    assert ctx.get("company_id") == 2
    # And it matched company 2's account, not company 1's.
    assert result.matches["1300"].xmlid == "l10n.acc_c2"
    assert result.matches["1300"].name == "C2 Recv"


def test_numeric_company_is_honoured_in_context() -> None:
    """A numeric ``--company`` id pins the read to that company too."""
    conn = _Connection()
    result = resolve_account_matches(conn, company="1")
    assert _account_context(conn).get("allowed_company_ids") == [1]
    assert result.matches["1300"].xmlid == "l10n.acc_c1"


def test_no_company_leaves_the_context_empty() -> None:
    """Without ``--company`` the read is unscoped (single-company builds)."""
    conn = _Connection()
    resolve_account_matches(conn, company=None)
    assert _account_context(conn) == {}
