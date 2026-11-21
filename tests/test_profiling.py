"""Tests for the value-level profiling engine (issue #349).

Every fixture here is synthetic. The algorithms were developed against a real
migration, but no real dataset, column name or sample value appears in the
tests or the docs — the shapes are reconstructed from scratch.
"""

import polars as pl
import pytest

from fluvo.lib import profiling
from fluvo.lib.mangling import ANGLO, DUTCH


# --------------------------------------------------------------------------- #
# fill rate
# --------------------------------------------------------------------------- #
def test_fill_rate_treats_null_blank_and_false_as_empty() -> None:
    """Null, whitespace-only and Odoo's "False" all count as unpopulated."""
    df = pl.DataFrame({"c": ["a", None, "   ", "False", "b"]})
    fill = profiling.fill_rate(df, "c")
    assert fill == {"total": 5, "filled": 2, "fill_rate": 0.4}


def test_fill_rate_on_empty_frame_is_zero_not_a_division_error() -> None:
    """An empty frame reports 0% rather than raising."""
    df = pl.DataFrame({"c": []}, schema={"c": pl.String})
    assert profiling.fill_rate(df, "c") == {
        "total": 0,
        "filled": 0,
        "fill_rate": 0.0,
    }


# --------------------------------------------------------------------------- #
# decade histogram / populations
# --------------------------------------------------------------------------- #
def test_decade_histogram_excludes_zero_and_null() -> None:
    """Zero has no order of magnitude, so it is excluded rather than binned."""
    series = pl.Series([0.0, None, 1.0, 9.9, 10.0, 999.0])
    assert profiling._decade_histogram(series) == {0: 2, 1: 1, 2: 1}


def test_decade_histogram_uses_absolute_value() -> None:
    """A negative value is binned by magnitude, not sign."""
    series = pl.Series([-500.0, 500.0])
    assert profiling._decade_histogram(series) == {2: 2}


@pytest.mark.parametrize(
    ("histogram", "expected"),
    [
        ({}, 0),
        ({0: 100}, 1),
        ({0: 50, 1: 50}, 1),  # adjacent decades are one population
        ({0: 50, 3: 50}, 2),  # a gap of >= 1 empty decade separates them
        ({0: 50, 1: 1, 3: 50}, 2),  # the 1-row decade is below the share
        ({0: 34, 3: 33, 6: 33}, 3),
    ],
)
def test_populations_counts_separated_clusters(
    histogram: dict[int, int], expected: int
) -> None:
    """Clusters are separated by an empty decade; thin decades do not bridge."""
    assert profiling._populations(histogram, profiling.POPULATION_MIN_SHARE) == expected


def test_median_decade_of_empty_histogram_is_none() -> None:
    """An empty histogram has no median, and says so rather than raising."""
    assert profiling._median_decade({}) is None


# --------------------------------------------------------------------------- #
# numeric profiling — the mixed-unit case
# --------------------------------------------------------------------------- #
def _two_unit_populations() -> pl.DataFrame:
    """A numeric column holding the same quantity in two different units.

    One population is small and fractional, the other is ~1000x larger and
    integral — the shape left behind when a sub-unit is entered into a field
    that is supposed to hold the base unit.

    Returns:
        pl.DataFrame: 60 fractional rows and 60 integral rows, plus a category
        column that separates them.
    """
    fine = [round(0.5 + i * 0.1, 2) for i in range(60)]
    coarse = [500.0 + i * 50 for i in range(60)]
    return pl.DataFrame(
        {
            "quantity": fine + coarse,
            "category": ["alpha"] * 60 + ["beta"] * 60,
        }
    )


def test_numeric_profile_flags_two_unit_populations() -> None:
    """Both independent tests fire on a column holding two unit populations."""
    report = profiling.profile_numeric_column(_two_unit_populations(), "quantity")
    assert "multi-population" in report["flags"]
    assert "whole-vs-decimal" in report["flags"]
    # The integral rows sit three decades above the fractional ones.
    assert report["whole"]["median_decade"] == 3
    assert report["decimal"]["median_decade"] == 0


def test_numeric_profile_leaves_a_clean_column_unflagged() -> None:
    """A single well-behaved population raises nothing."""
    df = pl.DataFrame({"qty": [float(i) + 0.5 for i in range(100)]})
    assert profiling.profile_numeric_column(df, "qty")["flags"] == []


def test_whole_vs_decimal_needs_support_on_both_sides() -> None:
    """A handful of decimals among integers is noise, not a second population."""
    df = pl.DataFrame({"qty": [float(1000 + i) for i in range(100)] + [0.5, 0.7]})
    report = profiling.profile_numeric_column(df, "qty")
    assert "whole-vs-decimal" not in report["flags"]


def test_numeric_profile_respects_the_locale() -> None:
    """A comma-decimal column parses under DUTCH and not under ANGLO.

    Under ANGLO the comma reads as a thousands separator, so "1,5" becomes 15
    and the whole column shifts a decade. This is why the locale is a
    parameter and never a guess.
    """
    df = pl.DataFrame({"qty": ["1,5", "2,5", "3,5"]})
    dutch = profiling.profile_numeric_column(df, "qty", locale=DUTCH)
    anglo = profiling.profile_numeric_column(df, "qty", locale=ANGLO)
    assert dutch["median"] == 2.5
    assert anglo["median"] == 25.0


def test_numeric_profile_group_by_reports_only_split_groups() -> None:
    """Groups are reported when they themselves split, not merely for existing."""
    fine = [round(0.5 + i * 0.1, 2) for i in range(30)]
    coarse = [500.0 + i * 50 for i in range(30)]
    df = pl.DataFrame(
        {
            # 'mixed' holds both populations; 'clean' holds only one.
            "quantity": fine + coarse + [float(i) + 0.5 for i in range(60)],
            "category": ["mixed"] * 60 + ["clean"] * 60,
        }
    )
    report = profiling.profile_numeric_column(df, "quantity", group_by="category")
    groups = {g["group"] for g in report["groups"]}
    assert groups == {"mixed"}


def test_numeric_profile_ignores_a_missing_group_column() -> None:
    """An absent --group-by field is skipped rather than raising."""
    report = profiling.profile_numeric_column(
        _two_unit_populations(), "quantity", group_by="no_such_field"
    )
    assert "groups" not in report


# --------------------------------------------------------------------------- #
# date profiling
# --------------------------------------------------------------------------- #
def test_date_profile_flags_mixed_formats_and_ambiguity() -> None:
    """Two format variants, and the values where day-first and month-first differ."""
    df = pl.DataFrame(
        {"d": ["2024-01-05"] * 10 + ["05/03/2024"] * 5 + ["31/12/2024"] * 3}
    )
    report = profiling.profile_date_column(df, "d")
    assert "mixed-formats" in report["flags"]
    assert "day-month-ambiguous" in report["flags"]
    assert report["formats"]["yyyy-mm-dd"] == 10
    # Only 05/03 is ambiguous; 31/12 cannot be a month-first date.
    assert report["ambiguous"] == 5


def test_date_profile_single_iso_format_is_clean() -> None:
    """A uniform ISO column raises nothing — an ISO date cannot be ambiguous."""
    df = pl.DataFrame({"d": ["2024-01-05", "2024-11-12", "2024-03-04"]})
    report = profiling.profile_date_column(df, "d")
    assert report["flags"] == []
    assert report["ambiguous"] == 0


def test_date_profile_flags_unrecognised_values() -> None:
    """Something that is not a date at all is reported, not silently dropped."""
    df = pl.DataFrame({"d": ["2024-01-05", "not a date"]})
    report = profiling.profile_date_column(df, "d")
    assert "unrecognised-values" in report["flags"]
    assert report["formats"]["(unrecognised)"] == 1


@pytest.mark.parametrize(
    ("value", "ambiguous"),
    [
        ("05/03/2024", True),
        ("31/12/2024", False),  # 31 cannot be a month
        ("03/03/2024", False),  # both readings agree
        ("2024-03-05", False),  # leading year fixes the order
    ],
)
def test_day_month_ambiguity_rules(value: str, ambiguous: bool) -> None:
    """Ambiguity needs both components <= 12 and different, and a non-ISO shape."""
    assert profiling._is_day_month_ambiguous(value) is ambiguous


# --------------------------------------------------------------------------- #
# enumeration profiling
# --------------------------------------------------------------------------- #
def test_enum_profile_flags_case_and_whitespace_collisions() -> None:
    """Values that fold together are reported with their raw spellings intact.

    The raw values matter: reporting a collision as "FOO vs Foo" when the real
    difference is a trailing space would send the reader looking for the wrong
    thing.
    """
    df = pl.DataFrame({"s": ["Foo"] * 5 + ["FOO "] * 3 + ["bar"] * 4 + ["typo"]})
    report = profiling.profile_enum_column(df, "s")
    assert report is not None
    assert "case-space-collisions" in report["flags"]
    assert report["collisions"] == [["FOO ", "Foo"]]
    assert report["tail"] == ["typo"]


def test_enum_profile_returns_none_for_free_text() -> None:
    """Above the cardinality ceiling a column is free text, not an enumeration."""
    df = pl.DataFrame({"s": [f"value-{i}" for i in range(80)]})
    assert profiling.profile_enum_column(df, "s") is None


def test_enum_profile_returns_none_for_an_empty_column() -> None:
    """A column with no populated values is not an enumeration either."""
    df = pl.DataFrame({"s": [None, "", "False"]})
    assert profiling.profile_enum_column(df, "s") is None


def test_enum_profile_counts_are_ordered_most_frequent_first() -> None:
    """The value list leads with the common members, so the tail reads as a tail."""
    df = pl.DataFrame({"s": ["a"] * 5 + ["b"] * 3 + ["c"]})
    report = profiling.profile_enum_column(df, "s")
    assert report is not None
    assert list(report["values"]) == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# qualifier pairs
# --------------------------------------------------------------------------- #
def test_qualifier_pair_flags_a_populated_value_with_an_empty_qualifier() -> None:
    """A fully-filled value column beside an empty unit column is the signature."""
    df = pl.DataFrame({"quantity": ["1.5", "2.5"], "quantity_uom_id": [None, None]})
    findings = profiling.profile_qualifier_pairs(df, ["quantity"], ["quantity_uom_id"])
    assert len(findings) == 1
    assert findings[0]["flags"] == ["unqualified-values"]
    assert findings[0]["qualifier_fill_rate"] == 0.0


def test_qualifier_pair_silent_when_the_qualifier_is_well_populated() -> None:
    """Nothing is reported when the qualifier keeps up with the value."""
    df = pl.DataFrame({"quantity": ["1.5", "2.5"], "quantity_uom_id": ["kg", "kg"]})
    assert (
        profiling.profile_qualifier_pairs(df, ["quantity"], ["quantity_uom_id"]) == []
    )


def test_qualifier_pair_silent_when_the_value_column_is_empty() -> None:
    """An empty value column has nothing to qualify, so it is not a finding."""
    df = pl.DataFrame({"quantity": [None, None], "quantity_uom_id": [None, None]})
    assert (
        profiling.profile_qualifier_pairs(df, ["quantity"], ["quantity_uom_id"]) == []
    )


@pytest.mark.parametrize(
    "column", ["id", "partner_id", "tag_ids", "category_id/id", "ID"]
)
def test_identifier_columns_are_not_measurements(column: str) -> None:
    """A key is a number without a unit, so it is never paired with a qualifier."""
    assert profiling.is_identifier_column(column)


@pytest.mark.parametrize("column", ["quantity", "weight", "price_unit", "amount"])
def test_measurement_columns_are_not_identifiers(column: str) -> None:
    """An ordinary measurement is not mistaken for a key."""
    assert not profiling.is_identifier_column(column)


def test_qualifier_pair_skips_identifier_columns() -> None:
    """An empty unit field does not flag the id column beside it.

    Pairing every numeric column with every qualifier made `id` an
    "unqualified value" on every model, which is noise that would have buried
    the real findings.
    """
    df = pl.DataFrame(
        {
            "id": ["1", "2"],
            "quantity": ["1.5", "2.5"],
            "uom_id": [None, None],
        }
    )
    findings = profiling.profile_qualifier_pairs(df, ["id", "quantity"], ["uom_id"])
    assert [f["value_column"] for f in findings] == ["quantity"]


def test_qualifier_with_a_stem_pairs_only_with_that_field() -> None:
    """`quantity_uom_id` qualifies `quantity`, not every number on the model."""
    df = pl.DataFrame(
        {
            "quantity": ["1.5", "2.5"],
            "weight": ["10.0", "20.0"],
            "quantity_uom_id": [None, None],
        }
    )
    findings = profiling.profile_qualifier_pairs(
        df, ["quantity", "weight"], ["quantity_uom_id"]
    )
    assert [f["value_column"] for f in findings] == ["quantity"]


def test_bare_qualifier_pairs_with_every_measurement() -> None:
    """A bare `currency_id` qualifies the record, so it covers both amounts."""
    df = pl.DataFrame(
        {
            "amount_total": ["10.0", "20.0"],
            "amount_tax": ["1.0", "2.0"],
            "currency_id": [None, None],
        }
    )
    findings = profiling.profile_qualifier_pairs(
        df, ["amount_total", "amount_tax"], ["currency_id"]
    )
    assert {f["value_column"] for f in findings} == {"amount_total", "amount_tax"}


def test_qualifier_with_an_unmatched_stem_pairs_with_nothing() -> None:
    """A stem naming a field the frame does not have yields no finding."""
    df = pl.DataFrame({"quantity": ["1.5"], "volume_uom_id": [None]})
    assert profiling.profile_qualifier_pairs(df, ["quantity"], ["volume_uom_id"]) == []


@pytest.mark.parametrize(
    "column",
    ["uom_id", "product_uom", "currency_id", "lang", "tz", "timezone"],
)
def test_looks_like_qualifier_recognises_the_hints(column: str) -> None:
    """Qualifier detection is by name fragment, case-insensitively."""
    assert profiling.looks_like_qualifier(column)
    assert profiling.looks_like_qualifier(column.upper())


@pytest.mark.parametrize("column", ["name", "quantity", "street", "ref"])
def test_looks_like_qualifier_rejects_ordinary_names(column: str) -> None:
    """An ordinary field name is not treated as a qualifier."""
    assert not profiling.looks_like_qualifier(column)


# --------------------------------------------------------------------------- #
# whole-frame profiling
# --------------------------------------------------------------------------- #
def test_profile_frame_collects_columns_qualifiers_and_flagged_names() -> None:
    """The frame-level pass wires the per-column passes together."""
    df = _two_unit_populations().with_columns(
        pl.lit(None, dtype=pl.String).alias("quantity_uom_id")
    )
    out = profiling.profile_frame(
        df, numeric=["quantity"], dates=[], enums=["category"]
    )
    assert out["rows"] == 120
    assert {r["column"] for r in out["columns"]} == {"quantity", "category"}
    assert out["qualifiers"][0]["qualifier_column"] == "quantity_uom_id"
    assert out["flagged"] == ["quantity"]


def test_profile_frame_infers_roles_when_not_told() -> None:
    """With no schema, roles come from the frame's dtypes as a fallback."""
    df = pl.DataFrame({"n": [1.0, 2.0, 3.0], "s": ["a", "b", "a"]})
    out = profiling.profile_frame(df)
    kinds = {r["column"]: r["kind"] for r in out["columns"]}
    assert kinds == {"n": "numeric", "s": "enumeration"}


def test_profile_frame_skips_columns_that_are_not_present() -> None:
    """A requested column the frame does not have is ignored, not an error."""
    df = pl.DataFrame({"n": [1.0, 2.0]})
    out = profiling.profile_frame(df, numeric=["n", "absent"], dates=[], enums=[])
    assert [r["column"] for r in out["columns"]] == ["n"]
