"""Tests for the immutable-record load guard (#358/#359).

Synthetic fixtures only. A tiny fake Odoo upserts on external id and keeps a
``state`` per ``account.move``, which is all the guard reads. The guard refuses
to load onto a posted or cancelled move before any write, because Odoo itself
does not protect a posted move over RPC (verified on 16-19, see
``docs/adr/0001-verification-359-findings.md``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fluvo.import_threaded import import_data
from fluvo.lib import immutable
from fluvo.lib.core import SourceReadError

# --- fake Odoo ------------------------------------------------------------------


class _DB:
    def __init__(self) -> None:
        self.recs: dict[str, dict[int, dict[str, Any]]] = {}
        self.imd: dict[tuple[str, str, str], int] = {}  # (model, module, name) -> id
        self.seq = 100
        self.loads: list[list[list[Any]]] = []
        self.read_error = False

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
        if "id" in crit:  # record lookup by db id (drops ids that are gone)
            want = set(crit["id"])
            recs = self.db.recs.get(self.name, {})
            flds = fields or []
            return [
                {"id": i, **{f: recs[i].get(f) for f in flds if f != "id"}}
                for i in recs
                if i in want
            ]
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

    def write(self, ids: list[int], vals: dict[str, Any]) -> bool:
        recs = self.db.recs.setdefault(self.name, {})
        for i in ids:
            recs[i].update(vals)
        return True

    def fields_get(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


class _Conn:
    def __init__(self, db: _DB) -> None:
        self.db = db

    def get_model(self, name: str) -> _Model:
        return _Model(self.db, name)


# --- rule registry / normalisation ----------------------------------------------


def test_unguarded_model_is_a_noop() -> None:
    """Models without a rule are never read or filtered."""
    db = _DB()
    db.read_error = True  # would raise if touched
    rows = [["a", "A"]]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "res.partner", ["id", "name"], rows, "id"
    )
    assert (kept, guarded) == (rows, [])


def test_normalize_xmlid_bare_and_qualified() -> None:
    """Bare ids resolve under __import__, like Odoo's load()."""
    assert immutable.normalize_xmlid("m1") == "__import__.m1"
    assert immutable.normalize_xmlid("mod.m3") == "mod.m3"
    assert immutable.normalize_xmlid("  ") == ""


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


# --- move-level guard -----------------------------------------------------------


def test_guard_posted_and_cancelled_moves_by_reason() -> None:
    """Posted and cancelled targets are guarded under their own reasons."""
    db = _DB()
    db.add("account.move", "t.posted", state="posted")
    db.add("account.move", "t.cancel", state="cancel")
    db.add("account.move", "t.draft", state="draft")
    rows = [
        ["t.posted", "A"],
        ["t.cancel", "B"],
        ["t.draft", "C"],
        ["t.new", "D"],  # does not exist yet -> created as draft
    ]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "account.move", ["id", "ref"], rows, "id"
    )
    assert kept == [["t.draft", "C"], ["t.new", "D"]]
    assert sorted((r[0], reason) for r, reason in guarded) == [
        ("t.cancel", immutable.CANCELLED_IMMUTABLE),
        ("t.posted", immutable.POSTED_IMMUTABLE),
    ]


def test_guard_o2m_continuation_rows_follow_parent() -> None:
    """In --o2m files the id-less line rows share their parent's fate."""
    db = _DB()
    db.add("account.move", "t.m1", state="posted")
    db.add("account.move", "t.m2", state="draft")
    rows = [["t.m1", "l1"], ["", "l2"], ["t.m2", "l3"], ["", "l4"]]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "account.move", ["id", "line"], rows, "id", o2m=True
    )
    assert kept == [["t.m2", "l3"], ["", "l4"]]
    assert [r for r, _ in guarded] == [["t.m1", "l1"], ["", "l2"]]
    assert {reason for _, reason in guarded} == {immutable.POSTED_IMMUTABLE}


# --- parent (line) guard --------------------------------------------------------


def test_new_line_on_posted_move_is_guarded_by_parent() -> None:
    """Lines aimed at a posted move are refused even when they are new."""
    db = _DB()
    db.add("account.move", "t.posted", state="posted")
    draft = db.add("account.move", "t.draft", state="draft")
    posted_id = db.imd[("account.move", "t", "posted")]
    db.add("account.move.line", "t.old", parent_state="posted")
    rows = [
        ["t.old", "t.posted"],  # existing posted line
        ["t.new1", "t.posted"],  # new line, posted parent (xmlid)
        ["t.new2", "t.draft"],  # new line, draft parent
    ]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "account.move.line", ["id", "move_id/id"], rows, "id"
    )
    assert kept == [["t.new2", "t.draft"]]
    assert sorted(r[0] for r, _ in guarded) == ["t.new1", "t.old"]
    assert {reason for _, reason in guarded} == {immutable.POSTED_IMMUTABLE}

    rows = [["t.n3", str(posted_id)], ["t.n4", str(draft)]]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "account.move.line", ["id", "move_id/.id"], rows, "id"
    )
    assert kept == [["t.n4", str(draft)]]
    assert [r[0] for r, _ in guarded] == ["t.n3"]


def test_line_parent_referenced_by_name_cannot_be_guarded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A move_id referenced by name is warned about, not silently guarded."""
    db = _DB()
    db.add("account.move", "t.posted", state="posted")
    rows = [["t.l1", "INV/001"]]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "account.move.line", ["id", "move_id"], rows, "id"
    )
    assert kept == rows and guarded == []
    assert any("referenced by name" in r.message for r in caplog.records)


# --- import_data integration ----------------------------------------------------


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


def test_rerun_refuses_posted_move_as_failed_and_reconciles(tmp_path: Path) -> None:
    """A re-run leaves a posted move untouched, fails its row, and reconciles."""
    db = _DB()
    ok, stats = _import(tmp_path, db, "id,ref\nt.m1,A\nt.m2,B\nt.m3,C\n")
    assert ok and stats["created_records"] == 3

    # m2 gets posted between runs (by the post step or by someone in Odoo:
    # the guard cannot tell, and treats both the same).
    db.recs["account.move"][db.imd[("account.move", "t", "m2")]]["state"] = "posted"
    db.loads.clear()
    ok, stats = _import(tmp_path, db, "id,ref\nt.m1,A2\nt.m2,B2\nt.m3,C2\n")

    assert ok
    assert stats["total_records"] == 3  # total is NOT shrunk
    assert stats["created_records"] == 2
    assert stats["failed_records"] == 1
    assert stats.get("unaccounted_records", 0) == 0
    assert stats["immutable_guarded"] == {immutable.POSTED_IMMUTABLE: 1}
    assert (
        stats["created_records"]
        + stats["failed_records"]
        + stats.get("unaccounted_records", 0)
        == stats["total_records"]
    )

    # the posted move is untouched and was never in a load() batch
    m2 = db.recs["account.move"][db.imd[("account.move", "t", "m2")]]
    assert m2["ref"] == "B"
    assert all(r[0] != "t.m2" for batch in db.loads for r in batch)

    # the refused row is in the fail file, with its reason
    fail = (tmp_path / "account_move_fail.csv").read_text()
    assert "t.m2" in fail and immutable.POSTED_IMMUTABLE in fail
    assert "t.m1" not in fail  # a converged draft is not a failure


def test_guarded_model_disables_streaming(tmp_path: Path) -> None:
    """Streaming is forced off so the guard can see the rows."""
    db = _DB()
    db.add("account.move", "t.m1", state="posted", ref="A")
    ok, stats = _import(tmp_path, db, "id,ref\nt.m1,X\n", stream=True)
    assert ok
    assert stats["immutable_guarded"] == {immutable.POSTED_IMMUTABLE: 1}
    # the posted move was never written to
    assert db.recs["account.move"][db.imd[("account.move", "t", "m1")]]["ref"] == "A"


def test_guard_read_failure_aborts_import(tmp_path: Path) -> None:
    """If state cannot be read, nothing is loaded (fail closed)."""
    db = _DB()
    db.read_error = True
    ok, stats = _import(tmp_path, db, "id,ref\nt.m1,A\n")
    assert not ok and stats == {}
    assert db.loads == []


# --- review item 2: records keyed by database id (.id) ---------------------------


def test_guard_by_dbid_column() -> None:
    """A file identifying moves by `.id` is guarded by a direct state read."""
    db = _DB()
    posted = db.add("account.move", "t.posted", state="posted")
    draft = db.add("account.move", "t.draft", state="draft")
    rows = [[str(posted), "A"], [str(draft), "B"]]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "account.move", [".id", "ref"], rows, "id"
    )
    assert kept == [[str(draft), "B"]]
    assert [r[0] for r, _ in guarded] == [str(posted)]
    assert {reason for _, reason in guarded} == {immutable.POSTED_IMMUTABLE}


# --- review item 3: id-less continuation rows without --o2m ----------------------


def test_idless_continuation_follows_guarded_parent_without_o2m() -> None:
    """A line row (blank own cells, o2m sub-columns) follows its parent's fate."""
    db = _DB()
    db.add("account.move", "t.posted", state="posted")
    db.add("account.move", "t.draft", state="draft")
    header = ["id", "ref", "line_ids/account_id/.id", "line_ids/debit"]
    rows = [
        ["t.posted", "P", "10", "5"],  # guarded parent
        ["", "", "11", "0"],  # continuation of the posted move
        ["t.draft", "D", "12", "5"],  # draft parent
        ["", "", "13", "0"],  # continuation of the draft move
    ]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "account.move", header, rows, "id", o2m=False
    )
    assert [r for r, _ in guarded] == [
        ["t.posted", "P", "10", "5"],
        ["", "", "11", "0"],
    ]
    assert kept == [["t.draft", "D", "12", "5"], ["", "", "13", "0"]]


def test_blank_id_new_record_is_kept_not_a_continuation() -> None:
    """A blank-id row with its own fields populated is a new record, kept."""
    db = _DB()
    db.add("account.move", "t.posted", state="posted")
    header = ["id", "ref", "line_ids/debit"]
    rows = [
        ["t.posted", "P", "5"],  # guarded
        ["", "NEWREF", "3"],  # blank id but ref set -> a new move, not a line
    ]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "account.move", header, rows, "id", o2m=False
    )
    assert [r for r, _ in guarded] == [["t.posted", "P", "5"]]
    assert kept == [["", "NEWREF", "3"]]


# --- review item 5: a dangling ir.model.data row must not abort -------------------


def test_dangling_ir_model_data_is_treated_as_absent() -> None:
    """An external id pointing at a deleted record is treated as not-yet-there."""
    db = _DB()
    db.imd[("account.move", "t", "ghost")] = 9999  # points at no live record
    rows = [["t.ghost", "A"], ["t.new", "B"]]
    kept, guarded = immutable.guard_rows(
        _Conn(db), "account.move", ["id", "ref"], rows, "id"
    )
    assert kept == rows and guarded == []  # recreated as new, never aborted


# --- review item 4: sequence/date-mismatch reason (shared load + post) -----------


def test_is_sequence_date_error() -> None:
    """Odoo's date-vs-sequence wording is recognised; other errors are not."""
    assert immutable.is_sequence_date_error(
        "The Date (12/31/2024) doesn't match the sequence number of the related "
        "Journal Entry (2023/0900)."
    )
    assert immutable.is_sequence_date_error("isn't aligned with the existing sequence")
    assert not immutable.is_sequence_date_error("some unrelated error")


def test_buckets_classify_sequence_date_mismatch() -> None:
    """Both the canonical tag and Odoo's wording bucket as a client decision."""
    from fluvo.lib import buckets

    assert (
        buckets.classify_failure(f"{immutable.SEQUENCE_DATE_MISMATCH}: ...")
        == buckets.CLIENT_DECISION
    )
    assert (
        buckets.classify_failure("The Date doesn't match the sequence number of ...")
        == buckets.CLIENT_DECISION
    )


def test_load_fail_writer_tags_sequence_date_mismatch() -> None:
    """The load fail writer relabels a date/sequence failure with the tag."""
    import csv
    import io

    from fluvo import import_threaded as it

    buf = io.StringIO()
    writer = it._BucketingFailWriter(csv.writer(buf))
    writer.writerow(
        [
            "t.m1",
            "2024-01-01",
            "The Date doesn't match the sequence number of the related Journal Entry",
        ]
    )
    out = buf.getvalue()
    assert immutable.SEQUENCE_DATE_MISMATCH in out
    assert "client_decision" in out


# --- review item 1: the `fluvo write` path is guarded too ------------------------


def test_write_path_refuses_posted_moves(tmp_path: Path) -> None:
    """`fluvo write` refuses a posted move before any write; draft is written."""
    from fluvo import write_threaded

    db = _DB()
    posted = db.add("account.move", "x.posted", state="posted", ref="OLD")
    draft = db.add("account.move", "x.draft", state="draft", ref="OLD")
    header = ["id", "ref"]
    data = [[str(posted), "NEW"], [str(draft), "NEW"]]
    fail = str(tmp_path / "account_move_write_fail.csv")
    with patch(
        "fluvo.write_threaded.conf_lib.get_connection_from_config",
        return_value=_Conn(db),
    ):
        ok = write_threaded.write_data("c.conf", "account.move", header, data, fail)
    assert ok is False  # a refused posted move means the write did not fully succeed
    assert db.recs["account.move"][posted]["ref"] == "OLD"  # untouched
    assert db.recs["account.move"][draft]["ref"] == "NEW"  # written
    text = Path(fail).read_text()
    assert str(posted) in text and immutable.POSTED_IMMUTABLE in text
    assert f'"{draft}"' not in text  # the draft is not a failure
