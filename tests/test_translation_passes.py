"""Tests for the #254 per-language translation write passes in the importer.

These drive :func:`fluvo.importer._run_translation_passes` (and its summary
renderer) directly, patching the low-level ``import_data`` engine so each pass's
generated CSV, language context and reconciliation can be inspected without a
live Odoo.
"""

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
                "model": kwargs.get("model"),
                "columns": df.columns,
                "rows": df.to_dicts(),
                "fail_file": kwargs.get("fail_file"),
            }
        )
        if fail_rows and kwargs.get("fail_file"):
            # Mimic the engine writing a fail file (header + fail_rows lines).
            Path(kwargs["fail_file"]).write_text(
                "id,_ERROR_REASON\n" + "".join("x,boom\n" for _ in range(fail_rows))
            )
        created = df.height - fail_rows
        return True, {"created_records": created, "total_records": df.height}

    return fake_import_data


def _source() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "id": ["p1", "p2", "p3"],
            "name": ["Alpha", "Beta", "Gamma"],
            "name@nl_NL": ["Alfa", "", "Gamma-nl"],
            "name@fr_FR": ["Alphe", "Bete", ""],
        }
    )


def test_translation_passes_build_per_language_csv(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Each language gets one update pass with the field@lang column renamed."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("fluvo.importer.import_threaded.import_data", _capture(calls))

    summaries = importer._run_translation_passes(
        config="c.conf",
        model="res.partner",
        translations={"nl_NL": ["name"], "fr_FR": ["name"]},
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

    by_lang = {c["context"]["lang"]: c for c in calls}
    assert set(by_lang) == {"nl_NL", "fr_FR"}
    # Base context is preserved and merged, not replaced.
    assert by_lang["nl_NL"]["context"]["tracking_disable"] is True
    # Column renamed from name@nl_NL -> name; never creates.
    assert by_lang["nl_NL"]["columns"] == ["id", "name"]
    assert by_lang["nl_NL"]["force_create"] is False
    # Empty nl_NL value for p2 is dropped; p1 and p3 remain.
    nl_ids = {r["id"] for r in by_lang["nl_NL"]["rows"]}
    assert nl_ids == {"p1", "p3"}
    # Empty fr_FR value for p3 is dropped.
    fr_ids = {r["id"] for r in by_lang["fr_FR"]["rows"]}
    assert fr_ids == {"p1", "p2"}

    nl_summary = next(s for s in summaries if s["lang"] == "nl_NL")
    assert nl_summary["attempted"] == 2
    assert nl_summary["written"] == 2
    assert nl_summary["failed"] == 0


def test_translation_passes_scope_to_imported_ids(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Rows whose external id was never imported are excluded from the pass."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("fluvo.importer.import_threaded.import_data", _capture(calls))

    importer._run_translation_passes(
        config="c.conf",
        model="res.partner",
        translations={"nl_NL": ["name"]},
        source_df=_source(),
        id_map={"p1": 1},  # only p1 was imported
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


def test_translation_passes_report_failures(tmp_path: Path, monkeypatch: Any) -> None:
    """A fail file left by the engine is reflected in the summary."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "fluvo.importer.import_threaded.import_data", _capture(calls, fail_rows=1)
    )

    summaries = importer._run_translation_passes(
        config="c.conf",
        model="res.partner",
        translations={"nl_NL": ["name"]},
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
    assert s["attempted"] == 2
    assert s["failed"] == 1
    assert s["written"] == 1
    assert s["fail_file"]


def test_translation_passes_skip_when_all_empty(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A language whose column is entirely blank issues no write pass."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("fluvo.importer.import_threaded.import_data", _capture(calls))

    df = pl.DataFrame({"id": ["p1"], "name": ["A"], "name@de_DE": [""]})
    summaries = importer._run_translation_passes(
        config="c.conf",
        model="res.partner",
        translations={"de_DE": ["name"]},
        source_df=df,
        id_map={"p1": 1},
        id_column="id",
        base_context={},
        max_conn=1,
        batch_size=10,
        separator=",",
        encoding="utf-8",
        output_dir=tmp_path,
    )
    assert calls == []
    assert summaries[0]["attempted"] == 0


def test_render_translation_summary_smoke() -> None:
    """The summary renderer runs for both clean and failed passes."""
    importer._render_translation_summary(
        [
            {
                "lang": "nl_NL",
                "fields": ["name"],
                "attempted": 2,
                "written": 2,
                "failed": 0,
                "fail_file": "",
            },
            {
                "lang": "fr_FR",
                "fields": ["name"],
                "attempted": 3,
                "written": 2,
                "failed": 1,
                "fail_file": "res_partner_fr_FR_translations_fail.csv",
            },
        ]
    )
    importer._render_translation_summary([])  # no-op path
