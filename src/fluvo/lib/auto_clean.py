"""Opt-in, type-aware pre-load coercion (vectorized with Polars).

Enabled via ``--auto-clean`` / the ``auto_clean`` flag (off by default, so default
behavior is unchanged). It applies only **locale-independent, safe** coercions so it
cannot silently corrupt values:

- strip leading/trailing whitespace on every column;
- normalize obvious null tokens (``NULL``/``None``/``nan``/``N/A`` ...) to empty;
- canonicalize boolean-typed fields (``yes/y/t/x/on`` -> ``True``; ``no/n/f/off`` ->
  ``False``).

Locale-specific reformatting (European decimals, date layouts) is **not** applied
automatically because the correct interpretation depends on the source locale; pass
``decimal_separator`` to opt into numeric reformatting, or do it explicitly in your
transform script with the ``clean`` mappers. Uncoercible values become empty and, if
that breaks a required field, the load-time bisect isolates that row to the fail file
rather than aborting the batch.
"""

from __future__ import annotations

import polars as pl

from ..logging_config import log
from . import clean_expr

_NULLISH = ["null", "none", "nan", "n/a", "na", "#n/a"]
_TRUE = ["1", "true", "t", "yes", "y", "x", "on"]
_FALSE = ["0", "false", "f", "no", "n", "off"]


def auto_clean_dataframe(
    df: pl.DataFrame,
    field_types: dict[str, str],
    *,
    decimal_separator: str | None = None,
    thousands_separator: str = ".",
) -> pl.DataFrame:
    """Apply safe, type-aware coercions to ``df`` before load().

    Args:
        df: The source data (all columns as strings).
        field_types: Map of base field name -> Odoo type (from ``fields_get``).
        decimal_separator: If given, reformat float/monetary fields from this
            decimal separator (e.g. ``","``) to ``.``. Off by default.
        thousands_separator: Thousands separator stripped when reformatting numbers.

    Returns:
        A new DataFrame with the coercions applied.
    """
    if df.is_empty():
        return df

    cols = df.columns
    # 1. Whitespace strip (safe) for every column.
    df = df.with_columns(
        [pl.col(c).cast(pl.String).str.strip_chars().alias(c) for c in cols]
    )
    # 2. Null-token normalization -> empty string.
    df = df.with_columns(
        [
            pl.when(pl.col(c).str.to_lowercase().is_in(_NULLISH))
            .then(pl.lit(""))
            .otherwise(pl.col(c))
            .alias(c)
            for c in cols
        ]
    )
    # 3. Type-aware coercions (boolean always; numeric only when opted in).
    type_exprs = []
    for c in cols:
        base = c.split("/")[0]
        ftype = field_types.get(base)
        if ftype == "boolean":
            low = pl.col(c).str.to_lowercase()
            type_exprs.append(
                pl.when(low.is_in(_TRUE))
                .then(pl.lit("True"))
                .when(low.is_in(_FALSE))
                .then(pl.lit("False"))
                .when(pl.col(c) == "")
                .then(pl.lit(""))
                .otherwise(pl.col(c))
                .alias(c)
            )
        elif ftype in ("float", "monetary") and decimal_separator:
            type_exprs.append(
                clean_expr.numeric(c, decimal_separator, thousands_separator).alias(c)
            )
    if type_exprs:
        df = df.with_columns(type_exprs)

    log.debug(f"auto-clean applied to {len(cols)} columns.")
    return df
