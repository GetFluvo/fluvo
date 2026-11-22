"""Tests for the console RunReporter adapter (issue #363)."""

from io import StringIO

from rich.console import Console

from fluvo.lib.core import Level, ReconStats, RunReporter
from fluvo.lib.internal.reporter import ConsoleReporter


def _reporter(*, quiet: bool = False) -> tuple[ConsoleReporter, StringIO]:
    """A ConsoleReporter whose console writes to a capturable buffer."""
    buf = StringIO()
    reporter = ConsoleReporter(quiet=quiet)
    # Swap the stderr console for one writing to a buffer, so output is asserted
    # deterministically without depending on a tty.
    reporter._console = Console(file=buf, force_terminal=False, width=200)
    return reporter, buf


def test_satisfies_the_reporter_protocol() -> None:
    """ConsoleReporter is structurally a RunReporter."""
    assert isinstance(ConsoleReporter(), RunReporter)


def test_on_phase_prints_the_name() -> None:
    """A phase name reaches the console."""
    reporter, buf = _reporter()
    reporter.on_phase("connecting")
    assert "connecting" in buf.getvalue()


def test_quiet_suppresses_phase_and_progress() -> None:
    """Quiet silences phase and progress, but not warnings/errors."""
    reporter, buf = _reporter(quiet=True)
    reporter.on_phase("connecting")
    reporter.on_progress(1, 3)
    reporter.on_message(Level.INFO, "fyi")
    assert buf.getvalue() == ""
    reporter.on_message(Level.WARNING, "careful")
    reporter.on_message(Level.ERROR, "boom")
    out = buf.getvalue()
    assert "careful" in out
    assert "boom" in out


def test_on_progress_shows_done_over_total() -> None:
    """A known total renders as done/total under the current phase."""
    reporter, buf = _reporter()
    reporter.on_phase("assessing")
    reporter.on_progress(2, 5)
    out = buf.getvalue()
    assert "2/5" in out
    assert "assessing" in out


def test_on_progress_appends_stats_when_present() -> None:
    """Running reconciliation counters are appended when given."""
    reporter, buf = _reporter()
    reporter.on_progress(5, 10, ReconStats(total=10, created=4, failed=1))
    out = buf.getvalue()
    assert "5/10" in out
    assert "4 created" in out
    assert "1 failed" in out


def test_on_progress_unknown_total_prints_nothing() -> None:
    """A non-positive total means 'unknown', so nothing is printed."""
    reporter, buf = _reporter()
    reporter.on_progress(3, 0)
    assert buf.getvalue() == ""


def test_on_message_levels() -> None:
    """info/warning/error each surface their text."""
    reporter, buf = _reporter()
    reporter.on_message(Level.INFO, "plain")
    reporter.on_message(Level.WARNING, "warn")
    reporter.on_message(Level.ERROR, "err")
    out = buf.getvalue()
    assert "plain" in out
    assert "warn" in out
    assert "err" in out
