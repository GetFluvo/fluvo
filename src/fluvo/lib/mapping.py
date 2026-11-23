"""Compile a declarative ``mapping:`` block into a ``dict[str, MapperFunc]`` (#352).

Most columns in a real migration are boring: copy this one, default that one,
re-key this relation. This module lets a ``flows.yml`` import step declare those
columns in YAML instead of a Python transform script, and compiles them to the
*same* ``mapper`` calls the hand-written scripts already use. It adds no
transformation logic and no new execution path: the compiled mapping is fed to
the existing :class:`~fluvo.lib.transform.Processor`, which writes a transformed
file the import then loads.

This first increment supports four primitives — ``copy``, ``const``, ``xmlid``
and ``substitute`` — on ``import`` steps only. ``relation`` (needs a live
connection), ``py:`` (code execution from YAML needs its own opt-in design) and
``write`` are deliberately deferred.

Validation happens in two phases so a mistake is caught early and precisely:

- **Structural** (:func:`parse_mapping`, at flow load) — the mapping's own shape:
  version, model, the ``id:`` block, one primitive per column, known keys. Raises
  :class:`~fluvo.lib.flow.FlowError`.
- **Against the source** (:func:`validate_against_source`, at step start, before
  any RPC) — columns the mapping names must exist, every source column must be
  mapped or explicitly ignored, ``id`` keys must be present, and an ``error``
  substitution must cover every value seen. Raises
  :class:`~fluvo.lib.core.InputError`.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import polars as pl

from . import mapper
from .core import InputError

MapperFunc = Callable[..., Any]

_PRIMITIVES = ("copy", "const", "xmlid", "substitute")
_ON_MISSING = ("error", "blank", "keep")
_TOP_KEYS = {"version", "model", "id", "columns", "ignore"}


@dataclass(frozen=True)
class ColumnRule:
    """One compiled column directive: exactly one primitive and its arguments.

    Attributes:
        kind: The primitive — ``copy``, ``const``, ``xmlid`` or ``substitute``.
        source: The source column the rule reads (all but ``const``).
        value: The constant value (``const`` only).
        prefix: The external-id prefix (``xmlid`` only).
        table: The value→value substitution table (``substitute`` only).
        on_missing: How ``substitute`` treats an unmapped value
            (``error``/``blank``/``keep``).
    """

    kind: str
    source: str | None = None
    value: Any = None
    prefix: str | None = None
    table: dict[str, str] | None = None
    on_missing: str | None = None


@dataclass(frozen=True)
class MappingSpec:
    """A parsed, structurally-valid ``mapping:`` block.

    Attributes:
        model: The Odoo model the mapped file targets.
        id_prefix: The external-id prefix for the row's own id.
        id_from: The source column the row's external id is built from.
        columns: Output column name -> its :class:`ColumnRule`, in declared order.
        ignore: Source columns deliberately dropped (not an error to omit them).
    """

    model: str
    id_prefix: str
    id_from: str
    columns: dict[str, ColumnRule] = field(default_factory=dict)
    ignore: tuple[str, ...] = ()


def _fail(where: str, message: str) -> None:
    """Raise a :class:`~fluvo.lib.flow.FlowError` with a located message.

    Args:
        where: Human-readable context (flow, step) prefixed to the message.
        message: The specific problem.

    Raises:
        FlowError: Always.
    """
    from .flow import FlowError

    raise FlowError(f"{where}: {message}")


def _parse_column(col: str, raw: Any, where: str) -> ColumnRule:  # noqa: C901
    """Validate one column directive into a :class:`ColumnRule`.

    Args:
        col: The output column name.
        raw: The raw directive mapping (e.g. ``{"copy": "Name"}``).
        where: Context for error messages.

    Returns:
        ColumnRule: The validated rule.
    """
    if col == "id":
        _fail(where, "column 'id' is reserved; use the `id:` block.")
    if not isinstance(raw, dict):
        _fail(where, f"column '{col}': must be a mapping with one primitive.")
    present = [k for k in raw if k in _PRIMITIVES]
    unknown = [k for k in raw if k not in _PRIMITIVES]
    if unknown:
        _fail(where, f"column '{col}': unknown primitive '{unknown[0]}'.")
    if len(present) != 1:
        _fail(
            where,
            f"column '{col}': exactly one primitive required, got {sorted(raw)}.",
        )
    kind = present[0]
    arg = raw[kind]
    if kind == "copy":
        if not isinstance(arg, str) or not arg:
            _fail(where, f"column '{col}': `copy` takes a source column name.")
        return ColumnRule(kind="copy", source=arg)
    if kind == "const":
        if isinstance(arg, (dict, list)):
            _fail(where, f"column '{col}': `const` takes a scalar value.")
        return ColumnRule(kind="const", value=arg)
    if kind == "xmlid":
        if not isinstance(arg, dict) or set(arg) != {"prefix", "from"}:
            _fail(where, f"column '{col}': `xmlid` needs `prefix` and `from`.")
        return ColumnRule(
            kind="xmlid", prefix=str(arg["prefix"]), source=str(arg["from"])
        )
    # substitute
    if not isinstance(arg, dict):
        _fail(where, f"column '{col}': `substitute` needs `from` and `map`.")
    unknown_sub = set(arg) - {"from", "map", "on_missing"}
    if unknown_sub:
        _fail(
            where, f"column '{col}': substitute unknown key '{sorted(unknown_sub)[0]}'."
        )
    if "from" not in arg or "map" not in arg:
        _fail(where, f"column '{col}': `substitute` needs `from` and `map`.")
    if not isinstance(arg["map"], dict):
        _fail(where, f"column '{col}': substitute `map` must be a value table.")
    on_missing = arg.get("on_missing", "error")
    if on_missing not in _ON_MISSING:
        _fail(
            where,
            f"column '{col}': substitute.on_missing must be {'|'.join(_ON_MISSING)}.",
        )
    table = {str(k): str(v) for k, v in arg["map"].items()}
    return ColumnRule(
        kind="substitute", source=str(arg["from"]), table=table, on_missing=on_missing
    )


def parse_mapping(raw: Any, *, where: str) -> MappingSpec:
    """Validate a mapping block's own structure into a :class:`MappingSpec`.

    Args:
        raw: The mapping body (from a sidecar file or an ``inline:`` block).
        where: Context (flow, step) prefixed to every error message.

    Returns:
        MappingSpec: The structurally-valid mapping. On any structural problem a
        :class:`~fluvo.lib.flow.FlowError` is raised (via :func:`_fail`), naming
        the flow, step and column.
    """
    if not isinstance(raw, dict):
        _fail(where, "mapping must be a mapping with `version`, `model` and `columns`.")
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        _fail(where, f"mapping: unknown key '{sorted(unknown)[0]}'.")
    if raw.get("version") != 1:
        _fail(where, "mapping version must be 1.")
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        _fail(where, "mapping: `model` is required.")
    id_block = raw.get("id")
    if not isinstance(id_block, dict) or set(id_block) != {"prefix", "from"}:
        _fail(where, "mapping: `id` needs `prefix` and `from`.")
    columns_raw = raw.get("columns")
    if not isinstance(columns_raw, dict) or not columns_raw:
        _fail(where, "mapping: `columns` must be a non-empty mapping.")
    ignore_raw = raw.get("ignore", [])
    if not isinstance(ignore_raw, list) or not all(
        isinstance(c, str) for c in ignore_raw
    ):
        _fail(where, "mapping: `ignore` must be a list of source column names.")
    columns = {
        col: _parse_column(col, spec, where) for col, spec in columns_raw.items()
    }
    return MappingSpec(
        model=str(model),
        id_prefix=str(id_block["prefix"]),
        id_from=str(id_block["from"]),
        columns=columns,
        ignore=tuple(ignore_raw),
    )


def _consumed_source_columns(spec: MappingSpec) -> set[str]:
    """Return the source columns the mapping reads.

    Args:
        spec: The mapping.

    Returns:
        set[str]: Every source column named by the ``id:`` block or a rule.
    """
    consumed = {spec.id_from}
    for rule in spec.columns.values():
        if rule.source:
            consumed.add(rule.source)
    return consumed


def validate_against_source(spec: MappingSpec, source: pl.DataFrame) -> None:
    """Check a mapping against the actual source rows, before any RPC.

    Args:
        spec: The structurally-valid mapping.
        source: The source rows (all columns read as strings).

    Raises:
        InputError: If the mapping names a missing column, leaves a source
            column neither mapped nor ignored, has empty ``id`` keys, or an
            ``error`` substitution does not cover every value seen.
    """
    source_cols = set(source.columns)
    consumed = _consumed_source_columns(spec)

    missing = consumed - source_cols
    if missing:
        raise InputError(
            f"mapping references source column(s) not in the file: {sorted(missing)}"
        )

    unaccounted = source_cols - consumed - set(spec.ignore)
    if unaccounted:
        raise InputError(
            "source column(s) neither mapped nor ignored: "
            f"{sorted(unaccounted)} (map them or add to `ignore`)"
        )

    id_col = source.get_column(spec.id_from)
    empty_mask = id_col.is_null() | (id_col.cast(pl.String).str.strip_chars() == "")
    empty_count = int(empty_mask.sum())
    if empty_count:
        sample = (
            source.filter(empty_mask).head(5).select(source.columns[0]).to_series()
        ).to_list()
        raise InputError(
            f"id: empty `{spec.id_from}` on {empty_count} row(s); first keys "
            f"(first source column): {sample}"
        )

    for col, rule in spec.columns.items():
        if rule.kind != "substitute" or rule.on_missing != "error":
            continue
        table = cast("dict[str, str]", rule.table)
        seen = (
            source.get_column(cast(str, rule.source))
            .cast(pl.String)
            .drop_nulls()
            .unique()
            .to_list()
        )
        unmapped = sorted({v for v in seen if v != "" and v not in table})
        if unmapped:
            raise InputError(
                f"substitute '{col}': unmapped value(s) {unmapped[:10]}"
                + (f" (+{len(unmapped) - 10} more)" if len(unmapped) > 10 else "")
            )


def _substitute(table: dict[str, str], source: str, on_missing: str) -> MapperFunc:
    """Build a substitution mapper honouring ``on_missing``.

    Args:
        table: The value→value lookup.
        source: The source column to read.
        on_missing: ``error`` (raise), ``blank`` (empty) or ``keep`` (original).

    Returns:
        MapperFunc: The substitution mapper.
    """
    key = mapper.val(source)

    def substitute_fun(line: dict[str, Any], state: dict[str, Any]) -> Any:
        raw = key(line, state)
        value = "" if raw is None else str(raw)
        if value in table:
            return table[value]
        if on_missing == "blank" or value == "":
            return ""
        if on_missing == "keep":
            return value
        # on_missing == "error": validation should have caught this, but never
        # emit a silently wrong value if a row slips through.
        raise InputError(f"substitute: unmapped value '{value}' in column '{source}'")

    return substitute_fun


def compile_mapping(spec: MappingSpec) -> dict[str, MapperFunc]:
    """Compile a validated mapping into ``output column -> MapperFunc``.

    Args:
        spec: The mapping, already validated against its source.

    Returns:
        dict[str, MapperFunc]: The mapper dict for a
        :class:`~fluvo.lib.transform.Processor`, with ``id`` first.
    """
    out: dict[str, MapperFunc] = {"id": mapper.m2o(spec.id_prefix, spec.id_from)}
    for col, rule in spec.columns.items():
        if rule.kind == "copy":
            out[col] = mapper.val(cast(str, rule.source))
        elif rule.kind == "const":
            out[col] = mapper.const(rule.value)
        elif rule.kind == "xmlid":
            out[col] = mapper.m2o(cast(str, rule.prefix), cast(str, rule.source))
        else:  # substitute
            out[col] = _substitute(
                cast("dict[str, str]", rule.table),
                cast(str, rule.source),
                rule.on_missing or "error",
            )
    return out
