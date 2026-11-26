"""The post step against a real Odoo (#358): load draft, post, re-run safely.

Synthetic data only. Covers, on the 16-19 matrix, the parts of ADR 0001 the
post step owns:

* draft moves loaded by Fluvo are posted by ``run_post_moves`` and reconcile
  (the move is the unit: ``created + failed + unaccounted == total``);
* the step is safe to re-run — a move already posted is counted under
  ``already_posted`` and left alone, nothing is posted twice;
* posting a batch is atomic in Odoo, so one unbalanced move is isolated by the
  per-move fallback: it fails (named in the fail file) while the good moves post.

The load-side guard (refusing a re-load onto a posted move) is covered by
``test_posted_moves_guard_e2e.py``; lock-date, name-changed and
sequence/date-mismatch classification are covered by the unit tests, which can
stage those conditions deterministically across versions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fluvo.lib.actions.move_poster import ALREADY_POSTED, run_post_moves
from fluvo.lib.core import RunStatus

from . import _runtime
from . import assertions as A
from . import generators as G

PREFIX = "post358"
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
        pytest.skip("post-step test installs 'account'; needs the managed stack")
    _runtime.install_module(target_db, "account")
    _runtime.restart_service("odoo")
    _runtime.wait_http_ready(str(odoo_endpoint["host"]), int(odoo_endpoint["port"]))
    journals = rpc.get_model("account.journal").search([("type", "=", "general")])
    if not journals:
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


def _moves_csv(path: Path, acc: dict, n: int, pfx: str) -> str:
    return _write(
        path,
        ["id", "ref", "date", "journal_id/.id"],
        [
            [f"{pfx}_move_{i}", f"{pfx} {i}", "2024-01-15", acc["journal"]]
            for i in range(n)
        ],
    )


def _lines_csv(
    path: Path, acc: dict, n: int, pfx: str, *, unbalanced: set[int] = frozenset()
) -> str:
    header = ["id", "move_id/id", "name", "account_id/.id", "debit", "credit"]
    rows = []
    for i in range(n):
        mid, amount = f"{pfx}_move_{i}", 100 + i
        credit = 0 if i in unbalanced else amount  # drop the credit leg -> unbalanced
        rows.append([f"{mid}_l1", mid, "dr", acc["debit"], amount, 0])
        rows.append([f"{mid}_l2", mid, "cr", acc["credit"], 0, credit])
    return _write(path, header, rows)


def _load(conn: dict, model: str, src: str, tmp: Path, **kw: Any) -> dict:
    fail = str(tmp / f"{model.replace('.', '_')}_fail.csv")
    ok, stats = A.import_with_stats(conn, model, src, fail, **kw)
    assert ok, stats
    return stats


def _state(rpc: Any, xmlid: str) -> str:
    res_id = A.xmlid_to_res_id(rpc, f"__import__.{xmlid}")
    assert res_id, xmlid
    return str(rpc.get_model("account.move").read([res_id], ["state"])[0]["state"])


def test_load_draft_then_post_and_rerun_is_safe(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path, accounting: dict
) -> None:
    """Load as draft, post, and re-run the post step as a no-op."""
    pfx = f"{PREFIX}a"
    n = 3
    moves = _moves_csv(tmp_path / "m.csv", accounting, n, pfx)
    lines = _lines_csv(tmp_path / "l.csv", accounting, n, pfx)
    A.assert_reconciled(_load(conn_config, "account.move", moves, tmp_path))
    A.assert_reconciled(
        _load(conn_config, "account.move.line", lines, tmp_path, context=LINE_CTX)
    )
    for i in range(n):
        assert _state(rpc, f"{pfx}_move_{i}") == "draft"

    res = run_post_moves(conn_config, moves, separator=",")
    assert res.ok, res.errors
    assert (res.stats.created, res.stats.failed) == (n, 0)
    assert res.stats.reconciled
    for i in range(n):
        assert _state(rpc, f"{pfx}_move_{i}") == "posted"

    # Re-running posts nothing; every move is already_posted.
    again = run_post_moves(conn_config, moves, separator=",")
    assert again.ok and again.stats.created == 0
    assert again.stats.reasons == {ALREADY_POSTED: n}
    assert again.stats.reconciled


def test_unbalanced_move_is_isolated_by_batch_fallback(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path, accounting: dict
) -> None:
    """One unbalanced move fails; the balanced ones still post (per-move fallback)."""
    pfx = f"{PREFIX}b"
    n = 3
    moves = _moves_csv(tmp_path / "m.csv", accounting, n, pfx)
    lines = _lines_csv(tmp_path / "l.csv", accounting, n, pfx, unbalanced={1})
    _load(conn_config, "account.move", moves, tmp_path)
    _load(conn_config, "account.move.line", lines, tmp_path, context=LINE_CTX)

    res = run_post_moves(conn_config, moves, separator=",", batch_size=10)
    assert res.status is RunStatus.PARTIAL
    assert res.stats.created == n - 1 and res.stats.failed == 1
    assert res.stats.reconciled
    assert _state(rpc, f"{pfx}_move_1") == "draft"  # the unbalanced one, not posted
    fail_text = Path(res.artifacts["fail_file"]).read_text()
    assert f"{pfx}_move_1" in fail_text


def _set_fiscalyear_lock(rpc: Any, company_id: int, date: str) -> bool:
    """Set the fiscal-year lock over RPC; return False if Odoo won't allow it.

    Odoo 17 answers a plain ``write`` to ``fiscalyear_lock_date`` with a
    ``RedirectWarning`` (it wants its wizard), so the lock cannot be set this way
    there; the caller skips in that case.
    """
    try:
        rpc.get_model("res.company").write([company_id], {"fiscalyear_lock_date": date})
        return True
    except Exception:  # RedirectWarning (17) or any refusal
        return False


def test_lock_date_move_is_blocked_and_left_draft(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path, accounting: dict
) -> None:
    """A move on/before the fiscal-year lock is refused and left untouched."""
    from fluvo.lib.actions.move_poster import LOCK_DATE

    pfx = f"{PREFIX}c"
    moves = _moves_csv(tmp_path / "m.csv", accounting, 1, pfx)  # dated 2024-01-15
    lines = _lines_csv(tmp_path / "l.csv", accounting, 1, pfx)
    _load(conn_config, "account.move", moves, tmp_path)
    _load(conn_config, "account.move.line", lines, tmp_path, context=LINE_CTX)

    move_id = A.xmlid_to_res_id(rpc, f"__import__.{pfx}_move_0")
    company_id = rpc.get_model("account.move").read([move_id], ["company_id"])[0][
        "company_id"
    ][0]
    before = rpc.get_model("account.move").read([move_id], ["date"])[0]["date"]
    if not _set_fiscalyear_lock(rpc, company_id, "2024-12-31"):
        pytest.skip(
            "cannot set fiscalyear_lock_date over RPC while unposted entries exist "
            "(RedirectWarning, e.g. Odoo 16/17)"
        )
    try:
        res = run_post_moves(conn_config, moves, separator=",")
        assert res.status is RunStatus.FAILED  # the only move was blocked
        # the fail file keys by the fully-qualified external id (a bare id with
        # no dot is written under the __import__ module, like Odoo's load()).
        assert _post_failures(res)[f"__import__.{pfx}_move_0"] == LOCK_DATE
        assert _state(rpc, f"{pfx}_move_0") == "draft"  # not posted
        after = rpc.get_model("account.move").read([move_id], ["date"])[0]["date"]
        assert str(after) == str(before)  # date untouched
    finally:
        _set_fiscalyear_lock(rpc, company_id, False)  # clear the lock for other tests


def _post_failures(res: Any) -> dict[str, str]:
    """Read a post fail file into ``id -> reason``."""
    import csv

    with open(res.artifacts["fail_file"], newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter=","))
    return {r[0]: r[1] for r in rows[1:]}
