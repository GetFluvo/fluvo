r"""#359 verification probe — observe real `account.move` posting semantics.

This is **exploratory**, not an assertion suite: it drives a live Odoo over RPC
and records what actually happens when a draft move is posted, so the positions
ADR 0001 marks *pending #359 verification* can be replaced with verified answers
per Odoo version. It writes a JSON findings file (``FLUVO_E2E_PROBE_OUT``) and
never asserts behaviour — only that the probe ran — so a surprising result is
captured for review rather than turning red. Every probe is isolated: a failure
in one is recorded and the rest still run.

Run per version, e.g.::

    FLUVO_E2E_ODOO_VERSION=18.0 FLUVO_E2E_PROBE_OUT=/tmp/probe_18.json \\
        uv run pytest tests/e2e/probe_post_359.py -o addopts= -p no:cacheprovider -s

Probes (ADR 0001 "Resolved positions" + the brief):
  A  a preset ``name`` on a draft survives ``action_post`` (same-year; and a
     number whose year differs from the move date — create-time vs post-time);
  B  posting a move dated on/before a company lock date: raise, or silent shift?
  C  batch post with one bad move: isolation / half-posted state;
  D  re-load (write) onto a posted move: the exact error, to classify;
  E  ``payment_reference`` and a forced receivable account survive posting.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any


def _err(exc: Exception) -> str:
    """Pull the Odoo exception class + human message from an RPC fault."""
    error = getattr(exc, "error", None)
    if isinstance(error, dict):
        data = error.get("data") or {}
        name = str(data.get("name") or "")
        msg = " ".join(str(data.get("message") or "").split())
        return f"{name}: {msg}"[:600]
    return " ".join((str(exc) or repr(exc)).split())[:600]


def _safe(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a probe section; record a section-level fault instead of aborting."""
    try:
        return fn()
    except Exception as exc:
        return {"_section_error": _err(exc)}


def _install_account(rpc: Any) -> None:
    mods = rpc.get_model("ir.module.module")
    rows = mods.search_read([["name", "=", "account"]], ["state"])
    if rows and rows[0]["state"] not in ("installed", "to upgrade"):
        mods.button_immediate_install([rows[0]["id"]])


def _main_company(rpc: Any) -> int:
    return int(rpc.get_model("res.company").search_read([], ["id"], limit=1)[0]["id"])


def _mk_account(rpc: Any, code: str, name: str, atype: str, reconcile: bool) -> int:
    model = rpc.get_model("account.account")
    found = model.search_read([["code", "=", code]], ["id"])
    if found:
        return int(found[0]["id"])
    return int(
        model.create(
            {"code": code, "name": name, "account_type": atype, "reconcile": reconcile}
        )
    )


def _mk_journal(rpc: Any, code: str, name: str, jtype: str) -> int:
    model = rpc.get_model("account.journal")
    found = model.search_read([["code", "=", code]], ["id"])
    if found:
        return int(found[0]["id"])
    return int(model.create({"name": name, "code": code, "type": jtype}))


def _entry(
    rpc: Any,
    journal: int,
    debit_acc: int,
    credit_acc: int,
    date: str,
    name: str | None,
    amount: float = 100.0,
) -> int:
    """Create a balanced two-line draft journal entry; return its id."""
    vals: dict[str, Any] = {
        "move_type": "entry",
        "date": date,
        "journal_id": journal,
        "line_ids": [
            (
                0,
                0,
                {"account_id": debit_acc, "name": "D", "debit": amount, "credit": 0.0},
            ),
            (
                0,
                0,
                {"account_id": credit_acc, "name": "C", "debit": 0.0, "credit": amount},
            ),
        ],
    }
    if name is not None:
        vals["name"] = name
    return int(rpc.get_model("account.move").create(vals))


def _state(rpc: Any, move_id: int) -> dict[str, Any]:
    row = rpc.get_model("account.move").read(
        [move_id], ["name", "state", "date", "payment_reference"]
    )[0]
    return {
        "name": row.get("name"),
        "state": row.get("state"),
        "date": row.get("date"),
        "payment_reference": row.get("payment_reference"),
    }


def _post(rpc: Any, ids: list[int]) -> dict[str, Any]:
    try:
        rpc.get_model("account.move").action_post(ids)
        return {"raised": False}
    except Exception as exc:
        return {"raised": True, "error": _err(exc)}


def _try(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        call()
        return {"raised": False}
    except Exception as exc:
        return {"raised": True, "error": _err(exc)}


def _lock_fields(rpc: Any) -> list[str]:
    present = rpc.get_model("res.company").fields_get(
        ["fiscalyear_lock_date", "period_lock_date"], ["type"]
    )
    return [f for f in ("fiscalyear_lock_date", "period_lock_date") if f in present]


def _recv_account(rpc: Any, move_id: int) -> Any:
    rows = rpc.get_model("account.move.line").search_read(
        [
            ["move_id", "=", move_id],
            ["account_id.account_type", "=", "asset_receivable"],
        ],
        ["account_id"],
    )
    return rows[0]["account_id"] if rows else None


def test_probe_account_move_post(rpc: Any) -> None:  # noqa: C901
    """Drive the probes and dump findings; only fails if the probe can't run."""
    version = os.environ.get("FLUVO_E2E_ODOO_VERSION", "unknown")
    out = os.environ.get("FLUVO_E2E_PROBE_OUT", f"/tmp/probe_{version}.json")  # noqa: S108
    _install_account(rpc)
    company = _main_company(rpc)
    comp = rpc.get_model("res.company")

    recv = _mk_account(rpc, "P1300", "Probe Recv", "asset_receivable", True)
    recv2 = _mk_account(rpc, "P1301", "Probe Recv Alt", "asset_receivable", True)
    income = _mk_account(rpc, "P8000", "Probe Income", "income", False)
    bank = _mk_account(rpc, "P1100", "Probe Bank", "asset_cash", False)
    jrnl = _mk_journal(rpc, "PMSC", "Probe Misc", "general")
    sale = _mk_journal(rpc, "PSAL", "Probe Sale", "sale")

    def probe_a() -> dict[str, Any]:
        a: dict[str, Any] = {}
        m1 = _entry(rpc, jrnl, bank, income, "2024-06-15", "PMSC/2024/0007")
        a["same_year"] = {
            "preset": "PMSC/2024/0007",
            "before": _state(rpc, m1),
            "post": _post(rpc, [m1]),
            "after": _state(rpc, m1),
        }
        cross: dict[str, Any] = {"preset": "2023-900", "date": "2024-12-31"}
        try:
            m2 = _entry(rpc, jrnl, bank, income, "2024-12-31", "2023-900")
            cross["create"] = {"raised": False}
            cross["post"] = _post(rpc, [m2])
            cross["after"] = _state(rpc, m2)
        except Exception as exc:
            cross["create"] = {"raised": True, "error": _err(exc)}
        a["cross_year"] = cross
        return a

    def probe_b() -> dict[str, Any]:
        b: dict[str, Any] = {"lock_fields": _lock_fields(rpc)}
        partner_b = rpc.get_model("res.partner").create({"name": "Probe Lock 359"})
        set_results = {}
        try:
            for f in b["lock_fields"]:
                set_results[f] = _try(
                    lambda f=f: comp.write([company], {f: "2024-12-31"})
                )
            b["lock_set"] = set_results
            if any(not r["raised"] for r in set_results.values()):
                le = _entry(rpc, jrnl, bank, income, "2024-06-15", None)
                b["entry_before_lock"] = {
                    "date_before": "2024-06-15",
                    "post": _post(rpc, [le]),
                    "after": _state(rpc, le),
                }
                li = rpc.get_model("account.move").create(
                    {
                        "move_type": "out_invoice",
                        "partner_id": partner_b,
                        "journal_id": sale,
                        "invoice_date": "2024-06-15",
                        "invoice_line_ids": [
                            (
                                0,
                                0,
                                {
                                    "name": "x",
                                    "quantity": 1,
                                    "price_unit": 50.0,
                                    "account_id": income,
                                    "tax_ids": [(6, 0, [])],
                                },
                            )
                        ],
                    }
                )
                b["invoice_before_lock"] = {
                    "invoice_date": "2024-06-15",
                    "post": _post(rpc, [li]),
                    "after": _state(rpc, li),
                }
        finally:
            for f in b["lock_fields"]:
                _try(lambda f=f: comp.write([company], {f: False}))
        return b

    def probe_c() -> dict[str, Any]:
        c: dict[str, Any] = {}
        batch_j = _mk_journal(rpc, "PBAT", "Probe Batch", "general")
        has_dep = "deprecated" in rpc.get_model("account.account").fields_get(
            ["deprecated"], ["type"]
        )
        c["has_deprecated_field"] = has_dep
        good_a = _entry(rpc, batch_j, bank, income, "2024-06-10", None)
        good_b = _entry(rpc, batch_j, bank, income, "2024-06-12", None)
        if not has_dep:
            c["note"] = "no 'deprecated' field; batch-fail trigger unavailable"
            c["good_only_post"] = _post(rpc, [good_a, good_b])
            return c
        dep = _mk_account(rpc, "PDEP", "Probe Deprecated", "asset_current", False)
        acc = rpc.get_model("account.account")
        try:
            bad = _entry(rpc, batch_j, dep, income, "2024-06-11", None)
            acc.write([dep], {"deprecated": True})
            c["batch_post"] = _post(rpc, [good_a, bad, good_b])
            c["states_after"] = {
                "good_a": _state(rpc, good_a)["state"],
                "bad": _state(rpc, bad)["state"],
                "good_b": _state(rpc, good_b)["state"],
            }
        finally:
            _try(lambda: acc.write([dep], {"deprecated": False}))
        return c

    def probe_d() -> dict[str, Any]:
        # The ADR assumes a posted move is immutable. Over RPC that protection
        # may be weaker, so each write records BOTH whether it raised AND whether
        # the value actually changed on read-back — silent mutation is the real
        # hazard for the idempotent load (it writes over RPC).
        d: dict[str, Any] = {}
        m3 = _entry(rpc, jrnl, recv, income, "2024-07-01", None)
        _post(rpc, [m3])
        mv = rpc.get_model("account.move")
        line = rpc.get_model("account.move.line").search_read(
            [["move_id", "=", m3], ["debit", ">", 0]], ["id", "account_id"]
        )

        def probe_write(
            field: str, new: Any, model: str, rec_id: int, read_field: str
        ) -> dict[str, Any]:
            before = rpc.get_model(model).read([rec_id], [read_field])[0][read_field]
            res = _try(lambda: rpc.get_model(model).write([rec_id], {field: new}))
            after = rpc.get_model(model).read([rec_id], [read_field])[0][read_field]
            res["before"] = before
            res["after"] = after
            res["mutated"] = before != after
            return res

        d["write_ref"] = probe_write("ref", "CHANGED-REF", "account.move", m3, "ref")
        d["write_name"] = probe_write("name", "CHANGED/1", "account.move", m3, "name")
        d["write_date"] = probe_write("date", "2024-07-02", "account.move", m3, "date")
        if line:
            lid = int(line[0]["id"])
            d["write_line_account"] = probe_write(
                "account_id", recv2, "account.move.line", lid, "account_id"
            )
        d["move_state_after_writes"] = _state(rpc, m3)["state"]
        d["unlink"] = _try(lambda: mv.unlink([m3]))
        return d

    def probe_e() -> dict[str, Any]:
        e: dict[str, Any] = {}
        partner = rpc.get_model("res.partner").create({"name": "Probe Cust 359"})
        e["forced_property"] = _try(
            lambda: rpc.get_model("res.partner").write(
                [partner], {"property_account_receivable_id": recv2}
            )
        )
        inv = rpc.get_model("account.move").create(
            {
                "move_type": "out_invoice",
                "partner_id": partner,
                "journal_id": sale,
                "invoice_date": "2024-05-01",
                "payment_reference": "INV-REF-359",
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "name": "Item",
                            "quantity": 1,
                            "price_unit": 242.0,
                            "account_id": income,
                            "tax_ids": [(6, 0, [])],
                        },
                    )
                ],
            }
        )
        e["before"] = _state(rpc, inv)
        e["recv_line_before"] = _recv_account(rpc, inv)
        e["post"] = _post(rpc, [inv])
        e["after"] = _state(rpc, inv)
        e["recv_line_after"] = _recv_account(rpc, inv)
        e["forced_recv_id"] = recv2
        return e

    findings: dict[str, Any] = {
        "version": version,
        "probes": {
            "A_name_survives": _safe(probe_a),
            "B_lock_date": _safe(probe_b),
            "C_batch_isolation": _safe(probe_c),
            "D_posted_immutable": _safe(probe_d),
            "E_invoice_fields_survive": _safe(probe_e),
        },
    }

    with open(out, "w", encoding="utf-8") as handle:
        json.dump(findings, handle, indent=2, default=str)
    print(f"\n#359 PROBE FINDINGS ({version}) -> {out}")
    print(json.dumps(findings, indent=2, default=str))
