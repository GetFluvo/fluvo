"""End-to-end flow-runner scenarios against a real Odoo (#251, PR 4).

Drives a real ``flows.yml`` through :func:`fluvo.__main__.run_project_flow` — the
same entry point the ``fluvo --flow-file`` CLI uses — so the full stack is
exercised: YAML parse + variable interpolation, per-step argv building, invoking
the real ``import`` / ``export`` commands, and the run summary / exit code.

``res.partner.category`` is used for the data steps: it is simple (id, name) and
not company-aware, so no multi-company guard interferes with the flow.
"""

from __future__ import annotations

import configparser
from typing import Any

import pytest

from fluvo.__main__ import run_project_flow

from . import assertions as A
from . import generators as G


def _write_conf(path: str, cfg: dict[str, Any]) -> str:
    """Materialise a ``[Connection]`` .conf file from the e2e config dict."""
    cp = configparser.ConfigParser()
    cp["Connection"] = {
        "hostname": str(cfg["hostname"]),
        "port": str(cfg["port"]),
        "database": str(cfg["database"]),
        "login": str(cfg["login"]),
        "password": str(cfg["password"]),
        "protocol": str(cfg["protocol"]),
        "user_id": str(cfg["uid"]),
    }
    with open(path, "w", encoding="utf-8") as handle:
        cp.write(handle)
    return path


def _cats(tmp_path: Any, prefix: str, n: int) -> str:
    """Write a category CSV (id,name) and return its path."""
    rows = [{"id": f"{prefix}_c{i}", "name": f"{prefix} {i}"} for i in range(n)]
    return G.write_csv(str(tmp_path / f"{prefix}.csv"), ["id", "name"], rows)


def test_flow_multistep_import_then_export(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any
) -> None:
    """A two-step flow imports categories then exports them, all against real Odoo."""
    conf = _write_conf(str(tmp_path / "conn.conf"), conn_config)
    prefix = "flowcat"
    n = 4
    cats = _cats(tmp_path, prefix, n)
    out = str(tmp_path / "exported.csv")

    flow_text = f"""\
version: 1
vars:
  conn: {conf}
flows:
  - name: load
    steps:
      - run: import
        with:
          connection_file: "{{{{ vars.conn }}}}"
          filename: {cats}
          model: res.partner.category
          separator: ","
          headless: true
      - run: export
        with:
          connection_file: "{{{{ vars.conn }}}}"
          model: res.partner.category
          fields: "id,name"
          output: {out}
          domain: "[['name', 'like', '{prefix} %']]"
          separator: ","
"""
    flow_file = str(tmp_path / "flows.yml")
    with open(flow_file, "w", encoding="utf-8") as handle:
        handle.write(flow_text)

    # No SystemExit -> the flow succeeded.
    run_project_flow(flow_file, flow_names=None, cli_vars={}, dry_run=False)

    # DB truth: every category was created.
    A.assert_db_count(rpc, "res.partner.category", [["name", "like", f"{prefix} %"]], n)
    # The export step wrote the file with all rows.
    import polars as pl

    exported = pl.read_csv(out, separator=",")
    assert exported.height == n
    assert set(exported.columns) >= {"id", "name"}


def test_flow_hard_failure_aborts_and_exits_nonzero(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any
) -> None:
    """A failing step aborts the flow (on_error: abort) and exits non-zero.

    The step *before* the failure still runs (proving abort is post-hoc), and the
    bad step raises SystemExit(1) out of run_project_flow.
    """
    conf = _write_conf(str(tmp_path / "conn.conf"), conn_config)
    prefix = "flowfail"
    good = _cats(tmp_path, prefix, 2)

    flow_text = f"""\
version: 1
vars:
  conn: {conf}
flows:
  - name: load
    on_error: abort
    steps:
      - run: import
        with:
          connection_file: "{{{{ vars.conn }}}}"
          filename: {good}
          model: res.partner.category
          separator: ","
          headless: true
      - run: import
        with:
          connection_file: "{{{{ vars.conn }}}}"
          filename: {good}
          model: res.does.not.exist
          separator: ","
          headless: true
"""
    flow_file = str(tmp_path / "flows.yml")
    with open(flow_file, "w", encoding="utf-8") as handle:
        handle.write(flow_text)

    with pytest.raises(SystemExit) as exc:
        run_project_flow(flow_file, flow_names=None, cli_vars={}, dry_run=False)
    assert exc.value.code == 1

    # The good step ran before the abort.
    A.assert_db_count(rpc, "res.partner.category", [["name", "like", f"{prefix} %"]], 2)


def test_flow_dry_run_touches_nothing(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any
) -> None:
    """--dry-run prints the plan and creates no records."""
    conf = _write_conf(str(tmp_path / "conn.conf"), conn_config)
    prefix = "flowdry"
    cats = _cats(tmp_path, prefix, 3)

    flow_text = f"""\
version: 1
vars:
  conn: {conf}
flows:
  - name: load
    steps:
      - run: import
        with:
          connection_file: "{{{{ vars.conn }}}}"
          filename: {cats}
          model: res.partner.category
          separator: ","
          headless: true
"""
    flow_file = str(tmp_path / "flows.yml")
    with open(flow_file, "w", encoding="utf-8") as handle:
        handle.write(flow_text)

    before = A.count(rpc, "res.partner.category", [["name", "like", f"{prefix} %"]])
    run_project_flow(flow_file, flow_names=None, cli_vars={}, dry_run=True)
    after = A.count(rpc, "res.partner.category", [["name", "like", f"{prefix} %"]])
    assert after == before == 0, "dry-run must not create records"
