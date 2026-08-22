"""Tests for Parquet as the export/import intermediate format (PLAN 4.8)."""

from pathlib import Path

import polars as pl

from fluvo.exporter import _write_export_frame
from fluvo.importer import _parquet_to_temp_csv


def test_parquet_to_temp_csv_coerces_types(tmp_path: Path) -> None:
    """A Parquet source is read and coerced to import-ready strings (bool -> 1/0)."""
    src = tmp_path / "data.parquet"
    pl.DataFrame(
        {"id": ["a", "b"], "name": ["x", "y"], "is_company": [True, False]}
    ).write_parquet(src)

    tmp_csv = _parquet_to_temp_csv(str(src), separator=",")
    try:
        # The temp CSV sits next to the Parquet file (so fail files resolve there).
        assert Path(tmp_csv).parent == src.resolve().parent
        rows = pl.read_csv(tmp_csv, separator=",", infer_schema_length=0).to_dicts()
        assert rows == [
            {"id": "a", "name": "x", "is_company": "1"},
            {"id": "b", "name": "y", "is_company": "0"},
        ]
    finally:
        Path(tmp_csv).unlink(missing_ok=True)


def test_write_export_frame_parquet_vs_csv(tmp_path: Path) -> None:
    """_write_export_frame picks Parquet or CSV by the output extension."""
    frame = pl.DataFrame({"id": ["a"], "n": [1]})

    pq = tmp_path / "out.parquet"
    _write_export_frame(frame, str(pq), separator=",")
    assert pl.read_parquet(pq).to_dicts() == [{"id": "a", "n": 1}]

    csv = tmp_path / "out.csv"
    _write_export_frame(frame, str(csv), separator=",")
    assert "id,n" in csv.read_text()
