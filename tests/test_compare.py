"""Tests for ``fluvo compare`` (#298).

The engine is pure, so the whole trap table is exercised offline. The bar the
issue sets is **zero false differences on identical data**: most of what follows
is the negative case — data that *looks* different because a spreadsheet mangled
it, and must not be reported.
"""

from __future__ import annotations

import polars as pl
import pytest

from fluvo.lib.compare import (
    ANGLO,
    DUTCH,
    CompareError,
    CompareOptions,
    FieldSpec,
    apply_column_map,
    build_field_specs,
    build_fix_frame,
    compare_frames,
    read_source_file,
    to_json,
    to_markdown,
)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _one(source_value: str, target_value: str, spec: FieldSpec, **options: object):
    """Compare a single cell and return the result."""
    source = pl.DataFrame({"k": ["1"], spec.name: [source_value]})
    target = pl.DataFrame({"k": ["1"], spec.name: [target_value]})
    return compare_frames(
        source,
        target,
        key="k",
        fields=[spec],
        options=CompareOptions(**options),  # type: ignore[arg-type]
    )


def _differs(source_value: str, target_value: str, spec: FieldSpec, **options) -> bool:
    """True when the two values are reported as a difference."""
    return _one(source_value, target_value, spec, **options).stats.differing_cells == 1


# --------------------------------------------------------------------------
# The trap table — these must NOT be reported as differences
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file_value", "odoo_value"),
    [
        ("1.0", "1"),  # Polars/Excel float repr vs an integer
        ("1", "1.0"),
        ("2,50", "2.5"),  # Dutch decimal comma
        ("1.234.567,89", "1234567.89"),  # Dutch thousands grouping
        ("-3,5", "-3.5"),
        ("0", "0.0"),
        ("", ""),
        ("3.0000001", "3"),  # below the rounding threshold
    ],
)
def test_numeric_traps_are_not_differences(file_value: str, odoo_value: str) -> None:
    """Equal numbers written differently are equal."""
    assert not _differs(file_value, odoo_value, FieldSpec("qty", "numeric"))


def test_dutch_decimal_is_not_read_as_thousands() -> None:
    """``1,5`` is one and a half in Dutch, not fifteen."""
    result = _one("1,5", "1.5", FieldSpec("qty", "numeric"), locale=DUTCH)
    assert result.stats.differing_cells == 0


def test_anglo_locale_reads_the_comma_as_thousands() -> None:
    """The same string means different things under the two locales."""
    assert not _differs("1,234", "1234", FieldSpec("qty", "numeric"), locale=ANGLO)
    # ...and under the Dutch reading the same string is a decimal, so it differs.
    assert _differs("1,234", "1234", FieldSpec("qty", "numeric"), locale=DUTCH)


@pytest.mark.parametrize(
    ("file_value", "odoo_value"),
    [
        ("007", "007"),
        ("7", "007"),  # Excel ate the leading zeros
        ("0012", "12"),
    ],
)
def test_leading_zero_traps_are_not_differences(
    file_value: str, odoo_value: str
) -> None:
    """A code Excel stripped to an integer still matches its padded form."""
    assert not _differs(file_value, odoo_value, FieldSpec("code", "text"))


def test_leading_zeros_are_reported_when_coercion_is_off() -> None:
    """Turning coercion off is how you *ask* for the mangling to be visible."""
    assert _differs("7", "007", FieldSpec("code", "text"), numeric_coercion=False)


@pytest.mark.parametrize(
    ("file_value", "odoo_value"),
    [
        ("WAAR", "True"),
        ("ONWAAR", "False"),
        ("Ja", "True"),
        ("Nee", "False"),
        ("Yes", "True"),
        ("1", "True"),
        ("0", "False"),
        ("", "False"),
        ("X", "True"),
    ],
)
def test_boolean_traps_are_not_differences(file_value: str, odoo_value: str) -> None:
    """Dutch, English and numeric booleans all reduce to the same value."""
    assert not _differs(file_value, odoo_value, FieldSpec("active", "boolean"))


def test_unknown_boolean_token_is_reported_not_guessed() -> None:
    """A token outside the known sets is surfaced, never assumed."""
    assert _differs("misschien", "True", FieldSpec("active", "boolean"))


@pytest.mark.parametrize(
    ("file_value", "odoo_value"),
    [
        ("2024-01-05", "2024-01-05"),
        ("05-01-2024", "2024-01-05"),  # day-month-year
        ("05/01/2024", "2024-01-05"),
        ("45000", "2023-03-15"),  # an Excel date serial
        ("2024-03-02", "2024-03-02 00:00:00"),  # datetime against a date
    ],
)
def test_date_traps_are_not_differences(file_value: str, odoo_value: str) -> None:
    """Every date shape these two sides use reduces to the same day."""
    assert not _differs(file_value, odoo_value, FieldSpec("date_start", "date"))


def test_date_order_is_a_parameter_not_a_guess() -> None:
    """``05-01-2024`` is 5 January or 1 May, and only the caller knows which."""
    spec = FieldSpec("date_start", "date")
    assert not _differs("05-01-2024", "2024-01-05", spec, date_order="dmy")
    assert not _differs("05-01-2024", "2024-05-01", spec, date_order="mdy")
    assert _differs("05-01-2024", "2024-01-05", spec, date_order="mdy")


def test_unparseable_date_keeps_its_raw_text_and_differs() -> None:
    """A malformed date must not quietly become null and match."""
    assert _differs("not a date", "2024-01-05", FieldSpec("date_start", "date"))


def test_datetime_granularity_can_ignore_the_time() -> None:
    """Comparing by day is opt-in, not the default."""
    spec = FieldSpec("write_date", "datetime")
    assert _differs("2024-01-05 10:00:00", "2024-01-05 11:30:00", spec)
    assert not _differs(
        "2024-01-05 10:00:00",
        "2024-01-05 11:30:00",
        spec,
        datetime_granularity="date",
    )


def test_many2one_tuple_compares_on_its_label() -> None:
    """A stringified RPC many2one is compared on the name the sheet holds."""
    assert not _differs(
        "Netherlands", "(166, 'Netherlands')", FieldSpec("country_id", "relation")
    )


def test_selection_label_matches_the_stored_key() -> None:
    """The workbook's label and the database's key are the same value."""
    spec = FieldSpec("state", "selection", {"sale": "sale", "confirmed": "sale"})
    assert not _differs("Confirmed", "sale", spec)
    assert _differs("Draft", "sale", spec)


def test_case_insensitive_is_opt_in() -> None:
    """Case is significant unless the caller says otherwise."""
    spec = FieldSpec("name", "text")
    assert _differs("acme", "Acme", spec)
    assert not _differs("acme", "Acme", spec, case_insensitive=True)


def test_an_empty_number_is_zero_because_odoo_has_no_unset_number() -> None:
    """Odoo reads an empty integer or float back as 0, so the two are one value."""
    assert not _differs("", "0", FieldSpec("color", "numeric"))
    assert not _differs("0.0", "", FieldSpec("color", "numeric"))
    assert _differs("", "1", FieldSpec("color", "numeric"))


def test_an_empty_numeric_key_is_unkeyed_not_zero() -> None:
    """The 0-for-empty rule must not turn a missing key into a joinable one."""
    source = pl.DataFrame({"code": ["", "1"], "name": ["ghost", "real"]})
    target = pl.DataFrame({"code": ["0", "1"], "name": ["zero", "real"]})
    result = compare_frames(
        source,
        target,
        key="code",
        fields=["name"],
        key_specs=[FieldSpec("code", "numeric")],
    )
    assert result.stats.unkeyed_source_rows == 1
    assert result.stats.matched_records == 1
    assert result.stats.differing_records == 0


def test_odoo_false_literal_is_empty_for_typed_fields_but_not_for_text() -> None:
    """'False' is Odoo's empty marker for a number, but possible content for a char."""
    assert not _differs("", "False", FieldSpec("qty", "numeric"))
    # A char field may legitimately contain the word "False", so it is not erased.
    assert _differs("", "False", FieldSpec("name", "text"))


# --------------------------------------------------------------------------
# Real differences must still be found
# --------------------------------------------------------------------------


def test_real_differences_are_reported_with_both_values() -> None:
    """Normalisation must not swallow a genuine difference."""
    source = pl.DataFrame({"vat": ["A1"], "name": ["Beta BV"]})
    target = pl.DataFrame({"vat": ["A1"], "name": ["Beta B.V."]})
    result = compare_frames(source, target, key="vat", fields=["name"])
    assert result.stats.differing_records == 1
    row = result.differences.row(0, named=True)
    assert row["vat"] == "A1"
    assert row["field"] == "name"
    assert row["source_value"] == "Beta BV"
    assert row["target_value"] == "Beta B.V."


def test_differences_expose_what_was_actually_compared() -> None:
    """A disputed result has to be explainable, so the normalised values travel."""
    source = pl.DataFrame({"k": ["1"], "active": ["ONWAAR"]})
    target = pl.DataFrame({"k": ["1"], "active": ["True"]})
    result = compare_frames(
        source, target, key="k", fields=[FieldSpec("active", "boolean")]
    )
    row = result.differences.row(0, named=True)
    assert (row["source_normalised"], row["target_normalised"]) == ("0", "1")


def test_coerced_matches_are_counted_not_hidden() -> None:
    """Coercion is visible in the stats, so it can be questioned."""
    source = pl.DataFrame({"k": ["1"], "code": ["7"]})
    target = pl.DataFrame({"k": ["1"], "code": ["007"]})
    result = compare_frames(source, target, key="k", fields=["code"])
    assert result.stats.differing_cells == 0
    assert result.stats.coerced_equal_cells == 1


# --------------------------------------------------------------------------
# Row accounting: nothing is silently dropped
# --------------------------------------------------------------------------


def test_every_row_lands_in_exactly_one_bucket() -> None:
    """Nothing is silently dropped, which is the point of the extra buckets."""
    source = pl.DataFrame(
        {
            "vat": ["A1", "A2", "A3", "A3", ""],
            "name": ["Acme", "Beta", "Gamma", "Gamma2", "Ghost"],
        }
    )
    target = pl.DataFrame({"vat": ["A1", "A9"], "name": ["Acme", "Zeta"]})
    result = compare_frames(source, target, key="vat", fields=["name"])
    s = result.stats
    assert s.matched_records == 1
    assert s.only_in_source == 1  # A2
    assert s.only_in_target == 1  # A9
    assert s.duplicate_source_keys == 2  # both A3 rows
    assert s.unkeyed_source_rows == 1  # the blank key
    accounted = (
        s.matched_records
        + s.only_in_source
        + s.duplicate_source_keys
        + s.unkeyed_source_rows
    )
    assert accounted == source.height


def test_bucket_frames_carry_the_original_rows() -> None:
    """A bucket hands back the whole source row, not a projection of it."""
    source = pl.DataFrame({"vat": ["A1"], "name": ["Acme"], "note": ["keep me"]})
    target = pl.DataFrame({"vat": ["A9"], "name": ["Zeta"]})
    result = compare_frames(source, target, key="vat", fields=["name"])
    assert result.only_in_source.to_dicts() == [
        {"vat": "A1", "name": "Acme", "note": "keep me"}
    ]


def test_composite_key_needs_every_part() -> None:
    """A partial composite key is unkeyed, not a wildcard match."""
    source = pl.DataFrame(
        {
            "code": ["X", "X", "Y"],
            "company": ["1", "2", ""],
            "name": ["a", "b", "c"],
        }
    )
    target = pl.DataFrame(
        {"code": ["X", "X"], "company": ["1", "2"], "name": ["a", "B"]}
    )
    result = compare_frames(source, target, key=["code", "company"], fields=["name"])
    assert result.stats.matched_records == 2
    assert result.stats.differing_records == 1
    assert result.stats.unkeyed_source_rows == 1  # Y has no company


def test_a_mangled_key_still_matches() -> None:
    """The key goes through the same normalisation as the fields."""
    source = pl.DataFrame({"code": ["7"], "name": ["Acme"]})
    target = pl.DataFrame({"code": ["007"], "name": ["Acme"]})
    result = compare_frames(source, target, key="code", fields=["name"])
    assert result.stats.matched_records == 1
    assert result.stats.differing_records == 0


def test_clean_is_true_only_when_everything_lines_up() -> None:
    """The one-line verdict used by --fail-on-difference."""
    frame = pl.DataFrame({"k": ["1"], "name": ["a"]})
    assert compare_frames(frame, frame, key="k", fields=["name"]).stats.clean
    other = pl.DataFrame({"k": ["2"], "name": ["a"]})
    assert not compare_frames(frame, other, key="k", fields=["name"]).stats.clean


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def test_missing_column_names_the_side_and_what_is_available() -> None:
    """An error has to say which side is missing what."""
    source = pl.DataFrame({"k": ["1"], "name": ["a"]})
    target = pl.DataFrame({"k": ["1"]})
    with pytest.raises(CompareError, match="target"):
        compare_frames(source, target, key="k", fields=["name"])


def test_an_empty_key_is_refused() -> None:
    """There is no meaningful comparison without a key."""
    frame = pl.DataFrame({"k": ["1"]})
    with pytest.raises(CompareError, match="at least one"):
        compare_frames(frame, frame, key=[], fields=[])


def test_apply_column_map_reports_an_unknown_column() -> None:
    """--map renames, and complains about a column the file lacks."""
    frame = pl.DataFrame({"BTW nummer": ["NL1"]})
    assert apply_column_map(frame, {"BTW nummer": "vat"}).columns == ["vat"]
    with pytest.raises(CompareError, match="Klantnaam"):
        apply_column_map(frame, {"Klantnaam": "name"})


# --------------------------------------------------------------------------
# Field specs from Odoo metadata
# --------------------------------------------------------------------------


def test_build_field_specs_maps_odoo_types_to_comparison_kinds() -> None:
    """fields_get metadata decides how each column is normalised."""
    odoo_fields = {
        "name": {"type": "char"},
        "qty": {"type": "float"},
        "active": {"type": "boolean"},
        "date_start": {"type": "date"},
        "country_id": {"type": "many2one"},
        "state": {"type": "selection", "selection": [("sale", "Confirmed")]},
    }
    specs = {
        s.name: s
        for s in build_field_specs([*odoo_fields, "country_id/id"], odoo_fields)
    }
    assert specs["name"].kind == "text"
    assert specs["qty"].kind == "numeric"
    assert specs["active"].kind == "boolean"
    assert specs["date_start"].kind == "date"
    assert specs["country_id"].kind == "relation"
    assert specs["state"].kind == "selection"
    assert specs["state"].selection == {"sale": "sale", "confirmed": "sale"}
    # An external-id specifier holds an xmlid, not a label.
    assert specs["country_id/id"].kind == "text"


def test_build_field_specs_falls_back_to_text_for_an_unknown_field() -> None:
    """A column Odoo does not declare is still comparable as text."""
    assert build_field_specs(["computed"], {})[0].kind == "text"


# --------------------------------------------------------------------------
# Reporting and the fix file
# --------------------------------------------------------------------------


def test_fix_frame_holds_normalised_source_values_keyed_by_external_id() -> None:
    """The fix file has to be loadable, so it carries canonical values."""
    source = pl.DataFrame({"vat": ["A1"], "active": ["ONWAAR"]})
    target = pl.DataFrame({"id": ["mod.a1"], "vat": ["A1"], "active": ["True"]})
    result = compare_frames(
        source, target, key="vat", fields=[FieldSpec("active", "boolean")]
    )
    fixes = build_fix_frame(result, target)
    # "0", not "ONWAAR": the file has to be loadable by `fluvo import`.
    assert fixes.to_dicts() == [{"id": "mod.a1", "active": "0"}]


def test_fix_frame_is_empty_when_nothing_differs() -> None:
    """Nothing to fix means an empty frame, not a crash."""
    frame = pl.DataFrame({"id": ["mod.a1"], "vat": ["A1"], "name": ["Acme"]})
    result = compare_frames(frame, frame, key="vat", fields=["name"])
    assert build_fix_frame(result, frame).is_empty()


def test_fix_frame_refuses_without_any_way_to_address_the_record() -> None:
    """Without an id there is no way to address the record."""
    source = pl.DataFrame({"vat": ["A1"], "name": ["a"]})
    target = pl.DataFrame({"vat": ["A1"], "name": ["b"]})
    result = compare_frames(source, target, key="vat", fields=["name"])
    with pytest.raises(CompareError, match="cannot be addressed"):
        build_fix_frame(result, target)


def test_fix_frame_falls_back_to_the_database_id() -> None:
    """A read-based export puts the database id in 'id'; that must not be loaded.

    Taking it at face value would make `fluvo import` read "10" as a new external
    id and create a duplicate, so the unambiguous '.id' is used instead.
    """
    source = pl.DataFrame({"vat": ["A1"], "name": ["a"]})
    target = pl.DataFrame({"id": ["10"], ".id": ["10"], "vat": ["A1"], "name": ["b"]})
    result = compare_frames(source, target, key="vat", fields=["name"])
    fixes = build_fix_frame(result, target)
    assert fixes.columns == [".id", "name"]
    assert fixes.to_dicts() == [{".id": "10", "name": "a"}]


def test_reports_render_and_hide_the_machine_facing_columns() -> None:
    """Humans see the four columns that matter; machines still get the rest."""
    source = pl.DataFrame({"vat": ["A1"], "name": ["a"]})
    target = pl.DataFrame({"vat": ["A1"], "name": ["b"]})
    result = compare_frames(source, target, key="vat", fields=["name"])
    markdown = to_markdown(result, "res.partner")
    assert "| vat | field | source_value | target_value |" in markdown
    assert "target_row" not in markdown
    payload = to_json(result, "res.partner")
    assert '"clean": false' in payload
    assert "target_row" in payload  # still there for machines


def test_markdown_says_so_when_there_is_nothing_to_report() -> None:
    """A clean comparison still produces a readable report."""
    frame = pl.DataFrame({"vat": ["A1"], "name": ["a"]})
    result = compare_frames(frame, frame, key="vat", fields=["name"])
    assert "None on the compared fields." in to_markdown(result, "res.partner")


# --------------------------------------------------------------------------
# Reading the source file
# --------------------------------------------------------------------------


def test_csv_is_read_as_text_so_nothing_is_mangled_twice(tmp_path) -> None:
    """Type inference on read would re-apply the mangling being looked for."""
    path = tmp_path / "rows.csv"
    path.write_text("code;qty\n007;1,50\n", encoding="utf-8")
    frame = read_source_file(str(path))
    assert frame.dtypes == [pl.String, pl.String]
    assert frame.row(0) == ("007", "1,50")


def test_excel_round_trip(tmp_path) -> None:
    """A workbook reads back as text, leading zeros intact."""
    pytest.importorskip("fastexcel")
    pytest.importorskip("xlsxwriter")
    path = tmp_path / "rows.xlsx"
    pl.DataFrame({"code": ["007"], "qty": [1.5]}).write_excel(str(path))
    frame = read_source_file(str(path))
    assert frame.dtypes == [pl.String, pl.String]
    assert frame.row(0)[0] == "007"


def test_unreadable_file_is_reported_with_its_path(tmp_path) -> None:
    """A read failure names the file."""
    with pytest.raises(CompareError, match="Could not read"):
        read_source_file(str(tmp_path / "missing.csv"))
