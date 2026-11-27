"""Tests for the post step (#358): run_post_moves and its reason buckets.

Synthetic fixtures only. A tiny fake Odoo resolves external ids, reads move
state/name/date/company, and posts moves — raising, renaming, or honouring a
lock date exactly where a real Odoo would, so every reconciliation bucket in
ADR 0001 is exercised without a live server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fluvo.lib.actions import move_poster
from fluvo.lib.actions.move_poster import (
    ALREADY_POSTED,
    DATE_CHANGED,
    LOCK_DATE,
    NAME_CHANGED,
    NOT_FOUND,
    WOULD_POST,
    post_fail_file_for,
    read_move_ids,
    run_post_moves,
)
from fluvo.lib.core import CancelToken, RunStatus
from fluvo.lib.immutable import SEQUENCE_DATE_MISMATCH

# --- fake Odoo ------------------------------------------------------------------


class _DB:
    def __init__(self) -> None:
        self.moves: dict[int, dict[str, Any]] = {}
        self.imd: dict[tuple[str, str, str], int] = {}  # (model, module, name) -> id
        self.companies: dict[int, dict[str, Any]] = {1: {"id": 1, "parent_id": False}}
        self.journals: dict[int, dict[str, Any]] = {10: {"id": 10, "type": "general"}}
        self.seq = 100
        self.refuse: dict[int, str] = {}  # id -> raise message on post
        self.rename_on_post: set[int] = set()
        self.shift_date_on_post: dict[int, str] = {}  # id -> new date (lock shift)
        self.posted_seq = 900
        # Which lock fields this "Odoo" exposes (simulate version differences).
        self.lock_fields: set[str] = set(move_poster._ALL_LOCK_FIELDS)
        self.post_calls: list[tuple[list[int], dict[str, Any]]] = []

    def add_move(self, xmlid: str, **vals: Any) -> int:
        module, name = xmlid.split(".", 1)
        self.seq += 1
        vals.setdefault("company_id", [1, "Co"])
        vals.setdefault("journal_id", [10, "GEN"])
        vals.setdefault("date", "2024-06-15")
        self.moves[self.seq] = {"id": self.seq, **vals}
        self.imd[("account.move", module, name)] = self.seq
        return self.seq

    def add_company(self, cid: int, parent: Any = False, **locks: Any) -> None:
        self.companies[cid] = {"id": cid, "parent_id": parent, **locks}

    def add_journal(self, jid: int, jtype: str) -> None:
        self.journals[jid] = {"id": jid, "type": jtype}


class _Model:
    def __init__(self, db: _DB, name: str) -> None:
        self.db, self.name = db, name

    def _store(self) -> dict[int, dict[str, Any]]:
        return {
            "account.move": self.db.moves,
            "res.company": self.db.companies,
            "account.journal": self.db.journals,
        }[self.name]

    def search_read(self, domain: list[Any], fields: Any = None) -> list[Any]:
        crit = {f: v for f, _op, v in domain}
        if self.name == "ir.model.data":
            return [
                {"module": mod, "name": nm, "res_id": rid}
                for (model, mod, nm), rid in self.db.imd.items()
                if model == crit["model"]
                and mod == crit["module"]
                and nm in crit["name"]
            ]
        store = self._store()
        ids = crit["id"] if "id" in crit else list(store)  # [] domain -> all
        flds = fields or []
        return [
            {"id": i, **{f: store[i].get(f) for f in flds if f != "id"}}
            for i in store
            if i in ids
        ]

    def read(self, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        store = self._store()
        return [
            {"id": i, **{f: store[i].get(f) for f in fields}} for i in ids if i in store
        ]

    def fields_get(self, names: list[str], *a: Any, **k: Any) -> dict[str, Any]:
        if self.name == "res.company":
            return {f: {} for f in names if f in self.db.lock_fields}
        return {}

    def action_post(
        self, ids: list[int], context: dict[str, Any] | None = None
    ) -> bool:
        self.db.post_calls.append((list(ids), dict(context or {})))
        for i in ids:  # Odoo posts a batch atomically: validate all first.
            if i in self.db.refuse:
                raise RuntimeError(self.db.refuse[i])
        for i in ids:
            move = self.db.moves[i]
            move["state"] = "posted"
            if i in self.db.shift_date_on_post:  # a lock the pre-check missed
                move["date"] = self.db.shift_date_on_post[i]
            if not move.get("name"):
                self.db.posted_seq += 1
                move["name"] = f"MISC/{self.db.posted_seq}"
            elif i in self.db.rename_on_post:
                move["name"] = f"{move['name']}-RENAMED"
        return False


class _Conn:
    def __init__(self, db: _DB) -> None:
        self.db = db

    def get_model(self, name: str) -> _Model:
        return _Model(self.db, name)


def _src(tmp: Path, text: str) -> str:
    p = tmp / "moves.csv"
    p.write_text(text)
    return str(p)


# --- helpers --------------------------------------------------------------------


def test_read_move_ids_dedups_and_skips_continuation_rows(tmp_path: Path) -> None:
    """o2m continuation rows carry no id and are ignored; ids dedup in order."""
    f = _src(tmp_path, "id;line\nm1;a\n;b\nt.m2;c\nm1;d\n")
    assert read_move_ids(f) == ["__import__.m1", "t.m2"]


def test_post_fail_file_for() -> None:
    """The post fail file sits next to the source."""
    assert post_fail_file_for("/d/moves.csv") == "/d/moves_post_fail.csv"


def test_missing_id_column_is_input_error(tmp_path: Path) -> None:
    """A file without the id column fails with a typed error, not a crash."""
    f = _src(tmp_path, "ref;x\nA;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(_DB()))
    assert res.status is RunStatus.FAILED
    assert res.errors[0].kind == "InputError"


# --- posting --------------------------------------------------------------------


def test_posts_drafts_and_reconciles(tmp_path: Path) -> None:
    """Draft moves are posted; the run reconciles and is OK."""
    db = _DB()
    db.add_move("t.d1", state="draft", name="2024/0001")
    db.add_move("t.d2", state="draft", name="2024/0002")
    f = _src(tmp_path, "id;x\nt.d1;1\nt.d2;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.ok and res.stats.created == 2
    assert res.stats.reconciled and res.stats.unexplained == 0
    assert all(db.moves[i]["state"] == "posted" for i in db.moves)


def test_blank_name_is_numbered_by_odoo_not_flagged(tmp_path: Path) -> None:
    """A draft with a blank name is numbered on post; that is not a rename."""
    db = _DB()
    db.add_move("t.d1", state="draft", name="")
    f = _src(tmp_path, "id;x\nt.d1;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.ok and res.stats.created == 1
    assert db.moves[db.imd[("account.move", "t", "d1")]]["name"].startswith("MISC/")


def test_rerun_counts_already_posted_as_unaccounted(tmp_path: Path) -> None:
    """A second run posts nothing and names the already-posted move."""
    db = _DB()
    db.add_move("t.d1", state="draft", name="2024/1")
    f = _src(tmp_path, "id;x\nt.d1;1\n")
    first = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    second = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert first.ok and first.stats.created == 1
    assert second.ok and second.stats.created == 0
    assert second.stats.reasons == {ALREADY_POSTED: 1}
    assert second.stats.reconciled


def test_not_found_and_non_draft_are_failures(tmp_path: Path) -> None:
    """A missing move and a cancelled move both fail, with distinct reasons."""
    db = _DB()
    db.add_move("t.cancel", state="cancel", name="X")
    f = _src(tmp_path, "id;x\nt.missing;1\nt.cancel;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.FAILED  # nothing posted
    assert res.stats.failed == 2
    fails = _read_fail(res)
    assert fails["t.missing"][0] == NOT_FOUND
    assert fails["t.cancel"][0] == "post_refused"


def test_lock_date_moves_are_not_attempted(tmp_path: Path) -> None:
    """A move on/before the fiscal-year lock date is failed, never posted."""
    db = _DB()
    db.companies[1]["fiscalyear_lock_date"] = "2024-12-31"
    db.add_move("t.locked", state="draft", name="L", date="2024-03-01")
    db.add_move("t.open", state="draft", name="O", date="2025-03-01")
    f = _src(tmp_path, "id;x\nt.locked;1\nt.open;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.PARTIAL
    assert res.stats.created == 1  # the open one
    fails = _read_fail(res)
    assert fails["t.locked"][0] == LOCK_DATE
    assert db.moves[db.imd[("account.move", "t", "locked")]]["state"] == "draft"


def test_name_changed_on_post_is_flagged(tmp_path: Path) -> None:
    """A preset name that changes on post is a failure, not a silent rename."""
    db = _DB()
    mid = db.add_move("t.d1", state="draft", name="2024/0001")
    db.rename_on_post.add(mid)
    f = _src(tmp_path, "id;x\nt.d1;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.FAILED
    assert _read_fail(res)["t.d1"][0] == NAME_CHANGED


def test_sequence_date_mismatch_is_classified(tmp_path: Path) -> None:
    """Odoo's date/sequence error maps to a named reason, not a generic one."""
    db = _DB()
    mid = db.add_move("t.d1", state="draft", name="2023/0900", date="2024-12-31")
    db.refuse[mid] = (
        "The Date (12/31/2024) doesn't match the sequence number of the "
        "related Journal Entry (2023/0900)."
    )
    f = _src(tmp_path, "id;x\nt.d1;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.FAILED
    assert _read_fail(res)["t.d1"][0] == SEQUENCE_DATE_MISMATCH


def test_batch_fallback_isolates_one_bad_move(tmp_path: Path) -> None:
    """One unpostable move in a batch does not block the good ones."""
    db = _DB()
    db.add_move("t.d1", state="draft", name="A")
    bad = db.add_move("t.bad", state="draft", name="B")
    db.add_move("t.d2", state="draft", name="C")
    db.refuse[bad] = "not balanced"
    f = _src(tmp_path, "id;x\nt.d1;1\nt.bad;1\nt.d2;1\n")
    res = run_post_moves("c", f, batch_size=10, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.PARTIAL
    assert res.stats.created == 2 and res.stats.failed == 1
    assert "t.bad" in _read_fail(res)
    assert res.stats.reconciled


def test_cancel_counts_remaining_as_cancelled(tmp_path: Path) -> None:
    """A cancel before posting counts the remaining drafts as cancelled."""
    db = _DB()
    db.add_move("t.d1", state="draft", name="A")
    token = CancelToken()
    token.cancel()
    f = _src(tmp_path, "id;x\nt.d1;1\n")
    res = run_post_moves("c", f, cancel=token, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.CANCELLED
    assert res.stats.reasons == {"cancelled": 1}
    assert res.stats.reconciled


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    """--dry-run reports what would post and what is blocked, posts nothing."""
    db = _DB()
    db.companies[1]["fiscalyear_lock_date"] = "2024-12-31"
    db.add_move("t.d1", state="draft", name="A", date="2025-01-01")
    db.add_move("t.locked", state="draft", name="B", date="2024-01-01")
    f = _src(tmp_path, "id;x\nt.d1;1\nt.locked;1\n")
    res = run_post_moves("c", f, dry_run=True, connect=lambda _c: _Conn(db))
    assert res.ok
    assert res.stats.created == 0
    assert res.stats.reasons[WOULD_POST] == 1
    assert res.stats.failed == 1  # the locked one
    assert res.stats.reconciled
    assert all(db.moves[i]["state"] == "draft" for i in db.moves)  # nothing posted


# --- fail-file reader -----------------------------------------------------------


def _read_fail(res: Any) -> dict[str, tuple[str, str]]:
    import csv

    path = res.artifacts["fail_file"]
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    return {r[0]: (r[1], r[2]) for r in rows[1:]}


# --- review item 6: all lock-date fields, journal gating, branch inheritance -----


def test_multiple_lock_fields_block_and_name_the_strictest(tmp_path: Path) -> None:
    """Each existing lock is read; the strictest applicable one blocks and is named."""
    db = _DB()
    db.companies[1]["fiscalyear_lock_date"] = "2024-01-31"
    db.companies[1]["tax_lock_date"] = "2024-12-31"  # stricter (later)
    db.add_move("t.m", state="draft", name="A", date="2024-06-01")
    f = _src(tmp_path, "id;x\nt.m;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.FAILED
    reason, message = _read_fail(res)["t.m"]
    assert reason == LOCK_DATE
    assert "tax_lock_date=2024-12-31" in message  # the strictest, named
    assert db.moves[db.imd[("account.move", "t", "m")]]["state"] == "draft"


def test_journal_lock_applies_only_to_its_journal_type(tmp_path: Path) -> None:
    """sale_lock_date blocks a sale-journal move but not a general one."""
    db = _DB()
    db.add_journal(20, "sale")
    db.companies[1]["sale_lock_date"] = "2024-12-31"
    db.add_move(
        "t.sale", state="draft", name="S", date="2024-06-01", journal_id=[20, "SALE"]
    )
    db.add_move("t.gen", state="draft", name="G", date="2024-06-01")  # general j.
    f = _src(tmp_path, "id;x\nt.sale;1\nt.gen;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.PARTIAL
    assert res.stats.created == 1  # the general move posts
    fails = _read_fail(res)
    assert fails["t.sale"][0] == LOCK_DATE and "sale_lock_date" in fails["t.sale"][1]
    assert "t.gen" not in fails


def test_branch_inherits_parent_company_lock(tmp_path: Path) -> None:
    """A branch with no lock of its own inherits its parent company's lock."""
    db = _DB()
    db.add_company(2, parent=[1, "Parent"])  # branch, no own lock
    db.companies[1]["fiscalyear_lock_date"] = "2024-12-31"  # on the parent
    db.add_move(
        "t.branch", state="draft", name="B", date="2024-06-01", company_id=[2, "Branch"]
    )
    f = _src(tmp_path, "id;x\nt.branch;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.FAILED
    assert _read_fail(res)["t.branch"][0] == LOCK_DATE


def test_missing_lock_field_version_is_tolerated(tmp_path: Path) -> None:
    """An Odoo without the 18+ lock fields still posts (fields_get gates reads)."""
    db = _DB()
    db.lock_fields = {"fiscalyear_lock_date", "period_lock_date"}  # a 16/17-style Odoo
    db.add_move("t.m", state="draft", name="A", date="2025-01-01")
    f = _src(tmp_path, "id;x\nt.m;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.ok and res.stats.created == 1


# --- review item 7: date post-condition (safety net) ----------------------------


def test_date_changed_on_post_is_flagged(tmp_path: Path) -> None:
    """A silent date shift the pre-check missed is caught after posting."""
    db = _DB()
    mid = db.add_move("t.m", state="draft", name="A", date="2024-06-01")
    db.shift_date_on_post[mid] = "2025-01-01"  # Odoo silently moves it on post
    f = _src(tmp_path, "id;x\nt.m;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.FAILED
    reason, message = _read_fail(res)["t.m"]
    assert reason == DATE_CHANGED
    assert "2024-06-01" in message and "2025-01-01" in message


# --- review item 10: multi-company posting context ------------------------------


def test_post_passes_allowed_company_ids(tmp_path: Path) -> None:
    """action_post receives allowed_company_ids for the batch's companies."""
    db = _DB()
    db.add_move("t.a", state="draft", name="A", company_id=[1, "Co1"])
    db.add_move("t.b", state="draft", name="B", company_id=[2, "Co2"])
    f = _src(tmp_path, "id;x\nt.a;1\nt.b;1\n")
    res = run_post_moves("c", f, batch_size=10, connect=lambda _c: _Conn(db))
    assert res.ok and res.stats.created == 2
    # one batch, both companies in allowed_company_ids
    assert db.post_calls
    _ids, context = db.post_calls[0]
    assert context.get("allowed_company_ids") == [1, 2]


def test_period_lock_date_is_not_pre_blocked(tmp_path: Path) -> None:
    """period_lock_date is left to the date post-condition, not pre-blocked.

    The #359 probe showed the adviser/admin user a migration runs as posts past
    a period lock with no date shift, so pre-blocking it would wrongly refuse a
    whole migration's pre-period moves.
    """
    db = _DB()
    db.lock_fields = {"fiscalyear_lock_date", "period_lock_date"}
    db.companies[1]["period_lock_date"] = "2024-12-31"  # set, but not pre-blocked
    db.add_move("t.m", state="draft", name="A", date="2024-06-01")
    f = _src(tmp_path, "id;x\nt.m;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert res.ok and res.stats.created == 1
    assert "fiscalyear_lock_date" in move_poster._ALL_LOCK_FIELDS
    assert "period_lock_date" not in move_poster._ALL_LOCK_FIELDS
