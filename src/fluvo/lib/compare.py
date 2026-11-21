"""Compare an Odoo model against a spreadsheet — ``fluvo compare`` (issue #298).

The recurring client question is *"does what's in Odoo match this spreadsheet?"*.
A generic diff tool cannot answer it: it does not know that ``country_id``
**displays** ``"Netherlands"`` and **stores** ``166``, that a Dutch workbook writes
``WAAR`` where Odoo stores ``True``, or that Excel turned the account code ``007``
into ``7``. Run naively, a string comparison reports so many false differences
that the result is worse than useless.

So the diff itself is the easy part and **the normalisation is the product**. This
module is that normalisation layer plus a frame-to-frame comparator:

* :func:`compare_frames` — pure, ``DataFrame`` in / results out. No Odoo
  connection, no file I/O, so the whole trap table below is unit-testable offline.
* :func:`run_compare` — the thin connector: read the source, export the model,
  look up the field types, render.

The two sides are symmetric, so the source does not have to be a spreadsheet.
:func:`read_source_file` reads a workbook; :func:`read_source_odoo` reads a second
Odoo instance over RPC (``--against-connection``, issue #350), which is the
overlap check a migration needs — old instance against new, no intermediate file.
Same comparator either way; only the reader differs.

The traps it closes, each of which a naive ``str(a) == str(b)`` gets wrong:

===================  ===========================  ==========================
Trap                 Spreadsheet side             Odoo side
===================  ===========================  ==========================
Numerics             ``1.0``                      ``1``
Leading zeros        ``7`` (Excel ate the zeros)  ``007``
Booleans             ``WAAR`` / ``Ja`` / ``1``    ``True``
Dates                an Excel serial number       ``2024-01-05``
Datetimes            ``05-01-2024 10:00``         ``2024-01-05 10:00:00``
many2one             ``"Netherlands"``            ``166`` / ``base.nl``
Selection            the label ``"Confirmed"``    the key ``"sale"``
===================  ===========================  ==========================

Two deliberate design rules, both learned from :mod:`fluvo.lib.mangling`:

**Locale is a parameter, never a guess.** Whether the comma is a decimal or a
thousands separator changes what ``1,100`` means by three orders of magnitude, and
the failure is silent. Likewise ``05-01-2024`` is January 5th or May 1st depending
on :attr:`CompareOptions.date_order`. Callers state both.

**Read-only, by construction.** Nothing here imports the importer or the writer,
and no function opens a write path. ``compare`` may *emit* a Fluvo-loadable CSV of
the differences so that "compare, then fix" stays two explicit steps rather than
one dangerous one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import polars as pl

from ..logging_config import log
from . import conf_lib
from .mangling import ANGLO, DUTCH, Locale

__all__ = [
    "ANGLO",
    "DUTCH",
    "CompareError",
    "CompareOptions",
    "CompareResult",
    "CompareStats",
    "FieldSpec",
    "Locale",
    "compare_frames",
    "run_compare",
]

# Excel's day 1 is 1900-01-01 but it also believes 1900 was a leap year, so the
# serial-number epoch that reproduces its arithmetic is 1899-12-30.
_EXCEL_EPOCH = datetime(1899, 12, 30)
# Excel's own ceiling: serial 2958465 is 9999-12-31. Anything above is not a date.
_EXCEL_SERIAL_MAX = 2958465

# Tokens a spreadsheet uses for a boolean, in the languages this actually meets.
# Anything outside these sets is left alone and will surface as a difference —
# guessing at an unknown token is how a comparison lies.
_TRUE_TOKENS = frozenset(
    {"1", "true", "t", "yes", "y", "waar", "ja", "j", "x", "wahr", "oui", "vrai"}
)
_FALSE_TOKENS = frozenset(
    {"", "0", "false", "f", "no", "n", "onwaar", "nee", "falsch", "non", "faux"}
)
# Odoo's RPC layer returns False for an empty value of any type; a CSV export of
# it therefore carries the literal "False". For a text field that could be real
# content, so this substitution is applied to typed kinds only.
_EMPTY_LITERALS = frozenset({"false", "none", "null"})

# Odoo field type -> the comparison kind that knows how to normalise it.
_KIND_BY_ODOO_TYPE = {
    "integer": "numeric",
    "float": "numeric",
    "monetary": "numeric",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "selection": "selection",
    "many2one": "relation",
    "many2many": "relation",
    "one2many": "relation",
    "char": "text",
    "text": "text",
    "html": "text",
    "binary": "text",
}

# Kinds whose values are free-form enough that a spreadsheet may have silently
# turned them into a number ("007" -> 7, "1" -> 1.0). For these, two values that
# both parse as numbers and are numerically equal count as equal.
_COERCIBLE_KINDS = frozenset({"text", "relation", "selection"})

#: What Odoo itself emits: a dot decimal separator and no thousands grouping.
#: This is the default for the target side, because the locale question only
#: arises for data that passed through a spreadsheet.
ODOO_LOCALE = Locale(decimal_sep=".", thousands_sep=None)

_DATE_FORMATS = {
    "dmy": ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"),
    "mdy": ("%m-%d-%Y", "%m/%d/%Y", "%m.%d.%Y"),
    "iso": (),
}


class CompareError(RuntimeError):
    """A comparison could not be set up or run."""


@dataclass(frozen=True)
class FieldSpec:
    """How one column should be normalised before it is compared.

    Attributes:
        name: The column name, identical on both frames.
        kind: One of ``text``, ``numeric``, ``boolean``, ``date``, ``datetime``,
            ``selection``, ``relation``. Unknown kinds fall back to ``text``.
        selection: For ``selection`` fields, a mapping of lower-cased label (and
            of the key itself) to the stored key, so a workbook holding the
            human-readable label compares equal to the database's key.
    """

    name: str
    kind: str = "text"
    selection: dict[str, str] | None = None


@dataclass(frozen=True)
class CompareOptions:
    """The decisions a comparison must not make on its own.

    Attributes:
        locale: Which of ``,`` and ``.`` is the decimal separator **on the source
            side**. Getting this wrong misreads ``1,100`` by a factor of a
            thousand, silently.
        target_locale: The same for the target side. Defaults to
            :data:`ODOO_LOCALE`, which is what an Odoo export always uses — set it
            only when the target is itself a spreadsheet.
        date_order: How to read an all-numeric date — ``dmy`` (the European
            default), ``mdy``, or ``iso`` to accept ISO dates only.
        numeric_coercion: When True (default), two text values that both parse as
            numbers and are numerically equal are treated as equal. This is what
            closes the leading-zero and ``1.0``/``1`` traps. Counted separately in
            :attr:`CompareStats.coerced_equal_cells` so it is never invisible.
        case_insensitive: Compare text and relation values ignoring case.
        float_decimals: Decimal places a numeric value is rounded to before
            comparison, absorbing float representation noise.
        datetime_granularity: ``second`` (default) or ``date`` to ignore the time
            part of a datetime field.
    """

    locale: Locale = DUTCH
    target_locale: Locale = ODOO_LOCALE
    date_order: str = "dmy"
    numeric_coercion: bool = True
    case_insensitive: bool = False
    float_decimals: int = 6
    datetime_granularity: str = "second"


@dataclass
class CompareStats:
    """Counts describing one comparison run."""

    source_rows: int = 0
    target_rows: int = 0
    matched_records: int = 0
    identical_records: int = 0
    differing_records: int = 0
    differing_cells: int = 0
    coerced_equal_cells: int = 0
    only_in_source: int = 0
    only_in_target: int = 0
    duplicate_source_keys: int = 0
    duplicate_target_keys: int = 0
    unkeyed_source_rows: int = 0
    unkeyed_target_rows: int = 0

    @property
    def clean(self) -> bool:
        """True when every row matched on both sides with no differences."""
        return (
            self.differing_records == 0
            and self.only_in_source == 0
            and self.only_in_target == 0
            and self.duplicate_source_keys == 0
            and self.duplicate_target_keys == 0
            and self.unkeyed_source_rows == 0
            and self.unkeyed_target_rows == 0
        )


@dataclass
class CompareResult:
    """The buckets a comparison produces.

    Every row of both inputs lands in exactly one bucket: nothing is silently
    dropped, which is the point of keeping the unkeyed and duplicate buckets
    rather than filtering them away.

    Attributes:
        differences: Long form — one row per differing *cell*, with the key
            columns plus ``field``, ``source_value`` and ``target_value``.
        only_in_source: Original source rows whose key is absent from the target.
        only_in_target: Original target rows whose key is absent from the source.
        duplicate_source_keys: Original source rows sharing a key with another
            source row. They cannot be compared unambiguously, so they are
            reported instead.
        duplicate_target_keys: The same, on the target side.
        unkeyed_source: Original source rows with an empty or partial key.
        unkeyed_target: The same, on the target side.
        stats: The counts.
    """

    differences: pl.DataFrame
    only_in_source: pl.DataFrame
    only_in_target: pl.DataFrame
    duplicate_source_keys: pl.DataFrame
    duplicate_target_keys: pl.DataFrame
    unkeyed_source: pl.DataFrame
    unkeyed_target: pl.DataFrame
    stats: CompareStats = field(default_factory=CompareStats)


# --------------------------------------------------------------------------
# Normalisation expressions
# --------------------------------------------------------------------------


def _raw(column: str) -> pl.Expr:
    """Read a column as a trimmed string, whatever its incoming dtype."""
    return pl.col(column).cast(pl.String, strict=False).str.strip_chars()


def _blank_typed(expr: pl.Expr) -> pl.Expr:
    """Map Odoo's empty markers (``False``/``None``/``null``) to the "" sentinel."""
    low = expr.str.to_lowercase()
    return (
        pl.when(expr.is_null() | low.is_in(list(_EMPTY_LITERALS)))
        .then(pl.lit(""))
        .otherwise(expr)
    )


def _numeric(expr: pl.Expr, options: CompareOptions) -> pl.Expr:
    """Parse a locale-formatted numeric string into a Float64 (null if it isn't one).

    The thousands separator is only stripped when the value is **actually
    grouped** -- ``1.234.567,89`` in Dutch, not ``1.0``. A spreadsheet cell that
    held a real number comes back from the reader as the Python repr ``1.0``
    whatever the workbook's locale, so blindly deleting every dot would read it
    as ten.

    Args:
        expr: A trimmed string expression.
        options: Supplies the locale.

    Returns:
        pl.Expr: A Float64 expression, null where the value is not a number.
    """
    cleaned = expr.str.replace_all(r"[\s\u00a0]", "")
    decimal_sep = options.locale.decimal_sep
    thousands_sep = options.locale.thousands_sep

    def _as_point(value: pl.Expr) -> pl.Expr:
        if decimal_sep == ".":
            return value
        return value.str.replace_all(decimal_sep, ".", literal=True)

    plain = _as_point(cleaned)
    if not thousands_sep:
        return plain.cast(pl.Float64, strict=False)
    group = re.escape(thousands_sep)
    point = re.escape(decimal_sep)
    is_grouped = cleaned.str.contains(
        rf"^[+-]?\d{{1,3}}({group}\d{{3}})+({point}\d+)?$"
    )
    ungrouped = _as_point(cleaned.str.replace_all(thousands_sep, "", literal=True))
    return (
        pl.when(is_grouped)
        .then(ungrouped)
        .otherwise(plain)
        .cast(pl.Float64, strict=False)
    )


def _numeric_canonical(expr: pl.Expr, options: CompareOptions) -> pl.Expr:
    """Canonical string for a number: rounded, with no trailing zeros ("1.0" -> "1").

    Args:
        expr: A trimmed string expression.
        options: Locale and rounding settings.

    Returns:
        pl.Expr: The canonical form, or null when the value is not numeric.
    """
    rounded = _numeric(expr, options).round(options.float_decimals)
    text = rounded.cast(pl.String, strict=False)
    # 1.0 -> 1, 1.2300 -> 1.23. Trim the fractional tail, then a bare ".".
    trimmed = text.str.replace(r"(\.\d*?)0+$", "${1}").str.replace(r"\.$", "")
    return pl.when(rounded.is_null()).then(None).otherwise(trimmed)


def _boolean_canonical(expr: pl.Expr) -> pl.Expr:
    """Canonical "1"/"0" for any known boolean token; unknown tokens pass through."""
    low = expr.fill_null("").str.to_lowercase()
    return (
        pl.when(low.is_in(list(_TRUE_TOKENS)))
        .then(pl.lit("1"))
        .when(low.is_in(list(_FALSE_TOKENS)))
        .then(pl.lit("0"))
        .otherwise(low)
    )


def _excel_serial(expr: pl.Expr) -> pl.Expr:
    """Read an Excel serial number as a datetime, or null when it is not one."""
    num = expr.cast(pl.Float64, strict=False)
    days = num.floor()
    seconds = ((num - days) * 86400).round(0).cast(pl.Int64, strict=False)
    moment = (
        pl.lit(_EXCEL_EPOCH, dtype=pl.Datetime("us"))
        + pl.duration(days=days.cast(pl.Int64, strict=False))
        + pl.duration(seconds=seconds)
    )
    valid = num.is_not_null() & (num >= 1) & (num <= _EXCEL_SERIAL_MAX)
    return pl.when(valid).then(moment).otherwise(None)


def _parsed_moment(expr: pl.Expr, options: CompareOptions) -> pl.Expr:
    """Parse a date/datetime written in any of the shapes these two sides use.

    Args:
        expr: A trimmed string expression.
        options: Supplies ``date_order`` for all-numeric dates.

    Returns:
        pl.Expr: A Datetime expression, null where nothing parsed.
    """
    # The ISO prefix covers "2024-01-05", "2024-01-05 10:00:00" and the "T" form
    # in one step, including fractional seconds Odoo sometimes emits.
    iso_date = expr.str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False)
    iso_time = (
        expr.str.replace("T", " ", literal=True)
        .str.slice(0, 19)
        .str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False, time_unit="us")
    )
    candidates = [iso_time, iso_date.cast(pl.Datetime("us"))]
    for fmt in _DATE_FORMATS.get(options.date_order, ()):
        candidates.append(
            expr.str.to_datetime(f"{fmt} %H:%M:%S", strict=False, time_unit="us")
        )
        candidates.append(
            expr.str.to_datetime(f"{fmt} %H:%M", strict=False, time_unit="us")
        )
        candidates.append(expr.str.to_date(fmt, strict=False).cast(pl.Datetime("us")))
    candidates.append(_excel_serial(expr))
    return pl.coalesce(candidates)


def _moment_canonical(
    expr: pl.Expr, options: CompareOptions, *, with_time: bool
) -> pl.Expr:
    """Canonical ISO form for a date or datetime, falling back to the raw string.

    An unparseable value keeps its raw text rather than becoming null, so a
    genuinely malformed cell shows up as a difference instead of quietly matching
    another malformed cell.

    Args:
        expr: A trimmed string expression.
        options: Date order and datetime granularity.
        with_time: Include the time part.

    Returns:
        pl.Expr: The canonical string.
    """
    moment = _parsed_moment(expr, options)
    fmt = (
        "%Y-%m-%d %H:%M:%S"
        if with_time and options.datetime_granularity != "date"
        else "%Y-%m-%d"
    )
    return pl.coalesce([moment.dt.strftime(fmt), _blank_typed(expr)])


def _text_canonical(expr: pl.Expr, options: CompareOptions) -> pl.Expr:
    """Canonical text: a many2one tuple reduced to its label, optionally casefolded."""
    # An RPC many2one arrives as "(166, 'Netherlands')" once stringified; compare
    # on the label, which is what the workbook holds.
    label = expr.str.extract(r"^\(\s*\d+\s*,\s*['\"](.*)['\"]\s*\)$", 1)
    value = pl.coalesce([label, expr]).fill_null("")
    return value.str.to_lowercase() if options.case_insensitive else value


def _selection_canonical(
    expr: pl.Expr, spec: FieldSpec, options: CompareOptions
) -> pl.Expr:
    """Map a selection label to its stored key; keys and unknown labels pass through."""
    low = expr.fill_null("").str.to_lowercase()
    if not spec.selection:
        return _text_canonical(expr, options)
    mapped = low.replace_strict(
        dict(spec.selection), default=None, return_dtype=pl.String
    )
    return pl.coalesce([mapped, low])


def _key_canonical(expr: pl.Expr, spec: FieldSpec, options: CompareOptions) -> pl.Expr:
    """Canonical form for a *key* column.

    Keys get one extra repair the compared fields do not: an all-digit key that
    lost its leading zeros to a spreadsheet (``007`` -> ``7``) is de-zeroed on both
    sides, because account codes, postcodes and SKUs are exactly what people key
    on. It is deliberately narrower than the numeric coercion used for field
    values -- parsing the key as a number would make a text key containing a comma
    normalise differently on each side, and a key that fails to join is a far
    worse outcome than a value that reads as different.

    Args:
        expr: A trimmed string expression.
        spec: The key field's comparison kind.
        options: Comparison settings.

    Returns:
        pl.Expr: The canonical key part.
    """
    canonical = _canonical(expr, spec, options).fill_null("")
    if not options.numeric_coercion or spec.kind not in _COERCIBLE_KINDS:
        return canonical
    return (
        pl.when(canonical.str.contains(r"^0+\d+$"))
        .then(canonical.str.replace(r"^0+", ""))
        .otherwise(canonical)
    )


def _canonical(expr: pl.Expr, spec: FieldSpec, options: CompareOptions) -> pl.Expr:
    """Build the canonical-string expression for one field spec.

    Args:
        expr: A trimmed string expression for the column.
        spec: The field's comparison kind.
        options: Locale and comparison settings.

    Returns:
        pl.Expr: A non-null Utf8 expression; "" is the empty sentinel.
    """
    if spec.kind == "numeric":
        # Odoo has no "unset" number: an empty integer or float reads back as 0,
        # so an empty cell and a 0 are the same value and must not differ.
        blank = _blank_typed(expr)
        fallback = pl.when(blank == "").then(pl.lit("0")).otherwise(blank)
        return pl.coalesce([_numeric_canonical(expr, options), fallback])
    if spec.kind == "boolean":
        return _boolean_canonical(expr)
    if spec.kind == "date":
        return _moment_canonical(expr, options, with_time=False)
    if spec.kind == "datetime":
        return _moment_canonical(expr, options, with_time=True)
    if spec.kind == "selection":
        return _selection_canonical(expr, spec, options)
    return _text_canonical(expr, options)


# --------------------------------------------------------------------------
# Frame preparation and comparison
# --------------------------------------------------------------------------

_KEY_SEP = "\x1f"


def _require_columns(df: pl.DataFrame, names: list[str], side: str) -> None:
    """Raise a clear error naming every column the frame is missing.

    Args:
        df: The frame to check.
        names: Columns that must exist.
        side: ``source`` or ``target``, for the message.

    Raises:
        CompareError: If any column is absent.
    """
    missing = [n for n in names if n not in df.columns]
    if missing:
        raise CompareError(
            f"The {side} data has no column(s) {missing}. "
            f"It has: {sorted(df.columns)}. "
            "Use --map to pair a differently-named spreadsheet column with an "
            "Odoo field."
        )


def _prepare(
    df: pl.DataFrame,
    specs: list[FieldSpec],
    key_specs: list[FieldSpec],
    options: CompareOptions,
    side: str,
) -> pl.DataFrame:
    """Reduce one frame to its comparison columns, prefixed per side.

    Args:
        df: The original frame.
        specs: Fields to compare.
        key_specs: Fields forming the match key.
        options: Comparison settings.
        side: ``s`` or ``t``; used as the column prefix.

    Returns:
        pl.DataFrame: ``__key``, ``__keyed``, ``{side}_row`` (index into ``df``),
        the canonical key parts, and per field the raw, canonical and (where the
        kind is coercible) numeric-canonical columns.
    """
    exprs: list[pl.Expr] = [
        pl.int_range(pl.len(), dtype=pl.UInt32).alias(f"{side}_row")
    ]

    key_parts: list[pl.Expr] = []
    key_filled: list[pl.Expr] = []
    for j, spec in enumerate(key_specs):
        raw_key = _raw(spec.name)
        canonical = _key_canonical(raw_key, spec, options)
        exprs.append(canonical.alias(f"__k{j}"))
        exprs.append(raw_key.fill_null("").alias(f"{side}_k{j}"))
        key_parts.append(canonical)
        # Whether a key is present is judged on the raw cell, never the canonical
        # one: an empty numeric canonicalises to "0", which is a value, not a key.
        key_filled.append(raw_key.fill_null("") != "")

    for i, spec in enumerate(specs):
        raw = _raw(spec.name)
        exprs.append(raw.alias(f"{side}_r{i}"))
        exprs.append(_canonical(raw, spec, options).fill_null("").alias(f"{side}_n{i}"))
        if options.numeric_coercion and spec.kind in _COERCIBLE_KINDS:
            exprs.append(_numeric_canonical(raw, options).alias(f"{side}_m{i}"))

    exprs += [filled.alias(f"__kf{j}") for j, filled in enumerate(key_filled)]
    prepared = df.select(exprs)
    key_cols = [f"__k{j}" for j in range(len(key_specs))]
    filled_cols = [f"__kf{j}" for j in range(len(key_specs))]
    return prepared.with_columns(
        pl.concat_str([pl.col(c) for c in key_cols], separator=_KEY_SEP).alias("__key"),
        pl.all_horizontal([pl.col(c) for c in filled_cols]).alias("__keyed"),
    ).drop(filled_cols)


def _split_keys(
    prepared: pl.DataFrame, side: str
) -> tuple[pl.DataFrame, list[int], list[int]]:
    """Separate the uniquely-keyed rows from the unkeyed and duplicated ones.

    Args:
        prepared: Output of :func:`_prepare`.
        side: ``s`` or ``t``.

    Returns:
        tuple[pl.DataFrame, list[int], list[int]]: ``(unique_rows,
        unkeyed_row_indices, duplicate_row_indices)``.
    """
    unkeyed = prepared.filter(~pl.col("__keyed"))
    keyed = prepared.filter(pl.col("__keyed"))
    counts = keyed.group_by("__key").len()
    dup_keys = counts.filter(pl.col("len") > 1).select("__key")
    duplicated = keyed.join(dup_keys, on="__key", how="semi")
    unique = keyed.join(dup_keys, on="__key", how="anti")
    return (
        unique,
        unkeyed[f"{side}_row"].to_list(),
        duplicated[f"{side}_row"].to_list(),
    )


def _equality_exprs(
    specs: list[FieldSpec], options: CompareOptions
) -> tuple[list[pl.Expr], list[pl.Expr]]:
    """Build the per-field "equal" and "equal only after numeric coercion" tests.

    Args:
        specs: Fields being compared.
        options: Comparison settings.

    Returns:
        tuple[list[pl.Expr], list[pl.Expr]]: ``(equal_exprs, coerced_exprs)``,
        one entry per field.
    """
    equal: list[pl.Expr] = []
    coerced: list[pl.Expr] = []
    for i, spec in enumerate(specs):
        exact = pl.col(f"s_n{i}") == pl.col(f"t_n{i}")
        if options.numeric_coercion and spec.kind in _COERCIBLE_KINDS:
            numeric_equal = (
                pl.col(f"s_m{i}").is_not_null()
                & pl.col(f"t_m{i}").is_not_null()
                & (pl.col(f"s_m{i}") == pl.col(f"t_m{i}"))
            )
            equal.append(exact | numeric_equal)
            coerced.append(~exact & numeric_equal)
        else:
            equal.append(exact)
            coerced.append(pl.lit(value=False))
    return equal, coerced


def _build_differences(
    matched: pl.DataFrame,
    specs: list[FieldSpec],
    key_names: list[str],
    equal: list[pl.Expr],
) -> pl.DataFrame:
    """Melt the per-field mismatches into one long frame.

    Args:
        matched: The joined, uniquely-keyed rows.
        specs: Fields being compared.
        key_names: Display names of the key columns.
        equal: Per-field equality expressions.

    Returns:
        pl.DataFrame: Key columns plus ``field``, the two raw values, the two
        normalised values actually compared (so a disputed result can be
        explained), and ``target_row`` -- the 0-based row of the record in the
        target frame, which is how a fix file finds its external id again.
    """
    schema: dict[str, Any] = {name: pl.String for name in key_names}
    schema |= {
        "field": pl.String,
        "source_value": pl.String,
        "target_value": pl.String,
        "source_normalised": pl.String,
        "target_normalised": pl.String,
        "target_row": pl.UInt32,
    }
    parts: list[pl.DataFrame] = []
    for i, spec in enumerate(specs):
        hits = matched.filter(~equal[i])
        if hits.is_empty():
            continue
        parts.append(
            hits.select(
                *[pl.col(f"s_k{j}").alias(name) for j, name in enumerate(key_names)],
                pl.lit(spec.name).alias("field"),
                pl.col(f"s_r{i}").alias("source_value"),
                pl.col(f"t_r{i}").alias("target_value"),
                pl.col(f"s_n{i}").alias("source_normalised"),
                pl.col(f"t_n{i}").alias("target_normalised"),
                pl.col("t_row").alias("target_row"),
            )
        )
    if not parts:
        return pl.DataFrame(schema=schema)
    return pl.concat(parts, how="vertical")


def compare_frames(
    source: pl.DataFrame,
    target: pl.DataFrame,
    *,
    key: list[str] | str,
    fields: list[FieldSpec] | list[str],
    key_specs: list[FieldSpec] | None = None,
    options: CompareOptions | None = None,
) -> CompareResult:
    """Compare two frames on a business key, with Odoo-aware normalisation.

    Pure: no connection, no I/O. ``source`` is conventionally the spreadsheet and
    ``target`` the Odoo export, but the comparison is symmetric, which is what
    makes an Odoo-to-Odoo comparison a later change of caller rather than of
    engine.

    Args:
        source: The left frame (typically the workbook).
        target: The right frame (typically the Odoo export).
        key: Column name, or list of names, forming the match key. A composite key
            matches only when every part is non-empty on both sides.
        fields: Fields to compare, as :class:`FieldSpec` objects or bare names
            (which default to ``text``).
        key_specs: Optional normalisation specs for the key columns themselves, so
            a key mangled by Excel still matches. Defaults to ``text``.
        options: Locale and comparison settings. Defaults to
            :class:`CompareOptions` (Dutch locale, day-month-year, coercion on).

    Returns:
        CompareResult: The buckets and the counts.

    Raises:
        CompareError: If a key or compared column is missing from either frame.
    """
    options = options or CompareOptions()
    key_names = [key] if isinstance(key, str) else list(key)
    if not key_names:
        raise CompareError("A comparison needs at least one --key column.")
    specs = [FieldSpec(f) if isinstance(f, str) else f for f in fields]
    ks = key_specs or [FieldSpec(name) for name in key_names]

    needed = key_names + [s.name for s in specs]
    _require_columns(source, needed, "source")
    _require_columns(target, needed, "target")

    src = _prepare(source, specs, ks, options, "s")
    tgt = _prepare(
        target, specs, ks, replace(options, locale=options.target_locale), "t"
    )
    src_unique, src_unkeyed, src_dup = _split_keys(src, "s")
    tgt_unique, tgt_unkeyed, tgt_dup = _split_keys(tgt, "t")

    joined = src_unique.drop("__keyed").join(
        tgt_unique.drop("__keyed", *[f"__k{j}" for j in range(len(ks))]),
        on="__key",
        how="full",
        coalesce=True,
    )
    only_source = joined.filter(pl.col("t_row").is_null())["s_row"].to_list()
    only_target = joined.filter(pl.col("s_row").is_null())["t_row"].to_list()
    matched = joined.filter(
        pl.col("s_row").is_not_null() & pl.col("t_row").is_not_null()
    )

    equal, coerced = _equality_exprs(specs, options)
    stats = CompareStats(
        source_rows=source.height,
        target_rows=target.height,
        matched_records=matched.height,
        only_in_source=len(only_source),
        only_in_target=len(only_target),
        duplicate_source_keys=len(src_dup),
        duplicate_target_keys=len(tgt_dup),
        unkeyed_source_rows=len(src_unkeyed),
        unkeyed_target_rows=len(tgt_unkeyed),
    )
    if specs and matched.height:
        flags = matched.select(
            pl.any_horizontal([~e for e in equal]).alias("__differs"),
            pl.sum_horizontal([(~e).cast(pl.Int64) for e in equal]).alias("__cells"),
            pl.sum_horizontal([c.cast(pl.Int64) for c in coerced]).alias("__coerced"),
        )
        stats.differing_records = int(flags["__differs"].sum())
        stats.differing_cells = int(flags["__cells"].sum())
        stats.coerced_equal_cells = int(flags["__coerced"].sum())
    stats.identical_records = stats.matched_records - stats.differing_records

    differences = _build_differences(matched, specs, key_names, equal)
    return CompareResult(
        differences=differences,
        only_in_source=source[only_source],
        only_in_target=target[only_target],
        duplicate_source_keys=source[src_dup],
        duplicate_target_keys=target[tgt_dup],
        unkeyed_source=source[src_unkeyed],
        unkeyed_target=target[tgt_unkeyed],
        stats=stats,
    )


# --------------------------------------------------------------------------
# Odoo-side metadata
# --------------------------------------------------------------------------


def build_field_specs(
    names: list[str], odoo_fields: dict[str, Any], *, lang_labels: bool = True
) -> list[FieldSpec]:
    """Turn ``fields_get`` metadata into comparison specs.

    Args:
        names: The field specifiers being compared, in Fluvo's export syntax
            (``name``, ``country_id/id``, ``country_id/.id``).
        odoo_fields: ``fields_get()`` output for the model.
        lang_labels: Include the selection labels, so a workbook holding the
            label matches the database's key.

    Returns:
        list[FieldSpec]: One spec per name, defaulting to ``text`` for a field the
        model does not declare (a computed column added by the consultant, say).
    """
    specs: list[FieldSpec] = []
    for name in names:
        base = name.split("/")[0]
        meta = odoo_fields.get(base, {})
        kind = _KIND_BY_ODOO_TYPE.get(str(meta.get("type", "")), "text")
        # "country_id/id" exports an external id, not a label: compare as text.
        if "/" in name:
            kind = "text"
        selection = None
        if kind == "selection" and lang_labels:
            selection = {}
            for entry in meta.get("selection") or []:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                key, label = str(entry[0]), str(entry[1])
                selection[key.lower()] = key
                selection[label.lower()] = key
        specs.append(FieldSpec(name=name, kind=kind, selection=selection or None))
    return specs


def _connect(config: str | dict[str, Any]) -> Any:
    """Open an Odoo connection from a config path or dict."""
    if isinstance(config, dict):
        return conf_lib.get_connection_from_dict(config)
    return conf_lib.get_connection_from_config(config_file=config)


def read_source_file(
    path: str, *, delimiter: str = ";", sheet: str | None = None
) -> pl.DataFrame:
    """Read a workbook or CSV with every column as text.

    Reading as text matters: letting a CSV reader infer types would re-apply the
    very mangling this module exists to see through (``007`` becoming ``7`` a
    second time).

    Args:
        path: Path to a ``.xlsx``/``.xls``/``.xlsm`` workbook, or a delimited file.
        delimiter: Field separator for the delimited case.
        sheet: Worksheet name; defaults to the first sheet.

    Returns:
        pl.DataFrame: All columns as ``String``.

    Raises:
        CompareError: If an Excel file is given but the ``excel`` extra is not
            installed, or the file cannot be read.
    """
    lowered = path.lower()
    try:
        if lowered.endswith(".ods"):
            frame = pl.read_ods(path, sheet_name=sheet, infer_schema_length=0)
        elif lowered.endswith((".xlsx", ".xlsm", ".xls")):
            frame = pl.read_excel(path, sheet_name=sheet, infer_schema_length=0)
        else:
            frame = pl.read_csv(
                path,
                separator=delimiter,
                infer_schema_length=0,
                truncate_ragged_lines=True,
            )
    except (ImportError, ModuleNotFoundError) as exc:
        raise CompareError(
            f"Reading '{path}' needs the spreadsheet extra. "
            "Install it with: pip install 'fluvo[excel]'"
        ) from exc
    except Exception as exc:
        raise CompareError(f"Could not read '{path}': {exc}") from exc
    typed: pl.DataFrame = frame
    return typed.select(pl.all().cast(pl.String, strict=False))


def _export_frame(
    config: str | dict[str, Any],
    model: str,
    fields: list[str],
    *,
    domain: list[Any] | None = None,
    context: dict[str, Any] | None = None,
    side: str = "Odoo",
) -> pl.DataFrame:
    """Export one Odoo model into an all-text frame.

    Everything is cast to ``String`` because the comparator canonicalises text:
    the typed values :func:`~fluvo.dataframe.export_dataframe` returns would make
    each kind need its own code path for no gain.

    Args:
        config: A connection config file path, or a config dict.
        model: The Odoo model to export.
        fields: Field specifiers, in Fluvo's export syntax.
        domain: An Odoo search domain.
        context: An Odoo context.
        side: Named in the error message, so a failure says *which* Odoo failed.

    Returns:
        pl.DataFrame: All columns as ``String``.

    Raises:
        CompareError: If the export fails.
    """
    from ..dataframe import FluvoError, export_dataframe

    try:
        frame = export_dataframe(
            config, model, fields, domain=domain or [], context=context or {}
        )
    except FluvoError as exc:
        raise CompareError(f"{side}: {exc}") from exc
    return frame.select(pl.all().cast(pl.String, strict=False))


def read_source_odoo(
    config: str | dict[str, Any],
    model: str,
    fields: list[str],
    *,
    domain: list[Any] | None = None,
    context: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Read the comparison's source side from a second Odoo instance.

    The spreadsheet adapter's counterpart (issue #350). Comparing two live
    instances was always possible — ``fluvo export`` one side, then compare the
    file — and this only removes the intermediate file.

    One thing it is **not** is a locale question. Odoo writes dot decimals and ISO
    dates whatever the user's language, so a caller comparing two Odoo instances
    wants :data:`ODOO_LOCALE` on *both* sides; :func:`run_compare` sets that.

    A failing export propagates :exc:`CompareError` from :func:`_export_frame`,
    naming the source side so it is clear which of the two instances failed.

    Args:
        config: Connection config path or dict for the **source** instance.
        model: The model to read, same name as on the target.
        fields: The key and compared field specifiers.
        domain: An Odoo search domain limiting the source records.
        context: An Odoo context for the source read.

    Returns:
        pl.DataFrame: All columns as ``String``.
    """
    return _export_frame(
        config,
        model,
        list(dict.fromkeys(fields)),
        domain=domain,
        context=context,
        side="source Odoo",
    )


def apply_column_map(df: pl.DataFrame, mapping: dict[str, str]) -> pl.DataFrame:
    """Rename spreadsheet columns to the Odoo field names they correspond to.

    Args:
        df: The source frame.
        mapping: ``{spreadsheet_column: odoo_field}``.

    Returns:
        pl.DataFrame: The renamed frame.

    Raises:
        CompareError: If a mapped column is not present.
    """
    missing = [c for c in mapping if c not in df.columns]
    if missing:
        raise CompareError(
            f"--map refers to column(s) {missing}, which the file does not have. "
            f"It has: {sorted(df.columns)}."
        )
    return df.rename(mapping)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _stat_rows(
    result: CompareResult, source_label: str, target_label: str
) -> list[tuple[str, str]]:
    """Build the ordered (label, value) pairs shown in every output format."""
    s = result.stats
    return [
        (f"Rows in {source_label}", str(s.source_rows)),
        (f"Rows in {target_label}", str(s.target_rows)),
        ("Matched on key", str(s.matched_records)),
        ("Identical", str(s.identical_records)),
        ("Differing records", str(s.differing_records)),
        ("Differing cells", str(s.differing_cells)),
        ("Equal after numeric coercion", str(s.coerced_equal_cells)),
        (f"Only in {source_label}", str(s.only_in_source)),
        (f"Only in {target_label}", str(s.only_in_target)),
        (f"Duplicate keys in {source_label}", str(s.duplicate_source_keys)),
        (f"Duplicate keys in {target_label}", str(s.duplicate_target_keys)),
        (f"Unkeyed rows in {source_label}", str(s.unkeyed_source_rows)),
        (f"Unkeyed rows in {target_label}", str(s.unkeyed_target_rows)),
    ]


#: Columns kept for machine consumption but dropped from a rendered table.
_INTERNAL_DIFF_COLUMNS = ("source_normalised", "target_normalised", "target_row")


def _display_differences(result: CompareResult) -> pl.DataFrame:
    """Drop the machine-facing columns before a human reads the differences."""
    return result.differences.drop(*_INTERNAL_DIFF_COLUMNS, strict=False)


def render_table(
    result: CompareResult,
    model: str,
    *,
    source_label: str = "file",
    target_label: str = "Odoo",
    sample: int = 20,
) -> None:
    """Print the summary and a sample of differences to the console.

    Args:
        result: The comparison result.
        model: The Odoo model compared, for the titles.
        source_label: Name for the left side.
        target_label: Name for the right side.
        sample: How many difference rows to show.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    summary = Table(title=f"fluvo compare — {model}", show_lines=False)
    summary.add_column("Metric", style="cyan")
    summary.add_column("Count", justify="right")
    for label, value in _stat_rows(result, source_label, target_label):
        summary.add_row(label, value)
    console.print(summary)

    if result.differences.is_empty():
        console.print("[green]No differences on the compared fields.[/green]")
        return
    shown = _display_differences(result)
    detail = Table(
        title=f"Differences (showing {min(sample, shown.height)} of {shown.height})"
    )
    for column in shown.columns:
        detail.add_column(column, overflow="fold")
    for row in shown.head(sample).iter_rows():
        detail.add_row(*["" if v is None else str(v) for v in row])
    console.print(detail)


def to_json(
    result: CompareResult,
    model: str,
    *,
    source_label: str = "file",
    target_label: str = "Odoo",
) -> str:
    """Serialise the whole result — every bucket, not just the summary.

    Args:
        result: The comparison result.
        model: The Odoo model compared.
        source_label: Name for the left side.
        target_label: Name for the right side.

    Returns:
        str: Indented JSON.
    """
    payload = {
        "model": model,
        "source": source_label,
        "target": target_label,
        "stats": vars(result.stats) | {"clean": result.stats.clean},
        "differences": result.differences.to_dicts(),
        "only_in_source": result.only_in_source.to_dicts(),
        "only_in_target": result.only_in_target.to_dicts(),
        "duplicate_source_keys": result.duplicate_source_keys.to_dicts(),
        "duplicate_target_keys": result.duplicate_target_keys.to_dicts(),
        "unkeyed_source": result.unkeyed_source.to_dicts(),
        "unkeyed_target": result.unkeyed_target.to_dicts(),
    }
    return json.dumps(payload, indent=2, default=str)


def to_markdown(
    result: CompareResult,
    model: str,
    *,
    source_label: str = "file",
    target_label: str = "Odoo",
    sample: int = 50,
) -> str:
    """Render the summary and a sample of differences as Markdown.

    Args:
        result: The comparison result.
        model: The Odoo model compared.
        source_label: Name for the left side.
        target_label: Name for the right side.
        sample: How many difference rows to include.

    Returns:
        str: A Markdown document.
    """
    lines = [f"# Comparison — `{model}`", "", "| Metric | Count |", "|---|---:|"]
    lines += [
        f"| {label} | {value} |"
        for label, value in _stat_rows(result, source_label, target_label)
    ]
    lines += ["", "## Differences", ""]
    if result.differences.is_empty():
        lines.append("None on the compared fields.")
        return "\n".join(lines) + "\n"
    shown = _display_differences(result)
    columns = shown.columns
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "---|" * len(columns))
    for row in shown.head(sample).iter_rows():
        cells = ["" if v is None else str(v).replace("|", "\\|") for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    if result.differences.height > sample:
        lines.append("")
        lines.append(f"_{result.differences.height - sample} further rows omitted._")
    return "\n".join(lines) + "\n"


def _addressing_column(target: pl.DataFrame, id_column: str) -> str:
    """Pick the column that can address an existing Odoo record.

    Prefers the external id, because it is portable and readable. Falls back to
    ``.id`` (the database id) when the export could not produce external ids --
    an unusable address is the one thing a fix file must never carry, since
    ``fluvo import`` would read it as a new external id and create duplicates.

    Args:
        target: The Odoo-side frame.
        id_column: The external-id column to prefer.

    Returns:
        str: ``id_column`` or ``".id"``.

    Raises:
        CompareError: If neither column is usable.
    """
    if id_column in target.columns:
        values = target[id_column].cast(pl.String, strict=False).drop_nulls()
        # An external id always carries its module prefix; a bare number is the
        # database id that Fluvo's read-based export puts in this column.
        if values.len() and values.str.contains(r"^[^.]+\.").any():
            return id_column
    if ".id" in target.columns:
        return ".id"
    raise CompareError(
        "Cannot build a fix file: the Odoo export produced neither usable external "
        "ids nor database ids, so the differing records cannot be addressed. "
        "Include 'id' in --fields (or leave --fields off)."
    )


def build_fix_frame(
    result: CompareResult, target: pl.DataFrame, *, id_column: str = "id"
) -> pl.DataFrame:
    """Build a Fluvo-loadable frame of the source values for the differing records.

    This is the explicit second step: ``compare`` never writes to Odoo, but it can
    hand you a file that ``fluvo import`` would. Review it before loading it — the
    source is the spreadsheet, and the spreadsheet is the side more likely to be
    wrong.

    Args:
        result: The comparison result.
        target: The Odoo-side frame, which must carry a column that addresses the
            record -- see :func:`_addressing_column`, which raises
            :class:`CompareError` when it carries none.
        id_column: The external-id column to prefer in ``target``.

    Returns:
        pl.DataFrame: the addressing column (``id``, or ``.id`` when the export
        could not produce external ids) plus one column per differing field,
        holding the **normalised source** value -- the canonical form Odoo will
        accept (``1``/``0`` for a boolean, an ISO date, a selection key), not the
        spreadsheet's raw text. Empty when there is nothing to fix.
    """
    column = _addressing_column(target, id_column)
    if result.differences.is_empty():
        return pl.DataFrame(schema={column: pl.String})
    ids = target.select(
        pl.col(column).cast(pl.String, strict=False).alias(column)
    ).with_row_index("target_row")
    joined = result.differences.join(ids, on="target_row", how="left")
    return joined.pivot(
        on="field",
        index=column,
        values="source_normalised",
        aggregate_function="first",
    )


# --------------------------------------------------------------------------
# The connector
# --------------------------------------------------------------------------


def _resolve_fields(
    requested: str | None,
    source_columns: list[str],
    key_names: list[str],
    odoo_fields: dict[str, Any],
) -> list[str]:
    """Decide which fields to compare.

    Args:
        requested: The ``--fields`` value, or None to infer.
        source_columns: Columns the file actually has.
        key_names: The key columns, which are matched on rather than compared.
        odoo_fields: ``fields_get()`` output, used to drop columns Odoo has no
            field for when inferring.

    Returns:
        list[str]: The field specifiers to compare.

    Raises:
        CompareError: If nothing is left to compare.
    """
    if requested:
        names = [f.strip() for f in requested.split(",") if f.strip()]
    else:
        names = [
            c
            for c in source_columns
            if c not in key_names and c.split("/")[0] in odoo_fields
        ]
    at_columns = [n for n in names if "@" in n]
    if at_columns:
        raise CompareError(
            f"Columns {at_columns} use the '@' (translation / per-company) "
            "convention, which compare does not support yet. Export one language "
            "or company at a time and compare those."
        )
    if not names:
        raise CompareError(
            "No fields to compare. Pass --fields, or name the file's columns after "
            "the Odoo fields they hold."
        )
    return names


def _source_fields_check(
    config: str | dict[str, Any],
    model: str,
    names: list[str],
    target_fields: dict[str, Any],
) -> None:
    """Check the source Odoo declares what is being compared, and agrees on types.

    A migration's whole point is that the two instances differ, so a field missing
    on the source is a normal, reportable situation rather than a crash — and a
    field that is an integer on one side and a char on the other is exactly the
    kind of thing a comparison should say out loud before producing numbers.

    Args:
        config: Connection config for the source instance.
        model: The model being compared.
        names: Key and field specifiers.
        target_fields: ``fields_get()`` of the target, for the type cross-check.

    Raises:
        CompareError: If the source model cannot be read, or lacks a field.
    """
    try:
        source_fields = _connect(config).get_model(model).fields_get()
    except Exception as exc:
        raise CompareError(
            f"Could not read the fields of '{model}' on the source Odoo: {exc}"
        ) from exc

    bases = list(dict.fromkeys(n.split("/")[0] for n in names))
    missing = [b for b in bases if b not in source_fields]
    if missing:
        raise CompareError(
            f"The source Odoo's '{model}' has no field(s) {missing}. The two "
            "instances differ there, so there is nothing to compare: drop them "
            "from --key/--fields, or compare the instances' own field names with "
            "two separate runs."
        )
    for base in bases:
        here = str(source_fields[base].get("type", ""))
        there = str(target_fields.get(base, {}).get("type", ""))
        if there and here != there:
            log.warning(
                f"Field '{base}' is '{here}' on the source Odoo and '{there}' on "
                f"the target. Comparing it as the target's type."
            )


def run_compare(  # noqa: C901
    config: str | dict[str, Any],
    model: str,
    file: str | None,
    key: str,
    *,
    against_connection: str | dict[str, Any] | None = None,
    against_domain: list[Any] | None = None,
    against_context: dict[str, Any] | None = None,
    fields: str | None = None,
    column_map: dict[str, str] | None = None,
    domain: list[Any] | None = None,
    context: dict[str, Any] | None = None,
    delimiter: str = ";",
    sheet: str | None = None,
    options: CompareOptions | None = None,
    fmt: str = "table",
    output: str | None = None,
    write_fixes: str | None = None,
) -> CompareResult:
    """Compare one Odoo model against a spreadsheet or a second Odoo, and report.

    Exactly one source must be given: ``file`` for a workbook or delimited file,
    or ``against_connection`` for a second Odoo instance (issue #350).

    When the source is an Odoo instance, both sides are read with
    :data:`ODOO_LOCALE` and ISO dates regardless of ``options`` — Odoo writes dot
    decimals and ISO dates whatever the user's language, so a spreadsheet locale
    would be the wrong question to ask.

    Args:
        config: A connection config file path, or a config dict, for the target.
        model: The Odoo model to compare against, on both sides.
        file: Path to the workbook or delimited source file, or None when
            ``against_connection`` names a source Odoo instead.
        key: Comma-separated business key column(s) present on both sides.
        against_connection: Connection config path or dict for a source **Odoo**
            instance, instead of ``file``.
        against_domain: Odoo search domain for the source read. Defaults to
            ``domain``, so one filter covers both sides; pass ``[]`` for no
            filter on the source.
        against_context: Odoo context for the source read. Defaults to ``context``.
        fields: Comma-separated fields to compare. Defaults to the file's columns
            that the model declares as fields, and is **required** with
            ``against_connection``, where there are no file columns to infer from.
        column_map: ``{spreadsheet_column: odoo_field}`` renames applied first.
            Not applicable to an Odoo source, which already uses field names.
        domain: Odoo search domain limiting the target records.
        context: Odoo context for the target export (e.g. ``{"lang": "nl_NL"}``).
        delimiter: Field separator, for a delimited source file.
        sheet: Worksheet name, for a workbook.
        options: Locale and comparison settings.
        fmt: ``table``, ``json`` or ``markdown``.
        output: Also write the report to this path.
        write_fixes: Write a Fluvo-loadable CSV of the source values for the
            differing records to this path.

    Returns:
        CompareResult: The buckets and the counts.

    Raises:
        CompareError: If no source or both sources are given, if the model has no
            such fields, or if either export fails.
    """
    from rich.console import Console

    from .internal.ui import _show_error_panel

    if (file is None) == (against_connection is None):
        raise CompareError(
            "Give exactly one source: a file to compare against, or "
            "--against-connection for a second Odoo instance."
        )

    options = options or CompareOptions()
    key_names = [k.strip() for k in key.split(",") if k.strip()]
    if not key_names:
        raise CompareError("--key needs at least one column name.")

    source_label = "file" if file is not None else "source Odoo"
    target_label = "Odoo" if file is not None else "target Odoo"

    source: pl.DataFrame | None = None
    if file is not None:
        source = read_source_file(file, delimiter=delimiter, sheet=sheet)
        if column_map:
            source = apply_column_map(source, column_map)
    elif column_map:
        raise CompareError(
            "--map renames spreadsheet columns, and an Odoo source already uses "
            "field names. Drop --map, or name the fields in --fields."
        )

    connection = _connect(config)
    try:
        odoo_fields = connection.get_model(model).fields_get()
    except Exception as exc:
        raise CompareError(f"Could not read the fields of '{model}': {exc}") from exc

    if source is None and not fields:
        raise CompareError(
            "--fields is required when comparing two Odoo instances: there are no "
            "file columns to infer from, and comparing every field of a model "
            "reports churn (write_date, computed values) rather than drift."
        )
    field_names = _resolve_fields(
        fields, source.columns if source is not None else [], key_names, odoo_fields
    )
    unknown = [n for n in key_names + field_names if n.split("/")[0] not in odoo_fields]
    if unknown:
        raise CompareError(
            f"Model '{model}' has no field(s) {unknown}. Check the spelling, or use "
            "--map to pair the spreadsheet's column with the Odoo field."
        )

    if against_connection is not None:
        _source_fields_check(
            against_connection, model, key_names + field_names, odoo_fields
        )
        source = read_source_odoo(
            against_connection,
            model,
            [*key_names, *field_names],
            domain=against_domain if against_domain is not None else domain,
            context=against_context if against_context is not None else context,
        )
        # Both sides now speak Odoo, so the spreadsheet locale is the wrong
        # question: a dot is a decimal point and a date is ISO, on both sides.
        options = replace(options, locale=ODOO_LOCALE, date_order="iso")
    if source is None:  # pragma: no cover - the XOR check above rules this out
        raise CompareError("No source side to compare.")

    # 'id' is exported alongside so a fix file can address each record, even when
    # the user did not ask for it. '.id' goes with it: when a selection or binary
    # field forces Fluvo's read-based export, a lone 'id' comes back as the
    # *database* id, and a fix file carrying that would create duplicates rather
    # than update. Asking for '.id' too triggers the xml-id enrichment, so 'id'
    # stays an external id in both export modes. It cannot be mixed with
    # non-xmlid relational sub-fields, so in that case it is left out and
    # :func:`build_fix_frame` falls back to the database id.
    export_fields = ["id", *key_names, *field_names]
    mixes_subfields = any(
        "/" in f and not f.endswith("/id") for f in key_names + field_names
    )
    if not mixes_subfields:
        export_fields.insert(1, ".id")
    export_fields = list(dict.fromkeys(export_fields))
    target = _export_frame(
        config,
        model,
        export_fields,
        domain=domain,
        context=context,
        side=target_label,
    )

    specs = build_field_specs(field_names, odoo_fields)
    key_specs = build_field_specs(key_names, odoo_fields)
    result = compare_frames(
        source,
        target,
        key=key_names,
        fields=specs,
        key_specs=key_specs,
        options=options,
    )

    fmt = fmt.lower()
    if fmt == "json":
        Console().print_json(
            to_json(result, model, source_label=source_label, target_label=target_label)
        )
    elif fmt == "markdown":
        Console().print(
            to_markdown(
                result, model, source_label=source_label, target_label=target_label
            )
        )
    else:
        render_table(
            result, model, source_label=source_label, target_label=target_label
        )
    if output:
        text = (
            to_markdown(
                result, model, source_label=source_label, target_label=target_label
            )
            if fmt == "markdown"
            else to_json(
                result, model, source_label=source_label, target_label=target_label
            )
        )
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(text)
        log.info(f"Comparison report written to {output}")

    if write_fixes:
        try:
            fixes = build_fix_frame(result, target)
        except CompareError as exc:
            _show_error_panel("Fix file", str(exc))
        else:
            fixes.write_csv(write_fixes, separator=delimiter)
            log.info(
                f"{fixes.height} record(s) written to {write_fixes}. Review it, then "
                f"load it with: fluvo import --model {model} --file {write_fixes}"
            )
    return result
