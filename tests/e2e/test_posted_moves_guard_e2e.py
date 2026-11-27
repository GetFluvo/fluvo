"""The load guard for posted ``account.move`` records, against a real Odoo (#358).

Synthetic data only. Proves the decided load semantics end to end on the 16-19
matrix (see ``docs/adr/0001-account-move-load-and-post.md`` and the #359
findings):

* the load lands moves as draft and converges on re-run;
* a move posted between runs (here: directly in Odoo) is **refused before any
  write** on re-run — its row is counted as ``failed`` with reason
  ``posted_immutable`` and written to the fail file, and the move is left byte
  for byte untouched and **still posted** (18/19 are where a blind line write
  would otherwise silently reset it to draft);
* lines (``account.move.line``) aimed at a posted move are guarded via the
  parent move's state, so no orphan line write reaches posted books;
* the reconciliation identity ``created + failed + unaccounted == total`` holds
  throughout.

The separate, explicit post step is covered under #358; this file is only the
load guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from . import _runtime
from . import assertions as A
from . import generators as G

PREFIX = "pmg358"
LINE_CTX = {"check_move_validity": False, "tracking_disable": True}


@pytest.fixture(scope="module")
def accounting(odoo_endpoint: dict[str, object], target_db: str, rpc: Any) -> dict:
    """Install ``account`` and return a general journal + two accounts.

    Args:
        odoo_endpoint: Managed Odoo endpoint details.
        target_db: The target database name.
        rpc: A connection to the target database.

    Returns:
        dict: ``journal`` id and ``debit``/``credit`` account ids.
    """
    if not odoo_endpoint["managed"]:
        pytest.skip("posted-moves guard test installs 'account'; needs managed stack")
    _runtime.install_module(target_db, "account")
    _runtime.restart_service("odoo")
    _runtime.wait_http_ready(str(odoo_endpoint["host"]), int(odoo_endpoint["port"]))
    journals = rpc.get_model("account.journal").search([("type", "=", "general")])
    if not journals:
        # Odoo 16 loads no chart for a country-less company; 17+ does.
        _runtime.install_module(target_db, "l10n_generic_coa")
        _runtime.restart_service("odoo")
        _runtime.wait_http_ready(str(odoo_endpoint["host"]), int(odoo_endpoint["port"]))
        journals = rpc.get_model("account.journal").search([("type", "=", "general")])
    domain = (
        [("account_type", "not in", ["asset_receivable", "liability_payable"])]
        if _has_account_type(rpc)
        else []
    )
    accounts = rpc.get_model("account.account").search(domain, limit=2)
    if not journals or len(accounts) < 2:
        pytest.skip("no chart of accounts / general journal after installing account")
    return {"journal": journals[0], "debit": accounts[0], "credit": accounts[1]}


def _has_account_type(rpc: Any) -> bool:
    return "account_type" in rpc.get_model("account.account").fields_get(
        ["account_type"]
    )


def _write(path: Path, header: list[str], rows: list[list[Any]]) -> str:
    G.write_csv(str(path), header, [dict(zip(header, r, strict=True)) for r in rows])
    return str(path)


def _moves_csv(path: Path, acc: dict, n: int, tag: str) -> str:
    """Write ``n`` move headers (no lines)."""
    return _write(
        path,
        ["id", "ref", "date", "journal_id/.id"],
        [
            [f"{PREFIX}_move_{i}", f"{PREFIX} {i} {tag}", "2024-01-15", acc["journal"]]
            for i in range(n)
        ],
    )


def _lines_csv(path: Path, acc: dict, n: int, tag: str) -> str:
    """Write two balancing lines per move, each with its own external id."""
    header = ["id", "move_id/id", "name", "account_id/.id", "debit", "credit"]
    rows = []
    for i in range(n):
        mid, amount = f"{PREFIX}_move_{i}", 100 + i
        rows.append([f"{mid}_l1", mid, f"dr {tag}", acc["debit"], amount, 0])
        rows.append([f"{mid}_l2", mid, f"cr {tag}", acc["credit"], 0, amount])
    return _write(path, header, rows)


def _post(rpc: Any, move_id: int) -> None:
    try:
        rpc.get_model("account.move").action_post([move_id])
    except Exception as e:  # some transports complain about a None return value
        if "cannot marshal None" not in str(e):
            raise


def _move(rpc: Any, xmlid: str) -> dict[str, Any]:
    res_id = A.xmlid_to_res_id(rpc, f"__import__.{xmlid}")
    assert res_id, xmlid
    return rpc.get_model("account.move").read([res_id], ["state", "ref"])[0]


def _load(conn: dict, model: str, src: str, tmp: Path, **kw: Any) -> dict:
    fail = str(tmp / f"{model.replace('.', '_')}_fail.csv")
    ok, stats = A.import_with_stats(conn, model, src, fail, **kw)
    assert ok, stats
    return stats


def test_rerun_onto_posted_move_is_refused_and_move_untouched(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path, accounting: dict
) -> None:
    """Load as draft, post one in Odoo, re-run: posted move failed + untouched."""
    n = 3
    moves = _moves_csv(tmp_path / "moves.csv", accounting, n, "v1")
    lines = _lines_csv(tmp_path / "lines.csv", accounting, n, "v1")

    # 1. First load: every move lands as draft, lines attach to them.
    A.assert_reconciled(_load(conn_config, "account.move", moves, tmp_path))
    A.assert_reconciled(
        _load(conn_config, "account.move.line", lines, tmp_path, context=LINE_CTX)
    )
    for i in range(n):
        assert _move(rpc, f"{PREFIX}_move_{i}")["state"] == "draft"

    # 2. Someone posts move 1 in Odoo between runs.
    _post(rpc, A.xmlid_to_res_id(rpc, f"__import__.{PREFIX}_move_1"))
    assert _move(rpc, f"{PREFIX}_move_1")["state"] == "posted"

    # 3. Re-run with changed values: drafts converge; the posted move and its
    #    two lines are refused before any write — counted as failed, named in
    #    the fail file, never silently skipped, never written over.
    moves = _moves_csv(tmp_path / "moves.csv", accounting, n, "v2")
    lines = _lines_csv(tmp_path / "lines.csv", accounting, n, "v2")

    st = _load(conn_config, "account.move", moves, tmp_path)
    A.assert_reconciled(st)  # posted row is failed, not unaccounted
    assert st["created_records"] == n - 1
    assert st["failed_records"] == 1
    assert st.get("immutable_guarded") == {"posted_immutable": 1}
    fails = A.read_fail_file(str(tmp_path / "account_move_fail.csv"))
    assert {r["id"] for r in fails} == {f"{PREFIX}_move_1"}
    assert all("posted_immutable" in r.values() for r in fails)

    st = _load(conn_config, "account.move.line", lines, tmp_path, context=LINE_CTX)
    A.assert_reconciled(st)
    assert st["failed_records"] == 2
    assert st.get("immutable_guarded") == {"posted_immutable": 2}

    # 4. DB truth: the posted move is byte-for-byte untouched and STILL POSTED
    #    (18/19 are where a blind line write would reset it to draft), while the
    #    drafts converged. No rows were duplicated.
    posted = _move(rpc, f"{PREFIX}_move_1")
    assert posted["state"] == "posted"
    assert posted["ref"].endswith("v1")  # not overwritten with v2
    assert _move(rpc, f"{PREFIX}_move_0")["ref"].endswith("v2")  # converged
    A.assert_db_count(rpc, "account.move", [("ref", "like", f"{PREFIX} %")], n)
    A.assert_db_count(
        rpc, "account.move.line", [("move_id.ref", "like", f"{PREFIX} %")], 2 * n
    )
    posted_lines = rpc.get_model("account.move.line").search_read(
        [("move_id.ref", "=", f"{PREFIX} 1 v1")], ["name"]
    )
    assert {ln["name"] for ln in posted_lines} == {"dr v1", "cr v1"}


def test_new_line_aimed_at_posted_move_is_guarded_by_parent(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path, accounting: dict
) -> None:
    """A brand-new line on a posted move is refused via the parent's state."""
    moves = _moves_csv(tmp_path / "m.csv", accounting, 1, "x")
    lines = _lines_csv(tmp_path / "l.csv", accounting, 1, "x")
    _load(conn_config, "account.move", moves, tmp_path)
    _load(conn_config, "account.move.line", lines, tmp_path, context=LINE_CTX)

    move_id = A.xmlid_to_res_id(rpc, f"__import__.{PREFIX}_move_0")
    _post(rpc, move_id)
    parent_states = rpc.get_model("account.move.line").search_read(
        [("move_id", "=", move_id)], ["parent_state"]
    )
    assert parent_states and {ln["parent_state"] for ln in parent_states} == {"posted"}

    # With check_move_validity off Odoo would accept a new line on a posted move
    # and unbalance posted books, so the guard must catch it via the move state,
    # for both the external-id and db-id reference styles.
    for col, ref in (("move_id/id", f"{PREFIX}_move_0"), ("move_id/.id", move_id)):
        new = _write(
            tmp_path / "new.csv",
            ["id", col, "name", "account_id/.id", "debit", "credit"],
            [[f"{PREFIX}_new", ref, "late", accounting["debit"], 1, 0]],
        )
        st = _load(conn_config, "account.move.line", new, tmp_path, context=LINE_CTX)
        A.assert_reconciled(st)
        assert st["failed_records"] == 1
        assert st.get("immutable_guarded") == {"posted_immutable": 1}
    # the posted move still has exactly its two original lines
    A.assert_db_count(rpc, "account.move.line", [("move_id", "=", move_id)], 2)
