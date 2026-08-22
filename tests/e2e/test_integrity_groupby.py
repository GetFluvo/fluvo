"""E2E: ``--groupby`` keeps parallel imports of contended data clean.

Deadlocks / "concurrent update" errors are a real-Odoo + PostgreSQL concurrency
phenomenon — they only exist with a live DB and ``--worker > 1``, so this cannot be
covered by the in-memory unit fakes. Deadlocks are also non-deterministic, so we do
NOT try to *force* one; instead we assert the **positive**: a high-contention
dataset imported in parallel **with** ``--groupby`` completes with no data loss and
no concurrent-update / serialization failures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import assertions as A
from . import generators as G


def _fail_path(tmp_path: Any, name: str) -> str:
    return str(tmp_path / f"{name}_fail.csv")


def test_groupby_keeps_contended_parallel_import_clean(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """Many children share a few parents; parallel + ``--groupby`` stays clean.

    Without grouping, several workers could write children of the *same* parent at
    once and hit "could not serialize access" / concurrent-update errors. Grouping
    by ``parent_id`` routes same-parent records to one worker, so the whole import
    must succeed: every record present, reconciled, and no serialization failures
    in the fail file.
    """
    prefix = "e2egrp"
    # High fanout -> few parents, each with many children -> strong write contention
    # on the shared parent rows during the (parallel) Pass-2 relation writes.
    rows = G.hierarchy(scale, prefix, fanout=max(2, scale // 4))
    csv_path = str(tmp_path / "groupby.csv")
    G.write_csv(csv_path, ["id", "name", "email", "is_company", "parent_id"], rows)

    fail_file = _fail_path(tmp_path, "groupby")
    success, stats = A.import_with_stats(
        conn_config,
        "res.partner",
        csv_path,
        fail_file,
        deferred_fields=["parent_id"],
        worker=4,
        groupby=["parent_id"],
    )

    # No silent loss, every record imported.
    A.assert_reconciled(stats)
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale)
    assert success
    # At least one parent actually linked (Pass 2 ran).
    linked = A.count(
        rpc,
        "res.partner",
        [["name", "like", f"{prefix} %"], ["parent_id", "!=", False]],
    )
    assert linked > 0, "Pass 2 linked no parents."

    # The positive assertion: grouping prevented concurrency failures.
    fp = Path(fail_file)
    if fp.exists():
        text = fp.read_text().lower()
        assert "concurrent update" not in text, "groupby did not prevent contention"
        assert "could not serialize" not in text, "groupby did not prevent contention"


def test_auto_groupby_parallel_import_is_clean(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, scale: int
) -> None:
    """End-to-end: --auto-groupby detects a column and imports cleanly in parallel.

    Drives the real ``run_import`` -> preflight auto-detection -> grouped parallel
    load against a live Odoo. ``country_id/id`` is a non-self many2one with
    duplication (and >1 distinct value), so the detector should partition Pass-1
    batches by it; every record must land with no concurrent-update failures.
    """
    from fluvo import importer

    prefix = "e2eautogrp"
    countries = ["base.be", "base.fr", "base.de", "base.nl"]
    rows = G.partners(scale, prefix)
    for i, row in enumerate(rows):
        row["country_id/id"] = countries[i % len(countries)]
    csv_path = str(tmp_path / "autogrp.csv")
    G.write_csv(csv_path, ["id", "name", "email", "is_company", "country_id/id"], rows)

    result = importer.run_import(
        config=conn_config,
        filename=csv_path,
        model="res.partner",
        deferred_fields=None,
        auto_defer=False,
        unique_id_field="id",
        no_preflight_checks=False,
        headless=True,
        worker=4,
        batch_size=50,
        skip=0,
        fail=False,
        separator=",",
        ignore=None,
        # Import explicitly under company 1 (== the CLI's --company-id 1, which sets
        # allowed_company_ids). Other e2e tests create a second company in the shared
        # DB, and the #255 company guard aborts a company-aware import that leaves the
        # company implicit on a multi-company DB — the guard is correct, so the test
        # states its intent rather than opting out with --allow-default-company.
        context={"allowed_company_ids": [1]},
        encoding="utf-8",
        o2m=False,
        groupby=None,
        auto_groupby=True,
    )

    assert result is not None, "auto-groupby import returned no id-map (failed)"
    A.assert_db_count(rpc, "res.partner", G.name_domain(prefix), scale)
    fail_path = Path(tmp_path) / "res_partner_fail.csv"
    if fail_path.exists():
        text = fail_path.read_text().lower()
        assert "concurrent update" not in text, "auto-groupby didn't prevent contention"
        assert "could not serialize" not in text
