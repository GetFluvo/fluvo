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


def test_coerce_tz_aware_datetime_normalised_to_utc() -> None:
    """A tz-aware datetime is converted to UTC before formatting (no silent shift)."""
    df = pl.DataFrame(
        {"id": ["a"], "ts": [datetime(2026, 8, 1, 12, 0, 0)]}
    ).with_columns(pl.col("ts").dt.replace_time_zone("Europe/Amsterdam"))
    out = _coerce_for_odoo(df).to_dicts()[0]
    # 12:00 Amsterdam (summer, +02:00) is 10:00 UTC — the stored wall clock, shifted.
    assert out["ts"] == "2026-08-01 10:00:00"


def test_coerce_non_finite_float_becomes_empty() -> None:
    """NaN and inf are not real Odoo values -> empty string."""
    df = pl.DataFrame({"id": ["a", "b", "c"], "f": [1.5, float("nan"), float("inf")]})
    out = _coerce_for_odoo(df)["f"].to_list()
    assert out == ["1.5", "", ""]


def test_coerce_rejects_unsupported_dtype() -> None:
    """List/struct columns are refused with a clear error, not silent garbage."""
    df = pl.DataFrame({"id": ["a"], "tags": [[1, 2, 3]]})
    with pytest.raises(FluvoError, match="no sound value"):
        _coerce_for_odoo(df)


def test_load_dataframe_rejects_at_columns() -> None:
    """field@lang / field@company columns are rejected (CLI-only), not sent to Odoo."""
    df = pl.DataFrame({"id": ["a"], "name@nl_NL": ["x"]})
    with pytest.raises(FluvoError, match="@"):
        load_dataframe(df, "c.conf", "res.partner")


def test_load_dataframe_requires_id_column() -> None:
    """A frame with no 'id' column is rejected — including a '.id'-only frame.

    The engine hardcodes the external-id key, so a bare '.id' row key is not a
    valid load; reject it up front rather than fail deep with only a log line.
    """
    with pytest.raises(FluvoError, match="'id'"):
        load_dataframe(pl.DataFrame({"name": ["x"]}), "c.conf", "res.partner")
    with pytest.raises(FluvoError, match="'id'"):
        load_dataframe(pl.DataFrame({".id": ["7"], "name": ["x"]}), "c.conf", "m")


def test_load_dataframe_rejects_bad_dtype_even_without_coercion() -> None:
    """An un-loadable dtype is refused even with coerce=False."""
    df = pl.DataFrame({"id": ["a"], "tags": [[1, 2]]})
    with pytest.raises(FluvoError, match="no sound value"):
        load_dataframe(df, "c.conf", "res.partner", coerce=False)


def test_load_dataframe_rejects_lazyframe() -> None:
    """A LazyFrame gives a clear error, not an AttributeError.

    In a plain run the isinstance guard raises FluvoError; under the typeguard
    session the annotation is enforced first and raises TypeCheckError — both are
    acceptable, the point is a clear failure rather than an AttributeError deep in.
    """
    try:
        from typeguard import TypeCheckError

        expected: tuple[type[Exception], ...] = (FluvoError, TypeCheckError)
    except ImportError:  # pragma: no cover - typeguard is a dev dependency
        expected = (FluvoError,)
    lf = pl.LazyFrame({"id": ["a"], "name": ["x"]})
    with pytest.raises(expected):
        load_dataframe(lf, "c.conf", "res.partner")  # type: ignore[arg-type]


@patch("fluvo.dataframe.run_import_for_migration")
def test_load_dataframe_partial_failure_is_not_success(mock_run: MagicMock) -> None:
    """Engine 'success' with failed rows must not read as success (no silent loss)."""
    mock_run.return_value = (True, {"created_records": 8, "failed_records": 2})
    df = pl.DataFrame({"id": ["a", "b"], "name": ["x", "y"]})
    ok, stats = load_dataframe(df, "c.conf", "res.partner")
    assert ok is False  # 2 rows failed -> overall not success
    assert stats["failed_records"] == 2
