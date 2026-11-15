"""Tests for the ``fluvo compare`` CLI wiring (#298).

The engine is covered in ``test_compare.py``; what is checked here is the
plumbing — option parsing, the locale and date-order choices reaching the
options object, and the exit codes a pipeline relies on.
"""

from __future__ import annotations

from unittest.mock import patch

import polars as pl
from click.testing import CliRunner

from fluvo import __main__
from fluvo.lib.compare import CompareResult, CompareStats

_EMPTY = pl.DataFrame()


def _result(**stats: int) -> CompareResult:
    """A CompareResult with empty buckets and the given counts."""
    return CompareResult(
        differences=_EMPTY,
        only_in_source=_EMPTY,
        only_in_target=_EMPTY,
        duplicate_source_keys=_EMPTY,
        duplicate_target_keys=_EMPTY,
        unkeyed_source=_EMPTY,
        unkeyed_target=_EMPTY,
        stats=CompareStats(**stats),  # type: ignore[arg-type]
    )


def _invoke(tmp_path, *extra: str, result: CompareResult | None = None):
    """Run `fluvo compare` with the connection and source files stubbed out."""
    connection = tmp_path / "connection.conf"
    connection.write_text("[Connection]\n", encoding="utf-8")
    source = tmp_path / "rows.csv"
    source.write_text("vat;name\nNL1;Acme\n", encoding="utf-8")
    runner = CliRunner()
    with patch("fluvo.lib.compare.run_compare") as run_compare:
        run_compare.return_value = result or _result()
        invocation = runner.invoke(
            __main__.cli,
            [
                "compare",
                "--connection-file",
                str(connection),
                "--model",
                "res.partner",
                "--file",
                str(source),
                "--key",
                "vat",
                *extra,
            ],
        )
    return invocation, run_compare


def test_options_reach_run_compare(tmp_path) -> None:
    """The command is a thin wrapper: every flag lands on the call."""
    invocation, run_compare = _invoke(
        tmp_path,
        "--fields",
        "name,street",
        "--sheet",
        "Blad1",
        "--case-insensitive",
    )
    assert invocation.exit_code == 0, invocation.output
    kwargs = run_compare.call_args.kwargs
    assert kwargs["model"] == "res.partner"
    assert kwargs["key"] == "vat"
    assert kwargs["fields"] == "name,street"
    assert kwargs["sheet"] == "Blad1"
    assert kwargs["options"].case_insensitive is True


def test_locale_and_date_order_are_explicit_choices(tmp_path) -> None:
    """Both default to the European reading and can be flipped."""
    _, run_compare = _invoke(tmp_path)
    options = run_compare.call_args.kwargs["options"]
    assert options.locale.decimal_sep == ","
    assert options.date_order == "dmy"

    _, run_compare = _invoke(tmp_path, "--locale", "anglo", "--date-order", "mdy")
    options = run_compare.call_args.kwargs["options"]
    assert options.locale.decimal_sep == "."
    assert options.date_order == "mdy"


def test_numeric_coercion_can_be_turned_off(tmp_path) -> None:
    """Leading zeros become reportable again with one flag."""
    _, run_compare = _invoke(tmp_path, "--no-numeric-coercion")
    assert run_compare.call_args.kwargs["options"].numeric_coercion is False


def test_map_builds_the_rename_dict(tmp_path) -> None:
    """--map is repeatable and splits on the first '=' only."""
    _, run_compare = _invoke(
        tmp_path, "--map", "BTW nummer=vat", "--map", "Klant naam=name"
    )
    assert run_compare.call_args.kwargs["column_map"] == {
        "BTW nummer": "vat",
        "Klant naam": "name",
    }


def test_malformed_map_is_rejected(tmp_path) -> None:
    """A --map without '=' is a usage error, not a silent no-op."""
    invocation, _ = _invoke(tmp_path, "--map", "vat")
    assert invocation.exit_code != 0
    assert "Sheet Column=odoo_field" in invocation.output


def test_malformed_domain_is_rejected(tmp_path) -> None:
    """A domain that is not a list literal fails before any connection is made."""
    invocation, run_compare = _invoke(tmp_path, "--domain", "not-a-domain")
    assert invocation.exit_code != 0
    assert "domain" in invocation.output.lower()
    run_compare.assert_not_called()


def test_malformed_context_is_rejected(tmp_path) -> None:
    """Same for a context that is not a dict literal."""
    invocation, run_compare = _invoke(tmp_path, "--context", "[1, 2]")
    assert invocation.exit_code != 0
    run_compare.assert_not_called()


def test_fail_on_difference_sets_the_exit_code(tmp_path) -> None:
    """For CI: a clean comparison exits 0, a dirty one exits 1."""
    dirty = _result(matched_records=1, differing_records=1)
    invocation, _ = _invoke(tmp_path, "--fail-on-difference", result=dirty)
    assert invocation.exit_code == 1

    clean = _result(matched_records=1)
    invocation, _ = _invoke(tmp_path, "--fail-on-difference", result=clean)
    assert invocation.exit_code == 0


def test_differences_alone_do_not_fail_the_command(tmp_path) -> None:
    """Without the flag, differences are a report, not an error."""
    dirty = _result(matched_records=1, differing_records=1)
    invocation, _ = _invoke(tmp_path, result=dirty)
    assert invocation.exit_code == 0


def test_a_setup_error_is_shown_and_exits_one(tmp_path) -> None:
    """A CompareError surfaces as a panel, not a traceback."""
    connection = tmp_path / "connection.conf"
    connection.write_text("[Connection]\n", encoding="utf-8")
    source = tmp_path / "rows.csv"
    source.write_text("vat;name\nNL1;Acme\n", encoding="utf-8")
    from fluvo.lib.compare import CompareError

    runner = CliRunner()
    with patch("fluvo.lib.compare.run_compare", side_effect=CompareError("boom")):
        invocation = runner.invoke(
            __main__.cli,
            [
                "compare",
                "--connection-file",
                str(connection),
                "--model",
                "res.partner",
                "--file",
                str(source),
                "--key",
                "vat",
            ],
        )
    assert invocation.exit_code == 1
    assert "boom" in invocation.output
