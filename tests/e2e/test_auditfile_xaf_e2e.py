"""End-to-end: stage a synthetic XAF and load it into a real Odoo.

Proves the whole audit-file pipeline against a live Odoo 18 with accounting:
generate a synthetic XAF, ``fluvo auditfile stage`` it, then run the generated
``flows.yml`` through the real import engine. Re-run it and assert the import is
idempotent (zero duplicate partners/accounts/journals/moves) and that the moves
land balanced and draft — the ADR 0001 contract, end to end.

Only the ``account`` module is needed (installed here over RPC); no chart
template is applied, because the connector creates the accounts, journals and
draft moves directly. The target company is selected explicitly because the
e2e database may hold more than one company.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner

from fluvo import __main__
from fluvo.auditfile import read_auditfile
from fluvo.auditfile.stage import stage
from tests.auditfile.xaf_fixtures import generate_xaf

from . import assertions as A

#: A test-specific external-id prefix so assertions never collide with other
#: tests or pre-existing data in the shared target database.
PREFIX = "xafe2e"

_MODELS = ("res.partner", "account.account", "account.journal", "account.move")


def _ensure_account_installed(rpc: Any) -> None:
    """Install the Odoo ``account`` module if it is not already present."""
    modules = rpc.get_model("ir.module.module")
    rows = modules.search_read([["name", "=", "account"]], ["state"])
    assert rows, "the 'account' module should exist in the Odoo image"
    if rows[0]["state"] not in ("installed", "to upgrade"):
        modules.button_immediate_install([rows[0]["id"]])


def _write_conn(conn_config: dict[str, Any], path: Path) -> str:
    """Write an INI connection file from the e2e config and return its path."""
    lines = ["[Connection]"]
    for key in ("hostname", "port", "database", "login", "password", "protocol"):
        lines.append(f"{key} = {conn_config[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _imported_count(rpc: Any, model: str) -> int:
    """Count records of ``model`` created under the test prefix."""
    rows = A.search_read(
        rpc,
        "ir.model.data",
        [["module", "=", PREFIX], ["model", "=", model]],
        ["res_id"],
    )
    return len(rows)


def _run_flow(flows_path: str) -> None:
    """Run the generated flow through the CLI and assert it succeeds."""
    result = CliRunner().invoke(__main__.cli, ["--flow-file", flows_path])
    assert result.exit_code == 0, result.output


def test_xaf_stage_load_idempotent_and_balanced(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Path
) -> None:
    """Stage and load a synthetic XAF twice: no duplicates, moves draft+balanced."""
    _ensure_account_installed(rpc)

    xaf = tmp_path / "company.xaf"
    generate_xaf(xaf, version="4.0", n_journals=2, tx_per_journal=3)
    ledger = read_auditfile(str(xaf))
    conn = _write_conn(conn_config, tmp_path / "conn.conf")

    out = tmp_path / "stage"
    staged = stage(
        ledger,
        str(out),
        mode="full_history",
        prefix=PREFIX,
        connection_file=conn,
        allow_warnings=True,
        company="base.main_company",
    )
    assert staged.flows_path is not None

    # First load.
    _run_flow(staged.flows_path)
    first = {model: _imported_count(rpc, model) for model in _MODELS}
    for model, n in first.items():
        assert n > 0, f"nothing imported for {model}"
    assert first["account.move"] == 1 + 6  # opening move + six transactions

    # Second load: upsert on external id must not duplicate anything.
    _run_flow(staged.flows_path)
    second = {model: _imported_count(rpc, model) for model in _MODELS}
    assert second == first, f"re-run changed counts: {first} -> {second}"

    # The moves landed draft and balanced (ADR 0001).
    move_ids = [
        row["res_id"]
        for row in A.search_read(
            rpc,
            "ir.model.data",
            [["module", "=", PREFIX], ["model", "=", "account.move"]],
            ["res_id"],
        )
    ]
    moves = A.search_read(
        rpc, "account.move", [["id", "in", move_ids]], ["state", "line_ids"]
    )
    assert moves, "no moves were imported"
    for move in moves:
        assert move["state"] == "draft", f"move {move['id']} is not draft"
        lines = A.search_read(
            rpc,
            "account.move.line",
            [["id", "in", move["line_ids"]]],
            ["debit", "credit"],
        )
        debit = sum(float(line["debit"]) for line in lines)
        credit = sum(float(line["credit"]) for line in lines)
        assert abs(debit - credit) < 0.01, f"move {move['id']} unbalanced"
