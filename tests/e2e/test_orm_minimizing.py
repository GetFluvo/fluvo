"""Acceptance tests for the ORM-minimizing load path.

Proves, against a real Odoo (RPC mode):
  (a) optimized import yields the *same* Odoo state as a naive import;
  (b) wall-time for both is measured and reported;
  (c) a second optimized run sends ~0 rows (idempotency);
  (d) one deliberately-bad row is isolated, not aborting the batch.

Datasets: a generated relational dataset (partners + country) and the committed
testdata/res_partner.csv.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import assertions as A
from . import generators as G

_COUNTRY_SPEC = [
    {
        "source_column": "country",
        "model": "res.country",
        "key_field": "name",
        "relation_field": "country_id",
        "to": "xmlid",
        "drop_source": True,
    }
]


def _country_id(rpc: Any, name: str) -> int:
    rows = A.search_read(rpc, "res.country", [["name", "=", name]], ["id"])
    assert rows, f"country {name!r} not found"
    return int(rows[0]["id"])


def test_optimized_matches_naive_and_reports_timing(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """(a)+(b): same resulting state via naive vs optimized, with timing reported."""
    n = scale
    be_id = _country_id(rpc, "Belgium")

    # Naive: bare country_id = country NAME -> Odoo name_search per row.
    naive_rows = G.partners_with_country(n, "ormNaive")
    for r in naive_rows:
        r["country_id"] = r["country"]
    naive_csv = str(tmp_path / "naive.csv")
    G.write_csv(
        naive_csv,
        ["id", "name", "email", "is_company", "country_id"],
        naive_rows,
    )
    t0 = time.monotonic()
    ok_naive, st_naive = A.import_with_stats(
        conn_config, "res.partner", naive_csv, str(tmp_path / "naive_fail.csv")
    )
    naive_secs = time.monotonic() - t0

    # Optimized: country natural key pre-resolved to country_id/id in Polars.
    opt_rows = G.partners_with_country(n, "ormOpt")
    opt_csv = str(tmp_path / "opt.csv")
    G.write_csv(opt_csv, ["id", "name", "email", "is_company", "country"], opt_rows)
    t0 = time.monotonic()
    ok_opt, st_opt = A.import_with_stats(
        conn_config,
        "res.partner",
        opt_csv,
        str(tmp_path / "opt_fail.csv"),
        resolve_relations=_COUNTRY_SPEC,
    )
    opt_secs = time.monotonic() - t0

    assert ok_naive and ok_opt
    A.assert_reconciled(st_naive)
    A.assert_reconciled(st_opt)

    # (a) Identical resulting state: same count, and country_id correctly set
    # to Belgium for the rows that should be Belgium, on both paths.
    A.assert_db_count(rpc, "res.partner", G.name_domain("ormNaive"), n)
    A.assert_db_count(rpc, "res.partner", G.name_domain("ormOpt"), n)
    naive_be = A.count(
        rpc, "res.partner", [["name", "like", "ormNaive %"], ["country_id", "=", be_id]]
    )
    opt_be = A.count(
        rpc, "res.partner", [["name", "like", "ormOpt %"], ["country_id", "=", be_id]]
    )
    assert naive_be == opt_be > 0, (
        f"country_id resolution differs: naive={naive_be} optimized={opt_be}"
    )

    # (b) Report wall-time (improvement scales with distinct-relation cardinality
    # and dataset size; meaningful at larger FLUVO_E2E_SCALE).
    ratio = naive_secs / opt_secs if opt_secs else float("inf")
    print(
        f"\n[ORM-min timing] naive={naive_secs:.2f}s optimized={opt_secs:.2f}s "
        f"speedup={ratio:.2f}x (n={n})"
    )
    # Soft guard: optimized must not be dramatically slower (no flaky hard floor).
    assert opt_secs <= naive_secs * 2.0 + 1.0


def test_second_run_is_idempotent(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """(c): re-running with skip-unchanged sends ~0 rows."""
    # Dotted module.name xmlids so the existing-record lookup (which requires
    # 'module.name') can find them on re-run. Only scalar text fields: boolean
    # "1" vs stored True is a known representation mismatch, orthogonal to this.
    rows = G.partners(scale, "ormIdem")
    for r in rows:
        r["id"] = f"x_ormidem.{r['id']}"
    csv_path = str(tmp_path / "idem.csv")
    G.write_csv(csv_path, ["id", "name", "email"], rows)

    ok1, _ = A.import_with_stats(
        conn_config, "res.partner", csv_path, str(tmp_path / "idem1_fail.csv")
    )
    assert ok1
    A.assert_db_count(rpc, "res.partner", G.name_domain("ormIdem"), scale)

    # Second run: anti-join should classify all as unchanged.
    ok2, st2 = A.import_with_stats(
        conn_config,
        "res.partner",
        csv_path,
        str(tmp_path / "idem2_fail.csv"),
        skip_unchanged=True,
    )
    assert ok2
    changed = st2.get("changed_records", st2.get("total_records", 0))
    assert changed == 0, f"expected ~0 changed rows on re-run, got {changed}"
    A.assert_db_count(rpc, "res.partner", G.name_domain("ormIdem"), scale)


def test_bad_row_is_isolated(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """(d): one deliberately-bad row is isolated; the rest import."""
    rows = G.partners(scale, "ormBad")
    rows[0]["name"] = ""  # name is required -> this row must fail
    bad_id = rows[0]["id"]
    csv_path = str(tmp_path / "bad.csv")
    fail = str(tmp_path / "bad_fail.csv")
    G.write_csv(csv_path, ["id", "name", "email", "is_company"], rows)

    _ok, stats = A.import_with_stats(
        conn_config, "res.partner", csv_path, fail, batch_size=50
    )
    A.assert_reconciled(stats)
    assert stats["failed_records"] == 1
    A.assert_db_count(rpc, "res.partner", G.name_domain("ormBad"), scale - 1)
    A.assert_failures(fail, expected_bad=(bad_id,))


def test_flat_file_idempotent_and_clean(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any
) -> None:
    """The committed testdata/res_partner.csv imports, auto-cleans, and re-runs ~0."""
    src = Path(__file__).resolve().parents[2] / "testdata" / "res_partner.csv"
    if not src.exists():
        import pytest

        pytest.skip("testdata/res_partner.csv not present")

    ok1, st1 = A.import_with_stats(
        conn_config,
        "res.partner",
        str(src),
        str(tmp_path / "td_fail.csv"),
        separator=";",
        auto_clean=True,
    )
    assert ok1
    A.assert_reconciled(st1)

    ok2, st2 = A.import_with_stats(
        conn_config,
        "res.partner",
        str(src),
        str(tmp_path / "td_fail2.csv"),
        separator=";",
        skip_unchanged=True,
        auto_clean=True,
    )
    assert ok2
    changed = st2.get("changed_records", st2.get("total_records", 0))
    assert changed == 0, f"testdata re-run should send ~0 rows, got {changed}"
