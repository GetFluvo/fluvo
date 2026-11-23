"""Execute a parsed ``flows.yml`` sequentially — the flow runner's executor (#251).

This is pure orchestration: it walks the selected flows and their steps in file
order and delegates each step to an injected ``run_step`` callable, so the engine
is unit-testable without the CLI. ``__main__`` supplies the real ``run_step``,
which invokes the matching fluvo command (``import`` / ``export`` / ``write`` /
``migrate``) through exactly one code path.

Failure semantics (agreed on #251; partial refined in #390):

- A step has three outcomes: ``ok`` (command exited zero, no failed rows),
  ``partial`` (command exited zero but some rows went to a fail file), and
  ``failed`` (command exited non-zero).
- A *partial* step is **not** silently a success: Odoo rejected rows and the
  loss must be visible. It drives ``on_error`` and the run's exit code exactly
  like a failure — only a fully ``ok`` step lets the run converge quietly. This
  closes #390, where a partial import was reported ``ok`` and exited 0, hiding
  dropped rows from CI and cron and letting later steps load children of
  rows that were never created.
- ``on_error: abort`` (the default) stops the whole run at the first step that
  is not ``ok``; ``on_error: continue`` records it and proceeds.
- The whole run exits non-zero if **any** step was not ``ok``, even under
  ``continue``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .flow import Flow

if TYPE_CHECKING:
    from .mapping import MappingSpec


@dataclass
class StepOutcome:
    """The result of running one step.

    Attributes:
        ok: Whether the step's command exited zero. This alone does not mean the
            step converged: a partial import exits zero but has ``fail_rows`` > 0.
            Use :attr:`status` / :attr:`succeeded` for the run-level decision.
        fail_file: Path to a fail file the step produced, if any.
        fail_rows: Number of failed rows in ``fail_file``.
        error: A short error description when the step failed.
    """

    ok: bool
    fail_file: str | None = None
    fail_rows: int = 0
    error: str | None = None

    @property
    def status(self) -> str:
        """The step's outcome: ``ok``, ``partial``, or ``failed``.

        Returns:
            str: ``failed`` if the command exited non-zero, ``partial`` if it
            exited zero but produced failed rows, else ``ok``.
        """
        if not self.ok:
            return "failed"
        if self.fail_rows > 0:
            return "partial"
        return "ok"

    @property
    def succeeded(self) -> bool:
        """Whether the step fully converged (``ok`` — no failed rows).

        Returns:
            bool: True only for a clean step; False for ``partial`` or ``failed``.
            This, not :attr:`ok`, drives ``on_error`` and the run exit code.
        """
        return self.status == "ok"


@dataclass
class StepResult:
    """A step's identity plus its outcome, for the run summary."""

    flow: str
    step_id: str
    command: str
    outcome: StepOutcome


RunStep = Callable[[str, dict[str, Any], "MappingSpec | None"], StepOutcome]


def run_flows(flows: list[Flow], run_step: RunStep) -> tuple[list[StepResult], bool]:
    """Run each selected flow's steps in order.

    Args:
        flows: The flows to run, already selected and in file order.
        run_step: Callable ``(command, with_, mapping) -> StepOutcome`` that
            executes one step. Injected so the engine can be tested without the
            CLI. ``mapping`` is the step's compiled ``mapping:`` block, or None.

    Returns:
        tuple[list[StepResult], bool]: All step results in run order, and whether
        an ``abort`` stopped the run early.
    """
    results: list[StepResult] = []
    for flow in flows:
        for step in flow.steps:
            outcome = run_step(step.run, step.with_, step.mapping)
            results.append(StepResult(flow.name, step.id, step.run, outcome))
            if not outcome.succeeded and step.on_error == "abort":
                return results, True
    return results, False


def any_failed(results: list[StepResult]) -> bool:
    """Return whether any step in the run did not fully succeed.

    A ``partial`` step (some rows rejected to a fail file) counts here just like
    an outright failure, so the run exits non-zero and the loss is visible (#390).

    Args:
        results: The step results from :func:`run_flows`.

    Returns:
        bool: True if at least one step was not ``ok`` (drives the non-zero exit).
    """
    return any(not r.outcome.succeeded for r in results)
