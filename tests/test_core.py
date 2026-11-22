"""Tests for the headless core contract (issue #363)."""

import threading
from dataclasses import FrozenInstanceError

import pytest

from fluvo.lib.compare import CompareError
from fluvo.lib.core import (
    CancelToken,
    ConnectError,
    FluvoError,
    InputError,
    NothingToDoError,
    NullReporter,
    OperationCancelledError,
    ReconStats,
    RunError,
    RunReporter,
    RunResult,
    RunStatus,
    SourceReadError,
)

# ---------------------------------------------------------------------------
# ReconStats — the core identity
# ---------------------------------------------------------------------------


def test_default_stats_are_reconciled() -> None:
    """All-zero counters satisfy the identity trivially."""
    assert ReconStats().reconciled


def test_reconciled_holds_when_buckets_sum_to_total() -> None:
    """Buckets summing to total means reconciled."""
    assert ReconStats(total=10, created=6, failed=3, unaccounted=1).reconciled


def test_reconciled_fails_on_a_silent_remainder() -> None:
    """A row in no bucket breaks the identity — the thing it exists to catch."""
    assert not ReconStats(total=10, created=6, failed=3, unaccounted=0).reconciled


def test_read_only_run_leaves_default_zero_stats() -> None:
    """A read-only capability writes nothing; stats stay at the reconciled default."""
    assert ReconStats().reconciled
    assert ReconStats() == RunResult(status=RunStatus.OK).stats


def test_counting_rows_as_read_keeps_the_identity() -> None:
    """Rows counted but not written still belong in a bucket, never a bare total."""
    # total must equal the buckets — a bare total with empty buckets is the bug above.
    assert ReconStats(total=500, unaccounted=500).reconciled
    assert not ReconStats(total=500).reconciled


# ---------------------------------------------------------------------------
# RunResult
# ---------------------------------------------------------------------------


def test_result_ok_only_for_ok_status() -> None:
    """Result.ok is True only for RunStatus.OK."""
    assert RunResult(status=RunStatus.OK).ok
    for status in (RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PARTIAL):
        assert not RunResult(status=status).ok


def test_result_defaults_are_empty_not_shared() -> None:
    """Mutable defaults are per-instance, not shared class state."""
    a = RunResult(status=RunStatus.OK)
    b = RunResult(status=RunStatus.OK)
    a.artifacts["fail_file"] = "out/fail.csv"
    a.errors.append(RunError(kind="InputError", message="x"))
    assert b.artifacts == {}
    assert b.errors == []


def test_run_status_serialises_as_its_value() -> None:
    """A str enum carries its wire value for any consumer."""
    assert RunStatus.CANCELLED.value == "cancelled"
    assert RunStatus("partial") is RunStatus.PARTIAL


# ---------------------------------------------------------------------------
# RunError
# ---------------------------------------------------------------------------


def test_run_error_of_captures_kind_and_message() -> None:
    """RunError.of records the subclass name and message, not the instance."""
    rec = RunError.of(SourceReadError("could not read the workbook"))
    assert rec.kind == "SourceReadError"
    assert rec.message == "could not read the workbook"


def test_run_error_is_frozen() -> None:
    """A captured error record is immutable."""
    rec = RunError(kind="InputError", message="bad")
    with pytest.raises(FrozenInstanceError):
        rec.message = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


def test_subclasses_are_fluvo_errors() -> None:
    """Every taxonomy member is a FluvoError, hence a RuntimeError."""
    taxonomy = (
        InputError,
        ConnectError,
        SourceReadError,
        NothingToDoError,
        OperationCancelledError,
    )
    for exc_type in taxonomy:
        assert issubclass(exc_type, FluvoError)
        assert issubclass(exc_type, RuntimeError)


def test_default_exit_code_is_one() -> None:
    """The CLI adapter's default mapping is a non-zero exit."""
    assert FluvoError().exit_code == 1
    assert InputError().exit_code == 1


def test_connect_error_does_not_shadow_builtin() -> None:
    """ConnectError is deliberately not, and does not subclass, the built-in."""
    assert ConnectError.__name__ == "ConnectError"
    assert not issubclass(ConnectError, ConnectionError)


def test_compare_error_joined_the_taxonomy() -> None:
    """CompareError now subclasses FluvoError without ceasing to be a RuntimeError."""
    assert issubclass(CompareError, FluvoError)
    assert issubclass(CompareError, RuntimeError)
    # Existing handlers that catch CompareError keep working.
    with pytest.raises(CompareError):
        raise CompareError("boom")


# ---------------------------------------------------------------------------
# CancelToken
# ---------------------------------------------------------------------------


def test_token_starts_uncancelled() -> None:
    """A fresh token is not cancelled."""
    assert not CancelToken().cancelled


def test_cancel_is_observable_and_idempotent() -> None:
    """cancel() sets the flag and may be called repeatedly."""
    token = CancelToken()
    token.cancel()
    token.cancel()
    assert token.cancelled


def test_raise_if_cancelled() -> None:
    """raise_if_cancelled raises only after cancel()."""
    token = CancelToken()
    token.raise_if_cancelled()  # no-op
    token.cancel()
    with pytest.raises(OperationCancelledError):
        token.raise_if_cancelled()


def test_wait_returns_false_on_timeout() -> None:
    """wait() reports a timeout as False when never cancelled."""
    assert CancelToken().wait(timeout=0.01) is False


def test_cancel_from_another_thread_unblocks_wait() -> None:
    """Any thread may cancel; a waiter wakes promptly — the whole point."""
    token = CancelToken()
    threading.Timer(0.02, token.cancel).start()
    assert token.wait(timeout=2.0) is True
    assert token.cancelled


# ---------------------------------------------------------------------------
# Reporters
# ---------------------------------------------------------------------------


def test_null_reporter_satisfies_protocol_and_is_silent() -> None:
    """NullReporter is a RunReporter and every method is a harmless no-op."""
    reporter = NullReporter()
    assert isinstance(reporter, RunReporter)
    reporter.on_phase("reading source")
    reporter.on_progress(1, 10)
    reporter.on_progress(5, 10, ReconStats(total=10, created=5))
    reporter.on_message("info", "halfway")


def test_a_recording_adapter_also_satisfies_the_protocol() -> None:
    """Any object with the three methods is a RunReporter (structural typing)."""

    class Recording:
        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []

        def on_phase(self, name: str) -> None:
            self.events.append(("phase", name))

        def on_progress(
            self, done: int, total: int, stats: ReconStats | None = None
        ) -> None:
            self.events.append(("progress", (done, total)))

        def on_message(self, level: str, text: str) -> None:
            self.events.append(("message", (level, text)))

    rec = Recording()
    assert isinstance(rec, RunReporter)
    rec.on_phase("x")
    assert rec.events == [("phase", "x")]
