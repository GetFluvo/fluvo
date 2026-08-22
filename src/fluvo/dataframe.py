"""Public Polars-DataFrame API: move data between a Polars frame and Odoo.

These are Fluvo's supported entry points for use as a **library** from any
Polars-native pipeline — Flowfile, notebooks, scripts, orchestrators — without
shelling out to the CLI:

* :func:`load_dataframe` — write a Polars DataFrame into an Odoo model (a direct
  load: two-pass relational writes via ``id``/``field/id``, reconciliation, and a
  fail file).
* :func:`export_dataframe` — read records from an Odoo model into a Polars
  DataFrame.

Column-naming follows ``fluvo import`` / ``fluvo export``: ``id`` external id,
``field/id`` relational lookup by external id, ``field/.id`` by database id.

Scope of :func:`load_dataframe` (a direct load, **not** the full CLI orchestration):
the pre-flight checks, auto-deferral heuristics, and the per-language / per-company
passes (``field@lang`` / ``field@company``) are CLI-only — those columns are
rejected here rather than silently dropped. Use the ``fluvo import`` CLI when you
need them.
"""

from __future__ import annotations

from typing import Any

import polars as pl

from . import export_threaded
from .importer import run_import_for_migration
from .logging_config import log


class FluvoError(RuntimeError):
    """A Fluvo library operation failed."""


# Polars dtypes that have no meaningful single-cell string form for an Odoo load.
# Casting them either raises (List/Duration/Binary) or silently produces garbage
# (Struct -> "{1}"), so we refuse them with a clear message instead.
_UNSUPPORTED_BASE_TYPES = (
    pl.List,
    pl.Array,
    pl.Struct,
    pl.Object,
    pl.Binary,
    pl.Duration,
)


def _reject_unsupported_dtypes(df: pl.DataFrame) -> None:
    """Raise if any column has a dtype with no sound scalar string form.

    Args:
        df: The frame to check.

    Raises:
        FluvoError: naming the first offending column and dtype.
    """
    for name, dtype in df.schema.items():
        if dtype.base_type() in _UNSUPPORTED_BASE_TYPES:
            raise FluvoError(
                f"Column '{name}' has dtype {dtype}, which has no sound value for "
                f"an Odoo load. Convert it to a scalar/string column first "
                f"(e.g. join a list into a comma-separated string of external ids)."
            )


def _coerce_for_odoo(df: pl.DataFrame) -> pl.DataFrame:
    """Cast every column to an Odoo-import-ready string.

    Odoo's ``load`` consumes strings shaped like a CSV cell, so a *natural* Polars
    frame (real booleans, dates, numbers, nulls) needs a light, predictable
    conversion:

    * Boolean -> ``"1"`` / ``"0"`` (Odoo expects 1/0, not ``true``/``false``).
    * Date -> ``YYYY-MM-DD``; Datetime -> ``YYYY-MM-DD HH:MM:SS`` (a tz-aware
      Datetime is converted to **UTC** first — Odoo stores naive UTC — so the wall
      clock is not silently shifted); Time -> ``HH:MM:SS``.
    * Float: non-finite values (``NaN``/``inf``) become empty (they are nulls in
      spirit); finite floats keep their string form. Note that a float column
      feeding an Odoo **integer** field will fail parsing (``"1.0"``): use an
      integer column for integer fields.
    * Everything else -> its string form.
    * Nulls -> ``""`` (empty string). On an *update* this clears the field rather
      than leaving it unchanged.

    Args:
        df: The source frame.

    Returns:
        pl.DataFrame: The same columns, every value an import-ready string.
        Columns with an un-loadable dtype (list/struct/binary/…) are refused first
        via :func:`_reject_unsupported_dtypes`.
    """
    _reject_unsupported_dtypes(df)
    exprs = []
    for name, dtype in df.schema.items():
        base = dtype.base_type()
        col = pl.col(name)
        if dtype == pl.Boolean:
            expr = col.cast(pl.Int8).cast(pl.Utf8)
        elif base == pl.Date:
            expr = col.dt.strftime("%Y-%m-%d")
        elif base == pl.Datetime:
            # Odoo stores naive UTC; normalise a tz-aware column to UTC and drop the
            # zone so strftime doesn't emit shifted wall-clock time.
            if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None:
                col = col.dt.convert_time_zone("UTC").dt.replace_time_zone(None)
            expr = col.dt.strftime("%Y-%m-%d %H:%M:%S")
        elif base == pl.Time:
            expr = col.dt.strftime("%H:%M:%S")
        elif base in (pl.Float32, pl.Float64):
            # NaN/inf are not real values for Odoo — treat them as empty.
            col = pl.when(col.is_finite()).then(col).otherwise(None)
            expr = col.cast(pl.Utf8, strict=False)
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
) -> tuple[bool, dict[str, Any]]:
    """Load a Polars DataFrame into an Odoo model.

    The DataFrame's column names are the import header — name them as you would a
    CSV for ``fluvo import`` (``id``, ``field/id``, ``field/.id``). An ``id``
    (external id) column is required, to match/create records. By default values are
    coerced from their Polars types to Odoo-import-ready strings (see
    :func:`_coerce_for_odoo`); pass ``coerce=False`` if the frame already holds
    import-ready strings.

    This is a **direct load**, not the full CLI orchestration: it does not run the
    pre-flight checks or the per-language / per-company passes. ``field@lang`` and
    ``field@company`` columns are therefore rejected here (use the ``fluvo import``
    CLI for those) rather than being sent to Odoo and silently failing.

    Args:
        df: The data to load (a materialised ``polars.DataFrame``).
        config: A connection config file path, or a config dict.
        model: The target Odoo model (e.g. ``"res.partner"``).
        worker: Number of parallel connections.
        batch_size: Records per load batch.
        fail_file: Path for rows that fail to import. **Strongly recommended:** when
            None and rows fail, those rows are only counted, not recoverable.
        coerce: When True (default), cast Polars types to import-ready strings.

    Returns:
        tuple[bool, dict[str, Any]]: ``(success, stats)``. ``success`` is True only
        when the load completed **and no rows failed** (``stats['failed_records']``
        is 0), so ``if success:`` is safe. ``stats`` carries the reconciliation
        counts. An empty frame is a no-op that returns success with zero counts.

    Raises:
        FluvoError: If ``df`` is not a materialised DataFrame, is missing an
            ``id`` column, uses ``@`` (translation/per-company) columns, or
            contains a column whose dtype cannot be coerced.
    """
    if not isinstance(df, pl.DataFrame):
        raise FluvoError(
            "load_dataframe expects a polars.DataFrame; call .collect() on a "
            "LazyFrame first."
        )
    at_columns = [c for c in df.columns if "@" in c]
    if at_columns:
        raise FluvoError(
            f"Columns {at_columns} use the '@' (translation / per-company) "
            "convention, which load_dataframe does not support — it is a direct "
            "load without the CLI's per-language/per-company passes. Use the "
            "`fluvo import` CLI for field@lang / field@company."
        )
    if "id" not in df.columns:
        raise FluvoError(
            "load_dataframe requires an 'id' (external id) column to match or "
            "create records."
        )
    # Refuse un-loadable dtypes up front, so coerce=False and empty frames are
    # guarded too (not only the coerce=True path).
    _reject_unsupported_dtypes(df)
    if df.is_empty():
        return True, {"total_records": 0, "created_records": 0, "failed_records": 0}

    prepared = _coerce_for_odoo(df) if coerce else df
    header = prepared.columns
    data = [list(row) for row in prepared.iter_rows()]
    success, raw_stats = run_import_for_migration(
        config=config,
        model=model,
        header=header,
        data=data,
        worker=worker,
        batch_size=batch_size,
        fail_file=fail_file,
    )
    # Pin the core reconciliation keys so callers can always read them.
    stats: dict[str, Any] = {
        "total_records": 0,
        "created_records": 0,
        "failed_records": 0,
        **raw_stats,
    }
    failed = int(stats["failed_records"])
    unaccounted = int(stats.get("unaccounted_records", 0))
    if failed and not fail_file:
        log.warning(
            f"{failed} row(s) failed to load into '{model}' and no fail_file was "
            "given — they are counted but not recoverable. Pass fail_file= to "
            "capture them."
        )
    if unaccounted:
        log.warning(
            f"{unaccounted} row(s) were unaccounted for loading into '{model}' "
            "(created + failed < total — often duplicate ids). Verify the result."
        )
    # A partial load is not a success: only report True when nothing failed.
    return (bool(success) and failed == 0), stats


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
    ``load_dataframe`` is a Polars-native round-trip. Values come back **typed**
    (booleans as ``Boolean``, integers as ``Int64``, etc., per Fluvo's Odoo→Polars
    type map), not as strings.

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
            f"Export of '{model}' failed. Check the connection and field names "
            "(see the logs for the underlying error)."
        )
    return frame
