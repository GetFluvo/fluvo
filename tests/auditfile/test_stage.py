"""Tests for the stage writer and the ``fluvo auditfile stage`` command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fluvo import __main__
from fluvo.auditfile import read_auditfile
from fluvo.auditfile.stage import load_account_overrides, stage
from fluvo.lib.core import InputError

from .xaf_fixtures import generate_xaf

_CSV_ARTIFACTS = (
    "partners.csv",
    "accounts.csv",
    "journals.csv",
    "moves.csv",
    "account_map.csv",
    "partner_aliases.csv",
    "open_items.csv",
)


def _ledger(tmp_path: Path, version: str = "4.0"):  # type: ignore[no-untyped-def]
    """Read a freshly generated fixture ledger."""
    path = tmp_path / f"f{version}.xaf"
    generate_xaf(path, version=version)
    return read_auditfile(str(path))


def test_stage_writes_all_artifacts(tmp_path: Path) -> None:
    """Staging writes every CSV, the maps and preflight.json."""
    out = tmp_path / "stage"
    stage(_ledger(tmp_path), str(out), allow_warnings=True)
    for name in (*_CSV_ARTIFACTS, "preflight.json", "flows.yml"):
        assert (out / name).is_file(), name
    data = json.loads((out / "preflight.json").read_text())
    assert "findings" in data


def test_stage_is_idempotent(tmp_path: Path) -> None:
    """The same ledger stages to byte-identical CSVs."""
    ledger = _ledger(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    stage(ledger, str(a), allow_warnings=True)
    stage(ledger, str(b), allow_warnings=True)
    for name in _CSV_ARTIFACTS:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_stage_blocks_on_warnings_by_default(tmp_path: Path) -> None:
    """Without --allow-warnings a warning blocks and no flows.yml is written."""
    out = tmp_path / "stage"
    result = stage(_ledger(tmp_path), str(out))
    assert result.blocked is True
    assert result.flows_path is None
    assert not (out / "flows.yml").exists()
    # Data and preflight are still written for inspection.
    assert (out / "moves.csv").is_file()
    assert (out / "preflight.json").is_file()


def test_stage_allow_warnings_writes_flows(tmp_path: Path) -> None:
    """--allow-warnings lets staging proceed and emits flows.yml."""
    out = tmp_path / "stage"
    result = stage(_ledger(tmp_path), str(out), allow_warnings=True)
    assert result.blocked is False
    assert result.flows_path is not None
    assert (out / "flows.yml").is_file()


def test_blocked_restage_removes_stale_flows(tmp_path: Path) -> None:
    """A re-stage that now blocks deletes the previous (runnable) flows.yml."""
    out = tmp_path / "stage"
    ledger = _ledger(tmp_path)
    first = stage(ledger, str(out), allow_warnings=True)
    assert first.flows_path is not None and (out / "flows.yml").is_file()
    second = stage(ledger, str(out))  # warnings now block
    assert second.blocked is True
    assert not (out / "flows.yml").exists()  # no stale, runnable flow left behind


def test_cutover_writes_open_invoices_and_step(tmp_path: Path) -> None:
    """Cutover (3.2) writes open_invoices.csv and an extra account.move step."""
    out = tmp_path / "stage"
    stage(_ledger(tmp_path, "3.2"), str(out), mode="cutover", allow_warnings=True)
    rows = (out / "open_invoices.csv").read_text().strip().splitlines()
    assert len(rows) >= 2  # header + at least one invoice
    assert "move_type" in rows[0]
    flows = (out / "flows.yml").read_text()
    assert flows.count("account.move") == 2  # the opening move + the invoices
    # The receivable property is staged in its own update file (not on the main
    # partner frame), so the invoice AR line is forced onto the source control.
    assert (
        "property_account_receivable_id/id"
        not in (out / "partners.csv").read_text().splitlines()[0]
    )
    recv = (out / "partner_receivable.csv").read_text()
    assert "property_account_receivable_id/id" in recv.splitlines()[0]
    assert "partner_receivable.csv" in flows


def test_full_history_has_no_open_invoices_step(tmp_path: Path) -> None:
    """full_history writes an empty open_invoices.csv and no extra move step."""
    out = tmp_path / "stage"
    stage(_ledger(tmp_path, "3.2"), str(out), allow_warnings=True)
    assert (out / "open_invoices.csv").is_file()
    assert (out / "flows.yml").read_text().count("account.move") == 1


def test_cutover_writes_open_items(tmp_path: Path) -> None:
    """Cutover staging records at least one open item."""
    out = tmp_path / "stage"
    result = stage(_ledger(tmp_path), str(out), mode="cutover", allow_warnings=True)
    assert result.report.mode == "cutover"
    lines = (out / "open_items.csv").read_text().strip().splitlines()
    assert len(lines) >= 2  # header + at least one item


def test_generated_flow_parses_and_resolves(tmp_path: Path) -> None:
    """The generated flows.yml is accepted by the flow runner (dry run)."""
    pytest.importorskip("yaml")
    conn = tmp_path / "conn.conf"
    conn.write_text("[Connection]\nhostname=localhost\n", encoding="utf-8")
    out = tmp_path / "stage"
    result = stage(
        _ledger(tmp_path), str(out), connection_file=str(conn), allow_warnings=True
    )
    assert result.flows_path is not None
    # Pin a wide terminal so the Rich dry-run panel does not word-wrap the step
    # command lines: the substring checks below span a space that Rich would
    # otherwise break across a line (and the panel border), and the runner's
    # default width differs by platform (this wrapped at 80 cols on macOS CI).
    res = CliRunner().invoke(
        __main__.cli,
        ["--flow-file", result.flows_path, "--dry-run"],
        env={"COLUMNS": "200"},
    )
    assert res.exit_code == 0, res.output
    assert "account.move --o2m" in res.output
    assert "res.partner" in res.output


def test_load_account_overrides_roundtrip(tmp_path: Path) -> None:
    """An edited account_map.csv is read back and overrides the mapper."""
    out = tmp_path / "stage"
    stage(_ledger(tmp_path), str(out), allow_warnings=True)
    overrides = load_account_overrides(str(out / "account_map.csv"))
    assert overrides.types["1300"] == "asset_receivable"
    # Receivable control accounts round-trip their reconcile flag, and codes
    # keep leading zeros (0500 must not be read back as the integer 500).
    assert overrides.reconcile["1300"] is True
    assert "0500" in overrides.types


def test_partner_banks_written_and_stepped(tmp_path: Path) -> None:
    """A 3.2 stage writes partner_banks.csv and a res.partner.bank flow step."""
    out = tmp_path / "stage"
    stage(_ledger(tmp_path, "3.2"), str(out), allow_warnings=True)
    bank_lines = (out / "partner_banks.csv").read_text().strip().splitlines()
    assert len(bank_lines) >= 2  # header + at least one bank
    assert "res.partner.bank" in (out / "flows.yml").read_text()


def test_partner_banks_step_omitted_when_empty(tmp_path: Path) -> None:
    """With no bank accounts (4.0), the bank step is left out of the flow."""
    out = tmp_path / "stage"
    stage(_ledger(tmp_path, "4.0"), str(out), allow_warnings=True)
    assert (out / "partner_banks.csv").is_file()  # still written (header only)
    assert "res.partner.bank" not in (out / "flows.yml").read_text()


def test_account_map_target_xmlid_roundtrips(tmp_path: Path) -> None:
    """An edited target_xmlid column is read back as an account match."""
    out = tmp_path / "stage"
    stage(_ledger(tmp_path), str(out), allow_warnings=True)
    amap = out / "account_map.csv"
    text = amap.read_text()
    assert "target_xmlid" in text.splitlines()[0]  # column is present to edit
    # Pin 1300 onto an existing record and re-read.
    rows = text.splitlines()
    header = rows[0].split(";")
    ti, ci = header.index("target_xmlid"), header.index("account_id")
    patched = [rows[0]]
    for row in rows[1:]:
        cells = row.split(";")
        if cells[ci] == "1300":
            cells[ti] = "l10n_nl.recv"
        patched.append(";".join(cells))
    amap.write_text("\n".join(patched) + "\n")
    assert load_account_overrides(str(amap)).matches["1300"] == "l10n_nl.recv"


def test_cli_match_accounts_requires_connection(tmp_path: Path) -> None:
    """--match-accounts without a connection file is a clear input error."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    out = tmp_path / "stage"
    result = CliRunner().invoke(
        __main__.cli,
        [
            "auditfile",
            "stage",
            str(path),
            "--out",
            str(out),
            "--match-accounts",
            "--allow-warnings",
        ],
    )
    assert result.exit_code != 0
    assert "connection-file" in result.output


def test_load_account_overrides_bad_columns(tmp_path: Path) -> None:
    """A map without the needed columns is an input error."""
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(InputError, match="account_id"):
        load_account_overrides(str(bad))


def test_cli_stage_allow_warnings(tmp_path: Path) -> None:
    """The CLI stages with --allow-warnings and reports the load command."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    out = tmp_path / "stage"
    result = CliRunner().invoke(
        __main__.cli,
        ["auditfile", "stage", str(path), "--out", str(out), "--allow-warnings"],
    )
    assert result.exit_code == 0, result.output
    assert "Staged" in result.output
    assert (out / "flows.yml").is_file()


def test_cli_stage_blocked_exits_nonzero(tmp_path: Path) -> None:
    """Without --allow-warnings a warning blocks the CLI with a non-zero exit."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    out = tmp_path / "stage"
    result = CliRunner().invoke(
        __main__.cli, ["auditfile", "stage", str(path), "--out", str(out)]
    )
    assert result.exit_code == 1
    assert "Blocked" in result.output


def test_full_history_omits_property_steps(tmp_path: Path) -> None:
    """full_history writes empty property files and no property flow steps."""
    out = tmp_path / "stage"
    stage(_ledger(tmp_path, "3.2"), str(out), allow_warnings=True)
    assert (out / "partner_receivable.csv").is_file()  # header only
    flows = (out / "flows.yml").read_text()
    assert "partner_receivable.csv" not in flows
    assert "partner_payable.csv" not in flows


def test_account_matches_file_is_written(tmp_path: Path) -> None:
    """The reviewable account_matches.csv is always written (header at least)."""
    out = tmp_path / "stage"
    stage(_ledger(tmp_path), str(out), allow_warnings=True)
    header = (out / "account_matches.csv").read_text().splitlines()[0]
    assert "target_xmlid" in header
    assert "status" in header


_NS32 = "http://www.auditfiles.nl/XAF/3.2"

#: A minimal 3.2 file whose declared transaction totals do NOT match the parse.
_BAD_TOTALS_XAF = (
    f'<?xml version="1.0"?><auditfile xmlns="{_NS32}">'
    "<header><startDate>2024-01-01</startDate><endDate>2024-12-31</endDate>"
    "<curCode>EUR</curCode></header><company><companyName>X</companyName>"
    "<generalLedger><ledgerAccount><accID>1300</accID><accDesc>Deb</accDesc>"
    "<accTp>B</accTp></ledgerAccount></generalLedger>"
    "<transactions><linesCount>2</linesCount>"
    "<totalDebit>999.00</totalDebit><totalCredit>999.00</totalCredit>"
    "<journal><jrnID>S</jrnID><jrnTp>S</jrnTp>"
    "<transaction><nr>1</nr><trDt>2024-06-30</trDt>"
    "<trLine><nr>1</nr><accID>1300</accID><amnt>100.00</amnt><amntTp>D</amntTp>"
    "</trLine>"
    "<trLine><nr>2</nr><accID>1300</accID><amnt>100.00</amnt><amntTp>C</amntTp>"
    "</trLine></transaction></journal></transactions>"
    "</company></auditfile>"
)


def test_control_total_mismatch_blocks_even_with_allow_warnings(tmp_path: Path) -> None:
    """A control-total mismatch is an error; --allow-warnings does not clear it."""
    path = tmp_path / "bad.xaf"
    path.write_text(_BAD_TOTALS_XAF, encoding="utf-8")
    out = tmp_path / "stage"
    result = stage(read_auditfile(str(path)), str(out), allow_warnings=True)
    assert result.blocked is True
    assert not (out / "flows.yml").exists()


def test_allow_control_total_mismatch_unblocks(tmp_path: Path) -> None:
    """Its own explicit override lets a mismatching file stage."""
    path = tmp_path / "bad.xaf"
    path.write_text(_BAD_TOTALS_XAF, encoding="utf-8")
    out = tmp_path / "stage"
    result = stage(
        read_auditfile(str(path)),
        str(out),
        allow_warnings=True,
        allow_control_total_mismatch=True,
    )
    assert result.blocked is False
    assert (out / "flows.yml").is_file()


def test_cli_allow_control_total_mismatch(tmp_path: Path) -> None:
    """The CLI exposes --allow-control-total-mismatch and it unblocks staging."""
    path = tmp_path / "bad.xaf"
    path.write_text(_BAD_TOTALS_XAF, encoding="utf-8")
    out = tmp_path / "stage"
    result = CliRunner().invoke(
        __main__.cli,
        [
            "auditfile",
            "stage",
            str(path),
            "--out",
            str(out),
            "--allow-warnings",
            "--allow-control-total-mismatch",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "flows.yml").is_file()


def test_cli_stage_with_account_map(tmp_path: Path) -> None:
    """A supplied --account-map overrides the typing on a re-stage."""
    path = tmp_path / "f.xaf"
    generate_xaf(path, version="4.0")
    out = tmp_path / "stage"
    CliRunner().invoke(
        __main__.cli,
        ["auditfile", "stage", str(path), "--out", str(out), "--allow-warnings"],
    )
    account_map = out / "account_map.csv"
    # Flip a heuristic account (9006) to an explicit type, as a user would.
    lines = account_map.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("9006;"):
            cols = line.split(";")
            cols[3] = "asset_fixed"
            cols[5] = "override"
            lines[i] = ";".join(cols)
    account_map.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out2 = tmp_path / "stage2"
    result = CliRunner().invoke(
        __main__.cli,
        [
            "auditfile",
            "stage",
            str(path),
            "--out",
            str(out2),
            "--account-map",
            str(account_map),
            "--allow-warnings",
        ],
    )
    assert result.exit_code == 0, result.output
    new_map = (out2 / "account_map.csv").read_text()
    assert "9006;9006;Tussenrekening 6;asset_fixed;False;override" in new_map


def test_stage_post_flag_adds_post_steps(tmp_path: Path) -> None:
    """`stage --post` appends a post step per account.move file; default: none."""
    out = tmp_path / "stage"
    stage(
        _ledger(tmp_path, "3.2"),
        str(out),
        mode="cutover",
        allow_warnings=True,
        post=True,
    )
    text = (out / "flows.yml").read_text()
    assert "run: post" in text
    assert "id: post_moves" in text  # the opening entry is posted
    # posting comes after every import step
    assert text.index("run: import") < text.index("run: post")

    plain = tmp_path / "stage_plain"
    stage(_ledger(tmp_path, "3.2"), str(plain), mode="cutover", allow_warnings=True)
    assert "run: post" not in (plain / "flows.yml").read_text()  # default stays draft


def test_generated_post_flow_parses_and_validates(tmp_path: Path) -> None:
    """The generated post flow parses and its post step's options validate."""
    from fluvo.__main__ import _command_option_specs, cli
    from fluvo.lib.flow import VALID_COMMANDS, parse_flow_file

    out = tmp_path / "stage"
    stage(
        _ledger(tmp_path, "3.2"),
        str(out),
        mode="cutover",
        allow_warnings=True,
        post=True,
    )
    known = {n: set(_command_option_specs(cli.commands[n])) for n in VALID_COMMANDS}
    ff = parse_flow_file(str(out / "flows.yml"), known_options=known)
    runs = [s.run for f in ff.flows for s in f.steps]
    assert "import" in runs and "post" in runs
