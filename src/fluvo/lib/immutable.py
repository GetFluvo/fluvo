"""Guard for records Odoo will not let an import change (issue #358).

A posted ``account.move`` is immutable: a re-run of the load can only fail on
it, skip it, or duplicate it. Fluvo's contract is that a re-run is safe and
that ``created + failed + unaccounted == total`` holds with every bucket
*named*. This module keeps both true:

* before the load, the live state of every targeted record is read;
* rows whose record is in an immutable state are taken out of the load and
  counted under an explicit reason (``posted_immutable``), never silently;
* if that state cannot be read, the run aborts rather than guess (fail closed).

Fluvo keeps no record of who posted a move: one posted in Odoo between runs
takes exactly the same path as one posted by ``fluvo workflow post-moves``.

The rules are an internal registry, not user configuration, so the set of
guarded models grows deliberately (one line per model) and stays tested.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .core import SourceReadError
from .internal.tools import to_xmlid

#: The reason key recorded in ``unaccounted_reasons`` for guarded rows.
POSTED_IMMUTABLE = "posted_immutable"

#: Chunk size for ``ir.model.data`` / record reads.
CHUNK = 2000


@dataclass(frozen=True)
class ImmutableRule:
    """When a record of a model can no longer be changed by an import.

    Attributes:
        field: The state field to read.
        values: The values of ``field`` that make the record immutable.
        reason: The ``unaccounted`` sub-reason guarded rows are counted under.
    """

    field: str
    values: frozenset[str]
    reason: str = POSTED_IMMUTABLE


IMMUTABLE_RULES: dict[str, ImmutableRule] = {
    "account.move": ImmutableRule("state", frozenset({"posted"})),
    "account.move.line": ImmutableRule("parent_state", frozenset({"posted"})),
}


def rule_for(model: str) -> ImmutableRule | None:
    """Return the immutability rule for a model, if it is guarded.

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
        fields: The fields to read (``id`` is always included).

    Returns:
        dict[str, dict[str, Any]]: External id -> record values.

    Raises:
        SourceReadError: When the records cannot be read. Callers fail closed:
            nothing proceeds blind over immutable records.
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
            for rec in model_obj.read(res_ids[i : i + CHUNK], fields):
                ext_id = res_to_ext.get(rec["id"])
                if ext_id is not None:
                    records[ext_id] = rec
        return records
    except Exception as e:
        raise SourceReadError(
            f"Could not read {fields} of existing '{model}' records to protect "
            f"immutable ones; aborting rather than proceeding blind. ({e})"
        ) from e


def resolve_states(
    connection: Any, model: str, ext_ids: Iterable[str], field: str
) -> dict[str, Any]:
    """Read the live value of ``field`` for each existing external id.

    Args:
        connection: An Odoo connection exposing ``get_model``.
        model: The target model.
        ext_ids: Fully qualified external ids (see :func:`normalize_xmlid`).
        field: The state field to read.

    Returns:
        dict[str, Any]: External id -> current value of ``field``.
    """
    records = resolve_records(connection, model, ext_ids, [field])
    return {x: rec.get(field) for x, rec in records.items()}


def partition_immutable(
    header: list[str],
    rows: list[list[Any]],
    id_field: str,
    immutable_ids: set[str],
    o2m: bool = False,
) -> tuple[list[list[Any]], list[list[Any]]]:
    """Split rows into those to load and those guarded as immutable.

    In ``--o2m`` files the id-less continuation rows that follow a parent row
    belong to it, so they follow the parent's fate and are counted with it.

    Args:
        header: The column names.
        rows: The data rows.
        id_field: The external-id column.
        immutable_ids: Fully qualified ids of records in an immutable state.
        o2m: Whether continuation rows belong to the preceding parent.

    Returns:
        tuple[list[list[Any]], list[list[Any]]]: ``(kept, skipped)``;
        ``len(kept) + len(skipped)`` always equals ``len(rows)``.
    """
    if not immutable_ids or id_field not in header:
        return rows, []
    idx = header.index(id_field)
    kept: list[list[Any]] = []
    skipped: list[list[Any]] = []
    parent_skipped = False
    for row in rows:
        raw = row[idx] if idx < len(row) else ""
        if raw:
            parent_skipped = normalize_xmlid(raw) in immutable_ids
            (skipped if parent_skipped else kept).append(row)
        elif o2m and parent_skipped:
            skipped.append(row)
        else:
            kept.append(row)
    return kept, skipped


def guard_rows(
    connection: Any,
    model: str,
    header: list[str],
    rows: list[list[Any]],
    id_field: str,
    o2m: bool = False,
) -> tuple[list[list[Any]], list[list[Any]], str | None]:
    """Apply the immutability guard for ``model`` to a loaded dataset.

    Args:
        connection: An Odoo connection exposing ``get_model``.
        model: The target model.
        header: The column names.
        rows: The data rows.
        id_field: The external-id column.
        o2m: Whether the file uses ``--o2m`` continuation rows.

    Returns:
        tuple[list[list[Any]], list[list[Any]], str | None]:
        ``(kept, skipped, reason)``; ``reason`` is None when the model
        is unguarded or nothing was skipped.
    """
    rule = rule_for(model)
    if rule is None or id_field not in header or not rows:
        return rows, [], None
    idx = header.index(id_field)
    ext_ids = [normalize_xmlid(r[idx]) for r in rows if idx < len(r) and r[idx]]
    states = resolve_states(connection, model, ext_ids, rule.field)
    immutable = {x for x, v in states.items() if v in rule.values}
    kept, skipped = partition_immutable(header, rows, id_field, immutable, o2m)
    return kept, skipped, (rule.reason if skipped else None)


def skipped_file_for(fail_file: str | None, source_file: str) -> str:
    """Return the sidecar path that guarded rows are written to.

    Sits next to the fail file (``x_fail.csv`` -> ``x_skipped.csv``), or next
    to the source when there is no fail file. Skipped rows never go into the
    fail file: a ``--fail`` re-run would retry them forever.

    Args:
        fail_file: The fail file path, if any.
        source_file: The source data file.

    Returns:
        str: The skipped-rows file path.
    """
    base = fail_file or source_file
    if base.endswith("_fail.csv"):
        return base[: -len("_fail.csv")] + "_skipped.csv"
    stem, _dot, _ext = base.rpartition(".")
    return f"{stem or base}_skipped.csv"


def write_skipped_file(
    path: str,
    header: list[str],
    rows: list[list[Any]],
    reason: str,
    separator: str,
    encoding: str,
) -> None:
    """Write guarded rows with a ``_SKIP_REASON`` column.

    Args:
        path: Destination path.
        header: The column names.
        rows: The skipped rows.
        reason: The reason written on every row.
        separator: CSV delimiter.
        encoding: File encoding.
    """
    import csv

    with open(path, "w", newline="", encoding=encoding) as handle:
        writer = csv.writer(handle, delimiter=separator, quoting=csv.QUOTE_ALL)
        writer.writerow([*header, "_SKIP_REASON"])
        width = len(header)
        for row in rows:
            writer.writerow([*(list(row) + [""] * width)[:width], reason])
