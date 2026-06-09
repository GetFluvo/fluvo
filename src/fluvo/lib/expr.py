"""Polars expression-based mappers for high-performance data transformations.

This module provides Polars-native equivalents of the row-by-row mapper functions
in the `mapper` module. These functions return `pl.Expr` objects that are executed
as vectorized operations, providing significant performance improvements over
row-by-row Python execution.

Usage:
    from fluvo.lib import expr

    processor = Processor(
        mapping={
            "name": expr.val("source_name"),
            "full_name": expr.concat(" ", "first_name", "last_name"),
            "price": expr.num("price_str"),
            "is_active": expr.bool_val("status", true_values=["active", "yes"]),
        },
        dataframe=df,
    )

Performance Note:
    These expression-based functions are typically 10-100x faster than their
    `mapper` module equivalents because they leverage Polars' vectorized
    execution engine instead of row-by-row Python iteration.
"""

from typing import Any, Optional, Union

import polars as pl

__all__ = [
    "bool_val",
    "coalesce",
    "concat",
    "concat_all",
    "cond",
    "const",
    "date",
    "datetime",
    "m2m",
    "m2o",
    "map_val",
    "num",
    "val",
]


def val(field: str, default: Any = None) -> pl.Expr:
    """Returns a Polars expression that gets a value from a column.

    This is the Polars-native equivalent of `mapper.val()`.

    Args:
        field: The source column name.
        default: The default value to use if the column value is null.

    Returns:
        A Polars expression.
    """
    if default is not None:
        return pl.col(field).fill_null(default)
    return pl.col(field)


def const(value: Any) -> pl.Expr:
    """Returns a Polars expression that always provides a constant value.

    This is the Polars-native equivalent of `mapper.const()`.

    Args:
        value: The constant value to return.

    Returns:
        A Polars literal expression.
    """
    return pl.lit(value)


def concat(separator: str, *fields: str) -> pl.Expr:
    """Returns a Polars expression that concatenates multiple columns.

    This is the Polars-native equivalent of `mapper.concat()`.

    Args:
        separator: The string to place between each value.
        *fields: Column names to concatenate.

    Returns:
        A Polars expression that concatenates the columns.
    """
    cols = [pl.col(f).cast(pl.String).fill_null("") for f in fields]
    return pl.concat_str(cols, separator=separator)


def concat_all(separator: str, *fields: str) -> pl.Expr:
    """Returns a Polars expression that concatenates columns only if all have values.

    If any column is null or empty, returns an empty string.
    This is the Polars-native equivalent of `mapper.concat_mapper_all()`.

    Args:
        separator: The string to place between each value.
        *fields: Column names to concatenate.

    Returns:
        A Polars expression.
    """
    # Check if all fields have non-null, non-empty values
    conditions = [
        pl.col(f).is_not_null() & (pl.col(f).cast(pl.String) != "") for f in fields
    ]
    all_valid = conditions[0]
    for cond in conditions[1:]:
        all_valid = all_valid & cond

    cols = [pl.col(f).cast(pl.String) for f in fields]
    return (
        pl.when(all_valid)
        .then(pl.concat_str(cols, separator=separator))
        .otherwise(pl.lit(""))
    )


def cond(
    field: str,
    true_value: Union[str, pl.Expr],
    false_value: Union[str, pl.Expr],
) -> pl.Expr:
    """Returns a Polars expression that applies conditional logic.

    This is the Polars-native equivalent of `mapper.cond()`.

    Args:
        field: The source column to check for a truthy value.
        true_value: Value/expression to use if condition is true.
                   If string, treated as a column name.
        false_value: Value/expression to use if condition is false.
                    If string, treated as a column name.

    Returns:
        A Polars expression.
    """
    true_expr = pl.col(true_value) if isinstance(true_value, str) else true_value
    false_expr = pl.col(false_value) if isinstance(false_value, str) else false_value

    # Check for truthy: not null and not empty string and not 0/False
    condition = (
        pl.col(field).is_not_null()
        & (pl.col(field).cast(pl.String) != "")
        & (pl.col(field).cast(pl.String) != "0")
        & (pl.col(field).cast(pl.String).str.to_lowercase() != "false")
    )

    return pl.when(condition).then(true_expr).otherwise(false_expr)


def bool_val(
    field: str,
    true_values: Optional[list[str]] = None,
    false_values: Optional[list[str]] = None,
    default: bool = False,
) -> pl.Expr:
    """Returns a Polars expression that converts a field to boolean "1" or "0".

    This is the Polars-native equivalent of `mapper.bool_val()`.

    Args:
        field: The source column to check.
        true_values: Values that should be considered True.
        false_values: Values that should be considered False.
        default: Default boolean value if no match.

    Returns:
        A Polars expression returning "1" or "0".
    """
    col = pl.col(field).cast(pl.String)
    default_str = "1" if default else "0"

    if true_values and false_values:
        return (
            pl.when(col.is_in(true_values))
            .then(pl.lit("1"))
            .when(col.is_in(false_values))
            .then(pl.lit("0"))
            .otherwise(pl.lit(default_str))
        )
    elif true_values:
        return pl.when(col.is_in(true_values)).then(pl.lit("1")).otherwise(pl.lit("0"))
    elif false_values:
        return pl.when(col.is_in(false_values)).then(pl.lit("0")).otherwise(pl.lit("1"))
    else:
        # Use truthiness of the value
        return (
            pl.when(
                col.is_not_null()
                & (col != "")
                & (col != "0")
                & (col.str.to_lowercase() != "false")
            )
            .then(pl.lit("1"))
            .otherwise(pl.lit(default_str))
        )


def num(
    field: str,
    default: Optional[Union[int, float]] = None,
    decimal_separator: str = ",",
) -> pl.Expr:
    """Returns a Polars expression that converts a field to a number.

    Handles European-style numbers with comma as decimal separator.
    This is the Polars-native equivalent of `mapper.num()`.

    Args:
        field: The source column name.
        default: Default value if conversion fails.
        decimal_separator: The decimal separator in the source data.

    Returns:
        A Polars expression returning a float.
    """
    col = pl.col(field).cast(pl.String)

    # Replace comma with dot for decimal conversion if needed
    if decimal_separator == ",":
        col = col.str.replace(",", ".")

    result = col.cast(pl.Float64, strict=False)

    if default is not None:
        result = result.fill_null(default)

    return result


def map_val(
    field: str,
    mapping_dict: dict[Any, Any],
    default: Any = None,
) -> pl.Expr:
    """Returns a Polars expression that translates values using a dictionary.

    This is the Polars-native equivalent of `mapper.map_val()`.

    Args:
        field: The source column name.
        mapping_dict: Dictionary mapping source values to target values.
        default: Default value if key is not found. If None, keeps original value.

    Returns:
        A Polars expression.
    """
    if default is not None:
        return pl.col(field).replace_strict(mapping_dict, default=default)
    return pl.col(field).replace(mapping_dict)


def coalesce(*fields: str) -> pl.Expr:
    """Returns a Polars expression that returns the first non-null value.

    Args:
        *fields: Column names to check in order.

    Returns:
        A Polars expression returning the first non-null value.
    """
    return pl.coalesce([pl.col(f) for f in fields])


def m2o(prefix: str, field: str, default: str = "") -> pl.Expr:
    """Returns a Polars expression that creates a Many2one external ID.

    This is the Polars-native equivalent of `mapper.m2o()`.

    Args:
        prefix: The XML ID prefix (e.g., 'my_module').
        field: The source column containing the value for the ID.
        default: Value to return if source is empty.

    Returns:
        A Polars expression returning the formatted external ID.
    """
    col = pl.col(field).cast(pl.String)

    # Sanitize the value: replace spaces and special chars with underscores
    sanitized = (
        col.str.replace_all(r"[^a-zA-Z0-9_]", "_")
        .str.replace_all(r"_+", "_")
        .str.strip_chars("_")
    )

    result = pl.concat_str([pl.lit(prefix), pl.lit("."), sanitized])

    # Return default if original value is null or empty
    return pl.when(col.is_null() | (col == "")).then(pl.lit(default)).otherwise(result)


def m2m(
    prefix: str,
    field: str,
    separator: str = ",",
    default: str = "",
) -> pl.Expr:
    """Returns a Polars expression that creates Many2many external IDs.

    Splits the field value by separator and creates external IDs for each part.
    This is the Polars-native equivalent of `mapper.m2m()`.

    Args:
        prefix: The XML ID prefix.
        field: The source column containing comma-separated values.
        separator: The separator used in the source data.
        default: Value to return if source is empty.

    Returns:
        A Polars expression returning comma-separated external IDs.
    """
    col = pl.col(field).cast(pl.String)

    # Split, sanitize each part, add prefix, and join back
    result = (
        col.str.split(separator)
        .list.eval(
            pl.element()
            .str.strip_chars()
            .str.replace_all(r"[^a-zA-Z0-9_]", "_")
            .str.replace_all(r"_+", "_")
            .str.strip_chars("_")
        )
        .list.eval(pl.concat_str([pl.lit(prefix), pl.lit("."), pl.element()]))
        .list.join(",")
    )

    return pl.when(col.is_null() | (col == "")).then(pl.lit(default)).otherwise(result)


def date(field: str, format: str) -> pl.Expr:
    """Returns a Polars expression that parses a date with a custom format.

    This provides the same functionality as the Processor's date_formats parameter
    but as an expression that can be used in mappings.

    Args:
        field: The source column name.
        format: The strftime format string (e.g., "%d/%m/%Y").

    Returns:
        A Polars expression returning a Date.
    """
    return pl.col(field).str.to_date(format, strict=False)


def datetime(field: str, format: str) -> pl.Expr:
    """Returns a Polars expression that parses a datetime with a custom format.

    Args:
        field: The source column name.
        format: The strftime format string (e.g., "%d/%m/%Y %H:%M:%S").

    Returns:
        A Polars expression returning a Datetime.
    """
    return pl.col(field).str.to_datetime(format, strict=False)
