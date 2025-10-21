"""Unit tests for CSV encoding enhancement with Korean characters."""

import tempfile

import polars as pl
import pytest

from odoo_data_flow.import_threaded import _sanitize_error_message


def test_korean_character_encoding_in_csv() -> None:
    """Test that Korean characters are properly handled in CSV output."""
    # Test that Korean text is properly encoded
    korean_text = "한국어 텍스트"  # Korean text: "Korean text"
    sanitized = _sanitize_error_message(korean_text)

    # Should not contain problematic characters that would break CSV
    assert ";" not in sanitized
    assert "\n" not in sanitized
    assert "\r" not in sanitized

    # Should still contain the Korean text
    assert "한국어" in sanitized
    assert "텍스트" in sanitized


def test_mixed_unicode_encoding_in_csv() -> None:
    """Test that mixed Unicode characters are properly handled in CSV output."""
    # Test mix of Korean, Chinese, and other Unicode characters
    mixed_unicode = "한국어 中文 text"  # Korean + Chinese + English
    sanitized = _sanitize_error_message(mixed_unicode)

    # Should not contain problematic characters that would break CSV
    assert ";" not in sanitized
    assert "\n" not in sanitized
    assert "\r" not in sanitized

    # Should still contain the Unicode text
    assert "한국어" in sanitized
    assert "中文" in sanitized
    assert "text" in sanitized


def test_error_message_with_unicode_characters() -> None:
    """Test that error messages with Unicode characters are properly sanitized."""
    # Test error message with Korean characters
    error_msg = "데이터 타입 오류: 숫자 필드에 텍스트 값이 전송되었습니다"
    # "Data type error: text values sent to numeric fields"
    sanitized = _sanitize_error_message(error_msg)

    # Should not contain problematic characters that would break CSV
    assert ";" not in sanitized
    assert "\n" not in sanitized
    assert "\r" not in sanitized

    # Should still contain the Korean error message
    assert "데이터" in sanitized
    assert "타입" in sanitized
    assert "오류" in sanitized


def test_csv_writer_handles_unicode_properly() -> None:
    """Test that CSV writer properly handles Unicode characters."""
    # Create a DataFrame with Korean characters
    df = pl.DataFrame(
        {
            "id": ["RES_PARTNER.1", "RES_PARTNER.2"],
            "name": ["김철수", "박영희"],  # Korean names
            "city": ["서울", "부산"],  # Seoul, Busan
            "_ERROR_REASON": [
                "데이터 타입 오류 발생",  # Data type error occurred
                "필수 필드 누락",  # Required field missing
            ],
        }
    )

    # Write to temporary file with UTF-8 encoding specified
    with tempfile.NamedTemporaryFile(
        mode="w+", delete=False, suffix=".csv", encoding="utf-8"
    ) as tmp:
        # This should work without issues now that we specify encoding in the
        # file handle
        df.write_csv(tmp.name, separator=";")

        # Read back and verify
        read_df = pl.read_csv(tmp.name, separator=";", encoding="utf8")

        # Should contain the Korean characters
        assert "김철수" in read_df["name"].to_list()
        assert "박영희" in read_df["name"].to_list()
        assert "서울" in read_df["city"].to_list()
        assert "부산" in read_df["city"].to_list()


def test_empty_dataframe_with_unicode_headers() -> None:
    """Test that empty DataFrames with Unicode headers are handled properly."""
    # Create an empty DataFrame with Korean column names
    df = pl.DataFrame(
        schema={
            "아이디": pl.String,  # ID
            "이름": pl.String,  # Name
            "도시": pl.String,  # City
            "_오류_이유": pl.String,  # _Error_Reason
        }
    )

    # Write to temporary file with UTF-8 encoding specified
    with tempfile.NamedTemporaryFile(
        mode="w+", delete=False, suffix=".csv", encoding="utf-8"
    ) as tmp:
        # This should work without issues now that we specify encoding in
        # the file handle
        df.write_csv(tmp.name, separator=";")

        # Read back and verify headers are preserved
        read_df = pl.read_csv(tmp.name, separator=";", encoding="utf8")

        # Headers should be preserved
        assert "아이디" in read_df.columns
        assert "이름" in read_df.columns
        assert "도시" in read_df.columns
        assert "_오류_이유" in read_df.columns


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
