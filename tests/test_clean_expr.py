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

    def test_zip_strip_prefix(self) -> None:
        """Test zip_strip_prefix removes country prefix."""
        result = apply_expr(clean_expr.zip_strip_prefix("col"), "NL-1234AB")
        assert result == "1234AB"

    def test_zip_strip_prefix_be(self) -> None:
        """Test zip_strip_prefix with BE prefix."""
        result = apply_expr(clean_expr.zip_strip_prefix("col"), "BE 1000")
        assert result == "1000"


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
