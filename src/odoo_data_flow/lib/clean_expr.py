"""Polars expression-based data cleaners for high-performance transformations.

This module provides Polars-native data cleaning functions that return `pl.Expr`
objects for vectorized operations. These are typically 10-100x faster than
row-by-row Python execution.

Usage:
    from odoo_data_flow.lib import clean_expr

    mapping = {
        "phone": clean_expr.phone("raw_phone"),
        "email": clean_expr.email("raw_email"),
        "website": clean_expr.url("raw_website"),
        "vat": clean_expr.vat("raw_vat"),
    }

For stateful operations (e.g., deriving website from email domain), use the
row-by-row `clean` module instead.
"""

from __future__ import annotations

from typing import Optional

import polars as pl

__all__ = [
    # String cleaners
    "strip",
    "normalize_space",
    "lower",
    "upper",
    "title",
    "capitalize",
    "remove",
    "keep",
    "replace",
    "regex_sub",
    "truncate",
    "default",
    # Phone cleaners
    "phone",
    "phone_digits",
    "phone_normalize",
    # Email cleaners
    "email",
    "email_domain",
    # URL cleaners
    "url",
    "url_https",
    "url_fix_www",
    "url_ensure_scheme",
    # VAT cleaners
    "vat",
    "vat_or_exempt",
    # Zip cleaners
    "zip_code",
    "zip_strip_prefix",
    # Name cleaners
    "name_strip_title",
    "name_strip_suffix",
    "name_split_first",
    "name_split_last",
    "name_filter_common",
    "name_clean",
    # Numeric cleaners
    "digits",
    "numeric",
    "integer",
    # Constants (extensible)
    "COMMON_EMAIL_PROVIDERS",
    "COMMON_FILTER_NAMES",
    "TITLES",
    "SUFFIXES",
    "VAT_EXEMPT_VALUES",
    "PHONE_COUNTRY_RULES",
]

# =============================================================================
# EXTENSIBLE CONSTANTS
# =============================================================================

COMMON_EMAIL_PROVIDERS: set[str] = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "icloud.com",
    "mail.com",
    "protonmail.com",
    "gmx.com",
    "gmx.net",
    "web.de",
    "t-online.de",
    "aol.com",
    "msn.com",
    "ymail.com",
    "googlemail.com",
}

COMMON_FILTER_NAMES: set[str] = {
    "test",
    "test user",
    "admin",
    "administrator",
    "info",
    "contact",
    "sales",
    "support",
    "webmaster",
    "noreply",
    "no-reply",
    "postmaster",
    "root",
    "user",
    "demo",
    "example",
}

TITLES: set[str] = {
    "mr",
    "mr.",
    "mrs",
    "mrs.",
    "ms",
    "ms.",
    "dr",
    "dr.",
    "prof",
    "prof.",
    "ir",
    "ir.",
    "ing",
    "ing.",
    "drs",
    "drs.",
    "mw",
    "mw.",
    "dhr",
    "dhr.",
    "mevr",
    "mevr.",
}

SUFFIXES: set[str] = {
    "jr",
    "jr.",
    "sr",
    "sr.",
    "ii",
    "iii",
    "iv",
    "phd",
    "ph.d.",
    "md",
    "m.d.",
    "esq",
    "esq.",
}

VAT_EXEMPT_VALUES: set[str] = {
    "no vat",
    "vat exempt",
    "exempt",
    "n/a",
    "church",
    "non-profit",
    "nonprofit",
    "stichting",
    "vereniging",
    "kerk",
    "geen btw",
    "btw vrijgesteld",
}

PHONE_COUNTRY_RULES: dict[str, dict[str, str]] = {
    "NL": {"country_code": "31", "mobile_prefix": "6", "national_prefix": "0"},
    "BE": {"country_code": "32", "mobile_prefix": "4", "national_prefix": "0"},
    "DE": {"country_code": "49", "mobile_prefix": "1", "national_prefix": "0"},
    "FR": {"country_code": "33", "mobile_prefix": "6", "national_prefix": "0"},
    "UK": {"country_code": "44", "mobile_prefix": "7", "national_prefix": "0"},
    "ES": {"country_code": "34", "mobile_prefix": "6", "national_prefix": ""},
    "IT": {"country_code": "39", "mobile_prefix": "3", "national_prefix": ""},
    "AT": {"country_code": "43", "mobile_prefix": "6", "national_prefix": "0"},
    "CH": {"country_code": "41", "mobile_prefix": "7", "national_prefix": "0"},
    "LU": {"country_code": "352", "mobile_prefix": "6", "national_prefix": ""},
}


# =============================================================================
# STRING CLEANERS
# =============================================================================


def strip(field: str) -> pl.Expr:
    """Strip leading and trailing whitespace.

    Uses Polars native string method - no regex.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.strip_chars()


def normalize_space(field: str) -> pl.Expr:
    """Collapse multiple whitespace characters to single space.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.strip_chars().str.replace_all(r"\s+", " ")


def lower(field: str) -> pl.Expr:
    """Convert to lowercase.

    Uses Polars native string method - no regex.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.to_lowercase()


def upper(field: str) -> pl.Expr:
    """Convert to uppercase.

    Uses Polars native string method - no regex.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.to_uppercase()


def title(field: str) -> pl.Expr:
    """Convert to title case.

    Uses Polars native string method - no regex.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.to_titlecase()


def capitalize(field: str) -> pl.Expr:
    """Capitalize first letter only.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String)
    first = col.str.slice(0, 1).str.to_uppercase()
    rest = col.str.slice(1).str.to_lowercase()
    return pl.concat_str([first, rest])


def remove(field: str, chars: str) -> pl.Expr:
    """Remove specific characters from string.

    Args:
        field: Source column name.
        chars: Characters to remove (as string, e.g., ".-:").

    Returns:
        Polars expression.
    """
    # Escape special regex chars and create character class
    escaped = "".join(f"\\{c}" if c in r"\.^$*+?{}[]|()" else c for c in chars)
    pattern = f"[{escaped}]"
    return pl.col(field).cast(pl.String).str.replace_all(pattern, "")


def keep(field: str, pattern: str) -> pl.Expr:
    """Keep only characters matching pattern.

    Args:
        field: Source column name.
        pattern: Regex character class (e.g., "0-9A-Za-z").

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.replace_all(f"[^{pattern}]", "")


def replace(field: str, old: str, new: str, literal: bool = True) -> pl.Expr:
    """Replace substring.

    Args:
        field: Source column name.
        old: String to replace.
        new: Replacement string.
        literal: If True, treat `old` as literal string (default).

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String)
    if literal:
        return col.str.replace_all(old, new, literal=True)
    return col.str.replace_all(old, new)


def regex_sub(field: str, pattern: str, replacement: str) -> pl.Expr:
    """Apply regex substitution.

    Args:
        field: Source column name.
        pattern: Regex pattern.
        replacement: Replacement string (can use $1, $2 for groups).

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.replace_all(pattern, replacement)


def truncate(field: str, max_length: int) -> pl.Expr:
    """Limit string to maximum length.

    Args:
        field: Source column name.
        max_length: Maximum number of characters.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.slice(0, max_length)


def default(field: str, default_value: str) -> pl.Expr:
    """Provide default value if null or empty.

    Args:
        field: Source column name.
        default_value: Value to use if field is null or empty.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String)
    return (
        pl.when(col.is_null() | (col.str.strip_chars() == ""))
        .then(pl.lit(default_value))
        .otherwise(col)
    )


# =============================================================================
# PHONE CLEANERS
# =============================================================================


def phone(field: str) -> pl.Expr:
    """Clean phone number, keeping digits and leading +.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String).str.strip_chars()
    has_plus = col.str.starts_with("+")
    digits = col.str.replace_all(r"[^\d]", "")

    return (
        pl.when(col.is_null() | (col == ""))
        .then(pl.lit(None))
        .when(has_plus)
        .then(pl.concat_str([pl.lit("+"), digits]))
        .otherwise(digits)
    )


def phone_digits(field: str) -> pl.Expr:
    """Extract only digits from phone number.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String).str.strip_chars()
    digits = col.str.replace_all(r"[^\d]", "")
    return (
        pl.when(col.is_null() | (col == ""))
        .then(pl.lit(None))
        .otherwise(digits)
    )


def phone_normalize(
    field: str,
    country: str,
    rules: Optional[dict[str, dict[str, str]]] = None,
) -> pl.Expr:
    """Normalize phone number for specific country.

    Converts national format to international format.
    E.g., for NL: "0612345678" -> "+31612345678", "06 12 34 56 78" -> "+31612345678"

    Args:
        field: Source column name.
        country: Country code (e.g., "NL", "BE", "DE").
        rules: Optional custom rules dict. Uses PHONE_COUNTRY_RULES if not provided.

    Returns:
        Polars expression.
    """
    rules_dict = rules or PHONE_COUNTRY_RULES
    if country not in rules_dict:
        # Fallback to basic phone cleaning
        return phone(field)

    rule = rules_dict[country]
    country_code = rule["country_code"]
    national_prefix = rule["national_prefix"]

    col = pl.col(field).cast(pl.String).str.strip_chars()
    digits = col.str.replace_all(r"[^\d+]", "")

    # Already international format
    has_plus = digits.str.starts_with("+")

    # Starts with national prefix (e.g., "0" for NL)
    if national_prefix:
        starts_national = digits.str.starts_with(national_prefix)
        national_digits = digits.str.slice(len(national_prefix))
    else:
        starts_national = pl.lit(False)
        national_digits = digits

    return (
        pl.when(col.is_null() | (col == ""))
        .then(pl.lit(None))
        .when(has_plus)
        .then(digits)  # Already international
        .when(starts_national)
        .then(pl.concat_str([pl.lit(f"+{country_code}"), national_digits]))
        .otherwise(pl.concat_str([pl.lit(f"+{country_code}"), digits]))
    )


# =============================================================================
# EMAIL CLEANERS
# =============================================================================


def email(field: str) -> pl.Expr:
    """Clean email: strip, lowercase, remove trailing noise like "(Name)".

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String)
    return (
        col.str.strip_chars()
        .str.replace(r"\s*\([^)]*\)\s*$", "")  # Remove (Name) suffix
        .str.strip_chars()
        .str.to_lowercase()
    )


def email_domain(field: str) -> pl.Expr:
    """Extract domain from email address.

    Args:
        field: Source column name.

    Returns:
        Polars expression returning the domain part.
    """
    col = pl.col(field).cast(pl.String).str.to_lowercase()
    # Use extract with regex to get domain after @
    return col.str.extract(r"@(.+)$", 1)


# =============================================================================
# URL CLEANERS
# =============================================================================


def url(field: str) -> pl.Expr:
    """Clean URL: strip, fix www, ensure https, convert http to https.

    This is an all-in-one cleaner that handles common URL issues in a single pass.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String).str.strip_chars()

    # Fix wwwexample.com → www.example.com (missing dot after www)
    # First check if it starts with www followed by non-dot
    starts_with_www_no_dot = col.str.contains(r"^www[^.]")
    starts_with_scheme_www_no_dot = col.str.contains(r"^https?://www[^.]")

    # Insert dot after www
    fixed = (
        pl.when(starts_with_scheme_www_no_dot)
        .then(col.str.replace(r"^(https?://)www", "${1}www."))
        .when(starts_with_www_no_dot)
        .then(col.str.replace(r"^www", "www."))
        .otherwise(col)
    )

    # Check if already has scheme
    has_scheme = fixed.str.contains(r"^https?://")

    # Add https:// if no scheme
    with_scheme = (
        pl.when(has_scheme).then(fixed).otherwise(pl.concat_str([pl.lit("https://"), fixed]))
    )

    # Convert http:// to https://
    result = with_scheme.str.replace("^http://", "https://")

    return pl.when(col.is_null() | (col == "")).then(pl.lit(None)).otherwise(result)


def url_https(field: str) -> pl.Expr:
    """Convert http:// to https://.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.replace("^http://", "https://")


def url_fix_www(field: str) -> pl.Expr:
    """Fix missing dot after www (wwwexample.com → www.example.com).

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String)

    # Check for patterns
    starts_with_www_no_dot = col.str.contains(r"^www[^.]")
    starts_with_scheme_www_no_dot = col.str.contains(r"^https?://www[^.]")

    return (
        pl.when(starts_with_scheme_www_no_dot)
        .then(col.str.replace(r"^(https?://)www", "${1}www."))
        .when(starts_with_www_no_dot)
        .then(col.str.replace(r"^www", "www."))
        .otherwise(col)
    )


def url_ensure_scheme(field: str, scheme: str = "https://") -> pl.Expr:
    """Add scheme if missing.

    Args:
        field: Source column name.
        scheme: Scheme to add (default: "https://").

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String).str.strip_chars()
    has_scheme = col.str.contains(r"^https?://")
    return (
        pl.when(col.is_null() | (col == ""))
        .then(pl.lit(None))
        .when(has_scheme)
        .then(col)
        .otherwise(pl.concat_str([pl.lit(scheme), col]))
    )


# =============================================================================
# VAT CLEANERS
# =============================================================================


def vat(field: str) -> pl.Expr:
    """Clean VAT number: keep only letters, digits, and hyphen, uppercase.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String)
    return (
        pl.when(col.is_null() | (col.str.strip_chars() == ""))
        .then(pl.lit(None))
        .otherwise(col.str.strip_chars().str.replace_all(r"[^A-Za-z0-9-]", "").str.to_uppercase())
    )


def vat_or_exempt(
    field: str,
    exempt_values: Optional[set[str]] = None,
    marker: str = "/",
    exempt_output: str = "vat exempt",
) -> pl.Expr:
    """Clean VAT or mark as exempt.

    If the value matches an exempt pattern, returns marker + exempt_output.
    Otherwise, cleans the VAT number normally.

    Args:
        field: Source column name.
        exempt_values: Values that indicate VAT exemption.
        marker: Prefix for exempt output (default: "/").
        exempt_output: Text after marker for exempt (default: "vat exempt").

    Returns:
        Polars expression.
    """
    exempt_set = exempt_values or VAT_EXEMPT_VALUES
    exempt_list = list(exempt_set)

    col = pl.col(field).cast(pl.String)
    lower_col = col.str.strip_chars().str.to_lowercase()

    is_exempt = lower_col.is_in(exempt_list)
    cleaned_vat = col.str.strip_chars().str.replace_all(r"[^A-Za-z0-9-]", "").str.to_uppercase()

    return (
        pl.when(col.is_null() | (col.str.strip_chars() == ""))
        .then(pl.lit(None))
        .when(is_exempt)
        .then(pl.lit(f"{marker}{exempt_output}"))
        .otherwise(cleaned_vat)
    )


# =============================================================================
# ZIP CODE CLEANERS
# =============================================================================


def zip_code(field: str) -> pl.Expr:
    """Clean zip code: strip and remove spaces.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.strip_chars().str.replace_all(r"\s+", "")


def zip_strip_prefix(field: str) -> pl.Expr:
    """Remove country prefix from zip code (e.g., "NL-1234AB" → "1234AB").

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String).str.strip_chars()
    # Remove patterns like "NL-", "BE-", "DE-" at start
    return col.str.replace(r"^[A-Z]{2,3}[-\s]?", "")


# =============================================================================
# NAME CLEANERS
# =============================================================================


def name_strip_title(field: str, titles: Optional[set[str]] = None) -> pl.Expr:
    """Remove common titles from name.

    Args:
        field: Source column name.
        titles: Set of titles to remove. Uses TITLES if not provided.

    Returns:
        Polars expression.
    """
    titles_set = titles or TITLES
    # Build pattern: ^(mr|mrs|ms|dr|...)\s+
    pattern = "^(" + "|".join(titles_set) + r")\s+"
    return (
        pl.col(field)
        .cast(pl.String)
        .str.strip_chars()
        .str.replace(f"(?i){pattern}", "")
        .str.strip_chars()
    )


def name_strip_suffix(field: str, suffixes: Optional[set[str]] = None) -> pl.Expr:
    """Remove common suffixes from name.

    Args:
        field: Source column name.
        suffixes: Set of suffixes to remove. Uses SUFFIXES if not provided.

    Returns:
        Polars expression.
    """
    suffixes_set = suffixes or SUFFIXES
    # Build pattern: \s+(jr|sr|ii|iii|...)$
    pattern = r"\s+(" + "|".join(suffixes_set) + ")$"
    return (
        pl.col(field)
        .cast(pl.String)
        .str.strip_chars()
        .str.replace(f"(?i){pattern}", "")
        .str.strip_chars()
    )


def name_split_first(field: str) -> pl.Expr:
    """Extract first name (first word).

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.strip_chars().str.split(" ").list.first()


def name_split_last(field: str) -> pl.Expr:
    """Extract last name (last word).

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    return pl.col(field).cast(pl.String).str.strip_chars().str.split(" ").list.last()


def name_filter_common(field: str, filter_names: Optional[set[str]] = None) -> pl.Expr:
    """Return null if name is a common placeholder.

    Args:
        field: Source column name.
        filter_names: Names to filter out. Uses COMMON_FILTER_NAMES if not provided.

    Returns:
        Polars expression (null if filtered).
    """
    names_set = filter_names or COMMON_FILTER_NAMES
    names_list = list(names_set)

    col = pl.col(field).cast(pl.String)
    lower_col = col.str.strip_chars().str.to_lowercase()

    return pl.when(lower_col.is_in(names_list)).then(pl.lit(None)).otherwise(col.str.strip_chars())


def name_clean(
    field: str,
    titles: Optional[set[str]] = None,
    suffixes: Optional[set[str]] = None,
) -> pl.Expr:
    """All-in-one name cleaner: strip, normalize space, remove titles/suffixes.

    Args:
        field: Source column name.
        titles: Titles to remove.
        suffixes: Suffixes to remove.

    Returns:
        Polars expression.
    """
    titles_set = titles or TITLES
    suffixes_set = suffixes or SUFFIXES

    title_pattern = "^(" + "|".join(titles_set) + r")\s+"
    suffix_pattern = r"\s+(" + "|".join(suffixes_set) + ")$"

    col = pl.col(field).cast(pl.String)
    return (
        col.str.strip_chars()
        .str.replace_all(r"\s+", " ")  # Normalize spaces
        .str.replace(f"(?i){title_pattern}", "")  # Remove titles
        .str.replace(f"(?i){suffix_pattern}", "")  # Remove suffixes
        .str.strip_chars()
    )


# =============================================================================
# NUMERIC CLEANERS
# =============================================================================


def digits(field: str) -> pl.Expr:
    """Keep only digits.

    Args:
        field: Source column name.

    Returns:
        Polars expression.
    """
    col = pl.col(field).cast(pl.String)
    return (
        pl.when(col.is_null() | (col.str.strip_chars() == ""))
        .then(pl.lit(None))
        .otherwise(col.str.replace_all(r"[^\d]", ""))
    )


def numeric(
    field: str,
    decimal_separator: str = ",",
    thousands_separator: str = ".",
) -> pl.Expr:
    """Parse decimal number with custom separators.

    Converts European format (1.234,56) to standard format (1234.56).

    Args:
        field: Source column name.
        decimal_separator: Character used for decimals (default: ",").
        thousands_separator: Character used for thousands (default: ".").

    Returns:
        Polars expression returning string in standard format.
    """
    col = pl.col(field).cast(pl.String).str.strip_chars()

    if thousands_separator:
        col = col.str.replace_all(thousands_separator, "", literal=True)

    if decimal_separator != ".":
        col = col.str.replace(decimal_separator, ".", literal=True)

    return col


def integer(field: str) -> pl.Expr:
    """Parse as integer string (remove decimals).

    Args:
        field: Source column name.

    Returns:
        Polars expression returning integer as string.
    """
    col = pl.col(field).cast(pl.String).str.strip_chars()
    # Remove everything after decimal point
    return col.str.replace(r"[.,]\d*$", "")
