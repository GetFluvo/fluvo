"""Value-level profiling for ``fluvo assess --profile`` (issue #349).

``fluvo assess`` on its own reports *metadata*: model inventory, row volumes,
field types, risk flags. It never looks at the values. Metadata cannot see the
problems that actually sink a migration, because those live in the data:

* a numeric column holding **two unit populations** -- the same quantity
  recorded in two different units, with nothing in the schema to say so;
* a date column carrying **several format variants**, some of them
  day/month-ambiguous;
* a text column used as an **enumeration** whose tail is typos and whose
  members collide once case and whitespace are folded;
* a **qualifier field** (unit of measure, currency, language) left empty
  beside a well-populated value field -- the canonical "mixed units, no unit
  recorded" signature.

Design contract (mirrors :mod:`fluvo.lib.mangling`, per #349):

- **Pure Polars.** Every public function is ``DataFrame in -> plain dict out``.
  No Odoo connection, no file I/O. Offline-testable, and reusable by any caller
  that can produce a DataFrame.
- **Locale is a parameter, never a guess.** Whether ``1.234`` is one thousand
  two hundred or one-and-a-bit decides every numeric finding in this module.
  Callers pass a :class:`~fluvo.lib.mangling.Locale`.
- **Detect only.** Nothing here fixes anything; repair is
  :mod:`fluvo.lib.mangling`'s job, and keeping detect separate from fix is
  deliberate.
- **Output is JSON-serialisable** so it folds into the existing ``assess``
  table / JSON / Markdown renderers unchanged.

Every finding carries a stable ``flags`` list, so a report consumer can act on
the flag rather than parse prose.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from itertools import pairwise
from typing import Any

import polars as pl

from .mangling import ANGLO, Locale

#: A text column with at most this many distinct values is treated as an
#: enumeration rather than free text. Above it, listing the values is noise.
ENUM_MAX_DISTINCT = 60

#: Minimum share of non-zero rows a decade must hold to count as part of a
#: population, rather than a handful of outliers.
POPULATION_MIN_SHARE = 0.03

#: Minimum rows on *each* side before the whole-number-vs-decimal comparison is
#: trusted. Below this the medians are noise.
WHOLE_DECIMAL_MIN_SUPPORT = 20

#: How far apart the whole-number and decimal median decades must sit before it
#: reads as two populations rather than ordinary spread.
WHOLE_DECIMAL_MIN_GAP = 2

#: Values used at most this many times are reported as the enumeration's tail.
ENUM_TAIL_MAX_COUNT = 2

#: Date-format variants recognised by :func:`profile_date_column`. Ordered:
#: the first match wins, so put the unambiguous ISO forms first.
DATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("yyyy-mm-dd", re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")),
    ("dd-mm-yyyy", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")),
    ("dd/mm/yyyy or mm/dd/yyyy", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("dd.mm.yyyy", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")),
    ("dd/mm/yy", re.compile(r"^\d{1,2}/\d{1,2}/\d{2}$")),
    ("yyyymmdd", re.compile(r"^(19|20)\d{6}$")),
    ("excel-serial", re.compile(r"^[1-5]\d{4}(\.\d+)?$")),
)

#: Field-name fragments whose job is to *qualify* another field's value. A
#: populated value column beside an empty qualifier is the mixed-unit
#: signature. Matched as substrings of the lowercased column name.
QUALIFIER_HINTS: tuple[str, ...] = (
    "uom",
    "unit",
    "currency",
    "lang",
    "timezone",
    "tz",
)


def _to_number(series: pl.Series, locale: Locale) -> pl.Series:
    """Parse a column to Float64 under an explicit locale.

    Args:
        series: The column to parse; any dtype, cast to String first.
        locale: Which separator is decimal and which groups thousands.

    Returns:
        pl.Series: Float64 values, with unparseable entries as null.
    """
    text = series.cast(pl.String).str.strip_chars()
    if locale.thousands_sep:
        text = text.str.replace_all(re.escape(locale.thousands_sep), "", literal=False)
    if locale.decimal_sep != ".":
        text = text.str.replace_all(re.escape(locale.decimal_sep), ".", literal=False)
    return text.cast(pl.Float64, strict=False)


def _decade_histogram(values: pl.Series) -> dict[int, int]:
    """Count values by order of magnitude, ``floor(log10(abs(v)))``.

    Zero and null are excluded: they have no order of magnitude, and including
    them would smear the populations this histogram exists to separate.

    Args:
        values: Float64 values.

    Returns:
        dict[int, int]: Decade exponent -> number of values in it.
    """
    counts: Counter[int] = Counter()
    for value in values.drop_nulls().to_list():
        if value == 0 or math.isnan(value) or math.isinf(value):
            continue
        counts[math.floor(math.log10(abs(value)))] += 1
    return dict(sorted(counts.items()))


def _populations(histogram: dict[int, int], min_share: float) -> int:
    """Count separated decade clusters in a histogram.

    A cluster is a run of decades each holding at least ``min_share`` of the
    values; two clusters are separate when at least one whole decade between
    them is empty (or below the share). Two clusters in one column is the
    order-of-magnitude signature of two populations sharing a field.

    Args:
        histogram: Decade exponent -> count, from :func:`_decade_histogram`.
        min_share: Minimum share of values for a decade to count.

    Returns:
        int: Number of separated clusters; 0 when nothing clears the share.
    """
    total = sum(histogram.values())
    if not total:
        return 0
    kept = sorted(d for d, n in histogram.items() if n / total >= min_share)
    if not kept:
        return 0
    clusters = 1
    for lower, upper in pairwise(kept):
        if upper - lower >= 2:
            clusters += 1
    return clusters


def _median_decade(histogram: dict[int, int]) -> int | None:
    """Return the median decade of a histogram, or None when it is empty.

    Args:
        histogram: Decade exponent -> count.

    Returns:
        int | None: The median decade exponent, or None if there are no values.
    """
    expanded: list[int] = []
    for decade, count in sorted(histogram.items()):
        expanded.extend([decade] * count)
    if not expanded:
        return None
    return expanded[len(expanded) // 2]


def _present_text(df: pl.DataFrame, column: str, *, strip: bool = True) -> pl.Series:
    """Return a column's populated values as strings.

    Empty means null, blank after stripping, or the literal ``"False"`` that
    Odoo returns for an unset scalar. A genuine text value of ``"False"`` is
    vanishingly rare next to that, and treating it as empty is the safer error.

    Args:
        df: The frame to read.
        column: Column name.
        strip: Whether to strip surrounding whitespace from the values that are
            returned. Emptiness is always judged on the stripped form, but the
            enumeration pass needs the **raw** values: stripping first would
            merge ``"FOO "`` into ``"FOO"`` and silently hide exactly the
            whitespace collision it is looking for.

    Returns:
        pl.Series: The populated values, as ``pl.String``.
    """
    raw = df.get_column(column).cast(pl.String).drop_nulls()
    stripped = raw.str.strip_chars()
    keep = ~stripped.is_in(["", "False"])
    return (stripped if strip else raw).filter(keep)


def fill_rate(df: pl.DataFrame, column: str) -> dict[str, Any]:
    """Report how much of a column is actually populated.

    Empty means null, or -- for text -- a value that is blank once stripped.
    Odoo exports ``False`` for unset scalars, so the string ``"False"`` counts
    as empty too; a real value of ``"False"`` in a text field is vanishingly
    rare next to that.

    Args:
        df: The frame to read.
        column: Column name.

    Returns:
        dict[str, Any]: ``total``, ``filled`` and ``fill_rate`` (0.0-1.0).
    """
    total = df.height
    if total == 0:
        return {"total": 0, "filled": 0, "fill_rate": 0.0}
    present = _present_text(df, column)
    filled = len(present)
    return {
        "total": total,
        "filled": filled,
        "fill_rate": round(filled / total, 4),
    }


def profile_numeric_column(
    df: pl.DataFrame,
    column: str,
    *,
    locale: Locale = ANGLO,
    group_by: str | None = None,
) -> dict[str, Any]:
    """Profile one numeric column for order-of-magnitude populations.

    Two independent tests, either of which flags the column:

    * ``multi-population`` -- the decade histogram falls into two or more
      separated clusters.
    * ``whole-vs-decimal`` -- the whole-number rows and the rows with a
      fractional part sit at least :data:`WHOLE_DECIMAL_MIN_GAP` decades
      apart. Recording the same quantity in two units typically leaves one
      unit integral and the other fractional, so this catches the case even
      when the two ranges overlap enough to defeat the cluster test.

    Args:
        df: The frame to read.
        column: The numeric column to profile.
        locale: Decimal/thousands convention. Never guessed.
        group_by: Optional column to repeat the analysis within, so a mix that
            is invisible overall but present inside one category still shows.

    Returns:
        dict[str, Any]: ``column``, ``kind``, ``fill``, ``min``/``median``/
        ``max``, ``decades``, ``whole``/``decimal`` sub-reports, ``flags``, and
        ``groups`` when ``group_by`` was given.
    """
    numbers = _to_number(df.get_column(column), locale)
    present = numbers.drop_nulls()
    whole = present.filter((present - present.round(0)).abs() < 1e-9)
    decimal = present.filter((present - present.round(0)).abs() >= 1e-9)

    overall = _decade_histogram(present)
    whole_hist = _decade_histogram(whole)
    decimal_hist = _decade_histogram(decimal)

    flags: list[str] = []
    if _populations(overall, POPULATION_MIN_SHARE) > 1:
        flags.append("multi-population")

    whole_median = _median_decade(whole_hist)
    decimal_median = _median_decade(decimal_hist)
    if (
        len(whole) >= WHOLE_DECIMAL_MIN_SUPPORT
        and len(decimal) >= WHOLE_DECIMAL_MIN_SUPPORT
        and whole_median is not None
        and decimal_median is not None
        and abs(whole_median - decimal_median) >= WHOLE_DECIMAL_MIN_GAP
    ):
        flags.append("whole-vs-decimal")

    report: dict[str, Any] = {
        "column": column,
        "kind": "numeric",
        "fill": fill_rate(df, column),
        "min": float(present.min()) if len(present) else None,  # type: ignore[arg-type]
        "median": float(present.median()) if len(present) else None,  # type: ignore[arg-type]
        "max": float(present.max()) if len(present) else None,  # type: ignore[arg-type]
        "decades": {str(k): v for k, v in overall.items()},
        "whole": {
            "count": len(whole),
            "decades": {str(k): v for k, v in whole_hist.items()},
            "median_decade": whole_median,
        },
        "decimal": {
            "count": len(decimal),
            "decades": {str(k): v for k, v in decimal_hist.items()},
            "median_decade": decimal_median,
        },
        "flags": flags,
    }

    if group_by is not None and group_by in df.columns:
        report["groups"] = _profile_numeric_groups(df, column, group_by, locale)
    return report


def _profile_numeric_groups(
    df: pl.DataFrame,
    column: str,
    group_by: str,
    locale: Locale,
    min_rows: int = 10,
) -> list[dict[str, Any]]:
    """Repeat the population test within each value of a grouping column.

    Only groups flagged as multi-population are returned, most-populous first:
    a report listing every clean group is unreadable and hides the finding.

    Args:
        df: The frame to read.
        column: The numeric column.
        group_by: The column to group on.
        locale: Decimal/thousands convention.
        min_rows: Skip groups with fewer non-zero values than this.

    Returns:
        list[dict[str, Any]]: One entry per flagged group, with ``group``,
        ``count`` and ``decades``.
    """
    flagged: list[dict[str, Any]] = []
    frame = pl.DataFrame(
        {
            "_group": df.get_column(group_by).cast(pl.String),
            "_value": _to_number(df.get_column(column), locale),
        }
    ).drop_nulls("_value")
    for (group,), subset in frame.group_by(["_group"]):
        histogram = _decade_histogram(subset.get_column("_value"))
        count = sum(histogram.values())
        if count < min_rows:
            continue
        if _populations(histogram, POPULATION_MIN_SHARE) > 1:
            flagged.append(
                {
                    "group": str(group),
                    "count": count,
                    "decades": {str(k): v for k, v in histogram.items()},
                }
            )
    flagged.sort(key=lambda row: -int(row["count"]))
    return flagged


def profile_date_column(df: pl.DataFrame, column: str) -> dict[str, Any]:
    """Profile one date-like column for format drift and day/month ambiguity.

    Args:
        df: The frame to read.
        column: The column to profile.

    Returns:
        dict[str, Any]: ``column``, ``kind``, ``fill``, ``formats`` (variant ->
        count, with ``(unrecognised)`` for the rest), ``ambiguous`` (count of
        values where the first two components are both <= 12 and differ, so
        day-first and month-first both parse but disagree), and ``flags``.
    """
    formats: Counter[str] = Counter()
    ambiguous = 0
    for raw in df.get_column(column).cast(pl.String).drop_nulls().to_list():
        value = raw.strip()
        if not value or value == "False":
            continue
        for name, pattern in DATE_PATTERNS:
            if pattern.match(value):
                formats[name] += 1
                if _is_day_month_ambiguous(value):
                    ambiguous += 1
                break
        else:
            formats["(unrecognised)"] += 1

    recognised = [name for name in formats if name != "(unrecognised)"]
    flags: list[str] = []
    if len(recognised) > 1:
        flags.append("mixed-formats")
    if formats["(unrecognised)"]:
        flags.append("unrecognised-values")
    if ambiguous:
        flags.append("day-month-ambiguous")

    return {
        "column": column,
        "kind": "date",
        "fill": fill_rate(df, column),
        "formats": dict(formats),
        "ambiguous": ambiguous,
        "flags": flags,
    }


def _is_day_month_ambiguous(value: str) -> bool:
    """Return True when a date string parses differently day-first vs month-first.

    Only non-ISO separators can be ambiguous: a leading four-digit year fixes
    the order.

    Args:
        value: A date string already matched against :data:`DATE_PATTERNS`.

    Returns:
        bool: True when the first two components are both <= 12 and differ.
    """
    if value[:4].isdigit() and len(value) >= 5 and value[4] in "-/.":
        return False
    parts = re.split(r"[-/.]", value)
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return False
    first, second = int(parts[0]), int(parts[1])
    return first <= 12 and second <= 12 and first != second


def profile_enum_column(
    df: pl.DataFrame,
    column: str,
    *,
    max_distinct: int = ENUM_MAX_DISTINCT,
) -> dict[str, Any] | None:
    """Profile one low-cardinality text column as an enumeration.

    Args:
        df: The frame to read.
        column: The column to profile.
        max_distinct: Above this many distinct values the column is free text,
            not an enumeration, and None is returned.

    Returns:
        dict[str, Any] | None: ``column``, ``kind``, ``fill``, ``distinct``,
        ``values`` (value -> count, most frequent first), ``tail`` (values used
        at most :data:`ENUM_TAIL_MAX_COUNT` times), ``collisions`` (groups of
        raw values that fold together on case and whitespace) and ``flags``;
        or None when the column is not an enumeration.
    """
    present = _present_text(df, column, strip=False)
    distinct = present.n_unique()
    if distinct == 0 or distinct > max_distinct:
        return None

    counts = Counter(present.to_list())
    folded: dict[str, list[str]] = {}
    for value in counts:
        key = re.sub(r"\s+", " ", value).strip().casefold()
        folded.setdefault(key, []).append(value)
    collisions = [sorted(group) for group in folded.values() if len(group) > 1]
    tail = sorted(v for v, n in counts.items() if n <= ENUM_TAIL_MAX_COUNT)

    flags: list[str] = []
    if collisions:
        flags.append("case-space-collisions")
    if tail:
        flags.append("tail-values")

    return {
        "column": column,
        "kind": "enumeration",
        "fill": fill_rate(df, column),
        "distinct": distinct,
        "values": dict(counts.most_common()),
        "tail": tail,
        "collisions": collisions,
        "flags": flags,
    }


def is_identifier_column(column: str) -> bool:
    """Return True when a numeric column is an identifier, not a measurement.

    A primary or foreign key is a number, but it is not a *quantity*: it has no
    unit, so pairing it with a unit-of-measure field produces a finding that is
    always wrong. Excluding these is what keeps the qualifier check quiet
    enough to be worth reading.

    Args:
        column: The column name.

    Returns:
        bool: True for ``id``, ``*_id``, ``*_ids`` and ``*/id`` export headers.
    """
    lowered = column.lower()
    return (
        lowered == "id" or lowered.endswith(("_id", "_ids")) or lowered.endswith("/id")
    )


def _qualifier_stem(qualifier: str) -> str:
    """Strip the qualifier fragment from a name, leaving what it qualifies.

    ``quantity_uom_id`` -> ``quantity``. A bare qualifier such as ``uom_id`` or
    ``currency_id`` leaves nothing, which marks it as qualifying the record as
    a whole rather than one named field.

    Args:
        qualifier: The qualifier column's name.

    Returns:
        str: The stem, lowercased, or an empty string for a bare qualifier.
    """
    stem = qualifier.lower()
    for suffix in ("_id", "_ids", "/id"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    for hint in QUALIFIER_HINTS:
        stem = stem.replace(hint, "")
    return stem.strip("_ /")


def profile_qualifier_pairs(
    df: pl.DataFrame,
    value_columns: list[str],
    qualifier_columns: list[str],
) -> list[dict[str, Any]]:
    """Compare each value column's fill rate against its qualifier's.

    A field whose job is to say *in what unit* a number is recorded -- unit of
    measure, currency, language -- is only meaningful when it is populated
    wherever the value is. A well-filled value column beside a sparse
    qualifier is the canonical "mixed units, no unit recorded" shape, and it is
    the check that generalises furthest beyond units.

    Which value a qualifier belongs to is decided by name, in two steps:

    1. When the qualifier's name carries a stem (``quantity_uom_id``), it is
       paired **only** with the value column that stem names.
    2. A bare qualifier (``uom_id``, ``currency_id``, ``lang``) qualifies the
       record as a whole, so it is paired with every measurement column.

    Identifier columns are never treated as measurements: a key is a number
    without a unit, and pairing one with a unit field is a finding that is
    always wrong.

    Args:
        df: The frame to read.
        value_columns: Columns carrying the values.
        qualifier_columns: Columns that qualify them.

    Returns:
        list[dict[str, Any]]: One entry per pair, with ``value_column``,
        ``qualifier_column``, both fill rates and ``flags``. Pairs where the
        qualifier is at least as well populated as the value are omitted.
    """
    measurements = [c for c in value_columns if not is_identifier_column(c)]
    lowered = {c.lower(): c for c in measurements}

    findings: list[dict[str, Any]] = []
    for qualifier_column in qualifier_columns:
        stem = _qualifier_stem(qualifier_column)
        if stem:
            targets = [lowered[stem]] if stem in lowered else []
        else:
            targets = measurements
        qualifier_fill = fill_rate(df, qualifier_column)
        for value_column in targets:
            if value_column == qualifier_column:
                continue
            value_fill = fill_rate(df, value_column)
            if not value_fill["filled"]:
                continue
            if qualifier_fill["fill_rate"] >= value_fill["fill_rate"]:
                continue
            findings.append(
                {
                    "value_column": value_column,
                    "qualifier_column": qualifier_column,
                    "value_fill_rate": value_fill["fill_rate"],
                    "qualifier_fill_rate": qualifier_fill["fill_rate"],
                    "flags": ["unqualified-values"],
                }
            )
    return findings


def looks_like_qualifier(column: str) -> bool:
    """Return True when a column name reads as a qualifier of another field.

    Args:
        column: The column name.

    Returns:
        bool: True when the lowercased name contains a :data:`QUALIFIER_HINTS`
        fragment.
    """
    lowered = column.lower()
    return any(hint in lowered for hint in QUALIFIER_HINTS)


def profile_frame(
    df: pl.DataFrame,
    *,
    numeric: list[str] | None = None,
    dates: list[str] | None = None,
    enums: list[str] | None = None,
    locale: Locale = ANGLO,
    group_by: str | None = None,
    enum_max_distinct: int = ENUM_MAX_DISTINCT,
) -> dict[str, Any]:
    """Profile a whole frame, by column role.

    Column roles are supplied by the caller rather than sniffed: ``assess``
    knows each field's Odoo type, which beats guessing from values. When a
    role list is omitted it is inferred from the frame's dtypes as a fallback
    for callers that have no schema.

    Args:
        df: The frame to profile.
        numeric: Columns to treat as numeric; inferred from dtypes when None.
        dates: Columns to treat as dates; inferred when None.
        enums: Columns to test as enumerations; inferred when None.
        locale: Decimal/thousands convention for the numeric pass.
        group_by: Optional grouping column for the numeric pass.
        enum_max_distinct: Enumeration cardinality ceiling.

    Returns:
        dict[str, Any]: ``rows``, ``columns`` (the per-column reports),
        ``qualifiers`` (the cross-field findings) and ``flagged`` (names of
        columns carrying at least one flag).
    """
    if numeric is None or dates is None or enums is None:
        inferred_numeric, inferred_dates, inferred_enums = _infer_roles(df)
        numeric = inferred_numeric if numeric is None else numeric
        dates = inferred_dates if dates is None else dates
        enums = inferred_enums if enums is None else enums

    reports: list[dict[str, Any]] = []
    for column in numeric:
        if column in df.columns:
            reports.append(
                profile_numeric_column(df, column, locale=locale, group_by=group_by)
            )
    for column in dates:
        if column in df.columns:
            reports.append(profile_date_column(df, column))
    for column in enums:
        if column in df.columns:
            enum_report = profile_enum_column(
                df, column, max_distinct=enum_max_distinct
            )
            if enum_report is not None:
                reports.append(enum_report)

    qualifiers = profile_qualifier_pairs(
        df,
        [c for c in numeric if c in df.columns],
        [c for c in df.columns if looks_like_qualifier(c)],
    )

    return {
        "rows": df.height,
        "columns": reports,
        "qualifiers": qualifiers,
        "flagged": sorted(
            {str(r["column"]) for r in reports if r["flags"]}
            | {str(q["value_column"]) for q in qualifiers}
        ),
    }


def _infer_roles(df: pl.DataFrame) -> tuple[list[str], list[str], list[str]]:
    """Split a frame's columns into numeric, date and enumeration candidates.

    A fallback for callers with no schema. Numeric and temporal dtypes are
    taken at face value; every string column becomes an enumeration candidate,
    and :func:`profile_enum_column` discards the ones that are free text.

    Args:
        df: The frame to inspect.

    Returns:
        tuple[list[str], list[str], list[str]]: Numeric, date and enumeration
        column names.
    """
    numeric: list[str] = []
    dates: list[str] = []
    enums: list[str] = []
    for column, dtype in zip(df.columns, df.dtypes, strict=True):
        if dtype.is_numeric():
            numeric.append(column)
        elif dtype in (pl.Date, pl.Datetime):
            dates.append(column)
        elif dtype == pl.String:
            enums.append(column)
    return numeric, dates, enums
