"""Tests for the posted-entry guard and the post-moves step (#358).

Synthetic fixtures only. A tiny fake Odoo upserts on external id and keeps a
``state`` per account.move, which is all the guard and the post step read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fluvo.import_threaded import import_data
from fluvo.lib import immutable
from fluvo.lib.actions.move_poster import (
    ALREADY_POSTED,
    post_fail_file_for,
    read_move_ids,
    run_post_moves,
)
from fluvo.lib.core import CancelToken, ReconStats, RunStatus, SourceReadError

# --- fake Odoo ------------------------------------------------------------------


class _DB:
    def __init__(self) -> None:
        self.recs: dict[str, dict[int, dict[str, Any]]] = {}
        self.imd: dict[tuple[str, str, str], int] = {}  # (model, module, name) -> id
        self.seq = 100
        self.loads: list[list[list[Any]]] = []
        self.read_error = False
        self.refuse_post: set[int] = set()

    def add(self, model: str, xmlid: str, **vals: Any) -> int:
        module, name = xmlid.split(".", 1)
        self.seq += 1
        self.recs.setdefault(model, {})[self.seq] = {"id": self.seq, **vals}
        self.imd[(model, module, name)] = self.seq
        return self.seq


class _Model:
    def __init__(self, db: _DB, name: str) -> None:
        self.db, self.name = db, name

    def search_read(self, domain: list[Any], fields: Any = None) -> list[Any]:
        if self.db.read_error:
            raise RuntimeError("access denied")
        crit = {f: v for f, _op, v in domain}
        return [
            {"module": mod, "name": nm, "res_id": rid}
            for (model, mod, nm), rid in self.db.imd.items()
            if model == crit["model"] and mod == crit["module"] and nm in crit["name"]
        ]

    def read(self, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        recs = self.db.recs.get(self.name, {})
        return [
            {"id": i, **{f: recs[i].get(f) for f in fields}} for i in ids if i in recs
        ]

    def load(
        self, header: list[str], rows: list[list[Any]], context: Any = None
    ) -> Any:
        self.db.loads.append(rows)
        ids = []
        for row in rows:
            vals = dict(zip(header, row, strict=False))
            xmlid = immutable.normalize_xmlid(vals.pop("id"))
            module, name = xmlid.split(".", 1)
            key = (self.name, module, name)
            if key in self.db.imd:
                rid = self.db.imd[key]
                self.db.recs[self.name][rid].update(vals)
            else:
                rid = self.db.add(self.name, xmlid, state="draft", **vals)
            ids.append(rid)
        return {"ids": ids, "messages": []}

    def action_post(self, ids: list[int]) -> bool:
        if set(ids) & self.db.refuse_post:
            raise RuntimeError("The entry is not balanced.")
        for i in ids:
            self.db.recs[self.name][i]["state"] = "posted"
        return False

    def fields_get(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


class _Conn:
    def __init__(self, db: _DB) -> None:
        self.db = db

    def get_model(self, name: str) -> _Model:
        return _Model(self.db, name)


# --- partition / resolve ---------------------------------------------------------


def test_unguarded_model_is_a_noop() -> None:
    """Models without a rule are never read or filtered."""
    db = _DB()
    db.read_error = True  # would raise if touched
    rows = [["a", "A"]]
    kept, skipped, reason = immutable.guard_rows(
        _Conn(db), "res.partner", ["id", "name"], rows, "id"
    )
    assert (kept, skipped, reason) == (rows, [], None)


def test_partition_plain_and_bare_ids() -> None:
    """Bare ids resolve under __import__, like Odoo's load()."""
    rows = [["m1", "x"], ["__import__.m2", "y"], ["mod.m3", "z"], ["", "w"]]
    kept, skipped = immutable.partition_immutable(
        ["id", "ref"], rows, "id", {"__import__.m1", "mod.m3"}
    )
    assert skipped == [["m1", "x"], ["mod.m3", "z"]]
    assert kept == [["__import__.m2", "y"], ["", "w"]]


def test_partition_o2m_continuation_rows_follow_parent() -> None:
    """In --o2m files the id-less line rows go with their parent move."""
    rows = [["m1", "l1"], ["", "l2"], ["m2", "l3"], ["", "l4"]]
    kept, skipped = immutable.partition_immutable(
        ["id", "line"], rows, "id", {"__import__.m1"}, o2m=True
    )
    assert skipped == [["m1", "l1"], ["", "l2"]]
    assert kept == [["m2", "l3"], ["", "l4"]]


def test_resolve_states_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lookups are chunked and only existing ids come back."""
    monkeypatch.setattr(immutable, "CHUNK", 2)
    db = _DB()
    for i in range(5):
        db.add("account.move", f"t.m{i}", state="posted" if i % 2 else "draft")
    ids = [f"t.m{i}" for i in range(5)] + ["t.missing"]
    states = immutable.resolve_states(_Conn(db), "account.move", ids, "state")
    assert states == {f"t.m{i}": ("posted" if i % 2 else "draft") for i in range(5)}


def test_resolve_fails_closed() -> None:
    """A state read error aborts instead of loading blind."""
    db = _DB()
    db.read_error = True
    with pytest.raises(SourceReadError):
        immutable.resolve_states(_Conn(db), "account.move", ["t.m"], "state")


def test_skipped_file_for() -> None:
    """The sidecar sits next to the fail file, else next to the source."""
    assert immutable.skipped_file_for("/o/account_move_fail.csv", "x.csv") == (
        "/o/account_move_skipped.csv"
    )
    assert immutable.skipped_file_for(None, "/d/moves.csv") == "/d/moves_skipped.csv"


def test_recon_stats_reasons() -> None:
    """Named reasons are part of unaccounted; the rest is unexplained."""
    st = ReconStats(total=10, created=5, failed=1, unaccounted=4)
    st.reasons["posted_immutable"] = 3
    assert st.reconciled
    assert st.unexplained == 1


# --- import_data integration ------------------------------------------------------


def _import(tmp_path: Path, db: _DB, csv_text: str, **kw: Any) -> Any:
    src = tmp_path / "moves.csv"
    src.write_text(csv_text)
    with patch(
        "fluvo.import_threaded.conf_lib.get_connection_from_config",
        return_value=_Conn(db),
    ):
        return import_data(
            config="c.conf",
            model="account.move",
            unique_id_field="id",
            file_csv=str(src),
            separator=",",
            max_connection=1,
            fail_file=str(tmp_path / "account_move_fail.csv"),
            enable_checkpoint=False,
            resume=False,
            adaptive_throttle=False,
            **kw,
        )


def test_rerun_counts_posted_moves_and_keeps_identity(tmp_path: Path) -> None:
    """A re-run leaves posted moves alone, names them, and reconciles."""
    db = _DB()
    csv_text = "id,ref\nt.m1,A\nt.m2,B\nt.m3,C\n"
    ok, stats = _import(tmp_path, db, csv_text)
    assert ok and stats["created_records"] == 3

    # m2 gets posted between runs (by fluvo or by someone in Odoo: same path)
    db.recs["account.move"][db.imd[("account.move", "t", "m2")]]["state"] = "posted"
    ok, stats = _import(tmp_path, db, "id,ref\nt.m1,A2\nt.m2,B2\nt.m3,C2\n")

    assert ok
    assert stats["total_records"] == 3  # total is NOT shrunk
    assert stats["created_records"] == 2
    assert stats["unaccounted_records"] == 1
    assert stats["unaccounted_reasons"] == {"posted_immutable": 1}
    assert (
        stats["created_records"]
        + stats["failed_records"]
        + stats["unaccounted_records"]
        == stats["total_records"]
    )
    # posted move untouched, drafts converged
    m2 = db.recs["account.move"][db.imd[("account.move", "t", "m2")]]
    assert m2["ref"] == "B"
    assert all(r[0] != "t.m2" for r in db.loads[-1])

    skipped = Path(stats["skipped_file"])
    assert skipped.name == "account_move_skipped.csv"
    text = skipped.read_text()
    assert "t.m2" in text and "posted_immutable" in text and "_SKIP_REASON" in text
    fail = tmp_path / "account_move_fail.csv"
    assert "t.m2" not in (fail.read_text() if fail.exists() else "")


def test_guarded_model_disables_streaming(tmp_path: Path) -> None:
    """Streaming is forced off so the guard can see the rows."""
    db = _DB()
    db.add("account.move", "t.m1", state="posted", ref="A")
    ok, stats = _import(tmp_path, db, "id,ref\nt.m1,X\n", stream=True)
    assert ok
    assert stats["unaccounted_reasons"] == {"posted_immutable": 1}


def test_guard_read_failure_aborts_import(tmp_path: Path) -> None:
    """If state cannot be read, nothing is loaded."""
    db = _DB()
    db.read_error = True
    ok, stats = _import(tmp_path, db, "id,ref\nt.m1,A\n")
    assert not ok and stats == {}
    assert db.loads == []


# --- post-moves --------------------------------------------------------------------


def _source(tmp_path: Path, text: str) -> str:
    p = tmp_path / "moves.csv"
    p.write_text(text)
    return str(p)


def test_read_move_ids_dedups_and_skips_continuation_rows(tmp_path: Path) -> None:
    """o2m continuation rows carry no id and are ignored."""
    f = _source(tmp_path, "id;line\nm1;a\n;b\nt.m2;c\nm1;d\n")
    assert read_move_ids(f) == ["__import__.m1", "t.m2"]


def test_post_moves_reconciles_all_buckets(tmp_path: Path) -> None:
    """Posted now, already posted, refused, missing and cancelled-state moves."""
    db = _DB()
    db.add("account.move", "t.d1", state="draft")
    db.add("account.move", "t.d2", state="draft")
    bad = db.add("account.move", "t.bad", state="draft")
    db.add("account.move", "t.p", state="posted")
    db.add("account.move", "t.c", state="cancel")
    db.refuse_post.add(bad)
    f = _source(tmp_path, "id;x\nt.d1;1\nt.d2;1\nt.bad;1\nt.p;1\nt.c;1\nt.nope;1\n")

    res = run_post_moves("c", f, batch_size=10, connect=lambda _c: _Conn(db))

    st = res.stats
    assert (st.total, st.created, st.failed, st.unaccounted) == (6, 2, 3, 1)
    assert st.reasons == {ALREADY_POSTED: 1}
    assert st.reconciled and st.unexplained == 0
    assert res.status is RunStatus.PARTIAL
    assert res.artifacts["fail_file"] == post_fail_file_for(f)
    text = Path(res.artifacts["fail_file"]).read_text()
    assert "not balanced" in text and "t.nope" in text and "'cancel'" in text
    assert "t.d1" not in text


def test_post_moves_rerun_is_safe(tmp_path: Path) -> None:
    """A second run posts nothing and reports everything as already posted."""
    db = _DB()
    db.add("account.move", "t.d1", state="draft")
    f = _source(tmp_path, "id;x\nt.d1;1\n")
    first = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    second = run_post_moves("c", f, connect=lambda _c: _Conn(db))
    assert first.ok and first.stats.created == 1
    assert second.ok and second.stats.created == 0
    assert second.stats.reasons == {ALREADY_POSTED: 1}
    assert second.stats.reconciled


def test_post_moves_cancel_is_counted(tmp_path: Path) -> None:
    """Cancelling before posting counts the remaining drafts as cancelled."""
    db = _DB()
    db.add("account.move", "t.d1", state="draft")
    token = CancelToken()
    token.cancel()
    f = _source(tmp_path, "id;x\nt.d1;1\n")
    res = run_post_moves("c", f, cancel=token, connect=lambda _c: _Conn(db))
    assert res.status is RunStatus.CANCELLED
    assert res.stats.reasons == {"cancelled": 1}
    assert res.stats.reconciled


def test_post_moves_missing_column_is_input_error(tmp_path: Path) -> None:
    """A file without the id column fails with a typed error."""
    f = _source(tmp_path, "ref;x\nA;1\n")
    res = run_post_moves("c", f, connect=lambda _c: _Conn(_DB()))
    assert res.status is RunStatus.FAILED
    assert res.errors[0].kind == "InputError"


# --- preflight ---------------------------------------------------------------------


def test_preflight_refuses_state_column(tmp_path: Path) -> None:
    """A state column on account.move is refused unless --ignore'd."""
    from fluvo.enums import PreflightMode
    from fluvo.lib.preflight import immutable_state_column_check

    f = _source(tmp_path, "id;ref;state\nm1;A;posted\n")
    with patch("fluvo.lib.preflight._show_error_panel") as panel:
        assert not immutable_state_column_check(
            PreflightMode.NORMAL, model="account.move", filename=f
        )
        panel.assert_called_once()
    assert immutable_state_column_check(
        PreflightMode.NORMAL, model="account.move", filename=f, ignore=["state"]
    )
    assert immutable_state_column_check(
        PreflightMode.NORMAL, model="res.partner", filename=f
    )
