"""Execute a parsed ``flows.yml`` sequentially — the flow runner's executor (#251).

This is pure orchestration: it walks the selected flows and their steps in file
order and delegates each step to an injected ``run_step`` callable, so the engine
is unit-testable without the CLI. ``__main__`` supplies the real ``run_step``,
which invokes the matching fluvo command (``import`` / ``export`` / ``write`` /
``migrate``) through exactly one code path.

Failure semantics (agreed on #251):

- A step "fails" when its command exits non-zero. A *partial* import (fail file
  written, CLI exit 0) is **not** a step failure — a step behaves exactly as the
  same command run by hand.
- ``on_error: abort`` (the default) stops the whole run at the first failing step;
  ``on_error: continue`` records the failure and proceeds.
- The whole run exits non-zero if **any** step failed, even under ``continue``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .flow import Flow


@dataclass
class StepOutcome:
    """The result of running one step.

    Attributes:
        ok: Whether the step's command exited zero (a partial import counts as ok).
        fail_file: Path to a fail file the step produced, if any.
        fail_rows: Number of failed rows in ``fail_file``.
        error: A short error description when the step failed.
    """

    ok: bool
    fail_file: str | None = None
    fail_rows: int = 0
    error: str | None = None


@dataclass
class StepResult:
    """A step's identity plus its outcome, for the run summary."""

    flow: str
    step_id: str
    command: str
    outcome: StepOutcome


RunStep = Callable[[str, dict[str, Any]], StepOutcome]


def run_flows(flows: list[Flow], run_step: RunStep) -> tuple[list[StepResult], bool]:
    """Run each selected flow's steps in order.

    Args:
        flows: The flows to run, already selected and in file order.
        run_step: Callable ``(command, with_) -> StepOutcome`` that executes one
            step. Injected so the engine can be tested without the CLI.

    Returns:
        tuple[list[StepResult], bool]: All step results in run order, and whether
        an ``abort`` stopped the run early.
    """
    results: list[StepResult] = []
    for flow in flows:
        for step in flow.steps:
            outcome = run_step(step.run, step.with_)
            results.append(StepResult(flow.name, step.id, step.run, outcome))
            if not outcome.ok and step.on_error == "abort":
                return results, True
    return results, False


def any_failed(results: list[StepResult]) -> bool:
    """Return whether any step in the run failed.

    Args:
        results: The step results from :func:`run_flows`.

    Returns:
        bool: True if at least one step failed (drives the non-zero exit).
    """
    return any(not r.outcome.ok for r in results)
