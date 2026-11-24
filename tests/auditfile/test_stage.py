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
    # The partner receivable property is staged so the invoice AR line is forced.
    partners = (out / "partners.csv").read_text()
    assert "property_account_receivable_id/id" in partners.splitlines()[0]


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
    res = CliRunner().invoke(
        __main__.cli, ["--flow-file", result.flows_path, "--dry-run"]
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
