"""Write a transform result to a stage directory the flow runner can load.

``fluvo auditfile stage`` turns an audit file into a self-contained directory:

* ``partners.csv``, ``accounts.csv``, ``journals.csv``, ``moves.csv`` — the
  Odoo-shaped data, upsert-keyed on deterministic external ids.
* ``account_map.csv`` — the editable account-typing proposals. Edit it and pass
  ``--account-map`` on a re-stage to override the mapper chain.
* ``partner_aliases.csv`` — every source customer/supplier id mapped to the
  surviving ``res.partner`` external id (dedup is lossless and auditable).
* ``open_items.csv`` — the per-partner open AR/AP breakdown (cutover).
* ``preflight.json`` — the structured preflight report.
* ``flows.yml`` — a generated flow that loads the CSVs in dependency order
  (partners → accounts → journals → moves) against the given connection, run
  with ``fluvo --flow-file <dir>/flows.yml``.

Staging never touches Odoo. Preflight is fail-fast: a blocking report writes the
data and ``preflight.json`` for inspection but **not** ``flows.yml``, and the
caller is told it blocked so the CLI can exit non-zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from ..lib.core import InputError
from .canonical import CanonicalLedger
from .preflight import PreflightReport, run_preflight
from .transform import MODE_FULL_HISTORY, TransformResult, transform

#: The model each staged CSV loads into, in dependency order.
_STEPS: tuple[tuple[str, str, str, bool], ...] = (
    # (step id, csv basename, odoo model, is_o2m)
    ("partners", "partners.csv", "res.partner", False),
    ("accounts", "accounts.csv", "account.account", False),
    ("journals", "journals.csv", "account.journal", False),
    ("moves", "moves.csv", "account.move", True),
)

#: Placeholder written into flows.yml when no connection file is supplied.
_CONNECTION_PLACEHOLDER = "connection.conf"

#: CSV delimiter for the staged files. Matches ``fluvo import``'s default
#: separator, so the generated flow (and a manual ``fluvo import``) read them
#: without a ``--sep`` override.
_CSV_SEP = ";"


@dataclass(frozen=True)
class StageResult:
    """The outcome of a stage.

    Attributes:
        out_dir: The directory written to.
        report: The preflight report.
        blocked: True when preflight blocked and no ``flows.yml`` was written.
        files: Artifact name → absolute path for everything written.
        flows_path: The ``flows.yml`` path, or None when blocked.
    """

    out_dir: str
    report: PreflightReport
    blocked: bool
    files: dict[str, str]
    flows_path: str | None


@dataclass(frozen=True)
class AccountOverrides:
    """An edited ``account_map.csv`` parsed into override maps.

    Attributes:
        types: ``account_id`` → ``account_type`` to force.
        reconcile: ``account_id`` → whether the account must be reconcilable,
            for the rows that set the column explicitly.
    """

    types: dict[str, str] = field(default_factory=dict)
    reconcile: dict[str, bool] = field(default_factory=dict)


def load_account_overrides(path: str) -> AccountOverrides:
    """Read an edited ``account_map.csv`` into override maps.

    Every column is read as text, so an account code like ``0100`` keeps its
    leading zeros instead of being inferred as the integer ``100`` (which would
    never match). The ``reconcile`` column is honoured when present.

    Args:
        path: Path to a CSV with at least ``account_id`` and ``account_type``.

    Returns:
        AccountOverrides: The type and reconcile overrides for non-empty rows.

    Raises:
        InputError: If the file cannot be read or lacks the needed columns.
    """
    try:
        # infer_schema_length=0 reads every column as a string, preserving
        # leading zeros in account codes.
        frame = pl.read_csv(path, separator=_CSV_SEP, infer_schema_length=0)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise InputError(f"could not read account map '{path}': {exc}") from exc
    if "account_id" not in frame.columns or "account_type" not in frame.columns:
        raise InputError(
            f"account map '{path}' must have 'account_id' and 'account_type' columns."
        )
    has_reconcile = "reconcile" in frame.columns
    types: dict[str, str] = {}
    reconcile: dict[str, bool] = {}
    for row in frame.to_dicts():
        account_id = row.get("account_id")
        account_type = row.get("account_type")
        if not (account_id and account_type):
            continue
        key = str(account_id)
        types[key] = str(account_type)
        if has_reconcile:
            raw = str(row.get("reconcile") or "").strip().lower()
            if raw in ("true", "1", "yes"):
                reconcile[key] = True
            elif raw in ("false", "0", "no"):
                reconcile[key] = False
    return AccountOverrides(types=types, reconcile=reconcile)


def _yaml_quote(value: str) -> str:
    """Return a double-quoted, escaped YAML scalar.

    Args:
        value: The raw string.

    Returns:
        str: A YAML-safe double-quoted scalar.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _flows_yaml(
    steps: list[tuple[str, str, str, bool]],
    connection_file: str,
    company: str | None,
) -> str:
    """Render the generated ``flows.yml`` for the staged CSVs.

    Args:
        steps: ``(step id, absolute csv path, model, is_o2m)`` in load order.
        connection_file: The connection file path to embed in every step.
        company: A company id or external id to target (``company_id`` on every
            step), or None to leave it to the import command's default handling.

    Returns:
        str: The YAML document.
    """
    lines = [
        "# Generated by `fluvo auditfile stage`. Review preflight.json first.",
        "# Load with: fluvo --flow-file <this-dir>/flows.yml",
        "version: 1",
        "flows:",
        "  - name: auditfile-import",
        "    on_error: abort",
        "    steps:",
    ]
    for step_id, csv_path, model, is_o2m in steps:
        lines.append("      - run: import")
        lines.append(f"        id: {step_id}")
        lines.append("        with:")
        lines.append(f"          connection_file: {_yaml_quote(connection_file)}")
        lines.append(f"          filename: {_yaml_quote(csv_path)}")
        lines.append(f"          model: {model}")
        if company:
            lines.append(f"          company_id: {_yaml_quote(company)}")
        if is_o2m:
            lines.append("          o2m: true")
    return "\n".join(lines) + "\n"


def _write_csv(frame: pl.DataFrame, path: Path) -> None:
    """Write a frame to CSV deterministically.

    Args:
        frame: The frame to write.
        path: The destination path.
    """
    frame.write_csv(path, separator=_CSV_SEP)


def stage(
    ledger: CanonicalLedger,
    out_dir: str,
    *,
    mode: str = MODE_FULL_HISTORY,
    prefix: str = "xaf",
    connection_file: str | None = None,
    account_overrides: AccountOverrides | None = None,
    allow_warnings: bool = False,
    company: str | None = None,
    dedup: bool = True,
    result_account: str | None = None,
) -> StageResult:
    """Transform a ledger and write a loadable stage directory.

    Args:
        ledger: The canonical ledger to stage.
        out_dir: The directory to write to (created if missing).
        mode: ``full_history`` or ``cutover``.
        prefix: The external-id prefix.
        connection_file: The Odoo connection file to embed in ``flows.yml``; a
            placeholder is written when omitted.
        account_overrides: Parsed overrides from an edited ``account_map.csv``.
        allow_warnings: When True, warnings do not block staging.
        company: A company id or external id to target, embedded as
            ``company_id`` on every flow step (required when the target Odoo has
            more than one company).
        dedup: Merge customer/supplier records sharing a KvK/VAT (default on).
        result_account: In cutover, a source account id to close the
            profit-and-loss accounts into; None carries each P&L balance.

    Returns:
        StageResult: What was written and the preflight report.
    """
    overrides = account_overrides or AccountOverrides()
    result: TransformResult = transform(
        ledger,
        prefix=prefix,
        mode=mode,
        account_overrides=overrides.types,
        reconcile_overrides=overrides.reconcile,
        dedup=dedup,
        result_account=result_account,
    )
    report = run_preflight(ledger, result)

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    def _emit(name: str, frame: pl.DataFrame) -> None:
        path = directory / name
        _write_csv(frame, path)
        files[name] = str(path.resolve())

    _emit("partners.csv", result.partners)
    _emit("accounts.csv", result.accounts)
    _emit("journals.csv", result.journals)
    _emit("moves.csv", result.moves)
    _emit("account_map.csv", result.account_map)
    _emit("partner_aliases.csv", result.alias_map)
    _emit("open_items.csv", result.open_items)

    preflight_path = directory / "preflight.json"
    preflight_path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files["preflight.json"] = str(preflight_path.resolve())

    blocked = report.is_blocking(allow_warnings=allow_warnings)
    flows = directory / "flows.yml"
    flows_path: str | None = None
    if blocked:
        # Never leave a stale, loadable flows.yml next to data that failed
        # preflight — a re-stage that now blocks must not look runnable.
        flows.unlink(missing_ok=True)
    else:
        connection = connection_file or _CONNECTION_PLACEHOLDER
        steps = [
            (step_id, files[csv], model, is_o2m)
            for step_id, csv, model, is_o2m in _STEPS
        ]
        flows.write_text(_flows_yaml(steps, connection, company), encoding="utf-8")
        flows_path = str(flows.resolve())
        files["flows.yml"] = flows_path

    return StageResult(
        out_dir=str(directory.resolve()),
        report=report,
        blocked=blocked,
        files=files,
        flows_path=flows_path,
    )
