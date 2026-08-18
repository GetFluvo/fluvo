"""Tests for the flow executor engine (#251)."""

from typing import Any

from fluvo.lib.flow import Flow, Step
from fluvo.lib.flow_runner import StepOutcome, any_failed, run_flows


def _step(run: str, step_id: str, on_error: str = "abort") -> Step:
    """Build a Step for tests."""
    return Step(run=run, with_={}, id=step_id, on_error=on_error)


def _flow(name: str, steps: list[Step]) -> Flow:
    """Build a Flow for tests."""
    return Flow(name=name, on_error="abort", steps=steps)


def test_runs_all_steps_in_order() -> None:
    """Every step runs, in flow then step order, when all succeed."""
    flows = [
        _flow("a", [_step("import", "a1"), _step("write", "a2")]),
        _flow("b", [_step("export", "b1")]),
    ]
    seen: list[str] = []

    def run_step(command: str, with_: dict[str, Any]) -> StepOutcome:
        seen.append(command)
        return StepOutcome(ok=True)

    results, aborted = run_flows(flows, run_step)
    assert aborted is False
    assert seen == ["import", "write", "export"]
    assert [r.step_id for r in results] == ["a1", "a2", "b1"]
    assert not any_failed(results)


def test_abort_stops_the_whole_run() -> None:
    """A failing step with on_error=abort stops the run immediately."""
    flows = [
        _flow("a", [_step("import", "a1", "abort"), _step("write", "a2")]),
        _flow("b", [_step("export", "b1")]),
    ]

    def run_step(command: str, with_: dict[str, Any]) -> StepOutcome:
        return StepOutcome(ok=command != "import")  # import fails

    results, aborted = run_flows(flows, run_step)
    assert aborted is True
    assert [r.step_id for r in results] == ["a1"]  # stopped after the failure
    assert any_failed(results)


def test_continue_records_failure_and_proceeds() -> None:
    """on_error=continue keeps going but the run still counts as failed."""
    flows = [
        _flow(
            "a",
            [_step("import", "a1", "continue"), _step("write", "a2", "continue")],
        ),
        _flow("b", [_step("export", "b1")]),
    ]

    def run_step(command: str, with_: dict[str, Any]) -> StepOutcome:
        return StepOutcome(ok=command != "import")  # only import fails

    results, aborted = run_flows(flows, run_step)
    assert aborted is False
    assert [r.step_id for r in results] == ["a1", "a2", "b1"]  # all ran
    assert any_failed(results)  # but the run failed overall
    assert results[0].outcome.ok is False


def test_partial_success_is_not_a_failure() -> None:
    """A step that exits ok (even with a fail file) does not fail the run."""
    flows = [_flow("a", [_step("import", "a1")])]

    def run_step(command: str, with_: dict[str, Any]) -> StepOutcome:
        return StepOutcome(ok=True, fail_file="x_fail.csv", fail_rows=3)

    results, aborted = run_flows(flows, run_step)
    assert aborted is False
    assert not any_failed(results)
    assert results[0].outcome.fail_rows == 3
