"""Tests for the Polars expression-based clean_expr module."""

from typing import Any

import polars as pl

from odoo_data_flow.lib import clean_expr


def apply_expr(expr: pl.Expr, value: Any) -> Any:
    """Helper to apply a Polars expression to a single value."""
    df = pl.DataFrame({"col": [value]})
    result = df.select(expr.alias("result"))["result"][0]
    return result


class TestStringCleaners:
    """Tests for string cleaner functions."""

    def test_strip(self) -> None:
        """Test strip cleaner."""
        result = apply_expr(clean_expr.strip("col"), "  hello  ")
        assert result == "hello"

    def test_strip_none(self) -> None:
        """Test strip with None."""
        result = apply_expr(clean_expr.strip("col"), None)
        assert result is None

    def test_normalize_space(self) -> None:
        """Test normalize_space collapses multiple spaces."""
        result = apply_expr(clean_expr.normalize_space("col"), "hello   world  ")
        assert result == "hello world"

    def test_lower(self) -> None:
        """Test lowercase conversion."""
        result = apply_expr(clean_expr.lower("col"), "HELLO World")
        assert result == "hello world"

    def test_upper(self) -> None:
        """Test uppercase conversion."""
        result = apply_expr(clean_expr.upper("col"), "hello World")
        assert result == "HELLO WORLD"

    def test_title(self) -> None:
        """Test title case conversion."""
        result = apply_expr(clean_expr.title("col"), "hello world")
        assert result == "Hello World"

    def test_capitalize(self) -> None:
        """Test capitalize first letter."""
        result = apply_expr(clean_expr.capitalize("col"), "hello WORLD")
        assert result == "Hello world"

    def test_remove(self) -> None:
        """Test removing specific characters."""
        result = apply_expr(clean_expr.remove("col", ".-"), "1.2-3")
        assert result == "123"

    def test_keep(self) -> None:
        """Test keeping only matching characters."""
        result = apply_expr(clean_expr.keep("col", "0-9"), "abc123def456")
        assert result == "123456"

    def test_replace(self) -> None:
        """Test string replacement."""
        result = apply_expr(clean_expr.replace("col", "-", "_"), "hello-world")
        assert result == "hello_world"

    def test_replace_regex_mode(self) -> None:
        """Test string replacement with literal=False (covers line 442)."""
        result = apply_expr(
            clean_expr.replace("col", r"\s+", " ", literal=False), "hello   world"
        )
        assert result == "hello world"

    def test_regex_sub(self) -> None:
        """Test regex substitution."""
        result = apply_expr(clean_expr.regex_sub("col", r"\s+", " "), "hello   world")
        assert result == "hello world"

    def test_truncate(self) -> None:
        """Test string truncation."""
        result = apply_expr(clean_expr.truncate("col", 5), "hello world")
        assert result == "hello"

    def test_default_with_null(self) -> None:
        """Test default value for null."""
        result = apply_expr(clean_expr.default("col", "N/A"), None)
        assert result == "N/A"

    def test_default_with_empty(self) -> None:
        """Test default value for empty string."""
        result = apply_expr(clean_expr.default("col", "N/A"), "   ")
        assert result == "N/A"

    def test_default_with_value(self) -> None:
        """Test default preserves existing value."""
        result = apply_expr(clean_expr.default("col", "N/A"), "hello")
        assert result == "hello"


class TestPhoneCleaners:
    """Tests for phone cleaner functions."""

    def test_phone_with_plus(self) -> None:
        """Test phone cleaning preserves leading +."""
        result = apply_expr(clean_expr.phone("col"), "+31 (6) 12-34-56-78")
        assert result == "+31612345678"

    def test_phone_without_plus(self) -> None:
        """Test phone cleaning without +."""
        result = apply_expr(clean_expr.phone("col"), "06 12 34 56 78")
        assert result == "0612345678"

    def test_phone_empty(self) -> None:
        """Test phone cleaning with empty value."""
        result = apply_expr(clean_expr.phone("col"), "")
        assert result is None

    def test_phone_digits(self) -> None:
        """Test phone_digits extracts only digits."""
        result = apply_expr(clean_expr.phone_digits("col"), "+31 (6) 12-34")
        assert result == "3161234"

    def test_phone_normalize_nl(self) -> None:
        """Test phone normalization for Netherlands."""
        result = apply_expr(clean_expr.phone_normalize("col", "NL"), "0612345678")
        assert result == "+31612345678"

    def test_phone_normalize_already_international(self) -> None:
        """Test phone normalization with already international format."""
        result = apply_expr(clean_expr.phone_normalize("col", "NL"), "+31612345678")
        assert result == "+31612345678"

    def test_phone_normalize_be(self) -> None:
        """Test phone normalization for Belgium."""
        result = apply_expr(clean_expr.phone_normalize("col", "BE"), "0412345678")
        assert result == "+32412345678"

    def test_phone_normalize_unknown_country(self) -> None:
        """Test phone normalization with unknown country falls back."""
        result = apply_expr(clean_expr.phone_normalize("col", "XX"), "+1234567890")
        assert result == "+1234567890"

    def test_phone_normalize_country_code_without_plus(self) -> None:
        """Test phone normalization when number starts with country code."""
        result = apply_expr(clean_expr.phone_normalize("col", "NL"), "31612345678")
        assert result == "+31612345678"

    def test_phone_normalize_00_prefix(self) -> None:
        """Test phone normalization with 00 international dialing prefix."""
        result = apply_expr(clean_expr.phone_normalize("col", "NL"), "0031612345678")
        assert result == "+31612345678"

    def test_phone_normalize_00_prefix_with_spaces(self) -> None:
        """Test phone normalization with 00 prefix and spaces."""
        result = apply_expr(clean_expr.phone_normalize("col", "NL"), "00 31 6 12345678")
        assert result == "+31612345678"

    def test_phone_normalize_be_country_code(self) -> None:
        """Test phone normalization for Belgium with raw country code."""
        result = apply_expr(clean_expr.phone_normalize("col", "BE"), "32412345678")
        assert result == "+32412345678"

    def test_phone_normalize_be_00_prefix(self) -> None:
        """Test phone normalization for Belgium with 00 prefix."""
        result = apply_expr(clean_expr.phone_normalize("col", "BE"), "0032412345678")
        assert result == "+32412345678"

    def test_phone_normalize_country_without_national_prefix(self) -> None:
        """Test phone normalization for country without national prefix (covers lines 577-578)."""
        # Spain has empty national_prefix in PHONE_COUNTRY_RULES
        result = apply_expr(clean_expr.phone_normalize("col", "ES"), "612345678")
        assert result == "+34612345678"


class TestEmailCleaners:
    """Tests for email cleaner functions."""

    def test_email_basic(self) -> None:
        """Test basic email cleaning."""
        result = apply_expr(clean_expr.email("col"), "  John@Example.COM  ")
        assert result == "john@example.com"

    def test_email_with_name_suffix(self) -> None:
        """Test email removes (Name) suffix."""
        result = apply_expr(clean_expr.email("col"), "john@example.com (John Doe)")
        assert result == "john@example.com"

    def test_email_empty(self) -> None:
        """Test email with empty value."""
        result = apply_expr(clean_expr.email("col"), "")
        assert result == ""

    def test_email_mailto_prefix(self) -> None:
        """Test email removes mailto: prefix."""
        result = apply_expr(clean_expr.email("col"), "mailto:john@example.com")
        assert result == "john@example.com"

    def test_email_mailto_prefix_uppercase(self) -> None:
        """Test email removes MAILTO: prefix (case insensitive)."""
        result = apply_expr(clean_expr.email("col"), "MAILTO:john@example.com")
        assert result == "john@example.com"

    def test_email_colon_separator(self) -> None:
        """Test email handles colon as separator."""
        result = apply_expr(clean_expr.email("col"), "label:john@example.com")
        assert result == "john@example.com"

    def test_email_multiple_colons(self) -> None:
        """Test email handles multiple colons."""
        result = apply_expr(clean_expr.email("col"), "Work:Sales:john@example.com")
        assert result == "john@example.com"

    def test_email_trailing_colon(self) -> None:
        """Test email handles trailing colon."""
        result = apply_expr(clean_expr.email("col"), "john@example.com:")
        assert result == "john@example.com"

    def test_email_domain(self) -> None:
        """Test email_domain extraction."""
        result = apply_expr(clean_expr.email_domain("col"), "user@example.com")
        assert result == "example.com"

    def test_email_domain_no_at(self) -> None:
        """Test email_domain with no @ symbol."""
        result = apply_expr(clean_expr.email_domain("col"), "not-an-email")
        assert result is None


class TestUrlCleaners:
    """Tests for URL cleaner functions."""

    def test_url_basic(self) -> None:
        """Test basic URL cleaning adds https."""
        result = apply_expr(clean_expr.url("col"), "example.com")
        assert result == "https://example.com"

    def test_url_fix_www(self) -> None:
        """Test URL fixes wwwexample.com."""
        result = apply_expr(clean_expr.url("col"), "wwwexample.com")
        assert result == "https://www.example.com"

    def test_url_http_to_https(self) -> None:
        """Test URL converts http to https."""
        result = apply_expr(clean_expr.url("col"), "http://example.com")
        assert result == "https://example.com"

    def test_url_already_https(self) -> None:
        """Test URL preserves existing https."""
        result = apply_expr(clean_expr.url("col"), "https://example.com")
        assert result == "https://example.com"

    def test_url_empty(self) -> None:
        """Test URL with empty value."""
        result = apply_expr(clean_expr.url("col"), "")
        assert result is None

    def test_url_https_only(self) -> None:
        """Test url_https converts http to https."""
        result = apply_expr(clean_expr.url_https("col"), "http://example.com")
        assert result == "https://example.com"

    def test_url_fix_www_only(self) -> None:
        """Test url_fix_www only."""
        result = apply_expr(clean_expr.url_fix_www("col"), "http://wwwtest.com")
        assert result == "http://www.test.com"

    def test_url_ensure_scheme(self) -> None:
        """Test url_ensure_scheme adds scheme."""
        result = apply_expr(clean_expr.url_ensure_scheme("col"), "example.com")
        assert result == "https://example.com"


class TestVatCleaners:
    """Tests for VAT cleaner functions."""

    def test_vat_basic(self) -> None:
        """Test basic VAT cleaning."""
        result = apply_expr(clean_expr.vat("col"), "NL 123.456.789.B01")
        assert result == "NL123456789B01"

    def test_vat_already_clean(self) -> None:
        """Test VAT with already clean value."""
        result = apply_expr(clean_expr.vat("col"), "NL123456789B01")
        assert result == "NL123456789B01"

    def test_vat_empty(self) -> None:
        """Test VAT with empty value."""
        result = apply_expr(clean_expr.vat("col"), "")
        assert result is None

    def test_vat_or_exempt_clean(self) -> None:
        """Test vat_or_exempt cleans normal VAT."""
        result = apply_expr(clean_expr.vat_or_exempt("col"), "NL123.456.789.B01")
        assert result == "NL123456789B01"

    def test_vat_or_exempt_exempt_value(self) -> None:
        """Test vat_or_exempt marks exempt."""
        result = apply_expr(clean_expr.vat_or_exempt("col"), "no vat")
        assert result == "/vat exempt"

    def test_vat_or_exempt_custom_values(self) -> None:
        """Test vat_or_exempt with custom exempt values."""
        result = apply_expr(
            clean_expr.vat_or_exempt("col", exempt_values={"kerk", "stichting"}), "kerk"
        )
        assert result == "/vat exempt"


class TestZipCleaners:
    """Tests for zip code cleaner functions."""

    def test_zip_code_basic(self) -> None:
        """Test basic zip code cleaning."""
        result = apply_expr(clean_expr.zip_code("col"), "1234 AB")
        assert result == "1234AB"

    def test_zip_code_removes_commas(self) -> None:
        """Test zip code removes commas."""
        result = apply_expr(clean_expr.zip_code("col"), "1234,AB")
        assert result == "1234AB"

    def test_zip_code_filters_e_prefix(self) -> None:
        """Test zip code filters out values starting with e-."""
        result = apply_expr(clean_expr.zip_code("col"), "e-mail")
        assert result is None

    def test_zip_strip_prefix(self) -> None:
        """Test zip_strip_prefix removes country prefix."""
        result = apply_expr(clean_expr.zip_strip_prefix("col"), "NL-1234AB")
        assert result == "1234AB"

    def test_zip_strip_prefix_be(self) -> None:
        """Test zip_strip_prefix with BE prefix."""
        result = apply_expr(clean_expr.zip_strip_prefix("col"), "BE 1000")
        assert result == "1000"


class TestCityCleaners:
    """Tests for city cleaner functions."""

    def test_city_basic(self) -> None:
        """Test basic city cleaning."""
        result = apply_expr(clean_expr.city("col"), "amsterdam")
        assert result == "Amsterdam"

    def test_city_removes_parenthetical(self) -> None:
        """Test city removes parenthetical notes."""
        result = apply_expr(clean_expr.city("col"), "Amsterdam (Noord-Holland)")
        assert result == "Amsterdam"

    def test_city_removes_trailing_postal(self) -> None:
        """Test city removes trailing postal codes."""
        result = apply_expr(clean_expr.city("col"), "Amsterdam 1012 AB")
        assert result == "Amsterdam"

    def test_city_removes_punctuation(self) -> None:
        """Test city removes leading/trailing punctuation."""
        result = apply_expr(clean_expr.city("col"), ",Amsterdam,")
        assert result == "Amsterdam"

    def test_city_normalizes_spaces(self) -> None:
        """Test city normalizes multiple spaces."""
        result = apply_expr(clean_expr.city("col"), "New   York")
        assert result == "New York"

    def test_city_filters_e_prefix(self) -> None:
        """Test city filters out values starting with e-."""
        result = apply_expr(clean_expr.city("col"), "e-mail")
        assert result is None

    def test_city_title_case(self) -> None:
        """Test city converts to title case."""
        result = apply_expr(clean_expr.city("col"), "NEW YORK")
        assert result == "New York"


class TestStreetCleaners:
    """Tests for street cleaner functions."""

    def test_street_basic(self) -> None:
        """Test basic street cleaning."""
        result = apply_expr(clean_expr.street("col"), "  123 Main Street  ")
        assert result == "123 Main Street"

    def test_street_removes_parenthetical(self) -> None:
        """Test street removes parenthetical notes."""
        result = apply_expr(clean_expr.street("col"), "123 Main St (Apt 4)")
        assert result == "123 Main St"

    def test_street_removes_punctuation(self) -> None:
        """Test street removes leading/trailing punctuation."""
        result = apply_expr(clean_expr.street("col"), ",123 Main St,")
        assert result == "123 Main St"

    def test_street_normalizes_spaces(self) -> None:
        """Test street normalizes multiple spaces."""
        result = apply_expr(clean_expr.street("col"), "123   Main   Street")
        assert result == "123 Main Street"

    def test_street_preserves_case(self) -> None:
        """Test street preserves original case."""
        result = apply_expr(clean_expr.street("col"), "123 MAIN STREET")
        assert result == "123 MAIN STREET"

    def test_street_filters_e_prefix(self) -> None:
        """Test street filters out values starting with e-."""
        result = apply_expr(clean_expr.street("col"), "e-mail")
        assert result is None


class TestNameCleaners:
    """Tests for name cleaner functions."""

    def test_name_strip_title(self) -> None:
        """Test name_strip_title removes titles."""
        result = apply_expr(clean_expr.name_strip_title("col"), "Mr. John Doe")
        assert result == "John Doe"

    def test_name_strip_title_dutch(self) -> None:
        """Test name_strip_title removes Dutch titles."""
        result = apply_expr(clean_expr.name_strip_title("col"), "Dhr. Jan Jansen")
        assert result == "Jan Jansen"

    def test_name_strip_suffix(self) -> None:
        """Test name_strip_suffix removes suffixes."""
        result = apply_expr(clean_expr.name_strip_suffix("col"), "John Doe Jr.")
        assert result == "John Doe"

    def test_name_split_first(self) -> None:
        """Test name_split_first extracts first name."""
        result = apply_expr(clean_expr.name_split_first("col"), "John Doe")
        assert result == "John"

    def test_name_split_last(self) -> None:
        """Test name_split_last extracts last name."""
        result = apply_expr(clean_expr.name_split_last("col"), "John Doe")
        assert result == "Doe"

    def test_name_filter_common(self) -> None:
        """Test name_filter_common filters test names."""
        result = apply_expr(clean_expr.name_filter_common("col"), "Test User")
        assert result is None

    def test_name_filter_common_keeps_real_name(self) -> None:
        """Test name_filter_common keeps real names."""
        result = apply_expr(clean_expr.name_filter_common("col"), "John Doe")
        assert result == "John Doe"

    def test_name_clean(self) -> None:
        """Test name_clean all-in-one cleaner."""
        result = apply_expr(clean_expr.name_clean("col"), "  Mr.  John   Doe  Jr.  ")
        assert result == "John Doe"


class TestNumericCleaners:
    """Tests for numeric cleaner functions."""

    def test_digits(self) -> None:
        """Test digits extracts only digits."""
        result = apply_expr(clean_expr.digits("col"), "abc123def456")
        assert result == "123456"

    def test_digits_empty(self) -> None:
        """Test digits with empty value."""
        result = apply_expr(clean_expr.digits("col"), "")
        assert result is None

    def test_numeric_european(self) -> None:
        """Test numeric with European format."""
        result = apply_expr(clean_expr.numeric("col", ",", "."), "1.234,56")
        assert result == "1234.56"

    def test_numeric_us(self) -> None:
        """Test numeric with US format."""
        result = apply_expr(clean_expr.numeric("col", ".", ","), "1,234.56")
        assert result == "1234.56"

    def test_numeric_no_thousands_separator(self) -> None:
        """Test numeric without thousands separator (covers branch 1211->1214)."""
        result = apply_expr(clean_expr.numeric("col", ",", ""), "1234,56")
        assert result == "1234.56"

    def test_numeric_dot_decimal_separator(self) -> None:
        """Test numeric with dot as decimal separator (already standard format)."""
        result = apply_expr(clean_expr.numeric("col", ".", ""), "1234.56")
        assert result == "1234.56"

    def test_integer(self) -> None:
        """Test integer removes decimals."""
        result = apply_expr(clean_expr.integer("col"), "42.99")
        assert result == "42"


class TestConstantsExtensibility:
    """Tests for constants extensibility."""

    def test_common_email_providers_is_set(self) -> None:
        """Test COMMON_EMAIL_PROVIDERS is a set."""
        assert isinstance(clean_expr.COMMON_EMAIL_PROVIDERS, set)
        assert "gmail.com" in clean_expr.COMMON_EMAIL_PROVIDERS

    def test_common_filter_names_is_set(self) -> None:
        """Test COMMON_FILTER_NAMES is a set."""
        assert isinstance(clean_expr.COMMON_FILTER_NAMES, set)
        assert "test" in clean_expr.COMMON_FILTER_NAMES

    def test_titles_is_set(self) -> None:
        """Test TITLES is a set."""
        assert isinstance(clean_expr.TITLES, set)
        assert "mr." in clean_expr.TITLES

    def test_phone_country_rules_is_dict(self) -> None:
        """Test PHONE_COUNTRY_RULES is a dict."""
        assert isinstance(clean_expr.PHONE_COUNTRY_RULES, dict)
        assert "NL" in clean_expr.PHONE_COUNTRY_RULES
        assert clean_expr.PHONE_COUNTRY_RULES["NL"]["country_code"] == "31"

    def test_can_extend_common_email_providers(self) -> None:
        """Test that COMMON_EMAIL_PROVIDERS can be extended."""
        original_len = len(clean_expr.COMMON_EMAIL_PROVIDERS)
        clean_expr.COMMON_EMAIL_PROVIDERS.add("test-custom-domain.com")
        assert len(clean_expr.COMMON_EMAIL_PROVIDERS) == original_len + 1
        clean_expr.COMMON_EMAIL_PROVIDERS.discard("test-custom-domain.com")


class TestDataFrameIntegration:
    """Tests for DataFrame integration."""

    def test_multiple_cleaners_in_mapping(self) -> None:
        """Test using multiple cleaners in a mapping."""
        df = pl.DataFrame(
            {
                "phone": ["+31 6 12 34 56 78", "06-87654321"],
                "email": ["JOHN@EXAMPLE.COM", "jane@test.com (Jane)"],
                "name": ["Mr. John Doe", "Ms. Jane Smith Jr."],
            }
        )

        result = df.select(
            clean_expr.phone("phone").alias("phone_clean"),
            clean_expr.email("email").alias("email_clean"),
            clean_expr.name_clean("name").alias("name_clean"),
        )

        assert result["phone_clean"][0] == "+31612345678"
        assert result["phone_clean"][1] == "0687654321"
        assert result["email_clean"][0] == "john@example.com"
        assert result["email_clean"][1] == "jane@test.com"
        assert result["name_clean"][0] == "John Doe"
        assert result["name_clean"][1] == "Jane Smith"

    def test_chaining_cleaners(self) -> None:
        """Test chaining cleaners using Polars method chaining."""
        df = pl.DataFrame({"text": ["  HELLO WORLD  "]})

        # Chain using native Polars expression methods
        result = df.select(
            pl.col("text").str.strip_chars().str.to_lowercase().alias("result")
        )

        assert result["result"][0] == "hello world"


class TestAddressCleaners:
    """Tests for address cleaner functions (city/postal separation)."""

    def test_city_from_combined_french(self) -> None:
        """Test extracting city from French-style combined field."""
        result = apply_expr(clean_expr.city_from_combined("col", "FR"), "75001 Paris")
        assert result == "Paris"

    def test_city_from_combined_dutch(self) -> None:
        """Test extracting city from Dutch-style combined field."""
        result = apply_expr(
            clean_expr.city_from_combined("col", "NL"), "Amsterdam 1012 AB"
        )
        assert result == "Amsterdam"

    def test_city_from_combined_uk(self) -> None:
        """Test extracting city from UK-style combined field."""
        result = apply_expr(
            clean_expr.city_from_combined("col", "GB"), "London SW1A 1AA"
        )
        assert result == "London"

    def test_city_from_combined_german(self) -> None:
        """Test extracting city from German-style combined field."""
        result = apply_expr(clean_expr.city_from_combined("col", "DE"), "10115 Berlin")
        assert result == "Berlin"

    def test_postal_from_combined_french(self) -> None:
        """Test extracting postal from French-style combined field."""
        result = apply_expr(clean_expr.postal_from_combined("col", "FR"), "75001 Paris")
        assert result == "75001"

    def test_postal_from_combined_dutch(self) -> None:
        """Test extracting postal from Dutch-style combined field."""
        result = apply_expr(
            clean_expr.postal_from_combined("col", "NL"), "Amsterdam 1012 AB"
        )
        assert result == "1012 AB"

    def test_postal_from_combined_uk(self) -> None:
        """Test extracting postal from UK-style combined field."""
        result = apply_expr(
            clean_expr.postal_from_combined("col", "GB"), "London SW1A 1AA"
        )
        assert result == "SW1A 1AA"

    def test_postal_from_combined_no_match(self) -> None:
        """Test extracting postal when no match returns empty."""
        result = apply_expr(clean_expr.postal_from_combined("col", "NL"), "Some City")
        assert result == ""

    def test_city_from_combined_unknown_country(self) -> None:
        """Test with unknown country returns original."""
        result = apply_expr(clean_expr.city_from_combined("col", "XX"), "Some Value")
        assert result == "Some Value"

    def test_postal_from_combined_unknown_country(self) -> None:
        """Test postal_from_combined with unknown country returns empty (covers line 1031)."""
        result = apply_expr(clean_expr.postal_from_combined("col", "ZZ"), "Some Value")
        assert result == ""

    def test_dataframe_city_postal_separation(self) -> None:
        """Test separating city and postal on a DataFrame."""
        df = pl.DataFrame(
            {
                "combined": ["75001 Paris", "10115 Berlin", "Amsterdam 1012 AB"],
                "country": ["FR", "DE", "NL"],
            }
        )

        # For each row, use the country to select the pattern
        # This is a simplified test - in practice you'd use when/then/otherwise
        result_fr = df.filter(pl.col("country") == "FR").select(
            clean_expr.city_from_combined("combined", "FR").alias("city"),
            clean_expr.postal_from_combined("combined", "FR").alias("postal"),
        )

        assert result_fr["city"][0] == "Paris"
        assert result_fr["postal"][0] == "75001"


class TestPostalPatternsConstant:
    """Tests for POSTAL_PATTERNS constant."""

    def test_postal_patterns_is_dict(self) -> None:
        """Test POSTAL_PATTERNS is available and is a dict."""
        assert isinstance(clean_expr.POSTAL_PATTERNS, dict)

    def test_postal_patterns_has_common_countries(self) -> None:
        """Test POSTAL_PATTERNS has common countries."""
        assert "NL" in clean_expr.POSTAL_PATTERNS
        assert "FR" in clean_expr.POSTAL_PATTERNS
        assert "DE" in clean_expr.POSTAL_PATTERNS
        assert "GB" in clean_expr.POSTAL_PATTERNS
        assert "US" in clean_expr.POSTAL_PATTERNS


class TestCompanySuffix:
    """Tests for company suffix normalization (Polars version)."""

    def test_normalize_dutch_bv(self) -> None:
        """Test normalizing Dutch BV variations."""
        assert apply_expr(clean_expr.company_suffix("col"), "Acme BV") == "Acme B.V."
        assert apply_expr(clean_expr.company_suffix("col"), "Acme Bv") == "Acme B.V."
        assert apply_expr(clean_expr.company_suffix("col"), "Acme bv") == "Acme B.V."

    def test_normalize_dutch_nv(self) -> None:
        """Test normalizing Dutch NV variations."""
        assert (
            apply_expr(clean_expr.company_suffix("col"), "Company NV") == "Company N.V."
        )

    def test_normalize_german_gmbh(self) -> None:
        """Test normalizing German GmbH variations."""
        assert apply_expr(clean_expr.company_suffix("col"), "Test gmbh") == "Test GmbH"
        assert apply_expr(clean_expr.company_suffix("col"), "Test GMBH") == "Test GmbH"
        assert apply_expr(clean_expr.company_suffix("col"), "Test GmbH") == "Test GmbH"

    def test_normalize_uk_ltd(self) -> None:
        """Test normalizing UK Ltd variations."""
        assert (
            apply_expr(clean_expr.company_suffix("col"), "Company Ltd")
            == "Company Ltd."
        )
        assert (
            apply_expr(clean_expr.company_suffix("col"), "Company ltd")
            == "Company Ltd."
        )
        assert (
            apply_expr(clean_expr.company_suffix("col"), "Company LTD")
            == "Company Ltd."
        )

    def test_normalize_uk_limited(self) -> None:
        """Test normalizing UK Limited to Ltd."""
        result = apply_expr(clean_expr.company_suffix("col"), "Smith & Sons Limited")
        assert result == "Smith & Sons Ltd."

    def test_normalize_us_llc(self) -> None:
        """Test normalizing US LLC."""
        assert (
            apply_expr(clean_expr.company_suffix("col"), "Company LLC") == "Company LLC"
        )
        assert (
            apply_expr(clean_expr.company_suffix("col"), "Company llc") == "Company LLC"
        )

    def test_normalize_french_sarl(self) -> None:
        """Test normalizing French SARL."""
        result = apply_expr(clean_expr.company_suffix("col"), "Company SARL")
        assert result == "Company S.A.R.L."

    def test_normalize_belgian_bvba(self) -> None:
        """Test normalizing Belgian BVBA."""
        result = apply_expr(clean_expr.company_suffix("col"), "Company BVBA")
        assert result == "Company B.V.B.A."

    def test_no_suffix_unchanged(self) -> None:
        """Test company name without suffix is unchanged."""
        result = apply_expr(clean_expr.company_suffix("col"), "Regular Company Name")
        assert result == "Regular Company Name"

    def test_empty_value(self) -> None:
        """Test empty values return None."""
        assert apply_expr(clean_expr.company_suffix("col"), "") is None
        assert apply_expr(clean_expr.company_suffix("col"), None) is None

    def test_dataframe_batch_processing(self) -> None:
        """Test processing multiple company names in a DataFrame."""
        df = pl.DataFrame(
            {
                "company": [
                    "Acme BV",
                    "Test GmbH",
                    "Corp Ltd",
                    "Regular Company",
                ]
            }
        )

        result = df.select(clean_expr.company_suffix("company").alias("normalized"))

        assert result["normalized"][0] == "Acme B.V."
        assert result["normalized"][1] == "Test GmbH"
        assert result["normalized"][2] == "Corp Ltd."
        assert result["normalized"][3] == "Regular Company"


class TestCompanySuffixConstant:
    """Tests for COMPANY_SUFFIX_CANONICAL constant (Polars module)."""

    def test_constant_is_dict(self) -> None:
        """Test COMPANY_SUFFIX_CANONICAL is a dict."""
        assert isinstance(clean_expr.COMPANY_SUFFIX_CANONICAL, dict)

    def test_contains_common_suffixes(self) -> None:
        """Test constant contains expected suffixes."""
        assert "bv" in clean_expr.COMPANY_SUFFIX_CANONICAL
        assert "gmbh" in clean_expr.COMPANY_SUFFIX_CANONICAL
        assert "ltd" in clean_expr.COMPANY_SUFFIX_CANONICAL
        assert "llc" in clean_expr.COMPANY_SUFFIX_CANONICAL
