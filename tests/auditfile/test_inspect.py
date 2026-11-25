"""Tests for ``fluvo auditfile inspect`` — the read-only overview."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from rich.console import Console

from fluvo import __main__
from fluvo.auditfile.canonical import LedgerSummary
from fluvo.auditfile.inspect import render, run_inspect, summary_to_dict

from .xaf_fixtures import generate_xaf


def test_render_unbalanced_branch() -> None:
    """The renderer warns and lists the imbalance when a ledger does not balance."""
    summary = LedgerSummary(
        source_format="xaf",
        source_version="4.0",
        software=None,
        company_name="X",
        company_id="1",
        fiscal_year="2024",
        date_start="2024-01-01",
        date_end="2024-12-31",
        currency="EUR",
        counts={"moves": 1, "lines": 1},
        total_debit=100.0,
        total_credit=40.0,
        unbalanced_moves=1,
    )
    console = Console(record=True, width=100)
    render(summary, console)
    text = console.export_text()
    assert "Not balanced" in text
    assert "unbalanced transaction" in text


def test_run_inspect_returns_summary(tmp_path: Path) -> None:
    """run_inspect returns the computed summary and renders without error."""
    path = tmp_path / "f.xaf"
    spec = generate_xaf(path, version="4.0")
    console = Console(record=True, width=100)
    summary = run_inspect(str(path), console=console)
    assert summary.company_name == spec.company_name
    assert summary.counts["moves"] == spec.n_moves
    text = console.export_text()
    assert "Audit file" in text
    assert "Balanced" in text


def test_run_inspect_json(tmp_path: Path) -> None:
    """--json emits a machine-readable object with the derived balance flag."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="3.2")
    console = Console(record=True, width=100)
    summary = run_inspect(str(path), as_json=True, console=console)
    payload = json.loads(console.export_text())
    assert payload["source_format"] == "xaf"
    assert payload["balanced"] is True
    assert payload["counts"]["partners"] == summary.counts["partners"]


def test_summary_to_dict_includes_balanced(tmp_path: Path) -> None:
    """summary_to_dict carries the derived balanced flag."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    summary = run_inspect(str(path), console=Console(record=True))
    data = summary_to_dict(summary)
    assert data["balanced"] == summary.balanced
    assert data["company_id"] == summary.company_id


def test_cli_inspect_exit_zero(tmp_path: Path) -> None:
    """The CLI command inspects a file and exits zero."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    result = CliRunner().invoke(__main__.cli, ["auditfile", "inspect", str(path)])
    assert result.exit_code == 0, result.output
    assert "Testbedrijf" in result.output


def test_cli_inspect_json(tmp_path: Path) -> None:
    """The CLI --json flag emits parseable JSON."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="3.2")
    result = CliRunner().invoke(
        __main__.cli, ["auditfile", "inspect", "--json", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert '"source_format"' in result.output


def test_cli_inspect_force_format(tmp_path: Path) -> None:
    """--format xaf reads the file without auto-detection."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    result = CliRunner().invoke(
        __main__.cli, ["auditfile", "inspect", "--format", "xaf", str(path)]
    )
    assert result.exit_code == 0, result.output


def test_cli_inspect_non_xaf_fails(tmp_path: Path) -> None:
    """Inspecting a non-XAF file exits non-zero with an error panel."""
    other = tmp_path / "other.xml"
    other.write_text("<root/>", encoding="utf-8")
    result = CliRunner().invoke(__main__.cli, ["auditfile", "inspect", str(other)])
    assert result.exit_code != 0
