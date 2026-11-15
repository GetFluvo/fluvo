"""Tests for the Excel-mangling detectors and fixers (issue #332).

All frames are synthetic -- the patterns mirror real observed shapes, but no
client data is copied in. No Odoo connection is used anywhere.
"""

import polars as pl
import pytest

from fluvo.lib.mangling import (
    ANGLO,
    DUTCH,
    MangleRefusalError,
    detect_case_duplicates,
    detect_normalization_collisions,
    detect_scientific_notation,
    detect_trailing_zero_loss,
    fix_scientific_notation,
    fix_trailing_zero_loss,
)

# --------------------------------------------------------------------------- #
# Pattern 1: scientific notation
# --------------------------------------------------------------------------- #


def test_sci_detect_and_recover_from_sibling() -> None:
    """A sci value is detected and recovered from its truth-bearing sibling."""
    df = pl.DataFrame(
        {
            "external_id": ["1130102001401", "1130102002100", "920871025"],
            "ref": ["1,1301E+12", "1,1301E+12", "920871,025"],
        }
    )
    det = detect_scientific_notation(
        df, "ref", recovery_column="external_id", locale=DUTCH
    )
    assert det.height == 2  # the plain decimal row is not scientific notation
    assert det.get_column("recoverable").to_list() == [True, True]
    assert det.get_column("recovery_value").to_list() == [
        "1130102001401",
        "1130102002100",
    ]

    fixed = fix_scientific_notation(df, det, "ref")
    assert fixed.get_column("ref").to_list() == [
        "1130102001401",
        "1130102002100",
        "920871,025",
    ]
    # schema preserved, no helper columns leak out
    assert fixed.columns == df.columns


def test_sci_detect_anglo_locale() -> None:
    """With a dot decimal separator the mantissa dot is recognised."""
    df = pl.DataFrame({"external_id": ["8603635009665"], "ref": ["8.60364E+12"]})
    det = detect_scientific_notation(
        df, "ref", recovery_column="external_id", locale=ANGLO
    )
    assert det.height == 1
    # A comma-decimal reader would NOT match this value.
    assert detect_scientific_notation(df, "ref", "external_id", DUTCH).height == 0


def test_sci_no_recovery_column_refuses() -> None:
    """A sci value with no column of truth is flagged unrecoverable and refused."""
    df = pl.DataFrame({"ref": ["1,1301E+12", "ok-123"]})
    det = detect_scientific_notation(df, "ref", recovery_column=None, locale=DUTCH)
    assert det.height == 1
    assert det.get_column("recoverable").to_list() == [False]

    with pytest.raises(MangleRefusalError, match="no recovery column"):
        fix_scientific_notation(df, det, "ref")


def test_sci_empty_recovery_value_is_unrecoverable() -> None:
    """A blank sibling value does not count as a source of truth."""
    df = pl.DataFrame({"external_id": [""], "ref": ["2,61101E+11"]})
    det = detect_scientific_notation(
        df, "ref", recovery_column="external_id", locale=DUTCH
    )
    assert det.get_column("recoverable").to_list() == [False]
    with pytest.raises(MangleRefusalError):
        fix_scientific_notation(df, det, "ref")


# --------------------------------------------------------------------------- #
# Pattern 2: trailing-zero loss
# --------------------------------------------------------------------------- #


def test_trailing_zero_detect_and_repad_dutch() -> None:
    """A family corroborates width 4; a short sibling is re-padded with zeros."""
    df = pl.DataFrame(
        {
            "ref": [
                "922473,007",  # width 3 -> needs one zero
                "922473,0070",  # width 4 (corroborates)
                "922473,0320",  # width 4 (corroborates)
            ]
        }
    )
    det = detect_trailing_zero_loss(df, "ref", locale=DUTCH, min_corroboration=2)
    assert det.height == 1
    row = det.row(0, named=True)
    assert row["normalized"] == "922473.007"
    assert row["suggested"] == "922473.0070"
    assert row["family_width"] == 4
    assert row["missing_zeros"] == 1
    assert row["ambiguous"] is False
    assert row["bucket_id"] == "tzl:len6"  # family keyed by prefix length

    fixed = fix_trailing_zero_loss(df, det, "ref")
    assert fixed.get_column("ref").to_list() == [
        "922473.0070",
        "922473,0070",
        "922473,0320",
    ]
    assert fixed.columns == df.columns


def test_trailing_zero_multiple_missing_zeros() -> None:
    """A suffix two zeros short is padded to the full family width."""
    df = pl.DataFrame({"ref": ["6000,2", "6000,090", "6000,630", "6000,170"]})
    det = detect_trailing_zero_loss(df, "ref", locale=DUTCH, min_corroboration=2)
    row = det.filter(pl.col("value") == "6000,2").row(0, named=True)
    assert row["family_width"] == 3
    assert row["missing_zeros"] == 2
    assert row["suggested"] == "6000.200"


def test_trailing_zero_detect_anglo_locale() -> None:
    """With a dot decimal, the comma convention does not apply."""
    df = pl.DataFrame({"ref": ["6000.09", "6000.090", "6000.170"]})
    det = detect_trailing_zero_loss(df, "ref", locale=ANGLO, min_corroboration=2)
    assert det.height == 1
    assert det.get_column("suggested").to_list() == ["6000.090"]
    # Read with the Dutch (comma) convention, none of these parse as decimals.
    assert detect_trailing_zero_loss(df, "ref", locale=DUTCH).height == 0


def test_trailing_zero_tied_width_is_ambiguous_and_refused() -> None:
    """When two widths tie for most common, the family width is ambiguous."""
    # prefix length 3, widths {1, 2} each seen once -> no modal winner.
    df = pl.DataFrame({"ref": ["834,5", "834,50"]})
    det = detect_trailing_zero_loss(df, "ref", locale=DUTCH, min_corroboration=2)
    assert det.height == 1  # only the short one is a candidate
    assert det.get_column("ambiguous").to_list() == [True]

    with pytest.raises(MangleRefusalError, match="uncorroborated"):
        fix_trailing_zero_loss(df, det, "ref")


def test_trailing_zero_ambiguous_skip_leaves_value_untouched() -> None:
    """on_ambiguous='skip' re-pads confident families and leaves ambiguous ones."""
    df = pl.DataFrame(
        {
            "ref": [
                "834,5",  # prefix-length-3 family: tied widths -> ambiguous
                "834,50",
                "9000,1",  # prefix-length-4 family: clear mode 3 -> confident
                "9000,100",
                "9000,250",
            ]
        }
    )
    det = detect_trailing_zero_loss(df, "ref", locale=DUTCH, min_corroboration=2)
    fixed = fix_trailing_zero_loss(df, det, "ref", on_ambiguous="skip")
    out = fixed.get_column("ref").to_list()
    assert out[0] == "834,5"  # ambiguous family: untouched
    assert out[2] == "9000.100"  # confident family: re-padded


def test_trailing_zero_corroboration_gate() -> None:
    """The modal width must clear ``min_corroboration`` members to be trusted."""
    # prefix length 3: width 2 is the clear mode (support 2), width 1 is short.
    df = pl.DataFrame({"ref": ["800,50", "800,60", "800,5"]})
    confident = detect_trailing_zero_loss(df, "ref", locale=DUTCH, min_corroboration=2)
    short = confident.filter(pl.col("value") == "800,5").row(0, named=True)
    assert short["ambiguous"] is False
    assert short["suggested"] == "800.50"

    # Demand three corroborators and the same modal width is no longer trusted.
    cautious = detect_trailing_zero_loss(df, "ref", locale=DUTCH, min_corroboration=3)
    assert cautious.filter(pl.col("value") == "800,5").row(0, named=True)["ambiguous"]


def test_trailing_zero_multi_separator_is_not_a_candidate() -> None:
    """A field-separator value (pattern 3) is never read as a decimal."""
    df = pl.DataFrame({"ref": ["000,630,2226", "000,630,2190"]})
    det = detect_trailing_zero_loss(df, "ref", locale=DUTCH)
    assert det.is_empty()


# --------------------------------------------------------------------------- #
# Pattern 3: normalisation collisions (no fixer)
# --------------------------------------------------------------------------- #


def test_normalization_collision_groups_distinct_sources() -> None:
    """Two distinct raw values folding to one key are grouped as a collision."""
    df = pl.DataFrame(
        {
            "ref": [
                "000,630,2226",  # comma as field separator
                "000.630.2226",  # already dotted -> same normalised key
                "000,630,2190",  # unique, not a collision
            ]
        }
    )
    det = detect_normalization_collisions(df, "ref", locale=DUTCH)
    assert det.height == 2
    assert det.get_column("bucket_id").unique().to_list() == ["ncol:000.630.2226"]
    assert "000,630,2190" not in det.get_column("value").to_list()


def test_normalization_collision_folds_separators() -> None:
    """A comma-separated identifier and its dotted twin collide on one key.

    Normalisation folds the structural separators to a canonical dot (the exact
    transform the ground-truth review applied: ``000,630,2226`` -> dotted), so
    distinct raw spellings of the same identifier are grouped for a human.
    """
    df = pl.DataFrame({"ref": ["1,234,567", "1.234.567", "1,234,568"]})
    det = detect_normalization_collisions(df, "ref", locale=DUTCH)
    assert det.height == 2
    assert det.get_column("bucket_id").unique().to_list() == ["ncol:1.234.567"]


def test_normalization_matches_ground_truth_transform() -> None:
    """The normalised key reproduces the reviewer's comma->dot output exactly."""
    df = pl.DataFrame({"ref": ["000,630,2226", "000.630.2226"]})
    det = detect_normalization_collisions(df, "ref", locale=DUTCH)
    assert det.get_column("normalized").unique().to_list() == ["000.630.2226"]


def test_normalization_no_collision_when_all_distinct() -> None:
    """Distinct normalised keys produce no collision rows."""
    df = pl.DataFrame({"ref": ["000,630,2226", "000,630,2190", "000,630,2225"]})
    det = detect_normalization_collisions(df, "ref", locale=DUTCH)
    assert det.is_empty()


# --------------------------------------------------------------------------- #
# Pattern 4: case-only duplicates (no fixer)
# --------------------------------------------------------------------------- #


def test_case_duplicates_grouped() -> None:
    """References differing only in case are grouped under a lowercased bucket."""
    df = pl.DataFrame(
        {"default_code": ["t27056298", "T27056298", "t28040722", "unique999"]}
    )
    det = detect_case_duplicates(df, "default_code")
    assert det.height == 2  # only the t/T27056298 pair
    assert det.get_column("bucket_id").unique().to_list() == ["case:t27056298"]
    assert "unique999" not in det.get_column("value").to_list()


def test_case_duplicates_none_when_unique() -> None:
    """No case collisions yields an empty detection."""
    df = pl.DataFrame({"default_code": ["abc", "def", "ghi"]})
    assert detect_case_duplicates(df, "default_code").is_empty()


# --------------------------------------------------------------------------- #
# Cross-cutting guarantees
# --------------------------------------------------------------------------- #


def test_detectors_do_not_mutate_input() -> None:
    """Every detector is pure: the caller's frame is never changed."""
    df = pl.DataFrame(
        {
            "external_id": ["1130102001401"],
            "ref": ["1,1301E+12"],
        }
    )
    snapshot = df.clone()
    detect_scientific_notation(df, "ref", "external_id", DUTCH)
    detect_trailing_zero_loss(df, "ref", DUTCH)
    detect_normalization_collisions(df, "ref", DUTCH)
    detect_case_duplicates(df, "ref")
    assert df.equals(snapshot)


def test_bucket_ids_are_stable_and_namespaced() -> None:
    """Bucket ids are deterministic and carry a per-pattern namespace prefix."""
    df = pl.DataFrame(
        {
            "external_id": ["1130102001401"],
            "ref": ["1,1301E+12"],
        }
    )
    first = detect_scientific_notation(df, "ref", "external_id", DUTCH)
    second = detect_scientific_notation(df, "ref", "external_id", DUTCH)
    assert (
        first.get_column("bucket_id").to_list()
        == second.get_column("bucket_id").to_list()
    )
    assert first.get_column("bucket_id").to_list() == ["sci:1130102001401"]


def test_empty_frame_detection_is_empty_not_error() -> None:
    """Detectors tolerate an empty input column without raising."""
    df = pl.DataFrame({"ref": []}, schema={"ref": pl.String})
    assert detect_scientific_notation(df, "ref").is_empty()
    assert detect_trailing_zero_loss(df, "ref").is_empty()
    assert detect_normalization_collisions(df, "ref").is_empty()
    assert detect_case_duplicates(df, "ref").is_empty()


def test_fixers_on_empty_detection_return_input_unchanged() -> None:
    """An empty detection is a no-op for the fixers."""
    df = pl.DataFrame({"ref": ["fine-001", "fine-002"]})
    empty_sci = detect_scientific_notation(df, "ref")
    empty_tzl = detect_trailing_zero_loss(df, "ref")
    assert fix_scientific_notation(df, empty_sci, "ref").equals(df)
    assert fix_trailing_zero_loss(df, empty_tzl, "ref").equals(df)


def test_fix_trailing_zero_rejects_bad_on_ambiguous() -> None:
    """A bad on_ambiguous value fails fast rather than silently."""
    df = pl.DataFrame({"ref": ["900,1", "900,100", "900,250"]})
    det = detect_trailing_zero_loss(df, "ref", DUTCH)
    with pytest.raises(ValueError, match="on_ambiguous"):
        fix_trailing_zero_loss(df, det, "ref", on_ambiguous="yolo")
