"""The headless core contract (issue #363).

A small set of types that let Fluvo's capabilities be driven by consumers other
than the terminal — a server process, an automation layer, a graphical UI —
without re-implementing orchestration or scraping the CLI's human-facing output.

The command line becomes *one* presentation adapter over these types; it is not
privileged. Nothing in this module imports ``rich``, ``typer`` or anything else
presentation-specific: the whole point is that the core can run with no console
attached. Adapters (a rich reporter for the CLI, a notification reporter for a
server, a queue/stream reporter for a UI) live next to their consumer, not here.

The contract has four parts:

* :class:`RunResult` + :class:`ReconStats` — the typed outcome every capability
  returns, carrying the reconciliation counters the whole test suite asserts.
* :class:`RunReporter` — how the core reports progress, without knowing who is
  listening. :class:`NullReporter` is the do-nothing default.
* :class:`CancelToken` — an externally settable stop signal for long-running
  work, replacing "the only way to stop is Ctrl+C on the main thread".
* :class:`FluvoError` and its subclasses — a typed error taxonomy, so no
  consumer has to read a ``None`` return as "fatal".

This module only defines the vocabulary. Migrating the existing capabilities to
speak it (``assess`` and ``compare`` first, the import engine later) is tracked
as follow-on work on the same issue.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FluvoError(RuntimeError):
    """Base for every error the core raises deliberately.

    Consumers map these to their own channel: the CLI to an error panel and a
    non-zero exit, a server to a tool error, a UI to a surfaced message. No
    consumer should have to interpret a ``None`` return as "fatal".

    ``exit_code`` is the process status the CLI adapter uses when the error
    reaches the top level; other adapters are free to ignore it.
    """

    exit_code: int = 1


class InputError(FluvoError):
    """The caller's request is malformed, incomplete or self-contradictory.

    A usage problem, not an environment one — the same call would fail again.
    """


class ConnectError(FluvoError):
    """Could not reach or authenticate against a target Odoo.

    Named ``ConnectError`` rather than ``ConnectionError`` on purpose, so it
    never shadows the built-in :class:`ConnectionError`.
    """


class SourceReadError(FluvoError):
    """A source — a file or a second Odoo instance — could not be read."""


class NothingToDoError(FluvoError):
    """The request is valid but resolves to no work.

    For example, no models match the selection, or none can be read on the
    target. Distinct from an error: nothing went wrong, there is just nothing
    to do, and the caller usually wants to say so rather than crash.
    """


class OperationCancelledError(FluvoError):
    """Raised internally to unwind long-running work once cancelled.

    The *top-level* contract is to return a reconciled :class:`RunResult` with
    :attr:`RunStatus.CANCELLED`, not to propagate this out of a capability; it
    exists so a deep worker can stop promptly and let the orchestrator settle
    the result.
    """


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):
    """How a capability run ended.

    A ``str`` enum so the value serialises as its name for any consumer.
    """

    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


class Level(str, Enum):
    """Severity of a :meth:`RunReporter.on_message`, as a fixed set.

    A ``str`` enum, so a member both serialises as its value and compares equal
    to that string — an adapter may branch on ``level == "error"`` or on
    ``level is Level.ERROR`` interchangeably.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class ReconStats:
    """The reconciliation counters, carrying Fluvo's core identity.

    ``created + failed + unaccounted == total`` holds *by construction* — it is
    the invariant the end-to-end suite asserts on every run (see
    ``tests/e2e/assertions.py``). It is a property of the *write* path: every
    row the engine is accountable for lands in exactly one bucket. Read-only
    capabilities (``assess``, ``compare``) do not write, so they leave these
    counters at their default of zero (which reconciles trivially) and report
    what they found through their own result types; the import engine populates
    all four.

    Keeping these as one type means live progress and the final result speak
    the same counters: a UI can show a moving total that simply settles.

    Attributes:
        total: Every row the run is accountable for.
        created: Rows written to the target.
        failed: Rows that could not be written and were recorded as failures.
            This includes a re-run's rows aimed at an already posted or
            cancelled ``account.move``: the load refuses them before any write
            (#358/#359) and records them in the fail file, because Odoo itself
            does not protect a posted move over RPC.
        unaccounted: Rows accounted for by an explicit reason other than
            created or failed — duplicates, records skipped as unchanged in
            idempotent mode, or a move already posted on a post-step re-run.
            Never a silent remainder: the point of the bucket is that it is
            named and asserted, not inferred.
        reasons: The named breakdown of ``unaccounted`` — e.g.
            ``{"already_posted": 3}`` for moves a post-step re-run left alone
            because they were already posted (#358). Whatever ``unaccounted``
            holds beyond these named reasons is :attr:`unexplained`.
    """

    total: int = 0
    created: int = 0
    failed: int = 0
    unaccounted: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def unexplained(self) -> int:
        """The part of ``unaccounted`` not covered by a named reason.

        Returns:
            int: ``unaccounted`` minus the sum of :attr:`reasons`.
        """
        return self.unaccounted - sum(self.reasons.values())

    @property
    def reconciled(self) -> bool:
        """Whether the core identity holds.

        Returns:
            bool: True when ``created + failed + unaccounted == total``.
        """
        return self.created + self.failed + self.unaccounted == self.total


@dataclass(frozen=True)
class RunError:
    """A typed, serialisable record of one error in a result.

    Holds the error's kind and message rather than a live exception instance,
    so a result can cross a process or protocol boundary unchanged.

    Attributes:
        kind: The :class:`FluvoError` subclass name (e.g. ``"SourceReadError"``).
        message: The human-readable message.
    """

    kind: str
    message: str

    @classmethod
    def of(cls, exc: FluvoError) -> RunError:
        """Build a record from a raised error.

        Args:
            exc: The error to capture.

        Returns:
            RunError: A record carrying the error's class name and message.
        """
        return cls(kind=type(exc).__name__, message=str(exc))


@dataclass
class RunResult:
    """The typed outcome of any capability run.

    One shape for every consumer. The CLI renders it; a server returns it; a UI
    displays it. Because progress and the final result share
    :class:`ReconStats`, a long-running run's live numbers and its settled
    numbers are the same type.

    Attributes:
        status: How the run ended.
        stats: The reconciliation counters.
        artifacts: Named paths the run produced — e.g. ``"fail_file"``,
            ``"checkpoint"``, ``"report"`` — mapping a name to a filesystem
            path, so a consumer can offer them without guessing locations.
        errors: Typed error records gathered during the run. A ``FAILED`` run
            carries at least one; a ``PARTIAL`` run may carry several while
            still having created rows.
    """

    status: RunStatus
    stats: ReconStats = field(default_factory=ReconStats)
    artifacts: dict[str, str] = field(default_factory=dict)
    errors: list[RunError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the run ended cleanly.

        Returns:
            bool: True only when :attr:`status` is :attr:`RunStatus.OK`.
        """
        return self.status is RunStatus.OK


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


@runtime_checkable
class RunReporter(Protocol):
    """How the core reports progress, without knowing who is listening.

    The core calls these methods; an adapter decides what they mean. The CLI
    supplies a ``rich``-backed adapter, a server a notification adapter, a UI a
    queue or stream adapter. Methods must never raise: reporting is a
    side-channel and must not be able to fail a run.
    """

    def on_phase(self, name: str) -> None:
        """Announce that the run has entered a named phase.

        Args:
            name: A short, stable phase label (e.g. ``"reading source"``).
        """

    def on_progress(
        self, done: int, total: int, stats: ReconStats | None = None
    ) -> None:
        """Report incremental progress within the current phase.

        Args:
            done: Units completed so far.
            total: Units expected in total; may be zero when unknown.
            stats: The running reconciliation counters, when the phase produces
                them (the import engine); None for phases that do not.
        """

    def on_message(self, level: Level, text: str) -> None:
        """Surface a human-readable message.

        Args:
            level: The message severity.
            text: The message.
        """


class NullReporter:
    """A :class:`RunReporter` that discards everything.

    The default when a caller supplies no reporter — the core can always report
    unconditionally without checking for ``None``.
    """

    def on_phase(self, name: str) -> None:
        """Discard a phase announcement.

        Args:
            name: Ignored.
        """

    def on_progress(
        self, done: int, total: int, stats: ReconStats | None = None
    ) -> None:
        """Discard a progress report.

        Args:
            done: Ignored.
            total: Ignored.
            stats: Ignored.
        """

    def on_message(self, level: Level, text: str) -> None:
        """Discard a message.

        Args:
            level: Ignored.
            text: Ignored.
        """


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class CancelToken:
    """An externally settable stop signal for long-running work.

    Replaces "the only way to stop is Ctrl+C on the main thread": a worker
    checks :attr:`cancelled` (or calls :meth:`raise_if_cancelled`) between units
    of work, and *any* thread may call :meth:`cancel`. Backed by a
    :class:`threading.Event`, so it is safe to set and read across threads.

    The CLI's signal handler becomes a call to :meth:`cancel`; a server or UI
    cancels from its own thread. Safe-cancel semantics (stop dispatching new
    work, let in-flight work finish, persist the checkpoint, return a reconciled
    result with :attr:`RunStatus.CANCELLED`) are the orchestrator's job; this
    type only carries the signal.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation. Idempotent and callable from any thread."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested.

        Returns:
            bool: True once :meth:`cancel` has been called.
        """
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`OperationCancelledError` if cancellation was requested.

        Raises:
            OperationCancelledError: When :meth:`cancel` has been called.
        """
        if self._event.is_set():
            raise OperationCancelledError("The operation was cancelled.")

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancelled or the timeout elapses.

        Lets a worker sleep (e.g. between retries) while staying promptly
        cancellable, instead of a bare ``time.sleep``.

        Args:
            timeout: Maximum seconds to wait; None waits indefinitely.

        Returns:
            bool: True if cancellation was requested, False if the timeout elapsed.
        """
        return self._event.wait(timeout)
