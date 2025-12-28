"""Row-by-row data cleaners for transformation pipelines.

This module provides data cleaning functions that return callables for use with
the mapper module's `postprocess` parameter. These are useful for:

1. Stateful operations (e.g., deriving website from email domain)
2. Integration with existing mapper-based code
3. Custom Python logic that can't be expressed as Polars expressions

For better performance with large datasets, prefer the Polars-native `clean_expr`
module when possible.

Usage:
    from odoo_data_flow.lib import mapper, clean

    mapping = {
        "phone": mapper.val("Phone", postprocess=clean.phone()),
        "email": mapper.val("Email", postprocess=clean.email()),
        "website": mapper.val("Website", postprocess=clean.website_from_email()),
    }
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Optional, Union

__all__ = [
    # Composition
    "pipe",
    "when",
    "fallback",
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
    "phone_clean",
    # Email cleaners
    "email",
    "email_domain",
    "website_from_email",
    # URL cleaners
    "url",
    "url_https",
    "url_fix_www",
    "url_ensure_scheme",
    # VAT cleaners
    "vat",
    "vat_or_exempt",
    "vat_clean",
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
    # Date cleaners
    "date_parse",
    "date_normalize",
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

# Type alias for cleaner functions
Cleaner = Callable[[Any], Any]
StatefulCleaner = Callable[..., Any]  # Can take 1 or 2 args

# =============================================================================
# PRE-COMPILED REGEX PATTERNS (for performance)
# =============================================================================

_PHONE_PATTERN = re.compile(r"[^\d]")
_PHONE_PLUS_PATTERN = re.compile(r"[^\d+]")
_EMAIL_NOISE_PATTERN = re.compile(r"\s*\([^)]*\)\s*$")
_MULTI_SPACE_PATTERN = re.compile(r"\s+")
_URL_WWW_FIX = re.compile(r"^(https?://)?www([^.\s])")
_URL_SCHEME_PATTERN = re.compile(r"^https?://")
_VAT_CLEAN_PATTERN = re.compile(r"[^A-Za-z0-9-]")
_ZIP_PREFIX_PATTERN = re.compile(r"^[A-Z]{2,3}[-\s]?")


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
# COMPOSITION FUNCTIONS
# =============================================================================


def pipe(*cleaners: Cleaner) -> Cleaner:
    """Chain multiple cleaners, applying left to right.

    Stops processing if value becomes None.

    Args:
        *cleaners: Cleaner functions to chain.

    Returns:
        A cleaner that applies all cleaners in sequence.
    """

    def piped(value: Any) -> Any:
        for cleaner in cleaners:
            if value is None:
                return None
            value = cleaner(value)
        return value

    return piped


def when(
    condition: Callable[[Any], bool],
    then: Cleaner,
    else_: Optional[Cleaner] = None,
) -> Cleaner:
    """Conditional cleaning.

    Args:
        condition: Function that returns True/False.
        then: Cleaner to apply if condition is True.
        else_: Cleaner to apply if condition is False (optional).

    Returns:
        A conditional cleaner.
    """

    def conditional(value: Any) -> Any:
        if condition(value):
            return then(value)
        elif else_ is not None:
            return else_(value)
        return value

    return conditional


def fallback(*cleaners: Cleaner) -> Cleaner:
    """Try cleaners until one returns a non-empty value.

    Args:
        *cleaners: Cleaner functions to try.

    Returns:
        A cleaner that tries each cleaner in order.
    """

    def try_cleaners(value: Any) -> Any:
        for cleaner in cleaners:
            result = cleaner(value)
            if result is not None and result != "":
                return result
        return value

    return try_cleaners


# =============================================================================
# STRING CLEANERS
# =============================================================================


def strip() -> Cleaner:
    """Remove leading and trailing whitespace."""

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    return clean


def normalize_space() -> Cleaner:
    """Collapse multiple whitespace characters to single space."""

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return _MULTI_SPACE_PATTERN.sub(" ", value.strip())
        return value

    return clean


def lower() -> Cleaner:
    """Convert to lowercase."""

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return value.lower()
        return value

    return clean


def upper() -> Cleaner:
    """Convert to uppercase."""

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return value.upper()
        return value

    return clean


def title() -> Cleaner:
    """Convert to title case."""

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return value.title()
        return value

    return clean


def capitalize() -> Cleaner:
    """Capitalize first letter only."""

    def clean(value: Any) -> Any:
        if isinstance(value, str) and value:
            return value[0].upper() + value[1:].lower()
        return value

    return clean


def remove(chars: str) -> Cleaner:
    """Remove specific characters.

    Args:
        chars: Characters to remove (as string).
    """
    # Pre-compile pattern for efficiency
    escaped = "".join(f"\\{c}" if c in r"\.^$*+?{}[]|()" else c for c in chars)
    pattern = re.compile(f"[{escaped}]")

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return pattern.sub("", value)
        return value

    return clean


def keep(char_pattern: str) -> Cleaner:
    """Keep only characters matching pattern.

    Args:
        char_pattern: Regex character class (e.g., "0-9A-Za-z").
    """
    pattern = re.compile(f"[^{char_pattern}]")

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return pattern.sub("", value)
        return value

    return clean


def replace(old: str, new: str) -> Cleaner:
    """Replace substring.

    Args:
        old: String to replace.
        new: Replacement string.
    """

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(old, new)
        return value

    return clean


def regex_sub(pattern: str, replacement: str) -> Cleaner:
    """Apply regex substitution.

    Args:
        pattern: Regex pattern.
        replacement: Replacement string.
    """
    compiled = re.compile(pattern)

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return compiled.sub(replacement, value)
        return value

    return clean


def truncate(max_length: int) -> Cleaner:
    """Limit string to maximum length.

    Args:
        max_length: Maximum number of characters.
    """

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return value[:max_length]
        return value

    return clean


def default(default_value: Any) -> Cleaner:
    """Provide default value if null or empty.

    Args:
        default_value: Value to return if input is None or empty string.
    """

    def clean(value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default_value
        return value

    return clean


# =============================================================================
# PHONE CLEANERS
# =============================================================================


def phone() -> Cleaner:
    """Clean phone number, keeping digits and leading +."""

    def clean(value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return None
        has_plus = value.startswith("+")
        digits_only = _PHONE_PATTERN.sub("", value)
        if not digits_only:
            return None
        return f"+{digits_only}" if has_plus else digits_only

    return clean


def phone_digits() -> Cleaner:
    """Extract only digits from phone number."""

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return None
        result = _PHONE_PATTERN.sub("", value)
        return result if result else None

    return clean


def phone_normalize(
    country: str,
    rules: Optional[dict[str, dict[str, str]]] = None,
) -> Cleaner:
    """Normalize phone number for specific country.

    Converts national format to international format.
    E.g., for NL: "0612345678" -> "+31612345678"

    Args:
        country: Country code (e.g., "NL", "BE", "DE").
        rules: Optional custom rules dict.
    """
    rules_dict = rules or PHONE_COUNTRY_RULES

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return None

        if country not in rules_dict:
            # Fallback to basic phone cleaning
            has_plus = value.startswith("+")
            digits_only = _PHONE_PATTERN.sub("", value)
            return f"+{digits_only}" if has_plus else digits_only

        rule = rules_dict[country]
        country_code = rule["country_code"]
        national_prefix = rule["national_prefix"]

        # Remove all non-digits except +
        cleaned = _PHONE_PLUS_PATTERN.sub("", value)

        # Already international format
        if cleaned.startswith("+"):
            return cleaned

        # Remove national prefix and add country code
        if national_prefix and cleaned.startswith(national_prefix):
            cleaned = cleaned[len(national_prefix) :]

        return f"+{country_code}{cleaned}"

    return clean


def phone_clean(
    country: Optional[str] = None,
    rules: Optional[dict[str, dict[str, str]]] = None,
) -> Cleaner:
    """All-in-one phone cleaner: strip, normalize format, apply country rules.

    Args:
        country: Optional country code for normalization.
        rules: Optional custom rules dict.
    """
    if country:
        return pipe(strip(), phone_normalize(country, rules))
    return pipe(strip(), phone())


# =============================================================================
# EMAIL CLEANERS
# =============================================================================


def email() -> Callable[..., Any]:
    """Clean email: strip, lowercase, remove trailing noise.

    Also stores domain in state for use by website_from_email().
    Can be called with 1 arg (value) or 2 args (value, state).
    """

    def clean(value: Any, state: Optional[dict[str, Any]] = None) -> Any:
        if not value or not isinstance(value, str):
            return value
        # Remove trailing noise like "(John)"
        value = _EMAIL_NOISE_PATTERN.sub("", value)
        value = value.strip().lower()

        if not value:
            return None

        # Store domain in state for website_from_email()
        if state is not None and "@" in value:
            state["_email_domain"] = value.split("@")[1]

        return value

    return clean


def email_domain() -> Cleaner:
    """Extract domain from email address."""

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return None
        if "@" in value:
            return value.lower().split("@")[1]
        return None

    return clean


def website_from_email(
    providers: Optional[set[str]] = None,
    scheme: str = "https://www.",
) -> Callable[..., Any]:
    """Derive website from previously parsed email domain (stateful).

    Only fills in website if the current value is empty AND the email domain
    is not a common provider (gmail, yahoo, etc.).

    Can be called with 1 arg (value) or 2 args (value, state).

    Args:
        providers: Email providers to exclude. Uses COMMON_EMAIL_PROVIDERS if not set.
        scheme: URL scheme to prepend (default: "https://www.").
    """
    providers_set = providers or COMMON_EMAIL_PROVIDERS

    def clean(value: Any, state: Optional[dict[str, Any]] = None) -> Any:
        # Only fill if website is empty
        if value and str(value).strip():
            return value

        if state is None:
            return value

        domain = state.get("_email_domain")
        if domain and domain not in providers_set:
            return f"{scheme}{domain}"

        return value

    return clean


# =============================================================================
# URL CLEANERS
# =============================================================================


def url() -> Cleaner:
    """All-in-one URL cleaner: strip, fix www, ensure https."""

    def clean(value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return None

        # Fix wwwexample.com → www.example.com
        value = _URL_WWW_FIX.sub(r"\1www.\2", value)

        # Add https:// if no scheme
        if not _URL_SCHEME_PATTERN.match(value):
            value = f"https://{value}"

        # Convert http:// to https://
        value = value.replace("http://", "https://", 1)

        return value

    return clean


def url_https() -> Cleaner:
    """Convert http:// to https://."""

    def clean(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("http://"):
            return "https://" + value[7:]
        return value

    return clean


def url_fix_www() -> Cleaner:
    """Fix missing dot after www (wwwexample.com → www.example.com)."""

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return _URL_WWW_FIX.sub(r"\1www.\2", value)
        return value

    return clean


def url_ensure_scheme(scheme: str = "https://") -> Cleaner:
    """Add scheme if missing.

    Args:
        scheme: Scheme to add (default: "https://").
    """

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return None
        if not _URL_SCHEME_PATTERN.match(value):
            return f"{scheme}{value}"
        return value

    return clean


# =============================================================================
# VAT CLEANERS
# =============================================================================


def vat() -> Cleaner:
    """Clean VAT number: keep only letters, digits, and hyphen, uppercase."""

    def clean(value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return None
        return _VAT_CLEAN_PATTERN.sub("", value).upper()

    return clean


def vat_or_exempt(
    exempt_values: Optional[set[str]] = None,
    marker: str = "/",
    exempt_output: str = "vat exempt",
) -> Cleaner:
    """Clean VAT or mark as exempt.

    If the value matches an exempt pattern, returns marker + exempt_output.
    Otherwise, cleans the VAT number normally.

    Args:
        exempt_values: Values that indicate VAT exemption.
        marker: Prefix for exempt output (default: "/").
        exempt_output: Text after marker for exempt (default: "vat exempt").
    """
    exempt_set = exempt_values or VAT_EXEMPT_VALUES

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        value_stripped = value.strip()
        if not value_stripped:
            return None

        if value_stripped.lower() in exempt_set:
            return f"{marker}{exempt_output}"

        return _VAT_CLEAN_PATTERN.sub("", value_stripped).upper()

    return clean


def vat_clean() -> Cleaner:
    """All-in-one VAT cleaner: strip, remove special chars, uppercase."""
    return pipe(strip(), vat())


# =============================================================================
# ZIP CODE CLEANERS
# =============================================================================


def zip_code() -> Cleaner:
    """Clean zip code: strip and remove spaces."""

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        return _MULTI_SPACE_PATTERN.sub("", value.strip())

    return clean


def zip_strip_prefix() -> Cleaner:
    """Remove country prefix from zip code (e.g., "NL-1234AB" → "1234AB")."""

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        return _ZIP_PREFIX_PATTERN.sub("", value.strip())

    return clean


# =============================================================================
# NAME CLEANERS
# =============================================================================


def name_strip_title(titles: Optional[set[str]] = None) -> Cleaner:
    """Remove common titles from name.

    Args:
        titles: Set of titles to remove.
    """
    titles_set = titles or TITLES
    pattern = re.compile("^(" + "|".join(re.escape(t) for t in titles_set) + r")\s+", re.IGNORECASE)

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        return pattern.sub("", value.strip()).strip()

    return clean


def name_strip_suffix(suffixes: Optional[set[str]] = None) -> Cleaner:
    """Remove common suffixes from name.

    Args:
        suffixes: Set of suffixes to remove.
    """
    suffixes_set = suffixes or SUFFIXES
    pattern = re.compile(
        r"\s+(" + "|".join(re.escape(s) for s in suffixes_set) + ")$", re.IGNORECASE
    )

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        return pattern.sub("", value.strip()).strip()

    return clean


def name_split_first() -> Cleaner:
    """Extract first name (first word)."""

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        parts = value.strip().split()
        return parts[0] if parts else value

    return clean


def name_split_last() -> Cleaner:
    """Extract last name (last word)."""

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        parts = value.strip().split()
        return parts[-1] if parts else value

    return clean


def name_filter_common(filter_names: Optional[set[str]] = None) -> Cleaner:
    """Return None if name is a common placeholder.

    Args:
        filter_names: Names to filter out.
    """
    names_set = filter_names or COMMON_FILTER_NAMES

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        if value.strip().lower() in names_set:
            return None
        return value.strip()

    return clean


def name_clean(
    titles: Optional[set[str]] = None,
    suffixes: Optional[set[str]] = None,
) -> Cleaner:
    """All-in-one name cleaner: strip, normalize space, remove titles/suffixes.

    Args:
        titles: Titles to remove.
        suffixes: Suffixes to remove.
    """
    return pipe(
        strip(),
        normalize_space(),
        name_strip_title(titles),
        name_strip_suffix(suffixes),
    )


# =============================================================================
# DATE CLEANERS
# =============================================================================


def date_parse(
    formats: list[str],
    output_format: str = "%Y-%m-%d",
) -> Cleaner:
    """Parse date from various formats.

    Tries each format in order until one succeeds.

    Args:
        formats: List of strptime format strings to try.
        output_format: Output format (default: ISO 8601).
    """

    def clean(value: Any) -> Any:
        if not value or not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return None

        for fmt in formats:
            try:
                dt = datetime.strptime(value, fmt)
                return dt.strftime(output_format)
            except ValueError:
                continue

        # Return original if no format matches
        return value

    return clean


def date_normalize(
    input_formats: Optional[list[str]] = None,
) -> Cleaner:
    """Normalize date to ISO format (YYYY-MM-DD).

    Args:
        input_formats: List of input formats to try. If not provided,
                      uses common European and US formats.
    """
    formats = input_formats or [
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%d %b %Y",
        "%d %B %Y",
    ]
    return date_parse(formats, "%Y-%m-%d")


# =============================================================================
# NUMERIC CLEANERS
# =============================================================================


def digits() -> Cleaner:
    """Keep only digits."""

    def clean(value: Any) -> Any:
        if not value:
            return value
        if isinstance(value, (int, float)):
            return str(int(value))
        if isinstance(value, str):
            result = _PHONE_PATTERN.sub("", value)
            return result if result else None
        return value

    return clean


def numeric(
    decimal_separator: str = ",",
    thousands_separator: str = ".",
) -> Cleaner:
    """Parse decimal number with custom separators.

    Converts European format (1.234,56) to standard format (1234.56).

    Args:
        decimal_separator: Character used for decimals.
        thousands_separator: Character used for thousands.
    """

    def clean(value: Any) -> Any:
        if not value:
            return value
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            value = value.strip()
            if thousands_separator:
                value = value.replace(thousands_separator, "")
            if decimal_separator != ".":
                value = value.replace(decimal_separator, ".")
            return value
        return value

    return clean


def integer() -> Cleaner:
    """Parse as integer string (remove decimals)."""

    def clean(value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return str(int(value))
        if isinstance(value, str):
            value = value.strip()
            # Remove everything after decimal point
            if "." in value:
                value = value.split(".")[0]
            if "," in value:
                value = value.split(",")[0]
            return value
        return value

    return clean
