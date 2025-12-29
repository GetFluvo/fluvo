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
from typing import Any, Callable, Optional

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
    # Address cleaners
    "separate_city_postal",
    "detect_country",
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
    "PHONE_PREFIX_TO_COUNTRY",
    "POSTAL_PATTERNS",
    "MAJOR_CITIES",
    # Company cleaners
    "company_suffix",
    "COMPANY_SUFFIX_CANONICAL",
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
_EMAIL_MAILTO_PATTERN = re.compile(r"^mailto:", re.IGNORECASE)
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
    "GB": {"country_code": "44", "mobile_prefix": "7", "national_prefix": "0"},
    "ES": {"country_code": "34", "mobile_prefix": "6", "national_prefix": ""},
    "IT": {"country_code": "39", "mobile_prefix": "3", "national_prefix": ""},
    "AT": {"country_code": "43", "mobile_prefix": "6", "national_prefix": "0"},
    "CH": {"country_code": "41", "mobile_prefix": "7", "national_prefix": "0"},
    "LU": {"country_code": "352", "mobile_prefix": "6", "national_prefix": ""},
    "PT": {"country_code": "351", "mobile_prefix": "9", "national_prefix": ""},
    "IS": {"country_code": "354", "mobile_prefix": "", "national_prefix": ""},
    "US": {"country_code": "1", "mobile_prefix": "", "national_prefix": "1"},
    "CA": {"country_code": "1", "mobile_prefix": "", "national_prefix": "1"},
}

# Phone prefix to country code mapping (for country detection)
PHONE_PREFIX_TO_COUNTRY: dict[str, str] = {
    "31": "NL",
    "32": "BE",
    "33": "FR",
    "34": "ES",
    "39": "IT",
    "41": "CH",
    "43": "AT",
    "44": "GB",
    "45": "DK",
    "46": "SE",
    "47": "NO",
    "48": "PL",
    "49": "DE",
    "351": "PT",
    "352": "LU",
    "353": "IE",
    "354": "IS",
    "358": "FI",
    "1": "US",  # Also CA, but default to US
}

# Postal code patterns by country
# Format: (regex_pattern, position) where position is "prefix" or "suffix"
POSTAL_PATTERNS: dict[str, tuple[str, str]] = {
    # Netherlands: 1234 AB (4 digits + space + 2 letters) - suffix position
    "NL": (r"\d{4}\s?[A-Z]{2}", "suffix"),
    # Belgium: 4 digits - prefix position
    "BE": (r"\d{4}", "prefix"),
    # Germany: 5 digits - prefix position
    "DE": (r"\d{5}", "prefix"),
    # France: 5 digits - prefix position
    "FR": (r"\d{5}", "prefix"),
    # UK: Complex alphanumeric - suffix position
    "GB": (r"[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}", "suffix"),
    "UK": (r"[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}", "suffix"),
    # US: 5 digits or 5+4 format - suffix position
    "US": (r"\d{5}(?:-\d{4})?", "suffix"),
    # Portugal: 4 digits + hyphen + 3 digits - prefix position
    "PT": (r"\d{4}-\d{3}", "prefix"),
    # Iceland: 3 digits - prefix position
    "IS": (r"\d{3}", "prefix"),
    # Spain: 5 digits - prefix position
    "ES": (r"\d{5}", "prefix"),
    # Italy: 5 digits - prefix position
    "IT": (r"\d{5}", "prefix"),
    # Austria: 4 digits - prefix position
    "AT": (r"\d{4}", "prefix"),
    # Switzerland: 4 digits - prefix position
    "CH": (r"\d{4}", "prefix"),
    # Luxembourg: 4 digits - prefix position (L- prefix optional)
    "LU": (r"(?:L-)?\d{4}", "prefix"),
    # Canada: A1A 1A1 format - suffix position
    "CA": (r"[A-Z]\d[A-Z]\s?\d[A-Z]\d", "suffix"),
    # Ireland: Eircode format - suffix position
    "IE": (r"[A-Z]\d{2}\s?[A-Z0-9]{4}", "suffix"),
    # Sweden: 5 digits (often with space: 123 45) - prefix position
    "SE": (r"\d{3}\s?\d{2}", "prefix"),
    # Norway: 4 digits - prefix position
    "NO": (r"\d{4}", "prefix"),
    # Denmark: 4 digits - prefix position
    "DK": (r"\d{4}", "prefix"),
    # Finland: 5 digits - prefix position
    "FI": (r"\d{5}", "prefix"),
    # Poland: 5 digits with hyphen (12-345) - prefix position
    "PL": (r"\d{2}-\d{3}", "prefix"),
}

# Major cities to country mapping (for country detection from city name)
MAJOR_CITIES: dict[str, str] = {
    # Netherlands
    "amsterdam": "NL",
    "rotterdam": "NL",
    "den haag": "NL",
    "the hague": "NL",
    "utrecht": "NL",
    "eindhoven": "NL",
    "groningen": "NL",
    "tilburg": "NL",
    "almere": "NL",
    "breda": "NL",
    "nijmegen": "NL",
    "arnhem": "NL",
    "maastricht": "NL",
    # Belgium
    "brussels": "BE",
    "brussel": "BE",
    "bruxelles": "BE",
    "antwerp": "BE",
    "antwerpen": "BE",
    "ghent": "BE",
    "gent": "BE",
    "charleroi": "BE",
    "liege": "BE",
    "luik": "BE",
    "bruges": "BE",
    "brugge": "BE",
    # Germany
    "berlin": "DE",
    "munich": "DE",
    "münchen": "DE",
    "hamburg": "DE",
    "frankfurt": "DE",
    "cologne": "DE",
    "köln": "DE",
    "düsseldorf": "DE",
    "stuttgart": "DE",
    "dortmund": "DE",
    "essen": "DE",
    "leipzig": "DE",
    "bremen": "DE",
    "dresden": "DE",
    "hanover": "DE",
    "hannover": "DE",
    "nuremberg": "DE",
    "nürnberg": "DE",
    # France
    "paris": "FR",
    "marseille": "FR",
    "lyon": "FR",
    "toulouse": "FR",
    "nice": "FR",
    "nantes": "FR",
    "strasbourg": "FR",
    "montpellier": "FR",
    "bordeaux": "FR",
    "lille": "FR",
    "rennes": "FR",
    # UK
    "london": "GB",
    "birmingham": "GB",
    "manchester": "GB",
    "glasgow": "GB",
    "liverpool": "GB",
    "leeds": "GB",
    "sheffield": "GB",
    "edinburgh": "GB",
    "bristol": "GB",
    "cardiff": "GB",
    "belfast": "GB",
    "newcastle": "GB",
    "nottingham": "GB",
    # Spain
    "madrid": "ES",
    "barcelona": "ES",
    "valencia": "ES",
    "seville": "ES",
    "sevilla": "ES",
    "zaragoza": "ES",
    "malaga": "ES",
    "málaga": "ES",
    "murcia": "ES",
    "bilbao": "ES",
    # Italy
    "rome": "IT",
    "roma": "IT",
    "milan": "IT",
    "milano": "IT",
    "naples": "IT",
    "napoli": "IT",
    "turin": "IT",
    "torino": "IT",
    "palermo": "IT",
    "genoa": "IT",
    "genova": "IT",
    "bologna": "IT",
    "florence": "IT",
    "firenze": "IT",
    "venice": "IT",
    "venezia": "IT",
    # Portugal
    "lisbon": "PT",
    "lisboa": "PT",
    "porto": "PT",
    "figueira da foz": "PT",
    # Iceland
    "reykjavik": "IS",
    "reykjavík": "IS",
    # Austria
    "vienna": "AT",
    "wien": "AT",
    "graz": "AT",
    "linz": "AT",
    "salzburg": "AT",
    "innsbruck": "AT",
    # Switzerland
    "zurich": "CH",
    "zürich": "CH",
    "geneva": "CH",
    "genève": "CH",
    "basel": "CH",
    "bern": "CH",
    "lausanne": "CH",
    # US
    "new york": "US",
    "los angeles": "US",
    "chicago": "US",
    "houston": "US",
    "phoenix": "US",
    "philadelphia": "US",
    "san antonio": "US",
    "san diego": "US",
    "dallas": "US",
    "san jose": "US",
    "austin": "US",
    "jacksonville": "US",
    "san francisco": "US",
    "seattle": "US",
    "denver": "US",
    "boston": "US",
    "washington": "US",
    "miami": "US",
    "atlanta": "US",
    # Canada
    "toronto": "CA",
    "montreal": "CA",
    "montréal": "CA",
    "vancouver": "CA",
    "calgary": "CA",
    "edmonton": "CA",
    "ottawa": "CA",
    "winnipeg": "CA",
    "quebec city": "CA",
    # Scandinavia
    "stockholm": "SE",
    "gothenburg": "SE",
    "malmö": "SE",
    "copenhagen": "DK",
    "københavn": "DK",
    "oslo": "NO",
    "bergen": "NO",
    "helsinki": "FI",
    # Other
    "dublin": "IE",
    "luxembourg": "LU",
    "warsaw": "PL",
    "warszawa": "PL",
    "krakow": "PL",
    "kraków": "PL",
    "prague": "CZ",
    "praha": "CZ",
    "budapest": "HU",
    "athens": "GR",
    "αθήνα": "GR",
}

# Company legal suffix canonical forms
# Key: normalized form (lowercase, no dots, no spaces)
# Value: canonical form with proper punctuation
# Note: When the same abbreviation is used in multiple countries with different
# canonical forms, we use the most internationally common form.
COMPANY_SUFFIX_CANONICAL: dict[str, str] = {
    # Netherlands
    "bv": "B.V.",
    "nv": "N.V.",
    "vof": "V.O.F.",
    "cv": "C.V.",
    "cvoa": "C.V.o.A.",
    # Belgium
    "bvba": "B.V.B.A.",
    "sprl": "S.P.R.L.",
    "cvba": "C.V.B.A.",
    "scrl": "S.C.R.L.",
    "vzvw": "V.Z.W.",  # non-profit
    "asbl": "A.S.B.L.",  # non-profit (French)
    # Germany
    "gmbh": "GmbH",
    "ag": "AG",
    "kg": "KG",
    "ohg": "OHG",
    "gbr": "GbR",
    "ug": "UG",
    "gmbhcokg": "GmbH & Co. KG",
    "kgaa": "KGaA",
    "ev": "e.V.",  # registered association
    # Austria (same as Germany plus)
    "gesmbh": "GesmbH",
    # France / International
    "sa": "S.A.",
    "sarl": "S.A.R.L.",  # French form (most common internationally)
    "sas": "S.A.S.",  # French form
    "snc": "S.N.C.",  # French form
    "sasu": "S.A.S.U.",
    "eurl": "E.U.R.L.",
    "sci": "S.C.I.",
    "scp": "S.C.P.",
    # UK
    "ltd": "Ltd.",
    "limited": "Ltd.",
    "plc": "PLC",
    "llp": "LLP",
    "cic": "CIC",
    # US
    "inc": "Inc.",
    "incorporated": "Inc.",
    "llc": "LLC",
    "corp": "Corp.",
    "corporation": "Corp.",
    "pllc": "PLLC",
    "lp": "LP",
    # Italy
    "spa": "S.p.A.",
    "srl": "S.r.l.",  # Italian form
    "sapa": "S.a.p.a.",
    # Spain
    "sl": "S.L.",
    "slne": "S.L.N.E.",
    "sau": "S.A.U.",
    "slu": "S.L.U.",
    # Portugal
    "lda": "Lda.",
    "unipessoallda": "Unipessoal Lda.",
    # Scandinavia
    "as": "A/S",  # Denmark/Norway (most common)
    "asa": "ASA",  # Norway (public)
    "ab": "AB",  # Sweden
    "aps": "ApS",  # Denmark
    "oy": "Oy",  # Finland
    "oyj": "Oyj",  # Finland (public)
    # Switzerland
    "sagl": "Sagl",  # Italian Switzerland
    # Poland
    "spzoo": "sp. z o.o.",
    "zoo": "z o.o.",
    # Czech Republic
    "sro": "s.r.o.",
    # Other
    "se": "SE",  # European Company
    "scop": "SCOP",  # French cooperative
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

    Converts various formats to international format with + prefix:
    - National format: "0612345678" -> "+31612345678"
    - Country code without +: "31612345678" -> "+31612345678"
    - International dialing (00): "0031612345678" -> "+31612345678"
    - Already international: "+31612345678" -> "+31612345678"

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

        # Already international format with +
        if cleaned.startswith("+"):
            return cleaned

        # International dialing format: 00 + country code (e.g., 0031...)
        if cleaned.startswith("00" + country_code):
            return "+" + cleaned[2:]

        # Starts with country code directly (e.g., 31612345678)
        if cleaned.startswith(country_code):
            return "+" + cleaned

        # National format: starts with national prefix (e.g., 0612345678)
        if national_prefix and cleaned.startswith(national_prefix):
            return f"+{country_code}{cleaned[len(national_prefix) :]}"

        # Fallback: assume it's a local number, add country code
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
    """Clean email: strip, lowercase, remove noise and invalid prefixes.

    Handles common issues:
    - Removes "mailto:" prefix
    - Handles colons as separators (takes first email)
    - Removes trailing noise like "(John)"

    Also stores domain in state for use by website_from_email().
    Can be called with 1 arg (value) or 2 args (value, state).
    """

    def clean(value: Any, state: Optional[dict[str, Any]] = None) -> Any:
        if not value or not isinstance(value, str):
            return value

        value = value.strip()

        # Remove mailto: prefix
        value = _EMAIL_MAILTO_PATTERN.sub("", value)

        # Handle colons as separators (take first email-like part)
        if ":" in value and "@" in value:
            # Split by colon and find first part containing @
            for part in value.split(":"):
                part = part.strip()
                if "@" in part:
                    value = part
                    break

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
# ADDRESS CLEANERS (City/Postal Separation & Country Detection)
# =============================================================================


def separate_city_postal(
    country: Optional[str] = None,
    patterns: Optional[dict[str, tuple[str, str]]] = None,
) -> Callable[[Any], tuple[str, str]]:
    """Separate city and postal code from a combined field.

    Handles common formats where city and postal code are stored together:
    - "75001 Paris" (French: postal prefix)
    - "Amsterdam 1012 AB" (Dutch: postal suffix)
    - "London SW1A 1AA" (UK: alphanumeric suffix)
    - "3080-055 Figueira Da Foz" (Portuguese: hyphenated postal)

    Args:
        country: Optional country code hint (e.g., "NL", "FR", "GB").
                 If provided, uses that country's postal pattern.
                 If not provided, tries to auto-detect from common patterns.
        patterns: Optional custom patterns dict. Uses POSTAL_PATTERNS if not set.

    Returns:
        A cleaner that returns (city, postal_code) tuple.
        If no postal found, returns (original_value, "").
    """
    patterns_dict = patterns or POSTAL_PATTERNS

    # Pre-compile patterns for performance
    compiled_patterns: list[tuple[str, re.Pattern[str], str]] = []

    if country and country.upper() in patterns_dict:
        # Use specific country pattern
        pattern_str, position = patterns_dict[country.upper()]
        compiled_patterns.append(
            (country.upper(), re.compile(pattern_str, re.IGNORECASE), position)
        )
    else:
        # Try all patterns (ordered by specificity)
        # More specific patterns first (PT, NL, GB, CA, IE, PL)
        priority_order = [
            "PT",
            "NL",
            "GB",
            "UK",
            "CA",
            "IE",
            "PL",
            "US",
            "DE",
            "FR",
            "IT",
            "ES",
            "SE",
            "FI",
            "BE",
            "AT",
            "CH",
            "LU",
            "NO",
            "DK",
            "IS",
        ]
        for cc in priority_order:
            if cc in patterns_dict:
                pattern_str, position = patterns_dict[cc]
                compiled_patterns.append(
                    (cc, re.compile(pattern_str, re.IGNORECASE), position)
                )

    def clean(value: Any) -> tuple[str, str]:
        if not value or not isinstance(value, str):
            return (str(value) if value else "", "")

        value = value.strip()
        if not value:
            return ("", "")

        # Try each pattern
        for _country_code, pattern, position in compiled_patterns:
            match = pattern.search(value.upper())
            if match:
                postal = match.group(0)
                # Get original case postal from the value
                start, end = match.start(), match.end()
                # Map positions back to original (non-uppercased) string
                original_postal = value[start:end]

                if position == "prefix":
                    # Postal at start: "75001 Paris"
                    city = value[end:].strip()
                else:
                    # Postal at end: "Amsterdam 1012 AB"
                    city = value[:start].strip()

                return (city, original_postal.strip())

        # No pattern matched - return original as city, empty postal
        return (value, "")

    return clean


def detect_country(
    phone: Optional[str] = None,
    postal: Optional[str] = None,
    city: Optional[str] = None,
    phone_prefixes: Optional[dict[str, str]] = None,
    postal_patterns: Optional[dict[str, tuple[str, str]]] = None,
    cities: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Detect country code from available hints (phone, postal code, city).

    Uses multiple signals to infer the country when it's missing:
    - Phone number international prefix (+31 → NL)
    - Postal code pattern matching (1012 AB → NL)
    - City name lookup (Amsterdam → NL)

    Priority: phone > postal > city (phone is most reliable)

    Args:
        phone: Phone number (e.g., "+31 6 12345678")
        postal: Postal code (e.g., "1012 AB")
        city: City name (e.g., "Amsterdam")
        phone_prefixes: Custom phone prefix mapping. Uses PHONE_PREFIX_TO_COUNTRY.
        postal_patterns: Custom postal patterns. Uses POSTAL_PATTERNS.
        cities: Custom city mapping. Uses MAJOR_CITIES.

    Returns:
        ISO country code (e.g., "NL") or None if not detected.

    Example:
        >>> detect_country(phone="+31 6 12345678")
        'NL'
        >>> detect_country(postal="1012 AB")
        'NL'
        >>> detect_country(city="Amsterdam")
        'NL'
        >>> detect_country(phone="+33 1 234", postal="75001", city="Paris")
        'FR'
    """
    prefixes = phone_prefixes or PHONE_PREFIX_TO_COUNTRY
    patterns = postal_patterns or POSTAL_PATTERNS
    city_map = cities or MAJOR_CITIES

    # 1. Try phone number (most reliable)
    if phone and isinstance(phone, str):
        # Clean phone number
        cleaned = re.sub(r"[^\d+]", "", phone.strip())
        if cleaned.startswith("+"):
            digits = cleaned[1:]
            # Try 3-digit prefixes first (e.g., 351, 352, 353, 354, 358)
            for prefix_len in [3, 2, 1]:
                prefix = digits[:prefix_len]
                if prefix in prefixes:
                    return prefixes[prefix]

    # 2. Try postal code pattern
    if postal and isinstance(postal, str):
        postal_upper = postal.strip().upper()
        # Check each pattern (ordered by specificity)
        priority_order = [
            "PT",
            "NL",
            "GB",
            "UK",
            "CA",
            "IE",
            "PL",
            "US",
            "DE",
            "FR",
            "IT",
            "ES",
            "SE",
            "FI",
            "BE",
            "AT",
            "CH",
            "LU",
            "NO",
            "DK",
            "IS",
        ]
        for cc in priority_order:
            if cc in patterns:
                pattern_str, _ = patterns[cc]
                if re.fullmatch(pattern_str, postal_upper, re.IGNORECASE):
                    # Normalize UK to GB
                    return "GB" if cc == "UK" else cc

    # 3. Try city name lookup
    if city and isinstance(city, str):
        city_lower = city.strip().lower()
        if city_lower in city_map:
            return city_map[city_lower]

    return None


# =============================================================================
# NAME CLEANERS
# =============================================================================


def name_strip_title(titles: Optional[set[str]] = None) -> Cleaner:
    """Remove common titles from name.

    Args:
        titles: Set of titles to remove.
    """
    titles_set = titles or TITLES
    pattern = re.compile(
        "^(" + "|".join(re.escape(t) for t in titles_set) + r")\s+", re.IGNORECASE
    )

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
# COMPANY NAME CLEANERS
# =============================================================================


def _normalize_company_suffix(suffix: str) -> str:
    """Normalize suffix for lookup: lowercase, no dots, no spaces."""
    return suffix.lower().replace(".", "").replace(" ", "")


def _build_suffix_pattern(normalized: str) -> str:
    """Build regex pattern for suffix that matches with/without dots/spaces.

    E.g., "bv" -> "[Bb]\\.?\\s*[Vv]"
    E.g., "gmbh" -> "[Gg]\\.?\\s*[Mm]\\.?\\s*[Bb]\\.?\\s*[Hh]"
    """
    parts = []
    for char in normalized:
        if char.isalpha():
            parts.append(f"[{char.upper()}{char.lower()}]")
        elif char == " ":
            continue  # Skip spaces, we'll add optional space matching
        else:
            parts.append(re.escape(char))
    # Join with optional dot and optional space between each character
    return r"\.?\s*".join(parts)


def company_suffix(
    suffixes: Optional[dict[str, str]] = None,
) -> Cleaner:
    """Normalize company legal suffix (e.g., "BV" → "B.V.", "gmbh" → "GmbH").

    Handles common variations:
    - Without dots: "BV", "NV", "GmbH"
    - With dots: "B.V.", "N.V."
    - Mixed case: "Bv", "bv", "BV"
    - With spaces: "B V" -> "B.V."

    Examples:
        >>> company_suffix()("Acme BV")
        'Acme B.V.'
        >>> company_suffix()("Example Bv")
        'Example B.V.'
        >>> company_suffix()("Test gmbh")
        'Test GmbH'
        >>> company_suffix()("Company B.V.")
        'Company B.V.'
        >>> company_suffix()("Corp Inc")
        'Corp Inc.'
        >>> company_suffix()("Smith & Sons Limited")
        'Smith & Sons Ltd.'

    Args:
        suffixes: Custom mapping from normalized suffix to canonical form.
                  Uses COMPANY_SUFFIX_CANONICAL if not set.
    """
    suffix_map = suffixes or COMPANY_SUFFIX_CANONICAL

    # Build regex patterns for all known suffixes
    # Sort by length (longest first) to match longer patterns first
    sorted_suffixes = sorted(suffix_map.keys(), key=len, reverse=True)

    # Build individual patterns
    patterns = []
    for normalized in sorted_suffixes:
        pattern = _build_suffix_pattern(normalized)
        patterns.append(f"({pattern})")

    # Build final pattern: match suffix at end of string, preceded by space
    # Also allow optional trailing dot
    combined_pattern = "|".join(patterns)
    full_pattern = re.compile(
        r"(\s+)(" + combined_pattern + r")\.?\s*$",
        re.IGNORECASE,
    )

    def clean(value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value

        value = value.strip()
        if not value:
            return None

        match = full_pattern.search(value)
        if match:
            # Get the space before suffix and the matched suffix
            space = match.group(1)
            matched_suffix = match.group(2)

            # Normalize the matched suffix for lookup
            normalized = _normalize_company_suffix(matched_suffix)
            if normalized in suffix_map:
                canonical = suffix_map[normalized]
                # Replace the suffix with canonical form (keep single space)
                return value[: match.start()] + " " + canonical

        return value

    return clean


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
