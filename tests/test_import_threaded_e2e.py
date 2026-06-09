"""Orchestration tests for import_data driven through an in-memory fake Odoo.

Rather than mocking model.load() per test, a small behavioural FakeOdoo implements
the few methods the import pipeline uses (load/write/create/search/read/fields_get
+ ir.model.data registration). Tests drive the *real* import_data orchestration
(Pass 1 + two-pass deferral + fail file) single-threaded against it.
"""

from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from fluvo.import_threaded import import_data


class _Store:
    """Shared in-memory database for the fake Odoo."""

    def __init__(self) -> None:
        self.records: dict[str, dict[int, dict[str, Any]]] = {}
        self.imd: list[dict[str, Any]] = []  # ir.model.data rows
        self._seq = 0
        self.fail_all = False  # when True, load() reports every record as failed
        self.fail_ids: set[str] = set()  # load() fails any batch containing these ids

    def next_id(self) -> int:
        self._seq += 1
        return self._seq


class _FakeModel:
    def __init__(self, name: str, store: _Store) -> None:
        self.model_name = name
        self._s = store
        self.connection: Any = None  # set by the connection; used for Pass-2 proxy

    # --- main model behaviour ---
    def load(
        self, header: list[str], rows: list[list[Any]], context: Any = None
    ) -> dict[str, Any]:
        if self._s.fail_all or any(
            dict(zip(header, row)).get("id") in self._s.fail_ids for row in rows
        ):
            # Whole batch fails -> the pipeline binary-searches to isolate the bad row.
            return {"ids": [], "messages": [{"message": "boom", "type": "error"}]}
        ids: list[int] = []
        recs = self._s.records.setdefault(self.model_name, {})
        for row in rows:
            vals = dict(zip(header, row))
            db_id = self._s.next_id()
            recs[db_id] = vals
            xmlid = vals.get("id")
            if xmlid:  # mimic Odoo auto-registering the external id
                module, name = (
                    xmlid.split(".", 1) if "." in xmlid else ("__import__", xmlid)
                )
                self._s.imd.append(
                    {
                        "id": self._s.next_id(),
                        "module": module,
                        "name": name,
                        "res_id": db_id,
                        "full": xmlid,
                    }
                )
            ids.append(db_id)
        return {"ids": ids, "messages": []}

    def write(self, ids: Any, vals: dict[str, Any], context: Any = None) -> bool:
        recs = self._s.records.setdefault(self.model_name, {})
        for i in ids if isinstance(ids, (list, tuple)) else [ids]:
            recs.setdefault(i, {}).update(vals)
        return True

    def create(self, vals: dict[str, Any], context: Any = None) -> int:
        db_id = self._s.next_id()
        self._s.records.setdefault(self.model_name, {})[db_id] = vals
        return db_id

    def fields_get(self, names: Optional[list[str]] = None, *a: Any) -> dict[str, Any]:
        return {n: {"type": "many2one"} for n in (names or [])}

    # --- ir.model.data behaviour ---
    def _match(self, domain: list[Any]) -> list[dict[str, Any]]:
        crit = {f: v for (f, _op, v) in domain}
        out = []
        for r in self._s.imd:
            if "module" in crit and (r["module"], r["name"]) != (
                crit.get("module"),
                crit.get("name"),
            ):
                continue
            if "module" not in crit and "name" in crit and r["full"] != crit["name"]:
                continue
            out.append(r)
        return out

    def search(
        self, domain: list[Any], limit: Any = None, context: Any = None
    ) -> list[int]:
        return [r["id"] for r in self._match(domain)][: (limit or None)]

    def search_read(
        self, domain: list[Any], fields: Any = None, context: Any = None
    ) -> list[dict[str, Any]]:
        return [{"res_id": r["res_id"]} for r in self._match(domain)]

    def read(self, ids: Any, fields: Any = None, context: Any = None) -> Any:
        wanted = ids[0] if isinstance(ids, list) else ids
        for r in self._s.imd:
            if r["id"] == wanted:
                return {"res_id": r["res_id"]}
        return {}


class _FakeConnection:
    def __init__(self, store: _Store) -> None:
        self._s = store

    def get_model(self, name: str) -> _FakeModel:
        model = _FakeModel(name, self._s)
        model.connection = self
        return model


def _run(
    tmp_path: Path,
    csv_text: str,
    fail_all: bool = False,
    fail_ids: Optional[set[str]] = None,
    seed_imd: Optional[list[dict[str, Any]]] = None,
    **kwargs: Any,
) -> Any:
    """Write a CSV and run import_data against a fresh FakeOdoo (single-threaded)."""
    src = tmp_path / "data.csv"
    src.write_text(csv_text)
    store = _Store()
    store.fail_all = fail_all
    store.fail_ids = set(fail_ids or [])
    store.imd.extend(seed_imd or [])
    with patch(
        "fluvo.import_threaded.conf_lib.get_connection_from_config",
        return_value=_FakeConnection(store),
    ):
        result = import_data(
            config="conn.conf",
            model="res.partner",
            unique_id_field="id",
            file_csv=str(src),
            separator=",",
            max_connection=1,
            fail_file=str(tmp_path / "fail.csv"),
            **kwargs,
        )
    return result, store


def test_import_data_happy_path(tmp_path: Path) -> None:
    """A clean single-pass import creates every record and returns an id map."""
    result, store = _run(tmp_path, "id,name\nrec_a,Alice\nrec_b,Bob\n")
    assert result is not None
    # both partners created
    assert len(store.records.get("res.partner", {})) == 2
    names = {v["name"] for v in store.records["res.partner"].values()}
    assert names == {"Alice", "Bob"}


def test_import_data_two_pass_self_ref(tmp_path: Path) -> None:
    """A deferred self-referencing field is resolved and written in Pass 2."""
    csv = "id,name,parent_id/id\ncompany_x,Company X,\ncontact_a,Contact A,company_x\n"
    result, store = _run(tmp_path, csv, deferred_fields=["parent_id/id"])
    assert result is not None
    recs = store.records["res.partner"]
    assert len(recs) == 2
    company_id = next(db for db, v in recs.items() if v.get("name") == "Company X")
    contact = next(v for v in recs.values() if v.get("name") == "Contact A")
    # Pass 2 must have written parent_id -> the company's db id.
    assert contact.get("parent_id") == company_id


def test_import_data_failures_written_to_fail_file(tmp_path: Path) -> None:
    """Records that fail to load are written to the fail file with the error."""
    _result, store = _run(tmp_path, "id,name\nrec_a,Alice\n", fail_all=True)
    fail = tmp_path / "fail.csv"
    assert fail.exists()
    content = fail.read_text()
    assert "rec_a" in content
    # the original record was not persisted
    assert not store.records.get("res.partner")


def test_import_data_skip_unchanged_all_new(tmp_path: Path) -> None:
    """skip_unchanged with no existing records imports everything (all new)."""
    result, store = _run(tmp_path, "id,name\nrec_a,Alice\n", skip_unchanged=True)
    assert result is not None
    assert len(store.records.get("res.partner", {})) == 1


def test_import_data_multiple_batches(tmp_path: Path) -> None:
    """A small batch_size splits the data into several load() batches."""
    csv = "id,name\na,A\nb,B\nc,C\n"
    result, store = _run(tmp_path, csv, batch_size=1)
    assert result is not None
    assert len(store.records.get("res.partner", {})) == 3


def test_import_data_partial_failure_rescues_good_records(tmp_path: Path) -> None:
    """A bad row is isolated to the fail file; the good rows still import."""
    csv = "id,name\ngood_a,A\nbad_x,X\ngood_c,C\n"
    _result, store = _run(tmp_path, csv, fail_ids={"bad_x"}, batch_size=10)
    names = {v["name"] for v in store.records.get("res.partner", {}).values()}
    assert "A" in names and "C" in names  # good rows rescued
    assert "X" not in names  # bad row isolated, not persisted
    assert "bad_x" in (tmp_path / "fail.csv").read_text()


def test_import_data_cross_model_two_pass(tmp_path: Path) -> None:
    """A deferred field referencing another model resolves via ir.model.data."""
    seed = [
        {
            "id": 9001,
            "module": "base",
            "name": "user_admin",
            "res_id": 2,
            "full": "base.user_admin",
        }
    ]
    csv = "id,name,user_id/id\nrec_a,Alice,base.user_admin\n"
    _result, store = _run(tmp_path, csv, deferred_fields=["user_id/id"], seed_imd=seed)
    contact = next(
        v for v in store.records["res.partner"].values() if v.get("name") == "Alice"
    )
    assert contact.get("user_id") == 2


def test_import_data_skip_existing(tmp_path: Path) -> None:
    """skip_existing imports only new records (fake reports none existing)."""
    result, store = _run(tmp_path, "id,name\nrec_a,A\n", skip_existing=True)
    assert result is not None
    assert len(store.records.get("res.partner", {})) == 1


def test_import_data_groupby_imports_all_records(tmp_path: Path) -> None:
    """Grouping by a column (split_by_cols / --groupby) loses no records."""
    csv = "id,name,country_id/id\na,A,be\nb,B,be\nc,C,fr\nd,D,be\n"
    _result, store = _run(tmp_path, csv, split_by_cols=["country_id/id"])
    recs = store.records.get("res.partner", {})
    assert len(recs) == 4  # every record imported despite partitioning
    assert {v["name"] for v in recs.values()} == {"A", "B", "C", "D"}


def test_import_data_groupby_with_deferred_no_loss(tmp_path: Path) -> None:
    """Grouping + a deferred self-ref still imports everything and resolves Pass 2."""
    csv = (
        "id,name,country_id/id,parent_id/id\n"
        "co,Co,be,\n"
        "a,A,be,co\n"
        "b,B,fr,co\n"
    )
    _result, store = _run(
        tmp_path, csv, split_by_cols=["country_id/id"], deferred_fields=["parent_id/id"]
    )
    recs = store.records.get("res.partner", {})
    assert len(recs) == 3
    co_id = next(db for db, v in recs.items() if v["name"] == "Co")
    for nm in ("A", "B"):
        child = next(v for v in recs.values() if v["name"] == nm)
        assert child.get("parent_id") == co_id
