"""Tests for the Polars expression-based mapper module."""

from datetime import date as date_type

import polars as pl

from fluvo.lib import expr
from fluvo.lib.transform import Processor


class TestVal:
    """Tests for expr.val()."""

    def test_val_returns_column_value(self) -> None:
        """Test that val returns the column value."""
        df = pl.DataFrame({"name": ["Alice", "Bob"]})
        result = df.select(expr.val("name"))
        assert result["name"].to_list() == ["Alice", "Bob"]

    def test_val_with_default(self) -> None:
        """Test that val uses default for null values."""
        df = pl.DataFrame({"name": ["Alice", None, "Bob"]})
        result = df.select(expr.val("name", default="Unknown").alias("name"))
        assert result["name"].to_list() == ["Alice", "Unknown", "Bob"]


class TestConst:
    """Tests for expr.const()."""

    def test_const_returns_literal(self) -> None:
        """Test that const returns a constant value for all rows."""
        df = pl.DataFrame({"name": ["Alice", "Bob", "Charlie"]})
        # Combine with a column expression to broadcast the literal
        result = df.select(pl.col("name"), expr.const("fixed").alias("value"))
        assert result["value"].to_list() == ["fixed", "fixed", "fixed"]

    def test_const_with_number(self) -> None:
        """Test const with numeric value."""
        df = pl.DataFrame({"name": ["Alice", "Bob"]})
        result = df.select(pl.col("name"), expr.const(100).alias("value"))
        assert result["value"].to_list() == [100, 100]


class TestConcat:
    """Tests for expr.concat()."""

    def test_concat_joins_columns(self) -> None:
        """Test that concat joins columns with separator."""
        df = pl.DataFrame({"first": ["John", "Jane"], "last": ["Doe", "Smith"]})
        result = df.select(expr.concat(" ", "first", "last").alias("full_name"))
        assert result["full_name"].to_list() == ["John Doe", "Jane Smith"]

    def test_concat_handles_nulls(self) -> None:
        """Test that concat treats null as empty string."""
        df = pl.DataFrame({"first": ["John", None], "last": ["Doe", "Smith"]})
        result = df.select(expr.concat(" ", "first", "last").alias("full_name"))
        assert result["full_name"].to_list() == ["John Doe", " Smith"]

    def test_concat_multiple_fields(self) -> None:
        """Test concat with more than two fields."""
        df = pl.DataFrame({"a": ["A"], "b": ["B"], "c": ["C"]})
        result = df.select(expr.concat("-", "a", "b", "c").alias("combined"))
        assert result["combined"].to_list() == ["A-B-C"]


class TestConcatAll:
    """Tests for expr.concat_all()."""

    def test_concat_all_with_all_values(self) -> None:
        """Test concat_all when all values present."""
        df = pl.DataFrame({"first": ["John"], "last": ["Doe"]})
        result = df.select(expr.concat_all(" ", "first", "last").alias("full_name"))
        assert result["full_name"].to_list() == ["John Doe"]

    def test_concat_all_with_null(self) -> None:
        """Test concat_all returns empty when any value is null."""
        df = pl.DataFrame({"first": ["John", None], "last": ["Doe", "Smith"]})
        result = df.select(expr.concat_all(" ", "first", "last").alias("full_name"))
        assert result["full_name"].to_list() == ["John Doe", ""]

    def test_concat_all_with_empty_string(self) -> None:
        """Test concat_all returns empty when any value is empty string."""
        df = pl.DataFrame({"first": ["John", ""], "last": ["Doe", "Smith"]})
        result = df.select(expr.concat_all(" ", "first", "last").alias("full_name"))
        assert result["full_name"].to_list() == ["John Doe", ""]


class TestCond:
    """Tests for expr.cond()."""

    def test_cond_true_branch(self) -> None:
        """Test cond returns true_value when condition is truthy."""
        df = pl.DataFrame(
            {
                "is_company": ["1", ""],
                "company_name": ["ACME Corp", ""],
                "contact_name": ["", "John Doe"],
            }
        )
        result = df.select(
            expr.cond("is_company", "company_name", "contact_name").alias("name")
        )
        assert result["name"].to_list() == ["ACME Corp", "John Doe"]

    def test_cond_with_false_string(self) -> None:
        """Test cond treats 'false' as falsy."""
        df = pl.DataFrame(
            {
                "flag": ["true", "false"],
                "a": ["A", "A"],
                "b": ["B", "B"],
            }
        )
        result = df.select(expr.cond("flag", "a", "b").alias("result"))
        assert result["result"].to_list() == ["A", "B"]


class TestBoolVal:
    """Tests for expr.bool_val()."""

    def test_bool_val_with_true_values(self) -> None:
        """Test bool_val with specified true values."""
        df = pl.DataFrame({"status": ["yes", "no", "yes", "maybe"]})
        result = df.select(expr.bool_val("status", true_values=["yes"]).alias("active"))
        assert result["active"].to_list() == ["1", "0", "1", "0"]

    def test_bool_val_with_false_values(self) -> None:
        """Test bool_val with specified false values."""
        df = pl.DataFrame({"status": ["active", "inactive", "active"]})
        result = df.select(
            expr.bool_val("status", false_values=["inactive"]).alias("active")
        )
        assert result["active"].to_list() == ["1", "0", "1"]

    def test_bool_val_with_both_lists(self) -> None:
        """Test bool_val with both true and false values."""
        df = pl.DataFrame({"status": ["yes", "no", "unknown"]})
        result = df.select(
            expr.bool_val(
                "status",
                true_values=["yes"],
                false_values=["no"],
                default=False,
            ).alias("active")
        )
        assert result["active"].to_list() == ["1", "0", "0"]

    def test_bool_val_truthiness(self) -> None:
        """Test bool_val uses truthiness when no lists provided."""
        df = pl.DataFrame({"value": ["something", "", None, "0"]})
        result = df.select(expr.bool_val("value").alias("truthy"))
        assert result["truthy"].to_list() == ["1", "0", "0", "0"]


class TestNum:
    """Tests for expr.num()."""

    def test_num_converts_integers(self) -> None:
        """Test num converts integer strings."""
        df = pl.DataFrame({"value": ["123", "456", "789"]})
        result = df.select(expr.num("value").alias("number"))
        assert result["number"].to_list() == [123.0, 456.0, 789.0]

    def test_num_converts_floats(self) -> None:
        """Test num converts float strings."""
        df = pl.DataFrame({"value": ["1.5", "2.75", "3.0"]})
        result = df.select(expr.num("value").alias("number"))
        assert result["number"].to_list() == [1.5, 2.75, 3.0]

    def test_num_handles_european_format(self) -> None:
        """Test num handles comma as decimal separator."""
        df = pl.DataFrame({"value": ["1,5", "2,75", "3,0"]})
        result = df.select(expr.num("value", decimal_separator=",").alias("number"))
        assert result["number"].to_list() == [1.5, 2.75, 3.0]

    def test_num_with_default(self) -> None:
        """Test num uses default for invalid values."""
        df = pl.DataFrame({"value": ["123", "invalid", None]})
        result = df.select(expr.num("value", default=0).alias("number"))
        assert result["number"].to_list() == [123.0, 0.0, 0.0]


class TestMapVal:
    """Tests for expr.map_val()."""

    def test_map_val_translates_values(self) -> None:
        """Test map_val translates using dictionary."""
        df = pl.DataFrame({"code": ["US", "UK", "DE"]})
        mapping = {"US": "United States", "UK": "United Kingdom", "DE": "Germany"}
        result = df.select(expr.map_val("code", mapping).alias("country"))
        assert result["country"].to_list() == [
            "United States",
            "United Kingdom",
            "Germany",
        ]

    def test_map_val_keeps_original_without_default(self) -> None:
        """Test map_val keeps original value when no default and no match."""
        df = pl.DataFrame({"code": ["US", "XX"]})
        mapping = {"US": "United States"}
        result = df.select(expr.map_val("code", mapping).alias("country"))
        # Without default, keeps original value
        assert result["country"].to_list() == ["United States", "XX"]

    def test_map_val_with_default(self) -> None:
        """Test map_val uses default for unknown values."""
        df = pl.DataFrame({"code": ["US", "XX"]})
        mapping = {"US": "United States"}
        result = df.select(
            expr.map_val("code", mapping, default="Unknown").alias("country")
        )
        assert result["country"].to_list() == ["United States", "Unknown"]


class TestCoalesce:
    """Tests for expr.coalesce()."""

    def test_coalesce_returns_first_non_null(self) -> None:
        """Test coalesce returns first non-null value."""
        df = pl.DataFrame(
            {
                "phone1": [None, "111", None],
                "phone2": ["222", None, None],
                "phone3": ["333", "333", "333"],
            }
        )
        result = df.select(expr.coalesce("phone1", "phone2", "phone3").alias("phone"))
        assert result["phone"].to_list() == ["222", "111", "333"]


class TestM2o:
    """Tests for expr.m2o()."""

    def test_m2o_creates_external_id(self) -> None:
        """Test m2o creates properly formatted external ID."""
        df = pl.DataFrame({"ref": ["ABC123", "DEF456"]})
        result = df.select(expr.m2o("__import__", "ref").alias("id"))
        assert result["id"].to_list() == ["__import__.ABC123", "__import__.DEF456"]

    def test_m2o_sanitizes_special_chars(self) -> None:
        """Test m2o sanitizes special characters."""
        df = pl.DataFrame({"ref": ["ABC 123", "DEF-456"]})
        result = df.select(expr.m2o("__import__", "ref").alias("id"))
        assert result["id"].to_list() == ["__import__.ABC_123", "__import__.DEF_456"]

    def test_m2o_with_empty_returns_default(self) -> None:
        """Test m2o returns default for empty values."""
        df = pl.DataFrame({"ref": ["ABC", "", None]})
        result = df.select(expr.m2o("__import__", "ref", default="").alias("id"))
        assert result["id"][0] == "__import__.ABC"
        assert result["id"][1] == ""
        assert result["id"][2] == ""


class TestM2m:
    """Tests for expr.m2m()."""

    def test_m2m_creates_external_ids(self) -> None:
        """Test m2m creates comma-separated external IDs."""
        df = pl.DataFrame({"tags": ["red,blue,green"]})
        result = df.select(expr.m2m("__import__", "tags").alias("ids"))
        assert result["ids"][0] == "__import__.red,__import__.blue,__import__.green"

    def test_m2m_sanitizes_values(self) -> None:
        """Test m2m sanitizes each value."""
        df = pl.DataFrame({"tags": ["red tag,blue-tag"]})
        result = df.select(expr.m2m("__import__", "tags").alias("ids"))
        assert result["ids"][0] == "__import__.red_tag,__import__.blue_tag"

    def test_m2m_with_empty_returns_default(self) -> None:
        """Test m2m returns default for empty values."""
        df = pl.DataFrame({"tags": ["red", "", None]})
        result = df.select(expr.m2m("__import__", "tags", default="").alias("ids"))
        assert result["ids"][0] == "__import__.red"
        assert result["ids"][1] == ""
        assert result["ids"][2] == ""


class TestDate:
    """Tests for expr.date()."""

    def test_date_parses_european_format(self) -> None:
        """Test date parses European DD/MM/YYYY format."""
        df = pl.DataFrame({"date_str": ["25/12/1990", "01/06/1985"]})
        result = df.select(expr.date("date_str", "%d/%m/%Y").alias("date"))
        assert result["date"][0] == date_type(1990, 12, 25)
        assert result["date"][1] == date_type(1985, 6, 1)

    def test_date_parses_us_format(self) -> None:
        """Test date parses US MM-DD-YYYY format."""
        df = pl.DataFrame({"date_str": ["12-25-1990"]})
        result = df.select(expr.date("date_str", "%m-%d-%Y").alias("date"))
        assert result["date"][0] == date_type(1990, 12, 25)


class TestDatetime:
    """Tests for expr.datetime()."""

    def test_datetime_parses_custom_format(self) -> None:
        """Test datetime parses custom format."""
        df = pl.DataFrame({"dt_str": ["25/12/2023 14:30:00"]})
        result = df.select(expr.datetime("dt_str", "%d/%m/%Y %H:%M:%S").alias("dt"))
        assert result["dt"].dtype == pl.Datetime
        dt_val = result["dt"][0]
        assert dt_val.year == 2023
        assert dt_val.month == 12
        assert dt_val.day == 25
        assert dt_val.hour == 14
        assert dt_val.minute == 30


class TestProcessorIntegration:
    """Tests for using expr with Processor."""

    def test_expr_in_processor_mapping(self) -> None:
        """Test that expr functions work in Processor mappings."""
        df = pl.DataFrame(
            {
                "first_name": ["John", "Jane"],
                "last_name": ["Doe", "Smith"],
                "active": ["yes", "no"],
            }
        )

        processor = Processor(
            mapping={
                "name": expr.concat(" ", "first_name", "last_name"),
                "is_active": expr.bool_val("active", true_values=["yes"]),
            },
            dataframe=df,
        )

        result = processor.process(filename_out="")

        assert result["name"].to_list() == ["John Doe", "Jane Smith"]
        assert result["is_active"].to_list() == ["1", "0"]

    def test_expr_mixed_with_polars(self) -> None:
        """Test that expr functions can be mixed with raw Polars expressions."""
        df = pl.DataFrame(
            {
                "price": ["10.5", "20.0"],
                "quantity": ["2", "3"],
            }
        )

        processor = Processor(
            mapping={
                "price": expr.num("price"),
                "qty": expr.num("quantity"),
                # Raw Polars expression
                "total": pl.col("price").cast(pl.Float64)
                * pl.col("quantity").cast(pl.Float64),
            },
            dataframe=df,
        )

        result = processor.process(filename_out="")

        assert result["price"].to_list() == [10.5, 20.0]
        assert result["qty"].to_list() == [2.0, 3.0]
        assert result["total"].to_list() == [21.0, 60.0]


class TestNumDecimalSeparator:
    """Tests for expr.num() decimal separator handling."""

    def test_num_with_dot_separator(self) -> None:
        """Test num with dot decimal separator (covers branch 226->229)."""
        df = pl.DataFrame({"price": ["10.5", "20.99"]})
        result = df.select(expr.num("price", decimal_separator=".").alias("price"))
        assert result["price"].to_list() == [10.5, 20.99]
