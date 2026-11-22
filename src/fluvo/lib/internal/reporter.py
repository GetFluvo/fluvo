"""Console adapter for the core :class:`~fluvo.lib.core.RunReporter` (issue #363).

The CLI's progress sink. It writes to **stderr**, leaving stdout for a
command's actual result (a report, JSON, a table), so progress never corrupts
piped output — the same discipline a stdio line-protocol consumer will need.

The library core (``assess``, ``compare``, later the import engine) only knows
the :class:`~fluvo.lib.core.RunReporter` protocol; this is one implementation of
it. A server or GUI supplies its own.
"""

from __future__ import annotations

from rich.console import Console

from ..core import Level, ReconStats


class ConsoleReporter:
    """A :class:`~fluvo.lib.core.RunReporter` that prints progress to stderr.

    Phase and message lines are shown; progress is shown only when a total is
    known, as an unobtrusive ``done/total`` suffix on the current phase. Nothing
    here touches stdout, and no method raises — reporting must never fail a run.

    Args:
        quiet: When True, suppress phase and progress lines but still show
            warnings and errors. Useful behind a ``--quiet`` flag.
    """

    def __init__(self, *, quiet: bool = False) -> None:
        self._console = Console(stderr=True)
        self._quiet = quiet
        self._phase = ""

    def on_phase(self, name: str) -> None:
        """Print the name of the phase just entered.

        Args:
            name: The phase label.
        """
        self._phase = name
        if not self._quiet:
            self._console.print(f"[dim]• {name}[/dim]")

    def on_progress(
        self, done: int, total: int, stats: ReconStats | None = None
    ) -> None:
        """Print a terse progress line, when a total is known.

        Args:
            done: Units completed so far.
            total: Units expected; zero or negative means unknown, so nothing
                is printed.
            stats: Running reconciliation counters, appended when present.
        """
        if self._quiet or total <= 0:
            return
        suffix = ""
        if stats is not None:
            suffix = f"  ({stats.created} created, {stats.failed} failed)"
        self._console.print(f"[dim]  {self._phase} {done}/{total}{suffix}[/dim]")

    def on_message(self, level: Level, text: str) -> None:
        """Print a message, coloured by severity.

        Args:
            level: The message severity.
            text: The message.
        """
        if level == Level.ERROR:
            self._console.print(f"[bold red]{text}[/bold red]")
        elif level == Level.WARNING:
            self._console.print(f"[yellow]{text}[/yellow]")
        elif not self._quiet:
            self._console.print(text)
