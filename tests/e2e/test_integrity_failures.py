"""Failure-handling integrity scenarios: bad data must fail loudly, never silently.

The guarantee under test: a malformed row ends up in the fail file with an
``_ERROR_REASON`` and the surrounding good rows still import - no batch-wide loss.
"""

from __future__ import annotations

from typing import Any

from . import assertions as A
from . import generators as G


def _fail_path(tmp_path: Any, name: str) -> str:
    return str(tmp_path / f"{name}_fail.csv")


def test_malformed_rows_go_to_fail_file(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Scenario 6: rows missing a required value land in the fail file.

    The good rows must still be created; the bad ones must be captured, not dropped.
    """
    prefix = "s6bad"
    n_bad = 5
    rows = G.partners(scale, prefix)
    rows, bad_ids = G.inject_malformed(rows, n_bad)
    csv_path = str(tmp_path / "bad.csv")
    fail = _fail_path(tmp_path, "bad")
    G.write_csv(csv_path, ["id", "name", "email", "is_company"], rows)

    _success, stats = A.import_with_stats(
        conn_config, "res.partner", csv_path, fail, worker=1, batch_size=20
    )

    A.assert_reconciled(stats)
    assert stats["failed_records"] == n_bad, (
        f"Expected {n_bad} failures, got {stats['failed_records']}."
    )
    # Good rows survived; bad rows did not pollute the DB.
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale - n_bad)
    A.assert_failures(
        fail,
        expected_bad=tuple(bad_ids),
        forbidden=tuple(r["id"] for r in rows if r["id"] not in bad_ids)[:5],
    )


def test_mixed_type_column_does_not_crash(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Scenario 7: a column mixing numeric and text values imports cleanly.

    Reproduces the Polars type-inference crash class (commit a1eeca5).
    """
    prefix = "s7mixed"
    rows = G.mixed_type_column(scale, prefix, column="ref")
    csv_path = str(tmp_path / "mixed.csv")
    G.write_csv(csv_path, ["id", "name", "email", "is_company", "ref"], rows)

    success, stats = A.import_with_stats(
        conn_config, "res.partner", csv_path, _fail_path(tmp_path, "mixed")
    )

    A.assert_reconciled(stats)
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale)
    # A text ref value round-tripped intact (not coerced to a number).
    text_refs = A.count(
        rpc, "res.partner", [["name", "like", f"{prefix} %"], ["ref", "like", "REF-%"]]
    )
    assert text_refs > 0, "Text values in mixed column were lost/coerced."
    assert success
