"""Tests for the #255 part-2 per-company write passes in the importer."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl

from fluvo import importer


def _capture(
    calls: list[dict[str, Any]], *, fail_rows: int = 0
) -> Callable[..., tuple[bool, dict[str, Any]]]:
    """Return an import_data stand-in that records its args and reads the CSV."""

    def fake_import_data(**kwargs: Any) -> tuple[bool, dict[str, Any]]:
        df = pl.read_csv(kwargs["file_csv"], separator=kwargs.get("separator", ";"))
        calls.append(
            {
                "context": kwargs.get("context"),
                "force_create": kwargs.get("force_create"),
                "columns": df.columns,
                "rows": df.to_dicts(),
            }
        )
        if fail_rows and kwargs.get("fail_file"):
            Path(kwargs["fail_file"]).write_text(
                "id,_ERROR_REASON\n" + "".join("x,boom\n" for _ in range(fail_rows))
            )
        return True, {"created_records": df.height - fail_rows}

    return fake_import_data


def _source() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "id": ["p1", "p2", "p3"],
            "standard_price@1": ["10", "", "30"],
            "standard_price@2": ["11", "22", ""],
        }
    )


def test_company_passes_build_per_company_csv(tmp_path: Path, monkeypatch: Any) -> None:
    """Each company gets one update pass with field@company renamed + its context."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("fluvo.importer.import_threaded.import_data", _capture(calls))

    summaries = importer._run_company_passes(
        config="c.conf",
        model="product.template",
        company_column_map={"standard_price@1": 1, "standard_price@2": 2},
        source_df=_source(),
        id_map={"p1": 1, "p2": 2, "p3": 3},
        id_column="id",
        base_context={"tracking_disable": True},
        max_conn=1,
        batch_size=10,
        separator=",",
        encoding="utf-8",
        output_dir=tmp_path,
    )

    by_company = {c["context"]["company_id"]: c for c in calls}
    assert set(by_company) == {1, 2}
    c1 = by_company[1]
    # Column renamed to the base field; never creates; base context preserved.
    assert c1["columns"] == ["id", "standard_price"]
    assert c1["force_create"] is False
    assert c1["context"]["tracking_disable"] is True
    # All the company-context keys are set for cross-version coverage.
    assert c1["context"]["allowed_company_ids"] == [1]
    assert c1["context"]["force_company"] == 1
    # Blank company-1 value for p2 dropped -> p1, p3 remain.
    assert {r["id"] for r in c1["rows"]} == {"p1", "p3"}
    # Blank company-2 value for p3 dropped -> p1, p2 remain.
    assert {r["id"] for r in by_company[2]["rows"]} == {"p1", "p2"}

    s1 = next(s for s in summaries if s["company"] == 1)
    assert s1["fields"] == ["standard_price"]
    assert s1["attempted"] == 2
    assert s1["written"] == 2
    assert s1["failed"] == 0


def test_company_passes_scope_to_imported_ids(tmp_path: Path, monkeypatch: Any) -> None:
    """Rows whose external id was never imported are excluded."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("fluvo.importer.import_threaded.import_data", _capture(calls))

    importer._run_company_passes(
        config="c.conf",
        model="product.template",
        company_column_map={"standard_price@1": 1},
        source_df=_source(),
        id_map={"p1": 1},  # only p1 imported
        id_column="id",
        base_context={},
        max_conn=1,
        batch_size=10,
        separator=",",
        encoding="utf-8",
        output_dir=tmp_path,
    )
    assert len(calls) == 1
    assert {r["id"] for r in calls[0]["rows"]} == {"p1"}


def test_company_passes_track_unaccounted(tmp_path: Path, monkeypatch: Any) -> None:
    """Rows neither written nor failed are surfaced as unaccounted."""

    def fake(**kwargs: Any) -> tuple[bool, dict[str, Any]]:
        df = pl.read_csv(kwargs["file_csv"], separator=kwargs.get("separator", ";"))
        return True, {"created_records": df.height - 1}

    monkeypatch.setattr("fluvo.importer.import_threaded.import_data", fake)

    summaries = importer._run_company_passes(
        config="c.conf",
        model="product.template",
        company_column_map={"standard_price@1": 1},
        source_df=_source(),
        id_map={"p1": 1, "p3": 3},
        id_column="id",
        base_context={},
        max_conn=1,
        batch_size=10,
        separator=",",
        encoding="utf-8",
        output_dir=tmp_path,
    )
    s = summaries[0]
    assert s["attempted"] == 2  # p1, p3 (p2 blank for company 1)
    assert s["written"] == 1
    assert s["unaccounted"] == 1


def test_company_passes_report_failures(tmp_path: Path, monkeypatch: Any) -> None:
    """A fail file left by the engine is reflected in the summary."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "fluvo.importer.import_threaded.import_data", _capture(calls, fail_rows=1)
    )

    summaries = importer._run_company_passes(
        config="c.conf",
        model="product.template",
        company_column_map={"standard_price@2": 2},
        source_df=_source(),
        id_map={"p1": 1, "p2": 2},
        id_column="id",
        base_context={},
        max_conn=1,
        batch_size=10,
        separator=",",
        encoding="utf-8",
        output_dir=tmp_path,
    )
    s = summaries[0]
    assert s["company"] == 2
    assert s["attempted"] == 2
    assert s["failed"] == 1
    assert s["written"] == 1
    assert s["fail_file"]


def test_render_company_summary_smoke() -> None:
    """The summary renderer runs for both clean and failed passes."""
    importer._render_company_summary(
        [
            {
                "company": 1,
                "fields": ["standard_price"],
                "attempted": 2,
                "written": 2,
                "failed": 0,
                "unaccounted": 0,
                "fail_file": "",
            },
            {
                "company": 2,
                "fields": ["standard_price"],
                "attempted": 3,
                "written": 2,
                "failed": 1,
                "unaccounted": 0,
                "fail_file": "product_template_company_2_fail.csv",
            },
        ]
    )
    importer._render_company_summary([])  # no-op path
