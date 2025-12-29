"""Tests for the row-by-row clean module."""

from typing import Any

from odoo_data_flow.lib import clean


class TestCompositionFunctions:
    """Tests for composition functions."""

    def test_pipe_basic(self) -> None:
        """Test pipe chains cleaners."""
        cleaner = clean.pipe(clean.strip(), clean.lower())
        assert cleaner("  HELLO  ") == "hello"

    def test_pipe_stops_on_none(self) -> None:
        """Test pipe stops processing on None."""
        cleaner = clean.pipe(lambda x: None, clean.lower())
        assert cleaner("HELLO") is None

    def test_pipe_empty(self) -> None:
        """Test pipe with no cleaners."""
        cleaner = clean.pipe()
        assert cleaner("hello") == "hello"

    def test_when_true(self) -> None:
        """Test when with true condition."""
        cleaner = clean.when(lambda x: len(x) > 5, clean.upper())
        assert cleaner("hello world") == "HELLO WORLD"
        assert cleaner("hi") == "hi"

    def test_when_with_else(self) -> None:
        """Test when with else branch."""
        cleaner = clean.when(lambda x: x.startswith("A"), clean.upper(), clean.lower())
        assert cleaner("ABC") == "ABC"
        assert cleaner("xyz") == "xyz"

    def test_fallback(self) -> None:
        """Test fallback tries cleaners until success."""
        cleaner = clean.fallback(
            lambda x: None if x == "skip" else None,
            lambda x: "found" if x == "skip" else None,
            lambda x: "default",
        )
        assert cleaner("skip") == "found"


class TestStringCleaners:
    """Tests for string cleaner functions."""

    def test_strip(self) -> None:
        """Test strip cleaner."""
        assert clean.strip()("  hello  ") == "hello"

    def test_strip_none(self) -> None:
        """Test strip with None."""
        assert clean.strip()(None) is None

    def test_strip_non_string(self) -> None:
        """Test strip with non-string."""
        assert clean.strip()(123) == 123

    def test_normalize_space(self) -> None:
        """Test normalize_space collapses multiple spaces."""
        assert clean.normalize_space()("hello   world  ") == "hello world"

    def test_lower(self) -> None:
        """Test lowercase conversion."""
        assert clean.lower()("HELLO World") == "hello world"

    def test_upper(self) -> None:
        """Test uppercase conversion."""
        assert clean.upper()("hello World") == "HELLO WORLD"

    def test_title(self) -> None:
        """Test title case conversion."""
        assert clean.title()("hello world") == "Hello World"

    def test_capitalize(self) -> None:
        """Test capitalize first letter."""
        assert clean.capitalize()("hello WORLD") == "Hello world"

    def test_remove(self) -> None:
        """Test removing specific characters."""
        assert clean.remove(".-")("1.2-3") == "123"

    def test_keep(self) -> None:
        """Test keeping only matching characters."""
        assert clean.keep("0-9")("abc123def456") == "123456"

    def test_replace(self) -> None:
        """Test string replacement."""
        assert clean.replace("-", "_")("hello-world") == "hello_world"

    def test_regex_sub(self) -> None:
        """Test regex substitution."""
        assert clean.regex_sub(r"\s+", " ")("hello   world") == "hello world"

    def test_truncate(self) -> None:
        """Test string truncation."""
        assert clean.truncate(5)("hello world") == "hello"

    def test_default_with_none(self) -> None:
        """Test default value for None."""
        assert clean.default("N/A")(None) == "N/A"

    def test_default_with_empty(self) -> None:
        """Test default value for empty string."""
        assert clean.default("N/A")("   ") == "N/A"

    def test_default_with_value(self) -> None:
        """Test default preserves existing value."""
        assert clean.default("N/A")("hello") == "hello"


class TestPhoneCleaners:
    """Tests for phone cleaner functions."""

    def test_phone_with_plus(self) -> None:
        """Test phone cleaning preserves leading +."""
        assert clean.phone()("+31 (6) 12-34-56-78") == "+31612345678"

    def test_phone_without_plus(self) -> None:
        """Test phone cleaning without +."""
        assert clean.phone()("06 12 34 56 78") == "0612345678"

    def test_phone_empty(self) -> None:
        """Test phone cleaning with empty value."""
        assert clean.phone()("") is None

    def test_phone_none(self) -> None:
        """Test phone cleaning with None."""
        assert clean.phone()(None) is None

    def test_phone_digits(self) -> None:
        """Test phone_digits extracts only digits."""
        assert clean.phone_digits()("+31 (6) 12-34") == "3161234"

    def test_phone_normalize_nl(self) -> None:
        """Test phone normalization for Netherlands."""
        assert clean.phone_normalize("NL")("0612345678") == "+31612345678"

    def test_phone_normalize_already_international(self) -> None:
        """Test phone normalization with already international format."""
        assert clean.phone_normalize("NL")("+31612345678") == "+31612345678"

    def test_phone_normalize_be(self) -> None:
        """Test phone normalization for Belgium."""
        assert clean.phone_normalize("BE")("0412345678") == "+32412345678"

    def test_phone_normalize_unknown_country(self) -> None:
        """Test phone normalization with unknown country falls back to basic."""
        assert clean.phone_normalize("XX")("+1234567890") == "+1234567890"

    def test_phone_normalize_country_code_without_plus(self) -> None:
        """Test phone normalization when number starts with country code."""
        assert clean.phone_normalize("NL")("31612345678") == "+31612345678"

    def test_phone_normalize_00_prefix(self) -> None:
        """Test phone normalization with 00 international dialing prefix."""
        assert clean.phone_normalize("NL")("0031612345678") == "+31612345678"

    def test_phone_normalize_00_prefix_with_spaces(self) -> None:
        """Test phone normalization with 00 prefix and spaces."""
        assert clean.phone_normalize("NL")("00 31 6 12345678") == "+31612345678"

    def test_phone_normalize_be_country_code(self) -> None:
        """Test phone normalization for Belgium with raw country code."""
        assert clean.phone_normalize("BE")("32412345678") == "+32412345678"

    def test_phone_normalize_be_00_prefix(self) -> None:
        """Test phone normalization for Belgium with 00 prefix."""
        assert clean.phone_normalize("BE")("0032412345678") == "+32412345678"

    def test_phone_clean_with_country(self) -> None:
        """Test phone_clean all-in-one cleaner."""
        assert clean.phone_clean("NL")("  06 12 34 56 78  ") == "+31612345678"

    def test_phone_clean_without_country(self) -> None:
        """Test phone_clean without country."""
        assert clean.phone_clean()("+31 6 1234") == "+3161234"


class TestEmailCleaners:
    """Tests for email cleaner functions."""

    def test_email_basic(self) -> None:
        """Test basic email cleaning."""
        assert clean.email()("  John@Example.COM  ") == "john@example.com"

    def test_email_with_name_suffix(self) -> None:
        """Test email removes (Name) suffix."""
        assert clean.email()("john@example.com (John Doe)") == "john@example.com"

    def test_email_empty(self) -> None:
        """Test email with empty value."""
        assert clean.email()("  ") is None

    def test_email_stores_domain_in_state(self) -> None:
        """Test email stores domain in state."""
        state: dict[str, Any] = {}
        clean.email()("user@example.com", state)
        assert state.get("_email_domain") == "example.com"

    def test_email_mailto_prefix(self) -> None:
        """Test email removes mailto: prefix."""
        assert clean.email()("mailto:john@example.com") == "john@example.com"

    def test_email_mailto_prefix_uppercase(self) -> None:
        """Test email removes MAILTO: prefix (case insensitive)."""
        assert clean.email()("MAILTO:john@example.com") == "john@example.com"

    def test_email_colon_separator(self) -> None:
        """Test email handles colon as separator."""
        assert clean.email()("label:john@example.com") == "john@example.com"

    def test_email_multiple_colons(self) -> None:
        """Test email handles multiple colons."""
        assert clean.email()("Work:Sales:john@example.com") == "john@example.com"

    def test_email_trailing_colon(self) -> None:
        """Test email handles trailing colon."""
        assert clean.email()("john@example.com:") == "john@example.com"

    def test_email_domain(self) -> None:
        """Test email_domain extraction."""
        assert clean.email_domain()("user@example.com") == "example.com"

    def test_email_domain_no_at(self) -> None:
        """Test email_domain with no @ symbol."""
        assert clean.email_domain()("not-an-email") is None

    def test_website_from_email_basic(self) -> None:
        """Test website_from_email derives website from state."""
        state = {"_email_domain": "example.com"}
        assert clean.website_from_email()("", state) == "https://www.example.com"

    def test_website_from_email_preserves_existing(self) -> None:
        """Test website_from_email preserves existing website."""
        state = {"_email_domain": "example.com"}
        assert (
            clean.website_from_email()("https://other.com", state)
            == "https://other.com"
        )

    def test_website_from_email_filters_providers(self) -> None:
        """Test website_from_email filters common providers."""
        state = {"_email_domain": "gmail.com"}
        assert clean.website_from_email()("", state) == ""

    def test_website_from_email_no_state(self) -> None:
        """Test website_from_email without state."""
        assert clean.website_from_email()("") == ""


class TestUrlCleaners:
    """Tests for URL cleaner functions."""

    def test_url_basic(self) -> None:
        """Test basic URL cleaning adds https."""
        assert clean.url()("example.com") == "https://example.com"

    def test_url_fix_www(self) -> None:
        """Test URL fixes wwwexample.com."""
        assert clean.url()("wwwexample.com") == "https://www.example.com"

    def test_url_http_to_https(self) -> None:
        """Test URL converts http to https."""
        assert clean.url()("http://example.com") == "https://example.com"

    def test_url_already_https(self) -> None:
        """Test URL preserves existing https."""
        assert clean.url()("https://example.com") == "https://example.com"

    def test_url_empty(self) -> None:
        """Test URL with empty value."""
        assert clean.url()("") is None

    def test_url_https_only(self) -> None:
        """Test url_https converts http to https."""
        assert clean.url_https()("http://example.com") == "https://example.com"

    def test_url_fix_www_only(self) -> None:
        """Test url_fix_www only."""
        assert clean.url_fix_www()("http://wwwtest.com") == "http://www.test.com"

    def test_url_ensure_scheme(self) -> None:
        """Test url_ensure_scheme adds scheme."""
        assert clean.url_ensure_scheme()("example.com") == "https://example.com"

    def test_url_ensure_scheme_custom(self) -> None:
        """Test url_ensure_scheme with custom scheme."""
        assert clean.url_ensure_scheme("http://")("example.com") == "http://example.com"


class TestVatCleaners:
    """Tests for VAT cleaner functions."""

    def test_vat_basic(self) -> None:
        """Test basic VAT cleaning."""
        assert clean.vat()("NL 123.456.789.B01") == "NL123456789B01"

    def test_vat_already_clean(self) -> None:
        """Test VAT with already clean value."""
        assert clean.vat()("NL123456789B01") == "NL123456789B01"

    def test_vat_empty(self) -> None:
        """Test VAT with empty value."""
        assert clean.vat()("") is None

    def test_vat_or_exempt_clean(self) -> None:
        """Test vat_or_exempt cleans normal VAT."""
        assert clean.vat_or_exempt()("NL123.456.789.B01") == "NL123456789B01"

    def test_vat_or_exempt_exempt_value(self) -> None:
        """Test vat_or_exempt marks exempt."""
        assert clean.vat_or_exempt()("no vat") == "/vat exempt"

    def test_vat_or_exempt_custom_values(self) -> None:
        """Test vat_or_exempt with custom exempt values."""
        result = clean.vat_or_exempt(exempt_values={"kerk", "stichting"})("kerk")
        assert result == "/vat exempt"

    def test_vat_or_exempt_custom_marker(self) -> None:
        """Test vat_or_exempt with custom marker."""
        result = clean.vat_or_exempt(marker="//")("no vat")
        assert result == "//vat exempt"

    def test_vat_clean(self) -> None:
        """Test vat_clean all-in-one cleaner."""
        assert clean.vat_clean()("  nl 123.456.789 b01  ") == "NL123456789B01"


class TestZipCleaners:
    """Tests for zip code cleaner functions."""

    def test_zip_code_basic(self) -> None:
        """Test basic zip code cleaning."""
        assert clean.zip_code()("1234 AB") == "1234AB"

    def test_zip_code_removes_commas(self) -> None:
        """Test zip code removes commas."""
        assert clean.zip_code()("1234,AB") == "1234AB"
        assert clean.zip_code()("12, 34") == "1234"

    def test_zip_code_filters_e_prefix(self) -> None:
        """Test zip code filters out values starting with e-."""
        assert clean.zip_code()("e-mail") is None
        assert clean.zip_code()("E-12345") is None
        assert clean.zip_code()("e-") is None

    def test_zip_strip_prefix(self) -> None:
        """Test zip_strip_prefix removes country prefix."""
        assert clean.zip_strip_prefix()("NL-1234AB") == "1234AB"

    def test_zip_strip_prefix_be(self) -> None:
        """Test zip_strip_prefix with BE prefix."""
        assert clean.zip_strip_prefix()("BE 1000") == "1000"


class TestNameCleaners:
    """Tests for name cleaner functions."""

    def test_name_strip_title(self) -> None:
        """Test name_strip_title removes titles."""
        assert clean.name_strip_title()("Mr. John Doe") == "John Doe"

    def test_name_strip_title_dutch(self) -> None:
        """Test name_strip_title removes Dutch titles."""
        assert clean.name_strip_title()("Dhr. Jan Jansen") == "Jan Jansen"

    def test_name_strip_title_case_insensitive(self) -> None:
        """Test name_strip_title is case insensitive."""
        assert clean.name_strip_title()("MR. John Doe") == "John Doe"

    def test_name_strip_suffix(self) -> None:
        """Test name_strip_suffix removes suffixes."""
        assert clean.name_strip_suffix()("John Doe Jr.") == "John Doe"

    def test_name_split_first(self) -> None:
        """Test name_split_first extracts first name."""
        assert clean.name_split_first()("John Doe") == "John"

    def test_name_split_last(self) -> None:
        """Test name_split_last extracts last name."""
        assert clean.name_split_last()("John Doe") == "Doe"

    def test_name_filter_common(self) -> None:
        """Test name_filter_common filters test names."""
        assert clean.name_filter_common()("Test User") is None

    def test_name_filter_common_case_insensitive(self) -> None:
        """Test name_filter_common is case insensitive."""
        assert clean.name_filter_common()("TEST USER") is None

    def test_name_filter_common_keeps_real_name(self) -> None:
        """Test name_filter_common keeps real names."""
        assert clean.name_filter_common()("John Doe") == "John Doe"

    def test_name_clean(self) -> None:
        """Test name_clean all-in-one cleaner."""
        assert clean.name_clean()("  Mr.  John   Doe  Jr.  ") == "John Doe"


class TestDateCleaners:
    """Tests for date cleaner functions."""

    def test_date_parse_european(self) -> None:
        """Test date_parse with European format."""
        cleaner = clean.date_parse(["%d/%m/%Y"])
        assert cleaner("31/12/2024") == "2024-12-31"

    def test_date_parse_us(self) -> None:
        """Test date_parse with US format."""
        cleaner = clean.date_parse(["%m/%d/%Y"])
        assert cleaner("12/31/2024") == "2024-12-31"

    def test_date_parse_multiple_formats(self) -> None:
        """Test date_parse tries multiple formats."""
        cleaner = clean.date_parse(["%d/%m/%Y", "%Y-%m-%d"])
        assert cleaner("31/12/2024") == "2024-12-31"
        assert cleaner("2024-12-31") == "2024-12-31"

    def test_date_parse_no_match(self) -> None:
        """Test date_parse returns original if no format matches."""
        cleaner = clean.date_parse(["%d/%m/%Y"])
        assert cleaner("not-a-date") == "not-a-date"

    def test_date_parse_custom_output(self) -> None:
        """Test date_parse with custom output format."""
        cleaner = clean.date_parse(["%d/%m/%Y"], output_format="%d-%m-%Y")
        assert cleaner("31/12/2024") == "31-12-2024"

    def test_date_normalize(self) -> None:
        """Test date_normalize handles common formats."""
        cleaner = clean.date_normalize()
        assert cleaner("31/12/2024") == "2024-12-31"
        assert cleaner("31-12-2024") == "2024-12-31"
        assert cleaner("2024-12-31") == "2024-12-31"


class TestNumericCleaners:
    """Tests for numeric cleaner functions."""

    def test_digits(self) -> None:
        """Test digits extracts only digits."""
        assert clean.digits()("abc123def456") == "123456"

    def test_digits_empty(self) -> None:
        """Test digits with empty result."""
        assert clean.digits()("abc") is None

    def test_digits_from_int(self) -> None:
        """Test digits from integer."""
        assert clean.digits()(123) == "123"

    def test_digits_from_float(self) -> None:
        """Test digits from float."""
        assert clean.digits()(123.45) == "123"

    def test_numeric_european(self) -> None:
        """Test numeric with European format."""
        assert clean.numeric(",", ".")("1.234,56") == "1234.56"

    def test_numeric_us(self) -> None:
        """Test numeric with US format."""
        assert clean.numeric(".", ",")("1,234.56") == "1234.56"

    def test_numeric_from_number(self) -> None:
        """Test numeric from number."""
        assert clean.numeric()(123.45) == "123.45"

    def test_integer(self) -> None:
        """Test integer removes decimals."""
        assert clean.integer()("42.99") == "42"

    def test_integer_with_comma(self) -> None:
        """Test integer with comma decimal."""
        assert clean.integer()("42,99") == "42"

    def test_integer_from_float(self) -> None:
        """Test integer from float."""
        assert clean.integer()(42.99) == "42"


class TestConstantsExtensibility:
    """Tests for constants extensibility."""

    def test_common_email_providers_is_set(self) -> None:
        """Test COMMON_EMAIL_PROVIDERS is a set."""
        assert isinstance(clean.COMMON_EMAIL_PROVIDERS, set)
        assert "gmail.com" in clean.COMMON_EMAIL_PROVIDERS

    def test_common_filter_names_is_set(self) -> None:
        """Test COMMON_FILTER_NAMES is a set."""
        assert isinstance(clean.COMMON_FILTER_NAMES, set)
        assert "test" in clean.COMMON_FILTER_NAMES

    def test_titles_is_set(self) -> None:
        """Test TITLES is a set."""
        assert isinstance(clean.TITLES, set)
        assert "mr." in clean.TITLES

    def test_phone_country_rules_is_dict(self) -> None:
        """Test PHONE_COUNTRY_RULES is a dict."""
        assert isinstance(clean.PHONE_COUNTRY_RULES, dict)
        assert "NL" in clean.PHONE_COUNTRY_RULES
        assert clean.PHONE_COUNTRY_RULES["NL"]["country_code"] == "31"

    def test_can_extend_common_email_providers(self) -> None:
        """Test that COMMON_EMAIL_PROVIDERS can be extended."""
        original_len = len(clean.COMMON_EMAIL_PROVIDERS)
        clean.COMMON_EMAIL_PROVIDERS.add("test-custom-domain.com")
        assert len(clean.COMMON_EMAIL_PROVIDERS) == original_len + 1
        clean.COMMON_EMAIL_PROVIDERS.discard("test-custom-domain.com")


class TestMapperIntegration:
    """Tests for mapper postprocess integration."""

    def test_cleaner_as_postprocess(self) -> None:
        """Test using cleaner as postprocess function."""
        # Simulating what mapper.val does with postprocess
        postprocess = clean.phone()
        result = postprocess("+31 (6) 12-34-56-78")
        assert result == "+31612345678"

    def test_pipe_as_postprocess(self) -> None:
        """Test using pipe as postprocess function."""
        postprocess = clean.pipe(clean.strip(), clean.upper())
        result = postprocess("  hello  ")
        assert result == "HELLO"

    def test_stateful_cleaner_with_state(self) -> None:
        """Test stateful cleaner receives state dict."""
        state: dict[str, Any] = {}

        # First call stores domain
        email_cleaner = clean.email()
        email_cleaner("user@example.com", state)

        # Second call uses domain
        website_cleaner = clean.website_from_email()
        result = website_cleaner("", state)

        assert result == "https://www.example.com"


class TestAddressCleaners:
    """Tests for address cleaner functions (city/postal separation)."""

    def test_separate_city_postal_french_prefix(self) -> None:
        """Test separating French-style postal (prefix)."""
        city, postal = clean.separate_city_postal("FR")("75001 Paris")
        assert city == "Paris"
        assert postal == "75001"

    def test_separate_city_postal_dutch_suffix(self) -> None:
        """Test separating Dutch-style postal (suffix)."""
        city, postal = clean.separate_city_postal("NL")("Amsterdam 1012 AB")
        assert city == "Amsterdam"
        assert postal == "1012 AB"

    def test_separate_city_postal_uk_suffix(self) -> None:
        """Test separating UK-style postal (alphanumeric suffix)."""
        city, postal = clean.separate_city_postal("GB")("London SW1A 1AA")
        assert city == "London"
        assert postal == "SW1A 1AA"

    def test_separate_city_postal_portuguese(self) -> None:
        """Test separating Portuguese hyphenated postal."""
        city, postal = clean.separate_city_postal("PT")("3080-055 Figueira Da Foz")
        assert city == "Figueira Da Foz"
        assert postal == "3080-055"

    def test_separate_city_postal_icelandic(self) -> None:
        """Test separating Icelandic 3-digit postal."""
        city, postal = clean.separate_city_postal("IS")("104 Reykjavík")
        assert city == "Reykjavík"
        assert postal == "104"

    def test_separate_city_postal_german(self) -> None:
        """Test separating German 5-digit postal."""
        city, postal = clean.separate_city_postal("DE")("10115 Berlin")
        assert city == "Berlin"
        assert postal == "10115"

    def test_separate_city_postal_us_suffix(self) -> None:
        """Test separating US 5-digit postal (suffix)."""
        city, postal = clean.separate_city_postal("US")("New York 10001")
        assert city == "New York"
        assert postal == "10001"

    def test_separate_city_postal_no_match(self) -> None:
        """Test when no postal pattern matches."""
        city, postal = clean.separate_city_postal("NL")("Some City")
        assert city == "Some City"
        assert postal == ""

    def test_separate_city_postal_auto_detect(self) -> None:
        """Test auto-detection of postal pattern without country hint."""
        # Dutch pattern is distinctive
        city, postal = clean.separate_city_postal()("Amsterdam 1012 AB")
        assert city == "Amsterdam"
        assert postal == "1012 AB"

    def test_separate_city_postal_empty(self) -> None:
        """Test with empty value."""
        city, postal = clean.separate_city_postal("NL")("")
        assert city == ""
        assert postal == ""


class TestCountryDetection:
    """Tests for country detection functions."""

    def test_detect_country_from_phone_nl(self) -> None:
        """Test detecting NL from phone number."""
        result = clean.detect_country(phone="+31 6 12345678")
        assert result == "NL"

    def test_detect_country_from_phone_fr(self) -> None:
        """Test detecting FR from phone number."""
        result = clean.detect_country(phone="+33 1 23456789")
        assert result == "FR"

    def test_detect_country_from_phone_pt(self) -> None:
        """Test detecting PT from 3-digit prefix."""
        result = clean.detect_country(phone="+351 912345678")
        assert result == "PT"

    def test_detect_country_from_postal_nl(self) -> None:
        """Test detecting NL from postal code."""
        result = clean.detect_country(postal="1012 AB")
        assert result == "NL"

    def test_detect_country_from_postal_pt(self) -> None:
        """Test detecting PT from hyphenated postal."""
        result = clean.detect_country(postal="3080-055")
        assert result == "PT"

    def test_detect_country_from_postal_uk(self) -> None:
        """Test detecting GB from UK postal."""
        result = clean.detect_country(postal="SW1A 1AA")
        assert result == "GB"

    def test_detect_country_from_city_with_custom_cities(self) -> None:
        """Test detecting country from city name with custom cities dict."""
        cities = {"amsterdam": "NL", "paris": "FR"}
        result = clean.detect_country(city="Amsterdam", cities=cities)
        assert result == "NL"

    def test_detect_country_from_city_case_insensitive(self) -> None:
        """Test city detection is case insensitive."""
        cities = {"paris": "FR"}
        result = clean.detect_country(city="PARIS", cities=cities)
        assert result == "FR"

    def test_detect_country_combined(self) -> None:
        """Test combined detection uses phone priority."""
        cities = {"paris": "FR"}
        result = clean.detect_country(phone="+33 1 234", postal="75001", city="Paris", cities=cities)
        assert result == "FR"

    def test_detect_country_no_match(self) -> None:
        """Test returns None when no match (no cities dict provided)."""
        result = clean.detect_country(city="Amsterdam")
        assert result is None

    def test_detect_country_city_not_in_dict(self) -> None:
        """Test returns None when city not in provided dict."""
        cities = {"paris": "FR"}
        result = clean.detect_country(city="Unknown City", cities=cities)
        assert result is None

    def test_detect_country_phone_fallback_to_postal(self) -> None:
        """Test falls back to postal when phone has no prefix."""
        result = clean.detect_country(phone="0612345678", postal="1012 AB")
        assert result == "NL"


class TestAddressConstantsExtensibility:
    """Tests for address-related constants extensibility."""

    def test_postal_patterns_is_dict(self) -> None:
        """Test POSTAL_PATTERNS is a dict."""
        assert isinstance(clean.POSTAL_PATTERNS, dict)

    def test_phone_prefix_to_country_is_dict(self) -> None:
        """Test PHONE_PREFIX_TO_COUNTRY is a dict."""
        assert isinstance(clean.PHONE_PREFIX_TO_COUNTRY, dict)


class TestCompanySuffix:
    """Tests for company name suffix normalization."""

    def test_normalize_dutch_bv(self) -> None:
        """Test normalizing Dutch BV variations."""
        cleaner = clean.company_suffix()
        assert cleaner("Acme BV") == "Acme B.V."
        assert cleaner("Acme Bv") == "Acme B.V."
        assert cleaner("Acme bv") == "Acme B.V."
        assert cleaner("Acme B.V.") == "Acme B.V."
        assert cleaner("Acme B.V") == "Acme B.V."

    def test_normalize_dutch_nv(self) -> None:
        """Test normalizing Dutch NV variations."""
        cleaner = clean.company_suffix()
        assert cleaner("Company NV") == "Company N.V."
        assert cleaner("Company N.V.") == "Company N.V."

    def test_normalize_german_gmbh(self) -> None:
        """Test normalizing German GmbH variations."""
        cleaner = clean.company_suffix()
        assert cleaner("Test gmbh") == "Test GmbH"
        assert cleaner("Test GMBH") == "Test GmbH"
        assert cleaner("Test GmbH") == "Test GmbH"

    def test_normalize_uk_ltd(self) -> None:
        """Test normalizing UK Ltd variations."""
        cleaner = clean.company_suffix()
        assert cleaner("Company Ltd") == "Company Ltd."
        assert cleaner("Company ltd") == "Company Ltd."
        assert cleaner("Company LTD") == "Company Ltd."
        assert cleaner("Company Ltd.") == "Company Ltd."

    def test_normalize_uk_limited(self) -> None:
        """Test normalizing UK Limited to Ltd."""
        cleaner = clean.company_suffix()
        assert cleaner("Smith & Sons Limited") == "Smith & Sons Ltd."
        assert cleaner("Smith & Sons limited") == "Smith & Sons Ltd."

    def test_normalize_us_inc(self) -> None:
        """Test normalizing US Inc variations."""
        cleaner = clean.company_suffix()
        assert cleaner("Corp Inc") == "Corp Inc."
        assert cleaner("Corp INC") == "Corp Inc."
        assert cleaner("Corp Inc.") == "Corp Inc."

    def test_normalize_us_llc(self) -> None:
        """Test normalizing US LLC."""
        cleaner = clean.company_suffix()
        assert cleaner("Company LLC") == "Company LLC"
        assert cleaner("Company llc") == "Company LLC"

    def test_normalize_french_sarl(self) -> None:
        """Test normalizing French SARL variations."""
        cleaner = clean.company_suffix()
        assert cleaner("Company SARL") == "Company S.A.R.L."
        assert cleaner("Company S.A.R.L.") == "Company S.A.R.L."

    def test_normalize_belgian_bvba(self) -> None:
        """Test normalizing Belgian BVBA."""
        cleaner = clean.company_suffix()
        assert cleaner("Company BVBA") == "Company B.V.B.A."
        assert cleaner("Company bvba") == "Company B.V.B.A."

    def test_normalize_italian_spa(self) -> None:
        """Test normalizing Italian S.p.A."""
        cleaner = clean.company_suffix()
        assert cleaner("Company SPA") == "Company S.p.A."
        assert cleaner("Company spa") == "Company S.p.A."

    def test_normalize_scandinavian_ab(self) -> None:
        """Test normalizing Swedish AB."""
        cleaner = clean.company_suffix()
        assert cleaner("Company AB") == "Company AB"
        assert cleaner("Company ab") == "Company AB"

    def test_normalize_danish_as(self) -> None:
        """Test normalizing Danish A/S."""
        cleaner = clean.company_suffix()
        assert cleaner("Company AS") == "Company A/S"
        assert cleaner("Company as") == "Company A/S"

    def test_no_suffix_unchanged(self) -> None:
        """Test company name without suffix is unchanged."""
        cleaner = clean.company_suffix()
        assert cleaner("Regular Company Name") == "Regular Company Name"

    def test_suffix_with_trailing_spaces(self) -> None:
        """Test handling trailing spaces."""
        cleaner = clean.company_suffix()
        assert cleaner("Acme BV  ") == "Acme B.V."

    def test_empty_value(self) -> None:
        """Test empty/None values."""
        cleaner = clean.company_suffix()
        assert cleaner(None) is None
        assert cleaner("") is None
        assert cleaner("  ") is None

    def test_custom_suffixes(self) -> None:
        """Test with custom suffix mapping."""
        custom_suffixes = {"xyz": "X.Y.Z."}
        cleaner = clean.company_suffix(suffixes=custom_suffixes)
        assert cleaner("Company XYZ") == "Company X.Y.Z."
        assert cleaner("Company xyz") == "Company X.Y.Z."

    def test_preserves_company_name(self) -> None:
        """Test that company name part is preserved."""
        cleaner = clean.company_suffix()
        assert cleaner("B&V Trading BV") == "B&V Trading B.V."
        assert cleaner("Test-Company GmbH") == "Test-Company GmbH"


class TestCompanySuffixConstant:
    """Tests for COMPANY_SUFFIX_CANONICAL constant."""

    def test_constant_is_dict(self) -> None:
        """Test COMPANY_SUFFIX_CANONICAL is a dict."""
        assert isinstance(clean.COMPANY_SUFFIX_CANONICAL, dict)

    def test_contains_common_suffixes(self) -> None:
        """Test constant contains expected suffixes."""
        assert "bv" in clean.COMPANY_SUFFIX_CANONICAL
        assert "nv" in clean.COMPANY_SUFFIX_CANONICAL
        assert "gmbh" in clean.COMPANY_SUFFIX_CANONICAL
        assert "ltd" in clean.COMPANY_SUFFIX_CANONICAL
        assert "llc" in clean.COMPANY_SUFFIX_CANONICAL

    def test_can_extend_suffixes(self) -> None:
        """Test that COMPANY_SUFFIX_CANONICAL can be extended."""
        original_size = len(clean.COMPANY_SUFFIX_CANONICAL)
        clean.COMPANY_SUFFIX_CANONICAL["testsuffix"] = "TEST"
        assert len(clean.COMPANY_SUFFIX_CANONICAL) == original_size + 1
        # Clean up
        del clean.COMPANY_SUFFIX_CANONICAL["testsuffix"]
