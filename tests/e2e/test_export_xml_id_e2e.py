"""The export -> edit -> import loop must not duplicate records (#339).

The unit tests pin the decision; this pins the consequence. A `selection` field
in the export header forces Fluvo's read path, and the read path returns database
ids. If one of those reaches the `id` column, loading the file back creates a
second copy of every record instead of updating it — silently, because both the
export and the import report success.

So: export with a selection field in the header, load the file straight back,
and assert the record count did not move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from fluvo import export_dataframe, load_dataframe

from . import assertions as A

MODEL = "res.partner"
PREFIX = "xmlid"
N = 4

_REFS = [f"{PREFIX}-{i}" for i in range(N)]
_DOMAIN = [["ref", "in", _REFS], ["active", "in", [True, False]]]
#: A selection field. Nothing about asking for it says "give me database ids",
#: but it is what switches the export to the read path.
_SELECTION_FIELD = "type"


def _seed(config: dict[str, Any]) -> None:
    """Load the partners this export runs against."""
    frame = pl.DataFrame(
        {
            "id": [f"{PREFIX}_p{i}" for i in range(N)],
            "name": [f"{PREFIX} partner {i}" for i in range(N)],
            "ref": _REFS,
            _SELECTION_FIELD: ["contact"] * N,
        }
    )
    ok, stats = load_dataframe(frame, config, MODEL)
    assert ok, f"seeding failed: {stats}"


def test_an_export_carrying_a_selection_field_round_trips_without_duplicating(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path
) -> None:
    """The regression of #339, against a live Odoo."""
    _seed(conn_config)
    A.assert_db_count(rpc, MODEL, _DOMAIN, N)
    before = {r["id"] for r in A.search_read(rpc, MODEL, _DOMAIN, ["id"])}

    # 1. Export exactly what a consultant would: the match key, some fields, and
    #    a selection field. No '.id', no --technical-names.
    exported = export_dataframe(
        conn_config,
        MODEL,
        ["id", "ref", "name", _SELECTION_FIELD],
        domain=_DOMAIN,
    )
    assert exported.height == N
    ids = exported["id"].cast(pl.String).to_list()

    # 2. Every id must be an external id. A database id is a bare number, and
    #    that is the bug: it is a *valid-looking* id, so the import accepts it.
    assert all(i and "." in i for i in ids), f"not external ids: {ids}"
    assert not any(str(i).isdigit() for i in ids), f"database ids leaked: {ids}"

    # 3. Load the file straight back. This is an update, so the count must not
    #    move -- a database id in the id column creates a second copy of each.
    path = tmp_path / "exported.csv"
    exported.write_csv(path, separator=";")
    frame = pl.read_csv(path, separator=";", infer_schema_length=0)
    ok, stats = load_dataframe(frame, conn_config, MODEL)
    assert ok, f"re-importing the export failed: {stats}"

    # The same records, not copies of them. Counting is not enough on its own:
    # the reconciliation stats call every written row "created" because Odoo's
    # load() returns ids for updates too, so the database is the only witness.
    A.assert_db_count(rpc, MODEL, _DOMAIN, N)
    after = {r["id"] for r in A.search_read(rpc, MODEL, _DOMAIN, ["id"])}
    assert after == before, (
        "the re-import did not address the existing records: "
        f"{sorted(after - before)} created, {sorted(before - after)} orphaned"
    )
