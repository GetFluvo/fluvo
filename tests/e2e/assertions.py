"""Integrity assertions and an import driver for the e2e suite.

The point of these helpers is to make every scenario prove the same four things:

1. **Reconciliation** - ``created + failed (+ expected unaccounted) == total``; the
   engine never silently loses a record (the #178 guarantee).
2. **DB truth** - the target database actually contains what we expect, queried over
   XML-RPC/JSON-RPC, not inferred from the importer's own bookkeeping.
3. **Fail-file completeness** - every intentionally-bad row lands in the fail file with
   an ``_ERROR_REASON``, and no good row does.
4. **Relational correctness** - deferred (Pass-2) relations resolve to the right record.
"""

from __future__ import annotations

import csv
from typing import Any, Optional

from odoo_data_flow import import_threaded, importer


def import_with_stats(
    conn_config: dict[str, Any],
    model: str,
    file_csv: str,
    fail_file: str,
    *,
    unique_id_field: str = "id",
    separator: str = ",",
    deferred_fields: Optional[list[str]] = None,
    worker: int = 1,
    batch_size: int = 100,
    groupby: Optional[list[str]] = None,
    skip_existing: bool = False,
    skip_unchanged: bool = False,
    context: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> tuple[bool, dict[str, Any]]:
    """Run the low-level import engine and return ``(success, stats)``.

    Checkpoints/resume are disabled so scenarios are independent and repeatable.

    Returns:
        The ``(success, stats)`` tuple from :func:`import_threaded.import_data`.
    """
    return import_threaded.import_data(
        config=conn_config,
        model=model,
        unique_id_field=unique_id_field,
        file_csv=file_csv,
        fail_file=fail_file,
        separator=separator,
        deferred_fields=deferred_fields or [],
        max_connection=worker,
        batch_size=batch_size,
        split_by_cols=groupby,
        skip_existing=skip_existing,
        skip_unchanged=skip_unchanged,
        context=context,
        enable_checkpoint=False,
        resume=False,
        **kwargs,
    )


def run_full_import(
    conn_config: dict[str, Any],
    model: str,
    file_csv: str,
    *,
    auto_defer: bool = False,
    deferred_fields: Optional[list[str]] = None,
    unique_id_field: Optional[str] = None,
    worker: int = 1,
    batch_size: int = 100,
    separator: str = ",",
    groupby: Optional[list[str]] = None,
    no_preflight_checks: bool = False,
) -> Optional[dict[str, int]]:
    """Drive the full CLI-equivalent import (preflight + deferral planning).

    Use this when the behaviour under test lives in the orchestration layer
    (e.g. auto-deferral / required-field handling), not just the engine.

    Returns:
        The external-id -> db-id map, or None on failure.
    """
    return importer.run_import(
        config=conn_config,
        filename=file_csv,
        model=model,
        deferred_fields=deferred_fields,
        auto_defer=auto_defer,
        unique_id_field=unique_id_field,
        no_preflight_checks=no_preflight_checks,
        headless=True,
        worker=worker,
        batch_size=batch_size,
        skip=0,
        fail=False,
        separator=separator,
        ignore=None,
        context={},
        encoding="utf-8",
        o2m=False,
        groupby=groupby,
        no_checkpoint=True,
        resume=False,
    )


# --- Reconciliation -------------------------------------------------------------


def assert_reconciled(stats: dict[str, Any], *, expect_unaccounted: int = 0) -> None:
    """Assert no records were silently dropped.

    Args:
        stats: The stats dict from :func:`import_with_stats`.
        expect_unaccounted: Records expected to be unaccounted (e.g. duplicate
            external IDs collapse to one create). Default 0 = nothing may vanish.
    """
    total = stats.get("total_records", 0)
    created = stats.get("created_records", 0)
    failed = stats.get("failed_records", 0)
    unaccounted = stats.get("unaccounted_records", 0)
    assert unaccounted == expect_unaccounted, (
        f"unaccounted_records={unaccounted}, expected {expect_unaccounted}. "
        f"(total={total}, created={created}, failed={failed})"
    )
    assert created + failed + unaccounted == total, (
        f"Reconciliation failed: created({created}) + failed({failed}) + "
        f"unaccounted({unaccounted}) != total({total})"
    )


# --- DB truth -------------------------------------------------------------------


def count(rpc: Any, model: str, domain: Optional[list[Any]] = None) -> int:
    """Count records in the target DB matching ``domain``."""
    return int(rpc.get_model(model).search_count(domain or []))


def search_read(
    rpc: Any, model: str, domain: list[Any], fields: list[str]
) -> list[dict[str, Any]]:
    """search_read against the target DB."""
    return list(rpc.get_model(model).search_read(domain, fields))


def xmlid_to_res_id(rpc: Any, xmlid: str) -> Optional[int]:
    """Resolve ``module.name`` to its database id via ``ir.model.data``."""
    module, _, name = xmlid.partition(".")
    rows = rpc.get_model("ir.model.data").search_read(
        [["module", "=", module], ["name", "=", name]], ["res_id"]
    )
    return int(rows[0]["res_id"]) if rows else None


def assert_db_count(
    rpc: Any, model: str, domain: list[Any], expected: int
) -> None:
    """Assert the target DB holds exactly ``expected`` records for ``domain``."""
    actual = count(rpc, model, domain)
    assert actual == expected, (
        f"Expected {expected} {model} records for {domain}, found {actual}."
    )


# --- Fail-file completeness -----------------------------------------------------


def read_fail_file(path: str, separator: str = ",") -> list[dict[str, str]]:
    """Read a fail file into a list of row dicts (empty if absent/header-only)."""
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh, delimiter=separator))
    except FileNotFoundError:
        return []


def assert_failures(
    path: str,
    *,
    id_field: str = "id",
    separator: str = ",",
    expected_bad: tuple[str, ...] = (),
    forbidden: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """Assert the fail file captured exactly the rows we sabotaged.

    Args:
        path: Fail-file path.
        id_field: Column holding the row's external id.
        separator: Delimiter the fail file was written with.
        expected_bad: External ids that MUST appear in the fail file.
        forbidden: External ids that must NOT appear (good rows must not be dropped).

    Returns:
        The parsed fail rows, for further scenario-specific assertions.
    """
    rows = read_fail_file(path, separator)
    if expected_bad:
        assert rows, f"Expected failures in {path} but it is empty."
        assert "_ERROR_REASON" in rows[0], (
            f"Fail file {path} is missing the _ERROR_REASON column."
        )
    captured = {r.get(id_field, "") for r in rows}
    missing = set(expected_bad) - captured
    assert not missing, f"Bad rows not captured in fail file: {sorted(missing)}"
    leaked = set(forbidden) & captured
    assert not leaked, f"Good rows wrongly sent to fail file: {sorted(leaked)}"
    return rows
