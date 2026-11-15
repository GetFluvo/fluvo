"""Detect and repair Excel-mangled reference columns (issue #332).

Part catalogues routinely pass through a spreadsheet before Fluvo ever sees
them, and the spreadsheet silently corrupts reference-like columns. Four
distinct, independent corruption patterns are observed on real data:

1. **Scientific notation** -- a long reference collapses to ``1,1301E+12``.
   The precision is *gone*; it can only be recovered from a sibling column
   that still holds the true value. When there is no such column we refuse.
2. **Trailing-zero loss** -- a fixed-width suffix loses its trailing zeros
   (``922473,007`` where the family suffix is 4 wide -> ``922473.0070``).
   Recovery is dataset-level inference: the canonical suffix width is read
   from sibling references in the same family, then short suffixes are
   re-padded. When the family width is not corroborated we refuse.
3. **Normalisation collisions** -- distinct source references normalise to
   the same canonical form (here the comma is a *thousands*/field separator,
   the opposite reading from pattern 2). Detected and grouped, never fixed.
4. **Case-only duplicates** -- ``t27056298`` and ``T27056298`` are distinct
   records that differ only in case. Detected and grouped, never fixed.

Design contract (non-negotiable, per #332):

- **Pure Polars.** Every function is ``DataFrame in -> DataFrame out``. No
  Odoo connection, no file I/O -- offline-testable and reusable by #298/#299.
- **Locale is a parameter, never a guess.** Comma-as-decimal vs
  comma-as-thousands silently inverts patterns 2 and 3; getting it wrong
  corrupts quietly, which is the one failure mode this whole module exists to
  prevent. Callers pass a :class:`Locale`; :data:`DUTCH` and :data:`ANGLO`
  are provided.
- **Detect and fix are separate.** A detector only reports; its output is
  inspectable before anything mutates. A fixer consumes that output and, when
  the repair is not unambiguous, *refuses* (raises :class:`MangleRefusalError`)
  rather than guessing.

Each detector emits a stable, namespaced ``bucket_id`` (``tzl:``, ``sci:``,
``ncol:``, ``case:``) so #299 can consume the groups deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl

from ..logging_config import log


class MangleRefusalError(Exception):
    """A fixer refused to mutate data because the repair was not unambiguous.

    Raised instead of guessing: an unrecoverable scientific-notation value, or
    a trailing-zero family whose canonical width is not corroborated. Refusing
    is the designed behaviour, not an error in the caller's input.
    """


@dataclass(frozen=True)
class Locale:
    """How a given dataset uses the comma and the dot in numeric references.

    This is the single parameter that disambiguates patterns 2 and 3. It is
    never inferred from the data, because an inverted guess corrupts silently.
    """

    decimal_sep: str
    thousands_sep: str | None = None


#: Dutch / continental convention: comma is the decimal separator, dot groups
#: thousands. This is the locale of the observed part-catalogue migration.
DUTCH = Locale(decimal_sep=",", thousands_sep=".")

#: Anglo convention: dot is the decimal separator, comma groups thousands.
ANGLO = Locale(decimal_sep=".", thousands_sep=",")

_ROW = "row_index"


def _as_str(column: str) -> pl.Expr:
    """Return the target column cast to String, so regex/str ops are safe.

    Args:
        column: Name of the reference column to operate on.

    Returns:
        pl.Expr: A Polars expression yielding the column as ``pl.String``.
    """
    return pl.col(column).cast(pl.String)


# --------------------------------------------------------------------------- #
# Pattern 1: scientific notation
# --------------------------------------------------------------------------- #
def detect_scientific_notation(
    df: pl.DataFrame,
    column: str,
    recovery_column: str | None = None,
    locale: Locale = DUTCH,
) -> pl.DataFrame:
    """Detect reference values mangled into scientific notation.

    A value such as ``1,1301E+12`` has lost its precision and cannot be
    rebuilt from itself. If a ``recovery_column`` is supplied, each affected
    row carries the sibling value that could restore the truth, and a
    ``recoverable`` flag; rows with no usable sibling are reported but flagged
    unrecoverable so :func:`fix_scientific_notation` can refuse.

    Args:
        df: The source frame.
        column: The reference column to inspect.
        recovery_column: Optional sibling column holding the true value.
        locale: Locale whose decimal separator the mantissa may use.

    Returns:
        pl.DataFrame: One row per affected input row with columns
        ``row_index``, ``value``, ``recovery_value``, ``recoverable`` and
        ``bucket_id``. Empty when nothing matches.
    """
    sep = re.escape(locale.decimal_sep)
    pattern = rf"^\s*\d+(?:{sep}\d+)?[eE][+-]?\d+\s*$"

    indexed = df.with_row_index(_ROW)
    affected = indexed.filter(_as_str(column).str.contains(pattern))

    if recovery_column is not None:
        recovery = pl.col(recovery_column).cast(pl.String).str.strip_chars()
        recovery_value = (
            pl.when(recovery.is_null() | (recovery.str.len_chars() == 0))
            .then(None)
            .otherwise(recovery)
        )
    else:
        recovery_value = pl.lit(None, dtype=pl.String)

    out = affected.select(
        pl.col(_ROW),
        _as_str(column).alias("value"),
        recovery_value.alias("recovery_value"),
    ).with_columns(
        recoverable=pl.col("recovery_value").is_not_null(),
        bucket_id=pl.lit("sci:")
        + pl.coalesce([pl.col("recovery_value"), pl.col("value")]),
    )
    return out.sort(_ROW)


def fix_scientific_notation(
    df: pl.DataFrame,
    detection: pl.DataFrame,
    column: str,
) -> pl.DataFrame:
    """Replace mangled scientific-notation values with their recovered truth.

    Consumes the output of :func:`detect_scientific_notation`. Precision was
    lost in the mangling, so the only honest repair is to copy a sibling
    column's value; there is nothing to compute from the mangled value itself.

    Args:
        df: The source frame to repair.
        detection: The detector's output, inspected before applying.
        column: The reference column to overwrite.

    Returns:
        pl.DataFrame: A new frame with the same schema as ``df`` and the
        recoverable rows replaced. If ``detection`` is empty, ``df`` is
        returned unchanged.

    Raises:
        MangleRefusalError: If any detected row is not recoverable (no sibling
            value). Refusing beats writing a guessed reference.
    """
    if detection.is_empty():
        return df

    unrecoverable = detection.filter(~pl.col("recoverable"))
    if not unrecoverable.is_empty():
        values = unrecoverable.get_column("value").to_list()
        raise MangleRefusalError(
            f"Refusing to fix {len(values)} scientific-notation value(s) with no "
            f"recovery column of truth: {values[:5]}"
            + (" ..." if len(values) > 5 else "")
        )

    repaired = detection.select(pl.col(_ROW), pl.col("recovery_value"))
    out = (
        df.with_row_index(_ROW)
        .join(repaired, on=_ROW, how="left")
        .with_columns(
            pl.coalesce([pl.col("recovery_value"), _as_str(column)]).alias(column)
        )
        .drop(_ROW, "recovery_value")
    )
    log.info(
        "Recovered %d scientific-notation value(s) from sibling column.", len(repaired)
    )
    return out


# --------------------------------------------------------------------------- #
# Pattern 2: trailing-zero loss
# --------------------------------------------------------------------------- #
def detect_trailing_zero_loss(
    df: pl.DataFrame,
    column: str,
    locale: Locale = DUTCH,
    min_corroboration: int = 2,
) -> pl.DataFrame:
    """Detect references that lost fixed-width trailing zeros, by family.

    The reference is read as ``<integer><decimal_sep><fraction>`` (exactly one
    separator). References whose integer part has the same number of digits
    form a *family* (a part-number schema is fixed-width per prefix length), and
    the family's canonical suffix width is the **most common** fraction width in
    it. A row whose fraction is shorter than the canonical width lost that many
    trailing zeros.

    The mode -- not the maximum -- is used, so a handful of genuinely longer
    codes in a family do not force everyone to be re-padded; grouping by prefix
    length (rather than the exact prefix) keeps families large enough to carry
    statistical weight. When the family's width is contested -- two widths tie
    for most common, or the modal width is attested by fewer than
    ``min_corroboration`` members -- the row is flagged ``ambiguous`` so
    :func:`fix_trailing_zero_loss` can refuse rather than pick a width.

    Args:
        df: The source frame.
        column: The reference column to inspect.
        locale: Locale whose ``decimal_sep`` splits integer from fraction.
        min_corroboration: How many members must attest the modal width for it
            to count as corroborated (default 2: a lone wide sibling is not
            enough to re-pad against).

    Returns:
        pl.DataFrame: One row per affected input row with ``row_index``,
        ``value``, ``normalized``, ``suggested``, ``family_width``,
        ``missing_zeros``, ``ambiguous`` and ``bucket_id``. Empty when nothing
        matches.
    """
    sep = locale.decimal_sep
    sep_re = re.escape(sep)
    decimal_pat = rf"^\d+{sep_re}\d+$"

    parsed = (
        df.with_row_index(_ROW)
        .with_columns(_value=_as_str(column))
        .filter(pl.col("_value").str.contains(decimal_pat))
        .with_columns(
            integer_part=pl.col("_value").str.split(sep).list.first(),
            fraction=pl.col("_value").str.split(sep).list.last(),
        )
        .with_columns(
            prefix_len=pl.col("integer_part").str.len_chars(),
            width=pl.col("fraction").str.len_chars(),
        )
    )
    if parsed.is_empty():
        return _empty_trailing_zero()

    # Canonical width per prefix-length family = the modal fraction width.
    # Count members at each width, then pick the width with the highest count
    # (ties broken towards the wider suffix, but flagged ambiguous below).
    counts = parsed.group_by("prefix_len", "width").agg(support=pl.len())
    counts = counts.with_columns(
        top=pl.col("support").max().over("prefix_len"),
    ).with_columns(
        n_tied=(pl.col("support") == pl.col("top")).sum().over("prefix_len"),
    )
    family = (
        counts.filter(pl.col("support") == pl.col("top"))
        .group_by("prefix_len")
        .agg(
            family_width=pl.col("width").max(),
            modal_support=pl.col("top").first(),
            tie=pl.col("n_tied").first() > 1,
        )
    )

    affected = (
        parsed.join(family, on="prefix_len", how="left")
        .filter(pl.col("width") < pl.col("family_width"))
        .with_columns(
            missing_zeros=pl.col("family_width") - pl.col("width"),
            normalized=pl.col("integer_part") + pl.lit(".") + pl.col("fraction"),
        )
        .with_columns(
            suggested=pl.col("normalized")
            + pl.lit("0").repeat_by(pl.col("missing_zeros")).list.join(""),
            ambiguous=pl.col("tie") | (pl.col("modal_support") < min_corroboration),
            bucket_id=pl.lit("tzl:len") + pl.col("prefix_len").cast(pl.String),
        )
        .select(
            pl.col(_ROW),
            pl.col("_value").alias("value"),
            "normalized",
            "suggested",
            "family_width",
            "missing_zeros",
            "ambiguous",
            "bucket_id",
        )
    )
    return affected.sort(_ROW)


def _empty_trailing_zero() -> pl.DataFrame:
    """Return the empty, correctly-typed trailing-zero detection frame.

    Returns:
        pl.DataFrame: An empty frame with the trailing-zero detection schema,
        so callers can rely on the columns existing even when nothing is
        detected.
    """
    return pl.DataFrame(
        schema={
            _ROW: pl.UInt32,
            "value": pl.String,
            "normalized": pl.String,
            "suggested": pl.String,
            "family_width": pl.UInt32,
            "missing_zeros": pl.UInt32,
            "ambiguous": pl.Boolean,
            "bucket_id": pl.String,
        }
    )


def fix_trailing_zero_loss(
    df: pl.DataFrame,
    detection: pl.DataFrame,
    column: str,
    on_ambiguous: str = "refuse",
) -> pl.DataFrame:
    """Re-pad references that lost trailing zeros, for corroborated families.

    Consumes the output of :func:`detect_trailing_zero_loss`. Only rows whose
    family width is corroborated (``ambiguous == False``) are ever written.

    Args:
        df: The source frame to repair.
        detection: The detector's output, inspected before applying.
        column: The reference column to overwrite.
        on_ambiguous: ``"refuse"`` (default) raises if any ambiguous row is
            present; ``"skip"`` leaves ambiguous rows untouched and re-pads
            only the corroborated ones.

    Returns:
        pl.DataFrame: A new frame with the same schema as ``df`` and the
        corroborated rows re-padded. If ``detection`` is empty, ``df`` is
        returned unchanged.

    Raises:
        ValueError: If ``on_ambiguous`` is neither ``"refuse"`` nor ``"skip"``.
        MangleRefusalError: If ``on_ambiguous == "refuse"`` and the detection holds
            any ambiguous family. An ambiguous width must not be guessed.
    """
    if on_ambiguous not in ("refuse", "skip"):
        raise ValueError(
            f"on_ambiguous must be 'refuse' or 'skip', got {on_ambiguous!r}"
        )
    if detection.is_empty():
        return df

    ambiguous = detection.filter(pl.col("ambiguous"))
    if not ambiguous.is_empty():
        if on_ambiguous == "refuse":
            families = ambiguous.get_column("bucket_id").unique().to_list()
            raise MangleRefusalError(
                f"Refusing to re-pad {ambiguous.height} row(s) in "
                f"{len(families)} family/families with an uncorroborated suffix "
                f"width: {families[:5]}"
                + (" ..." if len(families) > 5 else "")
                + ". Inspect the detection, lower min_corroboration, or pass "
                "on_ambiguous='skip'."
            )
        log.warning(
            "Skipping %d row(s) with an ambiguous family width; re-padding only "
            "corroborated families.",
            ambiguous.height,
        )

    confident = detection.filter(~pl.col("ambiguous")).select(
        pl.col(_ROW), pl.col("suggested")
    )
    out = (
        df.with_row_index(_ROW)
        .join(confident, on=_ROW, how="left")
        .with_columns(pl.coalesce([pl.col("suggested"), _as_str(column)]).alias(column))
        .drop(_ROW, "suggested")
    )
    log.info("Re-padded %d trailing-zero reference(s).", confident.height)
    return out


# --------------------------------------------------------------------------- #
# Pattern 3: normalisation collisions (detect/group only -- no fixer)
# --------------------------------------------------------------------------- #
def _normalize_expr(column: str, locale: Locale) -> pl.Expr:
    """Build the canonical-form expression used for collision grouping.

    Both the decimal and thousands separators are folded to ``.`` so that a
    source value using the comma as a field separator (``000,630,2226``) and a
    native dotted value (``000.630.2226``) land on the same key.

    Args:
        column: The reference column to normalise.
        locale: Locale providing the separators to fold.

    Returns:
        pl.Expr: A Polars expression yielding the normalised string key.
    """
    expr = _as_str(column)
    if locale.thousands_sep and locale.thousands_sep != ".":
        expr = expr.str.replace_all(locale.thousands_sep, ".", literal=True)
    if locale.decimal_sep != ".":
        expr = expr.str.replace_all(locale.decimal_sep, ".", literal=True)
    return expr


def detect_normalization_collisions(
    df: pl.DataFrame,
    column: str,
    locale: Locale = DUTCH,
) -> pl.DataFrame:
    """Group distinct source references that normalise to the same key.

    After folding the locale separators to ``.``, two *distinct* raw values
    may share one normalised form -- a collision. This is a human decision
    (which record wins), not a repair, so there is no fixer; the detector only
    groups.

    Args:
        df: The source frame.
        column: The reference column to inspect.
        locale: Locale whose separators define the normalised key.

    Returns:
        pl.DataFrame: One row per affected input row with ``row_index``, ``value``,
        ``normalized`` and ``bucket_id`` (``ncol:<normalized>``). Empty when no
        collisions exist.
    """
    indexed = df.with_row_index(_ROW).with_columns(
        _value=_as_str(column),
        _normalized=_normalize_expr(column, locale),
    )
    collided = indexed.with_columns(
        _distinct=pl.col("_value").n_unique().over("_normalized"),
    ).filter(pl.col("_distinct") > 1)

    out = collided.select(
        pl.col(_ROW),
        pl.col("_value").alias("value"),
        pl.col("_normalized").alias("normalized"),
        bucket_id=pl.lit("ncol:") + pl.col("_normalized"),
    )
    return out.sort(_ROW)


# --------------------------------------------------------------------------- #
# Pattern 4: case-only duplicates (detect/group only -- no fixer)
# --------------------------------------------------------------------------- #
def detect_case_duplicates(df: pl.DataFrame, column: str) -> pl.DataFrame:
    """Group references that are identical except for letter case.

    ``t27056298`` and ``T27056298`` are distinct records that collide
    case-insensitively -- a merge decision, not a repair, so there is no
    fixer.

    Args:
        df: The source frame.
        column: The reference column to inspect.

    Returns:
        pl.DataFrame: One row per affected input row with ``row_index``, ``value``,
        ``normalized`` (the lowercased key) and ``bucket_id``
        (``case:<lower>``). Empty when no case collisions exist.
    """
    indexed = df.with_row_index(_ROW).with_columns(
        _value=_as_str(column),
        _lower=_as_str(column).str.to_lowercase(),
    )
    collided = indexed.with_columns(
        _distinct=pl.col("_value").n_unique().over("_lower"),
    ).filter(pl.col("_distinct") > 1)

    out = collided.select(
        pl.col(_ROW),
        pl.col("_value").alias("value"),
        pl.col("_lower").alias("normalized"),
        bucket_id=pl.lit("case:") + pl.col("_lower"),
    )
    return out.sort(_ROW)
