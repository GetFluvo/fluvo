"""Tests for the public Polars-DataFrame API (`fluvo.load_dataframe` / export)."""

from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from fluvo import export_dataframe, load_dataframe
from fluvo.dataframe import FluvoError, _coerce_for_odoo


def test_coerce_types_to_import_ready_strings() -> None:
    """Bools -> 1/0, temporals -> ISO strings, nulls -> '', others -> str."""
    # Row 0 = real values, row 1 = all nulls, row 2 = another real row.
    df = pl.DataFrame(
        {
            "flag": [True, None, False],
            "d": [date(2026, 8, 1), None, date(2026, 1, 2)],
            "ts": [datetime(2026, 8, 1, 9, 30, 0), None, datetime(2026, 1, 2, 3, 4, 5)],
            "t": [time(9, 30, 0), None, time(1, 2, 3)],
            "n": [1, None, 3],
            "f": [1.5, None, 2.0],
        }
    )
    out = _coerce_for_odoo(df).to_dicts()
    assert out[0] == {
        "flag": "1",
        "d": "2026-08-01",
        "ts": "2026-08-01 09:30:00",
        "t": "09:30:00",
        "n": "1",
        "f": "1.5",
    }
    assert out[2]["flag"] == "0"  # False -> "0"
    # Every null in row 1 became an empty string.
    assert set(out[1].values()) == {""}


@patch("fluvo.dataframe.run_import_for_migration")
def test_load_dataframe_coerces_and_forwards(mock_run: MagicMock) -> None:
    """load_dataframe coerces then forwards header + rows to the import engine."""
    mock_run.return_value = (True, {"created_records": 2})
    df = pl.DataFrame({"id": ["a", "b"], "is_company": [True, False]})
    ok, stats = load_dataframe(df, "c.conf", "res.partner")
    assert ok and stats["created_records"] == 2
    kwargs = mock_run.call_args.kwargs
    assert kwargs["model"] == "res.partner"
    assert kwargs["header"] == ["id", "is_company"]
    assert kwargs["data"] == [["a", "1"], ["b", "0"]]  # bool coerced


@patch("fluvo.dataframe.run_import_for_migration")
def test_load_dataframe_coerce_false_passes_raw(mock_run: MagicMock) -> None:
    """coerce=False forwards the frame's values without conversion."""
    mock_run.return_value = (True, {})
    df = pl.DataFrame({"id": ["a"], "name": ["x"]})
    load_dataframe(df, "c.conf", "m", coerce=False)
    assert mock_run.call_args.kwargs["data"] == [["a", "x"]]


@patch("fluvo.dataframe.run_import_for_migration")
def test_load_dataframe_empty_is_noop(mock_run: MagicMock) -> None:
    """An empty frame is a no-op success and never calls the engine."""
    ok, stats = load_dataframe(pl.DataFrame({"id": []}), "c.conf", "m")
    assert ok and stats["created_records"] == 0
    mock_run.assert_not_called()


@patch("fluvo.dataframe.export_threaded.export_data")
def test_export_dataframe_returns_frame(mock_export: MagicMock) -> None:
    """export_dataframe returns the engine's DataFrame on success."""
    frame = pl.DataFrame({"id": ["a"], "name": ["x"]})
    mock_export.return_value = (True, "sid", 1, frame)
    out = export_dataframe(
        "c.conf", "res.partner", ["id", "name"], domain=[["x", "=", 1]]
    )
    assert out.to_dicts() == [{"id": "a", "name": "x"}]
    kwargs = mock_export.call_args.kwargs
    assert kwargs["header"] == ["id", "name"]
    assert kwargs["domain"] == [["x", "=", 1]]
    assert kwargs["output"] is None


@patch("fluvo.dataframe.export_threaded.export_data")
def test_export_dataframe_raises_on_failure(mock_export: MagicMock) -> None:
    """A failed export raises FluvoError rather than returning a bad/None frame."""
    mock_export.return_value = (False, "sid", 0, None)
    with pytest.raises(FluvoError):
        export_dataframe("c.conf", "res.partner", ["id"])
