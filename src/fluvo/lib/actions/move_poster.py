"""Post imported journal entries: the explicit non-idempotent step (#358).

Fluvo loads ``account.move`` records as draft, so the load converges on every
re-run like any other model. Posting is this separate, deliberate step. It is
itself safe to re-run: a move already posted (by an earlier run, or by someone
in Odoo) is counted under ``already_posted`` and left alone.

Every move named in the source file lands in exactly one bucket, so
``created + failed + unaccounted == total`` holds (the move is the unit):

* ``created`` — posted by this run;
* ``failed`` — not found, blocked by a lock date (``lock_date``), renamed by the
  post (``name_changed_on_post``), silently re-dated by the post
  (``date_changed_on_post`` — the safety net for a lock the pre-check missed),
  rejected for a sequence/date mismatch (``sequence_date_mismatch``), or
  otherwise refused by Odoo; written to a fail file with the reason;
* ``unaccounted`` — ``already_posted`` on re-run, ``would_post`` under
  ``--dry-run``, or ``cancelled`` when stopped through the :class:`CancelToken`.

What the #359 verification established and this step relies on (see
``docs/adr/0001-account-move-load-and-post.md``): posting a move dated on or
before a lock date **silently shifts** its date instead of raising, so such
moves are read and *not attempted*. The lock-date fields that the #359
verification showed cause a shift are read where they exist (fiscal-year, tax
and hard locks, plus the sale/purchase locks on 18+), inheriting a branch's
parent-company locks, and the strictest applicable one blocks the move
(``period_lock_date`` is left to the post-condition — see below). A name
consistent with the move's date survives
``action_post`` unchanged, so a changed name — or a changed date — is a real
anomaly worth flagging (``date_changed_on_post`` is the safety net for any lock
the pre-check could not see).
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass, field
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
from ..immutable import (
    SEQUENCE_DATE_MISMATCH,
    is_sequence_date_error,
    normalize_xmlid,
    resolve_records,
)
from ..odoo_errors import format_odoo_error

MODEL = "account.move"

#: Unaccounted reasons.
ALREADY_POSTED = "already_posted"
CANCELLED = "cancelled"
WOULD_POST = "would_post"

#: Failed reasons (see the ADR 0001 reconciliation table). ``SEQUENCE_DATE_MISMATCH``
#: is shared with the load guard (``fluvo.lib.immutable``).
LOCK_DATE = "lock_date"
NAME_CHANGED = "name_changed_on_post"
DATE_CHANGED = "date_changed_on_post"
NOT_FOUND = "not_found"
POST_REFUSED = "post_refused"

#: Fields read per move before posting.
_MOVE_FIELDS = ["state", "name", "date", "company_id", "journal_id"]

#: ``res.company`` lock-date fields the pre-check blocks on. Chosen from the
#: per-lock #359 verification (``docs/adr/0001-verification-359-findings.md``):
#: ``fiscalyear_lock_date`` silently shifts the date (18/19); ``sale_``/
#: ``purchase_lock_date`` silently shift their journal type (18+);
#: ``tax_lock_date`` is kept as a conservative block (it shifts a tax-bearing
#: move and the step cannot cheaply tell which moves carry tax);
#: ``hard_lock_date`` is irreversible. ``period_lock_date`` is deliberately NOT
#: here: the probe showed an adviser/admin (the migration user) posts past it
#: with no shift, so blocking it would wrongly refuse a whole migration's
#: pre-period moves — the ``date_changed_on_post`` post-condition nets the rare
#: restricted-user case. Only fields that exist on this Odoo are read.
_GLOBAL_LOCK_FIELDS = (
    "fiscalyear_lock_date",
    "tax_lock_date",
    "hard_lock_date",
)
_JOURNAL_LOCK_FIELD = {"sale": "sale_lock_date", "purchase": "purchase_lock_date"}
_ALL_LOCK_FIELDS = (*_GLOBAL_LOCK_FIELDS, *_JOURNAL_LOCK_FIELD.values())


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


def _m2o_id(value: Any) -> int | None:
    """Return the id of a many2one read value (``[id, name]`` or an int)."""
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    if isinstance(value, int):
        return value
    return None


@dataclass(frozen=True)
class _LockData:
    """Everything needed to decide if a move is behind a lock date.

    Attributes:
        by_company: Company id -> {lock field -> ISO date}, already max'd up the
            parent-company chain (branch inheritance on 17+).
        journal_type: Journal id -> its ``type`` (picks the sale/purchase lock).
    """

    by_company: dict[int, dict[str, str]] = field(default_factory=dict)
    journal_type: dict[int, str] = field(default_factory=dict)


def _existing_lock_fields(connection: Any) -> list[str]:
    """Return the lock-date fields that exist on ``res.company`` here.

    Odoo versions differ: 18 dropped ``period_lock_date`` and added
    ``sale_``/``purchase_``/``hard_lock_date``; older ones lack the latter. Only
    the fields that exist are read, so the step works unchanged across 16-19.

    Args:
        connection: An Odoo connection.

    Returns:
        list[str]: The subset of :data:`_ALL_LOCK_FIELDS` present on this Odoo.
    """
    available = connection.get_model("res.company").fields_get(list(_ALL_LOCK_FIELDS))
    return [f for f in _ALL_LOCK_FIELDS if f in available]


def _effective_company_locks(
    connection: Any, lock_fields: list[str]
) -> dict[int, dict[str, str]]:
    """Each company's effective lock dates, inheriting the parent chain's locks.

    A branch (17+) inherits its parent company's locks, so the effective lock is
    the latest date seen walking from the company up through its parents.

    Args:
        connection: An Odoo connection.
        lock_fields: The lock fields that exist (see :func:`_existing_lock_fields`).

    Returns:
        dict[int, dict[str, str]]: Company id -> {lock field -> ISO date}, only
        for companies that have at least one lock.
    """
    if not lock_fields:
        return {}
    rows = connection.get_model("res.company").search_read(
        [], ["parent_id", *lock_fields]
    )
    raw = {int(r["id"]): r for r in rows}
    out: dict[int, dict[str, str]] = {}
    for cid in raw:
        effective: dict[str, str] = {}
        node: int | None = cid
        seen: set[int] = set()
        while node is not None and node in raw and node not in seen:
            seen.add(node)
            row = raw[node]
            for f in lock_fields:
                v = row.get(f)
                if v and (f not in effective or str(v) > effective[f]):
                    effective[f] = str(v)
            node = _m2o_id(row.get("parent_id"))
        if effective:
            out[cid] = effective
    return out


def _journal_types(connection: Any, journal_ids: set[int]) -> dict[int, str]:
    """Read the ``type`` of each journal (sale/purchase/general/...)."""
    if not journal_ids:
        return {}
    rows = connection.get_model("account.journal").search_read(
        [("id", "in", sorted(journal_ids))], ["type"]
    )
    return {int(r["id"]): str(r.get("type") or "") for r in rows}


def _read_lock_data(connection: Any, records: dict[str, dict[str, Any]]) -> _LockData:
    """Read the lock dates and journal types needed to guard the given moves.

    Args:
        connection: An Odoo connection.
        records: The pre-post read of the moves.

    Returns:
        _LockData: Effective company locks and journal types for these moves.
    """
    lock_fields = _existing_lock_fields(connection)
    journal_ids = {
        jid
        for rec in records.values()
        if (jid := _m2o_id(rec.get("journal_id"))) is not None
    }
    return _LockData(
        by_company=_effective_company_locks(connection, lock_fields),
        journal_type=_journal_types(connection, journal_ids),
    )


def _lock_violation(rec: dict[str, Any], lock: _LockData) -> tuple[str, str] | None:
    """Return ``(lock_field, lock_date)`` if the move is on/before an applicable lock.

    The move is checked against every global lock plus, when its journal is a
    sale or purchase journal, that journal-type lock. The strictest (latest)
    violated lock is named.

    Args:
        rec: The move's pre-post values.
        lock: The lock dates and journal types.

    Returns:
        tuple[str, str] | None: ``(lock field, its date)``, or None if clear.
    """
    date = rec.get("date")
    if not date:
        return None
    company = _m2o_id(rec.get("company_id"))
    locks = lock.by_company.get(company) if company is not None else None
    if not locks:
        return None
    applicable = list(_GLOBAL_LOCK_FIELDS)
    jtype = lock.journal_type.get(_m2o_id(rec.get("journal_id")) or -1, "")
    if jtype in _JOURNAL_LOCK_FIELD:
        applicable.append(_JOURNAL_LOCK_FIELD[jtype])
    violated = [
        (f, locks[f]) for f in applicable if f in locks and str(date) <= locks[f]
    ]
    if not violated:
        return None
    return max(violated, key=lambda kv: kv[1])


def _call_post(model_obj: Any, ids: list[int], context: dict[str, Any]) -> None:
    """Call ``action_post``, tolerating a ``None`` return over some transports.

    The outcome is judged by re-reading ``state`` afterwards, not from the
    return value, so a marshalling complaint about ``None`` is harmless.

    Args:
        model_obj: The ``account.move`` proxy.
        ids: The move ids to post.
        context: The Odoo context (carries ``allowed_company_ids`` so a move of
            another allowed company posts instead of raising a company error).

    Raises:
        Exception: Any error other than the ``None`` marshalling complaint.
    """
    try:
        if context:
            model_obj.action_post(ids, context=context)
        else:
            model_obj.action_post(ids)
    except Exception as e:
        if "cannot marshal None" not in str(e):
            raise


def _post_batch(
    model_obj: Any, ids: list[int], errors: dict[int, str], context: dict[str, Any]
) -> None:
    """Post a batch, falling back to one-by-one so one bad move cannot block it.

    Posting several moves is a single transaction in Odoo (verified #359): one
    unpostable move rolls the whole batch back, so the fallback is required to
    post the good ones and pin the error on the bad one.

    Args:
        model_obj: The ``account.move`` proxy.
        ids: The draft move ids to post.
        errors: Filled with ``id -> message`` for moves Odoo refused.
        context: The Odoo context passed to ``action_post``.
    """
    try:
        _call_post(model_obj, ids, context)
        return
    except Exception as e:
        if len(ids) == 1:
            errors[ids[0]] = format_odoo_error(e)
            return
        log.debug(f"Batch post failed ({format_odoo_error(e)}); retrying one by one.")
    for move_id in ids:
        try:
            _call_post(model_obj, [move_id], context)
        except Exception as e:
            errors[move_id] = format_odoo_error(e)


def _post_context(
    records: dict[str, dict[str, Any]], chunk: list[str]
) -> dict[str, Any]:
    """Build the ``action_post`` context for a batch.

    Passing ``allowed_company_ids`` lets a move that belongs to another of the
    user's allowed companies post, instead of Odoo raising a company-mismatch
    error (#414 review item 10). Empty when the moves carry no company.

    Args:
        records: The pre-post read of all moves.
        chunk: The external ids in this batch.

    Returns:
        dict[str, Any]: The context, or ``{}`` when no company is known.
    """
    companies = sorted(
        {
            cid
            for x in chunk
            if (cid := _m2o_id(records.get(x, {}).get("company_id"))) is not None
        }
    )
    return {"allowed_company_ids": companies} if companies else {}


def _classify_before_post(
    ext_ids: list[str],
    records: dict[str, dict[str, Any]],
    lock: _LockData,
    failures: dict[str, tuple[str, str]],
    reasons: dict[str, int],
) -> list[str]:
    """Split the named moves into drafts-to-post vs. already-accounted.

    Args:
        ext_ids: The moves named in the source, in order.
        records: Live ``_MOVE_FIELDS`` per external id (missing if not in Odoo).
        lock: The lock dates and journal types to check against.
        failures: Filled with ``ext_id -> (reason, message)`` for refusals.
        reasons: Filled with unaccounted reason counts (``already_posted``).

    Returns:
        list[str]: The external ids of draft moves clear to post.
    """
    drafts: list[str] = []
    for ext_id in ext_ids:
        rec = records.get(ext_id)
        if rec is None:
            failures[ext_id] = (NOT_FOUND, "not found in Odoo (was the import run?)")
            continue
        state = rec.get("state")
        violation = _lock_violation(rec, lock) if state == "draft" else None
        if state == "posted":
            reasons[ALREADY_POSTED] = reasons.get(ALREADY_POSTED, 0) + 1
        elif state != "draft":
            failures[ext_id] = (
                POST_REFUSED,
                f"state is '{state}', only draft entries are posted",
            )
        elif violation is not None:
            lock_field, lock_date = violation
            failures[ext_id] = (
                LOCK_DATE,
                f"date {rec.get('date')} is on/before {lock_field}={lock_date}; "
                f"not attempted (posting would silently shift the date)",
            )
        else:
            drafts.append(ext_id)
    return drafts


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
    dry_run: bool = False,
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
        dry_run: Report what would post and what is blocked, without writing.
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
        records = resolve_records(connection, MODEL, ext_ids, _MOVE_FIELDS)
        lock = _read_lock_data(connection, records)
    except FluvoError as e:
        return RunResult(RunStatus.FAILED, stats, errors=[RunError.of(e)])
    except Exception as e:
        err = SourceReadError(f"Could not connect or read moves: {e}")
        return RunResult(RunStatus.FAILED, stats, errors=[RunError.of(err)])

    failures: dict[str, tuple[str, str]] = {}
    drafts = _classify_before_post(ext_ids, records, lock, failures, stats.reasons)

    if dry_run:
        stats.reasons[WOULD_POST] = len(drafts)
        stats.failed = len(failures)
        stats.unaccounted = sum(stats.reasons.values())
        reporter.on_message(
            Level.INFO,
            f"dry-run: would post {len(drafts)} move(s); "
            f"{len(failures)} blocked; see reasons.",
        )
        return RunResult(RunStatus.OK, stats)

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
        context = _post_context(records, chunk)
        _post_batch(model_obj, [records[x]["id"] for x in chunk], errors_by_id, context)
        attempted.extend(chunk)
        # No created/failed counters here: a post's outcome is only known once the
        # moves are re-read afterwards (see _classify_after_post), so showing them
        # mid-loop would print a stale "0 created, 0 failed". Report progress only.
        reporter.on_progress(len(attempted), len(drafts))

    # Judge the outcome by what Odoo now says, not by what the calls returned.
    if attempted:
        try:
            after = resolve_records(
                connection, MODEL, attempted, ["state", "name", "date"]
            )
        except FluvoError as e:
            stats.unaccounted = sum(stats.reasons.values())
            return RunResult(RunStatus.FAILED, stats, errors=[RunError.of(e)])
        for ext_id in attempted:
            _classify_after_post(ext_id, records, after, errors_by_id, failures, stats)

    stats.failed = len(failures)
    stats.unaccounted = sum(stats.reasons.values())

    artifacts: dict[str, str] = {}
    run_errors: list[RunError] = []
    if failures:
        path = _write_fail_file(
            file, fail_file, id_field, separator, encoding, failures
        )
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


def _classify_after_post(
    ext_id: str,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    errors_by_id: dict[int, str],
    failures: dict[str, tuple[str, str]],
    stats: ReconStats,
) -> None:
    """Score one attempted move by Odoo's state and name after posting.

    Args:
        ext_id: The move's external id.
        before: The pre-post read (``name`` is the expected number).
        after: The post-post read of ``state``/``name``.
        errors_by_id: Per-move error messages captured during posting.
        failures: Filled with ``ext_id -> (reason, message)`` on any anomaly.
        stats: Updated in place: ``created`` on a clean post.
    """
    now = after.get(ext_id) or {}
    move_id = before[ext_id]["id"]
    if now.get("state") != "posted":
        message = errors_by_id.get(move_id, "still not posted after action_post")
        reason = (
            SEQUENCE_DATE_MISMATCH if is_sequence_date_error(message) else POST_REFUSED
        )
        failures[ext_id] = (reason, message)
        return
    name_before = before[ext_id].get("name")
    if name_before and now.get("name") != name_before:
        # A name consistent with the date survives posting (verified #359), so a
        # change here is unexpected. A blank draft name (Odoo numbers it on post)
        # is the normal case and is not flagged.
        failures[ext_id] = (
            NAME_CHANGED,
            f"name changed on post: '{name_before}' -> '{now.get('name')}'",
        )
        return
    date_before = before[ext_id].get("date")
    if date_before and now.get("date") and str(now.get("date")) != str(date_before):
        # The safety net for any lock the pre-check missed (a lock exception, a
        # future Odoo version): posting silently moved the accounting date.
        failures[ext_id] = (
            DATE_CHANGED,
            f"accounting date shifted on post: '{date_before}' -> '{now.get('date')}' "
            f"(a lock date the pre-check did not catch)",
        )
        return
    stats.created += 1


def _write_fail_file(
    file: str,
    fail_file: str | None,
    id_field: str,
    separator: str,
    encoding: str,
    failures: dict[str, tuple[str, str]],
) -> str:
    """Write the moves that could not be posted, with reason and message.

    Args:
        file: The source CSV (locates the default fail file).
        fail_file: Explicit fail-file path, or None for the default.
        id_field: The external-id column name.
        separator: CSV delimiter.
        encoding: File encoding.
        failures: ``ext_id -> (reason, message)``.

    Returns:
        str: The path written.
    """
    path = fail_file or post_fail_file_for(file)
    with open(path, "w", newline="", encoding=encoding) as handle:
        writer = csv.writer(handle, delimiter=separator, quoting=csv.QUOTE_ALL)
        writer.writerow([id_field, "_REASON", "_ERROR_REASON"])
        for ext_id, (reason, message) in failures.items():
            writer.writerow([ext_id, reason, message])
    return path
