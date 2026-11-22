"""Posted journal entries vs the re-run contract, against a real Odoo (#358).

Synthetic data only. Covers the decided semantics end to end:

* the load lands moves as draft and converges on re-run;
* a move posted between runs (here: directly in Odoo) is left untouched and
  counted as ``posted_immutable`` — the identity still holds;
* ``workflow post-moves`` posts the drafts and is itself safe to re-run;
* lines (``account.move.line``) of a posted move are guarded via ``parent_state``;
* the two Odoo behaviours the design leans on: ``account.move.line.parent_state``
  is readable, and Odoo refuses a new line on a posted move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fluvo.lib.actions.move_poster import ALREADY_POSTED, run_post_moves

from . import _runtime
from . import assertions as A
from . import generators as G

PREFIX = "pm358"


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
        pytest.skip("posted-moves test installs 'account'; needs the managed stack")
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
    accounts = rpc.get_model("account.account").search(
        [("account_type", "not in", ["asset_receivable", "liability_payable"])]
        if _has_account_type(rpc)
        else [],
        limit=2,
    )
    if not journals or len(accounts) < 2:
        pytest.skip("no chart of accounts / general journal after installing account")
    return {"journal": journals[0], "debit": accounts[0], "credit": accounts[1]}


def _has_account_type(rpc: Any) -> bool:
    return "account_type" in rpc.get_model("account.account").fields_get(
        ["account_type"]
    )


LINE_CTX = {"check_move_validity": False, "tracking_disable": True}


def _write(path: Path, header: list[str], rows: list[list[Any]]) -> str:
    G.write_csv(str(path), header, [dict(zip(header, r, strict=True)) for r in rows])
    return str(path)


def _moves_csv(path: Path, acc: dict, n: int, tag: str, prefix: str = PREFIX) -> str:
    """Write ``n`` move headers (no lines)."""
    return _write(
        path,
        ["id", "ref", "date", "journal_id/.id"],
        [
            [f"{prefix}_move_{i}", f"{prefix} {i} {tag}", "2024-01-15", acc["journal"]]
            for i in range(n)
        ],
    )


def _lines_csv(path: Path, acc: dict, n: int, tag: str, prefix: str = PREFIX) -> str:
    """Write two balancing lines per move, each with its own external id."""
    header = ["id", "move_id/id", "name", "account_id/.id", "debit", "credit"]
    rows = []
    for i in range(n):
        mid, amount = f"{prefix}_move_{i}", 100 + i
        rows.append([f"{mid}_l1", mid, f"dr {tag}", acc["debit"], amount, 0])
        rows.append([f"{mid}_l2", mid, f"cr {tag}", acc["credit"], 0, amount])
    return _write(path, header, rows)


def _post(rpc: Any, move_id: int) -> None:
    try:
        rpc.get_model("account.move").action_post([move_id])
    except Exception as e:  # XML-RPC may complain about a None return
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


def test_posted_moves_rerun_contract(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path, accounting: dict
) -> None:
    """Load as draft, post one in Odoo, re-run, then post the rest twice."""
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

    # 3. Re-run both loads with changed values: drafts converge, the posted
    #    move and its two lines are named, never silently skipped or duplicated.
    moves = _moves_csv(tmp_path / "moves.csv", accounting, n, "v2")
    lines = _lines_csv(tmp_path / "lines.csv", accounting, n, "v2")
    st = _load(conn_config, "account.move", moves, tmp_path)
    A.assert_reconciled(
        st, expect_unaccounted=1, expect_reasons={"posted_immutable": 1}
    )
    assert f"{PREFIX}_move_1" in Path(st["skipped_file"]).read_text()
    st = _load(conn_config, "account.move.line", lines, tmp_path, context=LINE_CTX)
    A.assert_reconciled(
        st, expect_unaccounted=2, expect_reasons={"posted_immutable": 2}
    )
    assert _move(rpc, f"{PREFIX}_move_1")["ref"].endswith("v1")  # untouched
    assert _move(rpc, f"{PREFIX}_move_0")["ref"].endswith("v2")  # converged
    A.assert_db_count(rpc, "account.move", [("ref", "like", f"{PREFIX} %")], n)
    A.assert_db_count(
        rpc, "account.move.line", [("move_id.ref", "like", f"{PREFIX} %")], 2 * n
    )
    posted_lines = rpc.get_model("account.move.line").search_read(
        [("move_id.ref", "=", f"{PREFIX} 1 v1")], ["name"]
    )
    assert {ln["name"] for ln in posted_lines} == {"dr v1", "cr v1"}

    # 4. post-moves posts the drafts; the one posted in Odoo is counted.
    res = run_post_moves(conn_config, moves, separator=",")
    assert res.ok, res.errors
    assert (res.stats.created, res.stats.failed) == (n - 1, 0)
    assert res.stats.reasons == {ALREADY_POSTED: 1}
    assert res.stats.reconciled
    for i in range(n):
        assert _move(rpc, f"{PREFIX}_move_{i}")["state"] == "posted"

    # 5. Re-running post-moves is a no-op.
    again = run_post_moves(conn_config, moves, separator=",")
    assert again.ok and again.stats.created == 0
    assert again.stats.reasons == {ALREADY_POSTED: n}


def test_new_line_on_posted_move_is_guarded(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path, accounting: dict
) -> None:
    """parent_state is readable; a new line on a posted move is guarded."""
    pfx = f"{PREFIX}a"
    _load(
        conn_config,
        "account.move",
        _moves_csv(tmp_path / "m.csv", accounting, 1, "x", pfx),
        tmp_path,
    )
    _load(
        conn_config,
        "account.move.line",
        _lines_csv(tmp_path / "l.csv", accounting, 1, "x", pfx),
        tmp_path,
        context=LINE_CTX,
    )
    move_id = A.xmlid_to_res_id(rpc, f"__import__.{pfx}_move_0")
    _post(rpc, move_id)
    lines = rpc.get_model("account.move.line").search_read(
        [("move_id", "=", move_id)], ["parent_state"]
    )
    assert lines and {ln["parent_state"] for ln in lines} == {"posted"}

    # A brand-new line aimed at the posted move: with check_move_validity off
    # Odoo would accept it and unbalance posted books, so the guard must catch
    # it via the parent move's state, for both reference styles.
    for col, ref in (("move_id/id", f"{pfx}_move_0"), ("move_id/.id", move_id)):
        new = _write(
            tmp_path / "new.csv",
            ["id", col, "name", "account_id/.id", "debit", "credit"],
            [[f"{pfx}_new", ref, "late", accounting["debit"], 1, 0]],
        )
        st = _load(conn_config, "account.move.line", new, tmp_path, context=LINE_CTX)
        A.assert_reconciled(
            st, expect_unaccounted=1, expect_reasons={"posted_immutable": 1}
        )
    A.assert_db_count(rpc, "account.move.line", [("move_id", "=", move_id)], 2)


@pytest.mark.xfail(
    strict=True,
    reason="#368: --o2m counts continuation rows as failed and writes "
    "them to the fail file, though Odoo created the lines",
)
def test_o2m_journal_entry_reconciles(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path, accounting: dict
) -> None:
    """One move with two inline lines via --o2m reconciles without failures."""
    pfx = f"{PREFIX}o"
    header = [
        "id",
        "ref",
        "journal_id/.id",
        "line_ids/id",
        "line_ids/account_id/.id",
        "line_ids/debit",
        "line_ids/credit",
    ]
    src = _write(
        tmp_path / "o2m.csv",
        header,
        [
            [
                f"{pfx}_m",
                f"{pfx} m",
                accounting["journal"],
                f"{pfx}_l1",
                accounting["debit"],
                5,
                0,
            ],
            ["", "", "", f"{pfx}_l2", accounting["credit"], 0, 5],
        ],
    )
    fail = str(tmp_path / "o2m_fail.csv")
    ok, st = A.import_with_stats(conn_config, "account.move", src, fail, o2m=True)
    A.assert_db_count(rpc, "account.move.line", [("move_id.ref", "=", f"{pfx} m")], 2)
    assert ok and st["failed_records"] == 0, st
