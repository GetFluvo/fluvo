"""Pre-check that refuses to load onto records a re-run must not change (#358).

The load engine resolves a target by external id and writes to it. A target
that is no longer a clean ``draft`` ``account.move`` — it has been *posted* (or
*cancelled*) since the last run — must not be written over. Verification on
real Odoo 16/17/18/19 (#359, ``docs/adr/0001-verification-359-findings.md``)
showed the important point: **Odoo does not protect a posted move over RPC.**
A plain ``write`` to a posted move's ``ref``, ``name``, ``date`` or a line's
``account_id`` *succeeds and mutates it* with no error (on 18/19 a line write
even silently resets the move to ``draft``). So the load cannot rely on Odoo to
raise; it must read the target's state itself and refuse before writing.

This module is that guard:

* before the load, the live state of every targeted record is read, in
  batches (one ``ir.model.data`` lookup + one ``read`` per chunk);
* rows whose record is in a guarded state are taken out of the load and routed
  to the fail file under a named reason (``posted_immutable`` /
  ``cancelled_immutable``), never silently, counted as ``failed`` so that
  ``created + failed + unaccounted == total`` keeps holding;
* lines whose parent move is guarded are refused with it, so no orphan line
  write reaches a posted move (the ``--o2m`` continuation rows and any
  second-pass/deferred writes read the same, already-filtered dataset);
* if that state cannot be read, the run aborts rather than guess (fail closed).

Fluvo keeps no record of who posted a move: one posted in Odoo between runs
takes exactly the same path as one posted by the post step.

The rules are an internal registry, not user configuration, so the set of
guarded models grows deliberately (one line per model) and stays tested.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from ..logging_config import log
from .core import SourceReadError
from .internal.tools import to_xmlid

#: Reason keys recorded on guarded rows (the fail file's reason column).
POSTED_IMMUTABLE = "posted_immutable"
CANCELLED_IMMUTABLE = "cancelled_immutable"

#: Default state -> reason mapping shared by the move and its lines.
_REASON_BY_STATE: dict[str, str] = {
    "posted": POSTED_IMMUTABLE,
    "cancel": CANCELLED_IMMUTABLE,
}

#: Chunk size for ``ir.model.data`` / record reads.
CHUNK = 2000

#: Canonical reason for a move whose date and sequence number disagree. Odoo
#: rejects such a name (verified #359): at *create* on Odoo 16 — i.e. during the
#: load — and at *post* on 17-19. Shared so both the load fail file and the post
#: step (:mod:`fluvo.lib.actions.move_poster`) label it the same way.
SEQUENCE_DATE_MISMATCH = "sequence_date_mismatch"

#: Substrings Odoo uses for that rejection, matched case-insensitively.
_SEQ_DATE_MARKERS = (
    "match the sequence number",
    "aligned with the existing sequence",
    "sequence number of the related",
)


def is_sequence_date_error(message: str) -> bool:
    """Whether an Odoo error is a move date/sequence-number mismatch.

    Args:
        message: The Odoo error message.

    Returns:
        bool: True when the message is a date-vs-sequence rejection.
    """
    low = message.lower()
    return any(marker in low for marker in _SEQ_DATE_MARKERS)


@dataclass(frozen=True)
class ParentRule:
    """A row is also guarded when the parent record it points at is.

    Needed because a *new* child row has no state of its own yet. A line aimed
    at a posted move must be refused by looking at the move, not the line.

    Attributes:
        field: The many2one column on the child (``move_id``); the source may
            reference it as ``<field>/id`` or ``<field>/.id``.
        model: The parent model.
        state_field: The parent's state field.
    """

    field: str
    model: str
    state_field: str


@dataclass(frozen=True)
class ImmutableRule:
    """When a record of a model can no longer be changed by a load.

    Attributes:
        field: The state field to read.
        values: The values of ``field`` that make the record guarded.
        reason_by_state: State value -> reason recorded for a guarded row.
            A guarded state with no entry falls back to ``POSTED_IMMUTABLE``.
        parent: Optional rule for rows whose parent is guarded.
    """

    field: str
    values: frozenset[str]
    reason_by_state: dict[str, str] = dc_field(default_factory=lambda: _REASON_BY_STATE)
    parent: ParentRule | None = None

    def reason_for(self, state: Any) -> str:
        """Return the reason recorded for a given guarded state value.

        Args:
            state: The live state value read from the record.

        Returns:
            str: The reason key for the fail file.
        """
        return self.reason_by_state.get(str(state), POSTED_IMMUTABLE)


_GUARDED_STATES = frozenset({"posted", "cancel"})

IMMUTABLE_RULES: dict[str, ImmutableRule] = {
    "account.move": ImmutableRule("state", _GUARDED_STATES),
    "account.move.line": ImmutableRule(
        "parent_state",
        _GUARDED_STATES,
        parent=ParentRule("move_id", "account.move", "state"),
    ),
}


def rule_for(model: str) -> ImmutableRule | None:
    """Return the guard rule for a model, if it is guarded.

    Args:
        model: The technical model name.

    Returns:
        ImmutableRule | None: The rule, or None for an unguarded model.
    """
    return IMMUTABLE_RULES.get(model)


def normalize_xmlid(raw: str) -> str:
    """Return the fully qualified external id a load would use for ``raw``.

    Bare ids get the ``__import__`` module, exactly as Odoo's ``load`` does.

    Args:
        raw: The id value from the source row.

    Returns:
        str: ``module.name``, or ``""`` for an empty value.
    """
    ext_id = to_xmlid(str(raw).strip())
    if not ext_id:
        return ""
    return ext_id if "." in ext_id else f"__import__.{ext_id}"


def _names_by_module(ext_ids: Iterable[str]) -> dict[str, list[str]]:
    """Group distinct ``module.name`` ids by module.

    Args:
        ext_ids: Fully qualified external ids; empty values are dropped.

    Returns:
        dict[str, list[str]]: Module -> names.
    """
    grouped: dict[str, list[str]] = {}
    for ext_id in dict.fromkeys(ext_ids):
        if ext_id:
            module, name = ext_id.split(".", 1)
            grouped.setdefault(module, []).append(name)
    return grouped


def resolve_records(
    connection: Any, model: str, ext_ids: Iterable[str], fields: list[str]
) -> dict[str, dict[str, Any]]:
    """Read ``fields`` of every existing record behind the given external ids.

    Ids that do not exist yet are absent from the result.

    Args:
        connection: An Odoo connection exposing ``get_model``.
        model: The target model.
        ext_ids: Fully qualified external ids (see :func:`normalize_xmlid`).
        fields: The fields to read (``id`` is always included by Odoo).

    Returns:
        dict[str, dict[str, Any]]: External id -> record values.

    Raises:
        SourceReadError: When the records cannot be read. Callers fail closed:
            nothing proceeds blind over a guarded record.
    """
    names_by_module = _names_by_module(ext_ids)
    if not names_by_module:
        return {}

    try:
        ir_model_data = connection.get_model("ir.model.data")
        res_to_ext: dict[int, str] = {}
        for module, names in names_by_module.items():
            for i in range(0, len(names), CHUNK):
                rows = ir_model_data.search_read(
                    [
                        ("module", "=", module),
                        ("name", "in", names[i : i + CHUNK]),
                        ("model", "=", model),
                    ],
                    ["module", "name", "res_id"],
                )
                for row in rows:
                    res_to_ext[row["res_id"]] = f"{row['module']}.{row['name']}"

        records: dict[str, dict[str, Any]] = {}
        model_obj = connection.get_model(model)
        res_ids = list(res_to_ext)
        for i in range(0, len(res_ids), CHUNK):
            # search_read (not read): an ``ir.model.data`` row can point at a
            # record that has since been deleted. ``read`` would raise on that
            # missing id and abort the whole run; ``search_read`` with an
            # ``id in`` domain simply omits it, so the load recreates it as a
            # new record, exactly as it would without the guard.
            for rec in model_obj.search_read(
                [("id", "in", res_ids[i : i + CHUNK])], ["id", *fields]
            ):
                ext_id = res_to_ext.get(rec["id"])
                if ext_id is not None:
                    records[ext_id] = rec
        return records
    except Exception as e:
        raise SourceReadError(
            f"Could not read {fields} of existing '{model}' records to protect "
            f"posted/cancelled ones; aborting rather than proceeding blind. ({e})"
        ) from e


def resolve_states(
    connection: Any, model: str, ext_ids: Iterable[str], state_field: str
) -> dict[str, Any]:
    """Read the live value of ``state_field`` for each existing external id.

    Args:
        connection: An Odoo connection exposing ``get_model``.
        model: The target model.
        ext_ids: Fully qualified external ids (see :func:`normalize_xmlid`).
        state_field: The state field to read.

    Returns:
        dict[str, Any]: External id -> current value of ``state_field``.
    """
    records = resolve_records(connection, model, ext_ids, [state_field])
    return {x: rec.get(state_field) for x, rec in records.items()}


def resolve_states_by_dbid(
    connection: Any, model: str, db_ids: Iterable[int], state_field: str
) -> dict[int, Any]:
    """Read the live value of ``state_field`` for each database id.

    Ids that no longer exist are simply absent from the result (``search_read``
    drops them), so a stale id is treated as "not there", never an abort.

    Args:
        connection: An Odoo connection exposing ``get_model``.
        model: The target model.
        db_ids: Database ids to read.
        state_field: The state field to read.

    Returns:
        dict[int, Any]: Database id -> current value of ``state_field``.

    Raises:
        SourceReadError: When the records cannot be read (fail closed).
    """
    ids = sorted({int(i) for i in db_ids})
    if not ids:
        return {}
    try:
        model_obj = connection.get_model(model)
        out: dict[int, Any] = {}
        for i in range(0, len(ids), CHUNK):
            for rec in model_obj.search_read(
                [("id", "in", ids[i : i + CHUNK])], ["id", state_field]
            ):
                out[int(rec["id"])] = rec.get(state_field)
        return out
    except Exception as e:
        raise SourceReadError(
            f"Could not read '{state_field}' of '{model}' by id to protect "
            f"posted/cancelled records; aborting. ({e})"
        ) from e


def _top_id_column(
    header: list[str], id_field: str, id_is_dbid: bool
) -> tuple[int | None, bool]:
    """Resolve the column that identifies the record, and whether it is a db id.

    A file may identify records by their external id (the ``id_field`` column,
    usually ``id``) or by their Odoo database id (a ``.id`` column, or the write
    path's own ``id`` column, see ``id_is_dbid``). Either must be guarded
    (#413 review item 2).

    Args:
        header: The column names.
        id_field: The configured external-id column name.
        id_is_dbid: When True, the ``id_field`` column holds database ids (the
            ``fluvo write`` path uses the ``id`` column that way).

    Returns:
        tuple[int | None, bool]: ``(column_index, is_dbid)``; index is None when
        the file carries neither an external-id nor a ``.id`` column.
    """
    if id_field in header:
        return header.index(id_field), id_is_dbid or id_field == ".id"
    if ".id" in header:
        return header.index(".id"), True
    return None, False


def _dbid_key(raw: Any) -> str | None:
    """Return the string key for a database-id cell, or None if not an int."""
    try:
        return str(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _row_key(raw: Any, is_dbid: bool) -> str | None:
    """Normalise a record's identifier cell to its lookup key."""
    return _dbid_key(raw) if is_dbid else normalize_xmlid(str(raw))


def _top_level_reasons(
    connection: Any,
    model: str,
    rows: list[list[Any]],
    idx: int,
    is_dbid: bool,
    rule: ImmutableRule,
) -> dict[str, str]:
    """Build ``key -> reason`` for the guarded records a file targets directly.

    Args:
        connection: An Odoo connection.
        model: The guarded model.
        rows: The data rows.
        idx: The identifier column index.
        is_dbid: Whether that column holds database ids.
        rule: The model's guard rule.

    Returns:
        dict[str, str]: Lookup key -> reason, for guarded records only.
    """
    if is_dbid:
        ids = [k for r in rows if idx < len(r) and (k := _dbid_key(r[idx]))]
        states_by_id = resolve_states_by_dbid(
            connection, model, [int(i) for i in ids], rule.field
        )
        return {
            str(i): rule.reason_for(v)
            for i, v in states_by_id.items()
            if v in rule.values
        }
    ext_ids = [normalize_xmlid(r[idx]) for r in rows if idx < len(r) and r[idx]]
    states = resolve_states(connection, model, ext_ids, rule.field)
    return {x: rule.reason_for(v) for x, v in states.items() if v in rule.values}


def _is_continuation(
    row: list[Any], own_nonrel_idx: list[int], has_rel_subcols: bool, o2m: bool
) -> bool:
    """Whether an id-less row is a one2many continuation of the preceding record.

    Odoo's ``load`` attaches a row with no id of its own to the record before it
    in the batch when the file has relational sub-columns (``x/y``). Such a row
    must share its parent's fate — so a line is never left to land on the
    previous *draft* move when its real parent was guarded out (#413 review
    item 3). This holds whether or not ``--o2m`` was passed: the structural
    signal is an id-less row whose own, non-relational cells are all empty while
    the header carries sub-columns.

    Args:
        row: The data row (its identifier cell is already known to be empty).
        own_nonrel_idx: Indices of the record's own, non-relational columns.
        has_rel_subcols: Whether the header has any ``x/y`` sub-column.
        o2m: Whether ``--o2m`` was requested.

    Returns:
        bool: True when the row follows the preceding record.
    """
    if o2m:
        return True
    if not has_rel_subcols:
        return False
    return all(not str(row[i]).strip() for i in own_nonrel_idx if i < len(row))


def _split_rows(
    header: list[str],
    rows: list[list[Any]],
    idx: int,
    is_dbid: bool,
    reason_by_key: dict[str, str],
    o2m: bool,
) -> tuple[list[list[Any]], list[tuple[list[Any], str]]]:
    """Split rows into kept and guarded, carrying continuations with the parent.

    Args:
        header: The column names.
        rows: The data rows.
        idx: The identifier column index.
        is_dbid: Whether the identifier column holds database ids.
        reason_by_key: Lookup key -> reason, for guarded records only.
        o2m: Whether the file uses ``--o2m`` continuation rows.

    Returns:
        tuple[list[list[Any]], list[tuple[list[Any], str]]]: ``(kept, guarded)``;
        ``len(kept) + len(guarded) == len(rows)``.
    """
    own_nonrel_idx = [i for i, h in enumerate(header) if "/" not in h]
    has_rel_subcols = any("/" in h for h in header)
    kept: list[list[Any]] = []
    guarded: list[tuple[list[Any], str]] = []
    parent_reason: str | None = None
    for row in rows:
        raw = row[idx] if idx < len(row) else ""
        if str(raw).strip():
            key = _row_key(raw, is_dbid)
            parent_reason = reason_by_key.get(key) if key is not None else None
            if parent_reason is not None:
                guarded.append((row, parent_reason))
            else:
                kept.append(row)
        elif parent_reason is not None and _is_continuation(
            row, own_nonrel_idx, has_rel_subcols, o2m
        ):
            guarded.append((row, parent_reason))
        else:
            # A new record of its own (or a stray id-less row): it is never
            # guarded, and it becomes the parent of any rows that follow it.
            parent_reason = None
            kept.append(row)
    return kept, guarded


def guard_rows(
    connection: Any,
    model: str,
    header: list[str],
    rows: list[list[Any]],
    id_field: str,
    o2m: bool = False,
    id_is_dbid: bool = False,
) -> tuple[list[list[Any]], list[tuple[list[Any], str]]]:
    """Apply the guard for ``model`` to a loaded or written dataset.

    Args:
        connection: An Odoo connection exposing ``get_model``.
        model: The target model.
        header: The column names.
        rows: The data rows.
        id_field: The external-id column (a ``.id`` column is also honoured).
        o2m: Whether the file uses ``--o2m`` continuation rows.
        id_is_dbid: When True, the ``id_field`` column holds database ids
            (the ``fluvo write`` path).

    Returns:
        tuple[list[list[Any]], list[tuple[list[Any], str]]]: ``(kept, guarded)``;
        ``guarded`` is a list of ``(row, reason)``, empty when the model is
        unguarded or nothing was guarded.
    """
    rule = rule_for(model)
    if rule is None or not rows:
        return rows, []
    kept: list[list[Any]] = rows
    guarded: list[tuple[list[Any], str]] = []
    idx, is_dbid = _top_id_column(header, id_field, id_is_dbid)
    if idx is not None:
        reason_by_key = _top_level_reasons(connection, model, rows, idx, is_dbid, rule)
        kept, guarded = _split_rows(header, rows, idx, is_dbid, reason_by_key, o2m)
    if rule.parent is not None:
        kept, by_parent = _partition_by_parent(connection, header, kept, rule)
        guarded = guarded + by_parent
    return kept, guarded


def _parent_locks_by_xmlid(
    connection: Any,
    rows: list[list[Any]],
    idx: int,
    rule: ImmutableRule,
    parent: ParentRule,
) -> dict[str, str]:
    """Return reason-by-external-id for rows whose parent is guarded."""
    keys = {normalize_xmlid(r[idx]) for r in rows if idx < len(r) and r[idx]}
    states = resolve_states(connection, parent.model, keys, parent.state_field)
    return {k: rule.reason_for(v) for k, v in states.items() if v in rule.values}


def _parent_locks_by_dbid(
    connection: Any,
    rows: list[list[Any]],
    idx: int,
    rule: ImmutableRule,
    parent: ParentRule,
) -> dict[str, str]:
    """Return reason-by-database-id for rows whose parent is guarded (#358)."""
    db_ids = [int(r[idx]) for r in rows if idx < len(r) and r[idx]]
    states = resolve_states_by_dbid(
        connection, parent.model, db_ids, parent.state_field
    )
    return {str(i): rule.reason_for(v) for i, v in states.items() if v in rule.values}


def _partition_by_parent(
    connection: Any,
    header: list[str],
    rows: list[list[Any]],
    rule: ImmutableRule,
) -> tuple[list[list[Any]], list[tuple[list[Any], str]]]:
    """Split off rows whose parent record is in a guarded state.

    Args:
        connection: An Odoo connection exposing ``get_model``.
        header: The column names.
        rows: The data rows.
        rule: The child rule, whose ``parent`` points at the parent model.

    Returns:
        tuple[list[list[Any]], list[tuple[list[Any], str]]]: ``(kept, guarded)``
        with ``guarded`` a list of ``(row, reason)``.
    """
    parent = rule.parent
    if parent is None:  # pragma: no cover - the caller only calls with a parent
        return rows, []
    xml_col, db_col = f"{parent.field}/id", f"{parent.field}/.id"
    if xml_col in header:
        idx = header.index(xml_col)
        reason_by_key = _parent_locks_by_xmlid(connection, rows, idx, rule, parent)
        as_key = normalize_xmlid
    elif db_col in header:
        idx = header.index(db_col)
        reason_by_key = _parent_locks_by_dbid(connection, rows, idx, rule, parent)
        as_key = lambda raw: str(int(raw))  # noqa: E731
    else:
        if parent.field in header:
            log.warning(
                f"'{parent.field}' is referenced by name, so rows aimed at a "
                f"posted/cancelled '{parent.model}' cannot be detected before the "
                f"load. Use '{xml_col}' or '{db_col}' to have them guarded."
            )
        return rows, []
    if not reason_by_key:
        return rows, []
    kept: list[list[Any]] = []
    guarded: list[tuple[list[Any], str]] = []
    for row in rows:
        raw = row[idx] if idx < len(row) else ""
        reason = reason_by_key.get(as_key(raw)) if raw else None
        if reason is not None:
            guarded.append((row, reason))
        else:
            kept.append(row)
    return kept, guarded
