"""Post imported journal entries: the explicit non-idempotent step (#358).

Fluvo loads ``account.move`` records as draft, so the load converges on every
re-run like any other model. Posting — after which Odoo will not let the entry
change — is this separate, deliberate step. It is itself safe to re-run: an
entry that is already posted (by an earlier run, or by someone in Odoo) is
counted under the named reason ``already_posted`` and left alone.

Every move named in the source file lands in exactly one bucket:

* ``created`` — posted by this run;
* ``failed`` — not found, or Odoo refused to post it (written to a fail file);
* ``unaccounted`` — with a named reason: ``already_posted``, or ``cancelled``
  when the run was stopped through its :class:`CancelToken`.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from typing import Any

from ...logging_config import log
from .. import conf_lib
from ..core import (
    CancelToken,
    FluvoError,
    InputError,
    Level,
    NullReporter,
    ReconStats,
    RunError,
    RunReporter,
    RunResult,
    RunStatus,
    SourceReadError,
)
from ..immutable import normalize_xmlid, resolve_records

MODEL = "account.move"
ALREADY_POSTED = "already_posted"
CANCELLED = "cancelled"


def read_move_ids(
    file: str, id_field: str = "id", separator: str = ";", encoding: str = "utf-8"
) -> list[str]:
    """Read the distinct, fully qualified move ids from a source file.

    Id-less rows (``--o2m`` continuation lines) are ignored: a move is
    identified by its parent row.

    Args:
        file: The source CSV.
        id_field: The external-id column.
        separator: CSV delimiter.
        encoding: File encoding.

    Returns:
        list[str]: Distinct external ids in file order.

    Raises:
        InputError: When the id column is missing.
        SourceReadError: When the file cannot be read.
    """
    try:
        with open(file, encoding=encoding, newline="") as handle:
            reader = csv.reader(handle, delimiter=separator)
            header = next(reader, [])
            if id_field not in header:
                raise InputError(f"Column '{id_field}' not found in {file}.")
            idx = header.index(id_field)
            ids = (normalize_xmlid(r[idx]) for r in reader if idx < len(r) and r[idx])
            return list(dict.fromkeys(x for x in ids if x))
    except OSError as e:
        raise SourceReadError(f"Could not read {file}: {e}") from e


def post_fail_file_for(file: str) -> str:
    """Return the fail-file path for a post run over ``file``.

    Args:
        file: The source CSV.

    Returns:
        str: ``<stem>_post_fail.csv`` next to the source.
    """
    stem, dot, _ext = file.rpartition(".")
    return f"{stem if dot else file}_post_fail.csv"


def _connect(config: str | dict[str, Any]) -> Any:
    """Open an Odoo connection from a config path or dict.

    Args:
        config: Connection file path or dict.

    Returns:
        Any: The connection.
    """
    if isinstance(config, dict):
        return conf_lib.get_connection_from_dict(config)
    return conf_lib.get_connection_from_config(config)


def _call_post(model_obj: Any, ids: list[int]) -> None:
    """Call ``action_post``, tolerating a ``None`` return over XML-RPC.

    The outcome is judged by re-reading ``state`` afterwards, not from the
    return value, so a marshalling complaint about ``None`` is harmless.

    Args:
        model_obj: The ``account.move`` proxy.
        ids: The move ids to post.

    Raises:
        Exception: Any error other than the ``None`` marshalling complaint.
    """
    try:
        model_obj.action_post(ids)
    except Exception as e:
        if "cannot marshal None" not in str(e):
            raise


def _post_batch(model_obj: Any, ids: list[int], errors: dict[int, str]) -> None:
    """Post a batch, falling back to one-by-one so one bad move cannot block it.

    Args:
        model_obj: The ``account.move`` proxy.
        ids: The draft move ids to post.
        errors: Filled with ``id -> message`` for moves Odoo refused.
    """
    try:
        _call_post(model_obj, ids)
        return
    except Exception as e:
        if len(ids) == 1:
            errors[ids[0]] = str(e)
            return
        log.debug(f"Batch post failed ({e}); retrying record by record.")
    for move_id in ids:
        try:
            _call_post(model_obj, [move_id])
        except Exception as e:
            errors[move_id] = str(e)


def run_post_moves(  # noqa: C901
    config: str | dict[str, Any],
    file: str,
    id_field: str = "id",
    batch_size: int = 100,
    separator: str = ";",
    encoding: str = "utf-8",
    fail_file: str | None = None,
    reporter: RunReporter | None = None,
    cancel: CancelToken | None = None,
    connect: Callable[[str | dict[str, Any]], Any] | None = None,
) -> RunResult:
    """Post the draft journal entries named in a source file.

    Args:
        config: Connection file path or dict.
        file: The source CSV the moves were imported from.
        id_field: The external-id column.
        batch_size: Moves posted per RPC call.
        separator: CSV delimiter.
        encoding: File encoding.
        fail_file: Where to write moves that could not be posted; defaults to
            :func:`post_fail_file_for`.
        reporter: Progress sink; :class:`NullReporter` when omitted.
        cancel: Stop signal checked between batches.
        connect: Connection factory (for tests); defaults to ``conf_lib``.

    Returns:
        RunResult: A reconciled result. ``FAILED`` with a typed error when the
        run could not start; ``PARTIAL`` when some moves failed to post.
    """
    reporter = reporter or NullReporter()
    cancel = cancel or CancelToken()
    stats = ReconStats()
    try:
        ext_ids = read_move_ids(file, id_field, separator, encoding)
        stats.total = len(ext_ids)
        connection = (connect or _connect)(config)
        reporter.on_phase("reading move states")
        records = resolve_records(connection, MODEL, ext_ids, ["state"])
    except FluvoError as e:
        return RunResult(RunStatus.FAILED, stats, errors=[RunError.of(e)])
    except Exception as e:
        err = SourceReadError(f"Could not connect or read moves: {e}")
        return RunResult(RunStatus.FAILED, stats, errors=[RunError.of(err)])

    failures: dict[str, str] = {}
    drafts: list[str] = []
    for ext_id in ext_ids:
        rec = records.get(ext_id)
        state = rec.get("state") if rec else None
        if rec is None:
            failures[ext_id] = "not found in Odoo (was the import run first?)"
        elif state == "posted":
            stats.reasons[ALREADY_POSTED] = stats.reasons.get(ALREADY_POSTED, 0) + 1
        elif state == "draft":
            drafts.append(ext_id)
        else:
            failures[ext_id] = f"state is '{state}', only draft entries are posted"

    model_obj = connection.get_model(MODEL)
    reporter.on_phase("posting")
    cancelled = False
    attempted: list[str] = []
    errors_by_id: dict[int, str] = {}
    for start in range(0, len(drafts), max(1, batch_size)):
        if cancel.cancelled:
            cancelled = True
            stats.reasons[CANCELLED] = len(drafts) - start
            break
        chunk = drafts[start : start + batch_size]
        _post_batch(model_obj, [records[x]["id"] for x in chunk], errors_by_id)
        attempted.extend(chunk)
        reporter.on_progress(len(attempted), len(drafts), stats)

    # Judge the outcome by what Odoo now says, not by what the calls returned.
    if attempted:
        try:
            after = resolve_records(connection, MODEL, attempted, ["state"])
        except FluvoError as e:
            stats.unaccounted = sum(stats.reasons.values())
            return RunResult(RunStatus.FAILED, stats, errors=[RunError.of(e)])
        for ext_id in attempted:
            if (after.get(ext_id) or {}).get("state") == "posted":
                stats.created += 1
            else:
                move_id = records[ext_id]["id"]
                failures[ext_id] = errors_by_id.get(
                    move_id, "still not posted after action_post"
                )

    stats.failed = len(failures)
    stats.unaccounted = sum(stats.reasons.values())

    artifacts: dict[str, str] = {}
    run_errors: list[RunError] = []
    if failures:
        path = fail_file or post_fail_file_for(file)
        with open(path, "w", newline="", encoding=encoding) as handle:
            writer = csv.writer(handle, delimiter=separator, quoting=csv.QUOTE_ALL)
            writer.writerow([id_field, "_ERROR_REASON"])
            for ext_id, reason in failures.items():
                writer.writerow([ext_id, reason])
        artifacts["fail_file"] = path
        message = f"{len(failures)} move(s) not posted; see {path}."
        run_errors.append(RunError(kind="PostRefused", message=message))
        reporter.on_message(Level.WARNING, message)

    if cancelled:
        status = RunStatus.CANCELLED
    elif failures:
        status = RunStatus.PARTIAL if stats.created else RunStatus.FAILED
    else:
        status = RunStatus.OK
    return RunResult(status, stats, artifacts=artifacts, errors=run_errors)
