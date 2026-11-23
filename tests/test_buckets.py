"""Tests for the failure-bucket classifier (#299)."""

from pathlib import Path

from fluvo.lib import buckets


class TestClassifyFailure:
    """The pure (error_reason) -> bucket classifier."""

    def test_missing_external_id_is_blocked_on_missing_data(self) -> None:
        """An unresolved external id is blocked on missing reference data."""
        reason = "No matching record found for external id 'mod.parent' in field 'x'"
        assert buckets.classify_failure(reason) == buckets.BLOCKED_MISSING_DATA

    def test_missing_required_is_blocked_on_missing_data(self) -> None:
        """A missing required value is blocked on missing data."""
        assert (
            buckets.classify_failure("Missing required value for field 'name'")
            == buckets.BLOCKED_MISSING_DATA
        )

    def test_posted_move_is_a_client_decision(self) -> None:
        """A posted, immutable move needs a person to decide (#358)."""
        assert (
            buckets.classify_failure("Entry is already posted and cannot be modified")
            == buckets.CLIENT_DECISION
        )

    def test_number_conversion_is_fluvo_fixable(self) -> None:
        """A number-format failure is something Fluvo can propose a fix for."""
        assert (
            buckets.classify_failure("invalid literal for int() with base 10: '1E+11'")
            == buckets.FLUVO_FIXABLE
        )

    def test_unknown_reason_is_unclassified(self) -> None:
        """An unrecognised reason falls through to the honest default."""
        assert (
            buckets.classify_failure("Some constraint violated in a novel way")
            == buckets.UNCLASSIFIED
        )

    def test_empty_or_none_is_unclassified(self) -> None:
        """An empty or missing reason is unclassified, never a crash."""
        assert buckets.classify_failure("") == buckets.UNCLASSIFIED
        assert buckets.classify_failure(None) == buckets.UNCLASSIFIED

    def test_classification_is_case_insensitive(self) -> None:
        """Matching ignores case so upper-cased reasons still classify."""
        assert (
            buckets.classify_failure("NO MATCHING RECORD FOUND for id")
            == buckets.BLOCKED_MISSING_DATA
        )


class TestBucketCountsFromFailFile:
    """The decoupled, post-hoc tally over a finished fail file."""

    def test_counts_from_bucket_column_when_present(self, tmp_path: Path) -> None:
        """When _FLUVO_BUCKET is present it is tallied directly."""
        ff = tmp_path / "fail.csv"
        ff.write_text(
            "id;name;_ERROR_REASON;_FLUVO_BUCKET\n"
            "1;a;No matching record found;blocked_missing_data\n"
            "2;b;No matching record found;blocked_missing_data\n"
            "3;c;weird;unclassified\n"
        )
        counts = buckets.bucket_counts_from_fail_file(str(ff), separator=";")
        assert counts == {"blocked_missing_data": 2, "unclassified": 1}

    def test_falls_back_to_classifying_reason(self, tmp_path: Path) -> None:
        """Without a bucket column, each row's _ERROR_REASON is classified."""
        ff = tmp_path / "fail.csv"
        ff.write_text(
            "id;name;_ERROR_REASON\n"
            "1;a;No matching record found for external id\n"
            "2;b;a brand new constraint\n"
        )
        counts = buckets.bucket_counts_from_fail_file(str(ff), separator=";")
        assert counts == {"blocked_missing_data": 1, "unclassified": 1}

    def test_counts_sum_to_data_rows(self, tmp_path: Path) -> None:
        """Every data row is counted exactly once, so counts sum to the total."""
        ff = tmp_path / "fail.csv"
        ff.write_text(
            "id;_ERROR_REASON;_FLUVO_BUCKET\n"
            "1;x;blocked_missing_data\n"
            "2;y;client_decision\n"
            "3;z;unclassified\n"
        )
        counts = buckets.bucket_counts_from_fail_file(str(ff), separator=";")
        assert sum(counts.values()) == 3

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """A missing or unreadable fail file tallies to nothing, never raises."""
        assert buckets.bucket_counts_from_fail_file(str(tmp_path / "nope.csv")) == {}
