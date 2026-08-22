"""Public Polars-DataFrame API: move data between a Polars frame and Odoo.

These are Fluvo's supported entry points for use as a **library** from any
Polars-native pipeline — Flowfile, notebooks, scripts, orchestrators — without
shelling out to the CLI:

* :func:`load_dataframe` — write a Polars DataFrame into an Odoo model (the real
  two-pass relational load, reconciliation, and fail file).
* :func:`export_dataframe` — read records from an Odoo model into a Polars
  DataFrame.

Column-naming follows the same conventions as ``fluvo import`` / ``fluvo export``
(``id`` external id, ``field/id`` relational lookup, ``field@lang`` translation,
``field@company`` per-company value).
"""

from __future__ import annotations

from typing import Any

import polars as pl

from . import export_threaded
from .importer import run_import_for_migration


class FluvoError(RuntimeError):
    """A Fluvo library operation failed."""


def _coerce_for_odoo(df: pl.DataFrame) -> pl.DataFrame:
    """Cast every column to an Odoo-import-ready string.

    Odoo's ``load`` consumes strings shaped like a CSV cell, so a *natural* Polars
    frame (real booleans, dates, numbers, nulls) needs a light, predictable
    conversion:

    * Boolean -> ``"1"`` / ``"0"`` (Odoo expects 1/0, not ``true``/``false``).
    * Date -> ``YYYY-MM-DD``; Datetime -> ``YYYY-MM-DD HH:MM:SS``; Time -> ``HH:MM:SS``.
    * Everything else -> its string form.
    * Nulls -> ``""`` (empty string).

    Args:
        df: The source frame.

    Returns:
        pl.DataFrame: The same columns, every value an import-ready string.
    """
    exprs = []
    for name, dtype in df.schema.items():
        col = pl.col(name)
        base = dtype.base_type()
        if dtype == pl.Boolean:
            expr = col.cast(pl.Int8).cast(pl.Utf8)
        elif base == pl.Date:
            expr = col.dt.strftime("%Y-%m-%d")
        elif base == pl.Datetime:
            expr = col.dt.strftime("%Y-%m-%d %H:%M:%S")
        elif base == pl.Time:
            expr = col.dt.strftime("%H:%M:%S")
        else:
            expr = col.cast(pl.Utf8, strict=False)
        exprs.append(expr.fill_null("").alias(name))
    return df.select(exprs)


def load_dataframe(
    df: pl.DataFrame,
    config: str | dict[str, Any],
    model: str,
    *,
    worker: int = 1,
    batch_size: int = 100,
    fail_file: str | None = None,
    coerce: bool = True,
) -> tuple[bool, dict[str, int]]:
    """Load a Polars DataFrame into an Odoo model.

    The DataFrame's column names are the import header — name them exactly as you
    would a CSV for ``fluvo import`` (``id``, ``field/id``, ``field@lang``,
    ``field@company``). By default values are coerced from their Polars types to
    Odoo-import-ready strings (see :func:`_coerce_for_odoo`); pass
    ``coerce=False`` if the frame already holds import-ready strings.

    Args:
        df: The data to load.
        config: A connection config file path, or a config dict.
        model: The target Odoo model (e.g. ``"res.partner"``).
        worker: Number of parallel connections.
        batch_size: Records per load batch.
        fail_file: Optional path for rows that fail to import (never silently
            dropped). When None, no fail file is written.
        coerce: When True (default), cast Polars types to import-ready strings.

    Returns:
        tuple[bool, dict[str, int]]: ``(overall_success, stats)`` from Fluvo's
        import engine, so the caller can branch on a partial failure. An empty
        frame is a no-op that returns success with zero counts.
    """
    if df.is_empty():
        return True, {"total_records": 0, "created_records": 0, "failed_records": 0}
    prepared = _coerce_for_odoo(df) if coerce else df
    header = prepared.columns
    data = [list(row) for row in prepared.iter_rows()]
    return run_import_for_migration(
        config=config,
        model=model,
        header=header,
        data=data,
        worker=worker,
        batch_size=batch_size,
        fail_file=fail_file,
    )


def export_dataframe(
    config: str | dict[str, Any],
    model: str,
    fields: list[str],
    *,
    domain: list[Any] | None = None,
    context: dict[str, Any] | None = None,
    worker: int = 1,
    batch_size: int = 1000,
    technical_names: bool = False,
) -> pl.DataFrame:
    """Export records from an Odoo model into a Polars DataFrame.

    The mirror of :func:`load_dataframe`: ``export_dataframe`` then
    ``load_dataframe`` is a Polars-native round-trip. Values come back as strings
    (as ``fluvo export`` produces them).

    Args:
        config: A connection config file path, or a config dict.
        model: The source Odoo model.
        fields: Fields to export (the same specifiers as ``fluvo export --fields``,
            e.g. ``["id", "name", "country_id/id"]``).
        domain: An Odoo search domain (defaults to all records).
        context: An Odoo context (e.g. ``{"lang": "nl_NL"}``).
        worker: Number of parallel connections.
        batch_size: Records per fetch batch.
        technical_names: Force the high-performance raw read mode.

    Returns:
        pl.DataFrame: The exported records (one row per record, columns = fields).

    Raises:
        FluvoError: If the export fails.
    """
    success, _session, _count, frame = export_threaded.export_data(
        config=config,
        model=model,
        domain=domain or [],
        header=list(fields),
        output=None,
        context=context or {},
        max_connection=worker,
        batch_size=batch_size,
        technical_names=technical_names,
        streaming=False,
    )
    if not success or frame is None:
        raise FluvoError(
            f"Export of '{model}' failed. Check the connection and field names."
        )
    return frame
