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

import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from fluvo import __main__
from fluvo.auditfile import read_auditfile
from fluvo.auditfile.match import resolve_account_matches
from fluvo.auditfile.stage import stage
from fluvo.lib import conf_lib
from tests.auditfile.xaf_fixtures import generate_xaf

from . import _runtime
from . import assertions as A

#: A test-specific external-id prefix so assertions never collide with other
#: tests or pre-existing data in the shared target database.
PREFIX = "xafe2e"

#: A separate prefix for the chart-matching scenario, kept apart from PREFIX.
PREFIX_MATCH = "xafe2em"

#: A separate prefix for the cutover scenario.
PREFIX_CUT = "xafe2ec"

_MODELS = ("res.partner", "account.account", "account.journal", "account.move")

#: Name of the dedicated database for the cutover scenario. It gets its own DB
#: so its synthetic opening/clearing journals never collide with another
#: audit-file import into a shared company.
_CUTOVER_DB = os.environ.get("FLUVO_E2E_CUTOVER_DB", "fluvo_e2e_cutover")


@pytest.fixture(scope="session")
def cutover_endpoint(odoo_endpoint: dict[str, Any]) -> dict[str, Any]:
    """A connection config dict for a fresh, isolated cutover database."""
    if odoo_endpoint["managed"] and not _runtime.database_exists(_CUTOVER_DB):
        _runtime.create_database(_CUTOVER_DB)
    return {
        "hostname": odoo_endpoint["host"],
        "port": odoo_endpoint["port"],
        "database": _CUTOVER_DB,
        "login": "admin",
        "password": os.environ.get("FLUVO_E2E_ADMIN_PWD", "admin"),
        "protocol": os.environ.get("FLUVO_E2E_PROTOCOL", "jsonrpc"),
    }


@pytest.fixture(scope="session")
def rpc_cutover(cutover_endpoint: dict[str, Any]) -> Any:
    """An Odoo connection to the dedicated cutover database."""
    return conf_lib.get_connection_from_dict(cutover_endpoint)


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


def _res_ids(rpc: Any, module: str, model: str) -> list[int]:
    """Database ids of records imported under ``module`` for ``model``."""
    return [
        int(row["res_id"])
        for row in A.search_read(
            rpc,
            "ir.model.data",
            [["module", "=", module], ["model", "=", model]],
            ["res_id"],
        )
    ]


#: The account_type / reconcile a pre-existing target account is created with,
#: keyed by code. The control accounts must be reconcilable and of the matching
#: type, or matching them would (rightly) be a preflight error.
_TARGET_ACCOUNT = {
    "1300": ("asset_receivable", True),
    "1600": ("liability_payable", True),
}


def _ensure_account(rpc: Any, code: str) -> int:
    """Ensure an account with ``code`` exists and carries an external id.

    Stands in for a chart the target already has. Control accounts are created
    reconcilable and of the right type (as a real localisation chart would be).
    Returns the account id.
    """
    accounts = rpc.get_model("account.account")
    found = accounts.search_read([["code", "=", code]], ["id"])
    account_type, reconcile = _TARGET_ACCOUNT.get(code, ("asset_current", False))
    acc_id = (
        int(found[0]["id"])
        if found
        else int(
            accounts.create(
                {
                    "code": code,
                    "name": f"Existing {code}",
                    "account_type": account_type,
                    "reconcile": reconcile,
                }
            )
        )
    )
    imd = rpc.get_model("ir.model.data")
    if not imd.search_read(
        [["model", "=", "account.account"], ["res_id", "=", acc_id]], ["id"]
    ):
        imd.create(
            {
                "module": "chart_fixture",
                "name": f"acc_{code}",
                "model": "account.account",
                "res_id": acc_id,
            }
        )
    return acc_id


def test_xaf_matches_existing_chart_and_imports_banks(
    conn_config_source: dict[str, Any], rpc_source: Any, tmp_path: Path
) -> None:
    """Match an already-installed chart by code and import partner bank accounts.

    Runs against a *separate* database so the pre-existing chart is the only
    chart present. Pre-populate it with the file's account codes (standing in
    for an installed localisation chart), stage a 3.2 file with the matches, and
    prove every account is *referenced* — no duplicate codes created, no new
    account external ids under the import prefix — while the partners' bank
    accounts land as ``res.partner.bank`` records.
    """
    _ensure_account_installed(rpc_source)

    xaf = tmp_path / "company.xaf"
    generate_xaf(xaf, version="3.2", n_journals=2, tx_per_journal=2)
    ledger = read_auditfile(str(xaf))
    conn = _write_conn(conn_config_source, tmp_path / "conn.conf")

    # The target already has every account the file uses (an installed chart).
    codes = [str(c) for c in ledger.accounts.get_column("account_id").to_list()]
    account_ids = {code: _ensure_account(rpc_source, code) for code in codes}
    before = A.count(rpc_source, "account.account", [["code", "in", codes]])

    matches = resolve_account_matches(rpc_source, company="base.main_company")
    assert all(matches.matches.get(code) for code in codes), (
        "all codes should be matchable"
    )
    # The control-default read (res.partner.default_get over RPC) must run live
    # and yield either a real account xmlid or None (this build has no chart, so
    # typically None) — never raise. It drives the F suppression offline.
    for default in (matches.default_receivable, matches.default_payable):
        assert default is None or (isinstance(default, str) and "." in default)

    staged = stage(
        ledger,
        str(tmp_path / "stage"),
        mode="full_history",
        prefix=PREFIX_MATCH,
        connection_file=conn,
        account_matches=matches,
        allow_warnings=True,
        company="base.main_company",
    )
    assert set(staged.matched_accounts) == set(codes)
    assert staged.flows_path is not None
    _run_flow(staged.flows_path)

    def _counts() -> dict[str, int]:
        """Snapshot the record counts a re-run must not change."""
        partner_ids = _res_ids(rpc_source, PREFIX_MATCH, "res.partner")
        return {
            "accounts_with_code": A.count(
                rpc_source, "account.account", [["code", "in", codes]]
            ),
            "created_account_xmlids": len(
                _res_ids(rpc_source, PREFIX_MATCH, "account.account")
            ),
            "moves": len(_res_ids(rpc_source, PREFIX_MATCH, "account.move")),
            "partners": len(partner_ids),
            "banks": A.count(
                rpc_source, "res.partner.bank", [["partner_id", "in", partner_ids]]
            ),
        }

    first = _counts()
    # No account was duplicated, and the import created no account external ids
    # of its own — every account was referenced onto the existing chart.
    assert first["accounts_with_code"] == before
    assert first["created_account_xmlids"] == 0

    # Move lines on the receivable reference the pre-existing 1300 account.
    move_ids = _res_ids(rpc_source, PREFIX_MATCH, "account.move")
    recv_lines = A.count(
        rpc_source,
        "account.move.line",
        [["move_id", "in", move_ids], ["account_id", "=", account_ids["1300"]]],
    )
    assert recv_lines > 0, "no move line referenced the existing 1300 account"

    # Partner bank accounts were imported.
    assert first["partners"] > 0
    assert first["banks"] > 0, "no res.partner.bank records were imported"

    # A production re-run (same file, same company, matched chart) is idempotent:
    # upsert on the deterministic external ids changes nothing.
    _run_flow(staged.flows_path)
    assert _counts() == first, "re-running the matched + bank import was not idempotent"


def test_xaf_cutover_open_items_as_invoices(
    cutover_endpoint: dict[str, Any], rpc_cutover: Any, tmp_path: Path
) -> None:
    """Cutover stages open items as real draft invoices on a clearing account.

    On a dedicated database: stage a 3.2 file in cutover, load it, and assert
    each open item is a draft invoice with the exact residual as ``amount_total``
    (no tax, no rounding line), its receivable line on the source control
    account, the accounting date set to the cutover (not the original invoice
    date) and the entry in the dedicated opening-sales journal, and the clearing
    account netting to zero. Re-run to prove it is idempotent.
    """
    _ensure_account_installed(rpc_cutover)

    xaf = tmp_path / "company.xaf"
    generate_xaf(xaf, version="3.2", n_journals=1, tx_per_journal=1)
    ledger = read_auditfile(str(xaf))
    conn = _write_conn(cutover_endpoint, tmp_path / "conn.conf")

    staged = stage(
        ledger,
        str(tmp_path / "stage"),
        mode="cutover",
        prefix=PREFIX_CUT,
        connection_file=conn,
        allow_warnings=True,
        company="base.main_company",
    )
    assert staged.flows_path is not None
    _run_flow(staged.flows_path)

    clearing_id = A.xmlid_to_res_id(rpc_cutover, f"{PREFIX_CUT}.account_OPENCL")
    control_id = A.xmlid_to_res_id(rpc_cutover, f"{PREFIX_CUT}.account_1300")
    sale_journal_id = A.xmlid_to_res_id(
        rpc_cutover, f"{PREFIX_CUT}.journal__opening_sale"
    )
    assert clearing_id and control_id and sale_journal_id

    move_ids = _res_ids(rpc_cutover, PREFIX_CUT, "account.move")
    invoices = A.search_read(
        rpc_cutover,
        "account.move",
        [
            ["id", "in", move_ids],
            [
                "move_type",
                "in",
                ["out_invoice", "out_refund", "in_invoice", "in_refund"],
            ],
        ],
        [
            "move_type",
            "state",
            "amount_total",
            "ref",
            "line_ids",
            "date",
            "invoice_date",
            "journal_id",
        ],
    )
    # The source number is in ``ref`` (Odoo numbers the move itself, so the
    # cutover date cannot clash with a historical number's year).
    by_ref = {inv["ref"]: inv for inv in invoices}
    # Prior-year open invoice (1210.00) and the year's open invoice (101.00).
    assert by_ref["2023-900"]["amount_total"] == 1210.0  # exact residual, no tax
    assert by_ref["DOC1"]["amount_total"] == 101.0
    # The accounting date is the cutover (period end), not the original invoice
    # date (2023-12-15), so the entry lands in the open period; the invoice date
    # is still preserved for the aged balance.
    assert by_ref["2023-900"]["date"] == "2024-12-31"
    assert by_ref["2023-900"]["invoice_date"] == "2023-12-15"
    for inv in invoices:
        assert inv["state"] == "draft"
        # Each open-item invoice posts to the dedicated opening-sales journal,
        # not whatever default Odoo would otherwise pick.
        assert inv["journal_id"][0] == sale_journal_id
        line_accounts = {
            ln["account_id"][0]
            for ln in A.search_read(
                rpc_cutover,
                "account.move.line",
                [["id", "in", inv["line_ids"]]],
                ["account_id"],
            )
        }
        assert control_id in line_accounts, "AR line not on the source control account"
        assert clearing_id in line_accounts, "no clearing line on the invoice"

    def _clearing_net() -> float:
        lines = A.search_read(
            rpc_cutover,
            "account.move.line",
            [["account_id", "=", clearing_id]],
            ["debit", "credit"],
        )
        return sum(float(ln["debit"]) - float(ln["credit"]) for ln in lines)

    assert abs(_clearing_net()) < 0.01, "clearing account did not net to zero"

    # Idempotent: a second load changes nothing.
    before = A.count(rpc_cutover, "account.move", [["id", "in", move_ids]])
    _run_flow(staged.flows_path)
    assert _res_ids(rpc_cutover, PREFIX_CUT, "account.move")
    assert A.count(rpc_cutover, "account.move", [["id", "in", move_ids]]) == before
    assert abs(_clearing_net()) < 0.01
