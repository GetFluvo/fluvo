"""Parse, validate, and interpolate declarative flow files (``flows.yml``).

This is the pure parsing layer for the ``fluvo --flow-file`` runner (issue #251):
it turns a ``flows.yml`` into a validated :class:`FlowFile` with fully-resolved
variables, and raises :class:`FlowError` with a clear, user-facing message on any
problem. It executes nothing and has no dependency on the CLI commands — the
executor (a separate layer) maps each validated step onto its command.

Schema (v1)::

    version: 1
    vars:                       # optional; interpolated as {{ vars.NAME }}
      env: uat
      conn: "conf/{{ vars.env }}_connection.conf"
    flows:                      # one or more named flows, run in file order
      - name: partners
        on_error: abort         # abort (default) | continue
        steps:
          - run: import         # import | export | write | migrate
            id: create-partners # optional, unique within the flow
            with:               # keys are the command's CLI flags, dashes->underscores
              connection_file: "{{ vars.conn }}"
              file: data/partners.csv
              model: res.partner

Variable precedence is ``--var`` > ``env.*`` (OS environment) > the ``vars`` block,
scoped to names the author declared (or passed with ``--var``). ``vars`` may
reference other vars; they are resolved to a fixed point with cycle detection.
Connections are always referenced by *file path* — a flow file never contains a
password.
"""

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, cast

import yaml

SUPPORTED_VERSION = 1
VALID_COMMANDS = ("import", "export", "write", "migrate")
VALID_ON_ERROR = ("abort", "continue")

_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTERP_RE = re.compile(r"\{\{\s*(vars|env)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_MAX_VAR_DEPTH = 25  # defensive cap on nested var-in-var resolution

_TOP_KEYS = {"version", "vars", "flows"}
_FLOW_KEYS = {"name", "on_error", "steps"}
_STEP_KEYS = {"run", "id", "with", "on_error"}


class FlowError(Exception):
    """A ``flows.yml`` is malformed or invalid.

    The message is user-facing and should name the problem precisely (which flow,
    which step, which key) so the author can fix it without guesswork.
    """


@dataclass
class Step:
    """A single step in a flow: one invocation of a fluvo command."""

    run: str
    with_: dict[str, Any]
    id: str
    on_error: str


@dataclass
class Flow:
    """A named, ordered sequence of steps."""

    name: str
    on_error: str
    steps: list[Step]


@dataclass
class FlowFile:
    """A parsed, validated flow file with variables fully resolved."""

    version: int
    vars: dict[str, str]
    flows: list[Flow]


def _interpolate(text: str, get_var: Callable[[str], str], *, where: str) -> str:
    """Replace ``{{ vars.X }}`` / ``{{ env.X }}`` markers in a string.

    Args:
        text: The string to interpolate.
        get_var: Resolver for ``vars.X`` references.
        where: Human-readable context for error messages (e.g. the step).

    Returns:
        str: The interpolated string.

    Raises:
        FlowError: If a referenced ``env.X`` variable is not set.
    """

    def repl(match: "re.Match[str]") -> str:
        namespace, name = match.group(1), match.group(2)
        if namespace == "vars":
            return get_var(name)
        value = os.environ.get(name)
        if value is None:
            raise FlowError(
                f"{where}: undefined environment variable "
                f"'{{{{ env.{name} }}}}' (it is not set in the environment)."
            )
        return value

    return _INTERP_RE.sub(repl, text)


def _resolve_vars(raw: dict[str, str]) -> dict[str, str]:
    """Resolve var-in-var references to a fixed point, detecting cycles.

    Args:
        raw: The raw variable values (after source precedence is applied), each of
            which may itself contain ``{{ vars.X }}`` / ``{{ env.X }}`` markers.

    Returns:
        dict[str, str]: Fully-resolved variable values.

    Raises:
        FlowError: On a reference cycle, an undefined ``vars`` reference, or
            nesting deeper than the safety cap.
    """
    resolved: dict[str, str] = {}
    resolving: list[str] = []

    def resolve_one(name: str) -> str:
        if name in resolved:
            return resolved[name]
        if name in resolving:
            cycle = " -> ".join([*resolving, name])
            raise FlowError(f"Variable reference cycle detected: {cycle}")
        if name not in raw:
            raise FlowError(
                f"Undefined variable '{{{{ vars.{name} }}}}' "
                "(it is not declared in `vars` and was not passed with --var)."
            )
        if len(resolving) >= _MAX_VAR_DEPTH:
            raise FlowError(
                f"Variable nesting too deep (> {_MAX_VAR_DEPTH}) while resolving "
                f"'{name}'; check for an overly-deep or cyclic reference."
            )
        resolving.append(name)
        value = _interpolate(raw[name], resolve_one, where=f"var '{name}'")
        resolving.pop()
        resolved[name] = value
        return value

    for name in raw:
        resolve_one(name)
    return resolved


def _effective_raw_vars(
    vars_block: dict[str, str], cli_vars: dict[str, str]
) -> dict[str, str]:
    """Apply source precedence to raw (pre-interpolation) variable values.

    Precedence is ``--var`` > ``env.*`` > the ``vars`` block, scoped to names the
    author declared or passed with ``--var`` — an arbitrary OS variable never
    becomes a flow variable on its own (use ``{{ env.X }}`` for that).

    Args:
        vars_block: The declared ``vars`` mapping from the file.
        cli_vars: Overrides passed via ``--var NAME=VALUE``.

    Returns:
        dict[str, str]: The effective raw value for each variable name.
    """
    raw: dict[str, str] = {}
    for name in set(vars_block) | set(cli_vars):
        if name in cli_vars:
            raw[name] = cli_vars[name]
        elif name in os.environ:
            raw[name] = os.environ[name]
        else:
            raw[name] = vars_block[name]
    return raw


def _require(condition: bool, message: str) -> None:
    """Raise :class:`FlowError` with ``message`` unless ``condition`` holds.

    Args:
        condition: The invariant that must hold.
        message: The user-facing error message if it does not.

    Raises:
        FlowError: If ``condition`` is falsy.
    """
    if not condition:
        raise FlowError(message)


def _parse_vars(data: Any) -> dict[str, str]:
    """Validate and stringify the top-level ``vars`` block.

    Args:
        data: The raw value of the ``vars`` key (may be ``None``).

    Returns:
        dict[str, str]: Declared variables as strings.

    Raises:
        FlowError: If ``vars`` is not a mapping or has non-identifier names.
    """
    if data is None:
        return {}
    _require(isinstance(data, dict), "`vars` must be a mapping of name to value.")
    out: dict[str, str] = {}
    for name, value in data.items():
        _require(
            isinstance(name, str) and bool(_VAR_NAME_RE.match(name)),
            f"Invalid variable name '{name}': use letters, digits and underscores, "
            "starting with a letter or underscore.",
        )
        _require(
            not isinstance(value, (dict, list)),
            f"Variable '{name}' must be a scalar (string/number/bool), not a "
            "list or mapping.",
        )
        out[name] = "" if value is None else str(value)
    return out


def _interp_with_value(value: Any, resolved: dict[str, str], where: str) -> Any:
    """Interpolate variable markers inside a ``with:`` value (string or list).

    Args:
        value: A scalar or list value from a step's ``with`` mapping.
        resolved: Fully-resolved variables.
        where: Context for error messages.

    Returns:
        Any: The value with any string markers interpolated.

    Raises:
        FlowError: On an undefined variable reference.
    """

    def get_var(name: str) -> str:
        if name not in resolved:
            raise FlowError(f"{where}: undefined variable '{{{{ vars.{name} }}}}'.")
        return resolved[name]

    if isinstance(value, str):
        return _interpolate(value, get_var, where=where)
    if isinstance(value, list):
        return [
            _interpolate(v, get_var, where=where) if isinstance(v, str) else v
            for v in value
        ]
    return value


def _parse_step(
    raw: Any,
    index: int,
    flow_name: str,
    flow_on_error: str,
    resolved_vars: dict[str, str],
    seen_ids: set[str],
    known_options: Optional[dict[str, set[str]]],
) -> Step:
    """Validate one raw step mapping into a :class:`Step`.

    Args:
        raw: The raw step mapping.
        index: 1-based position of the step within its flow.
        flow_name: The enclosing flow's name (for messages and default ids).
        flow_on_error: The flow's ``on_error`` default, inherited unless overridden.
        resolved_vars: Fully-resolved variables for ``with`` interpolation.
        seen_ids: Step ids already used in this flow (mutated to track uniqueness).
        known_options: Optional map of command -> valid ``with`` keys; when given,
            unknown keys are rejected.

    Returns:
        Step: The validated step.

    Raises:
        FlowError: On any structural or reference problem.
    """
    loc = f"flow '{flow_name}', step {index}"
    _require(isinstance(raw, dict), f"{loc}: each step must be a mapping.")
    unknown = set(raw) - _STEP_KEYS
    _require(
        not unknown,
        f"{loc}: unknown key(s) {sorted(unknown)}; valid keys are "
        f"{sorted(_STEP_KEYS)}.",
    )

    run = raw.get("run")
    _require(
        isinstance(run, str) and run in VALID_COMMANDS,
        f"{loc}: `run` must be one of {list(VALID_COMMANDS)}, got {run!r}.",
    )
    run = cast(str, run)  # narrowed by _require above

    on_error = raw.get("on_error", flow_on_error)
    _require(
        on_error in VALID_ON_ERROR,
        f"{loc}: `on_error` must be one of {list(VALID_ON_ERROR)}, got {on_error!r}.",
    )

    with_raw = raw.get("with")
    _require(
        isinstance(with_raw, dict),
        f"{loc}: `with` must be a mapping of options for `{run}`.",
    )
    with_raw = cast("dict[str, Any]", with_raw)
    if known_options is not None:
        valid = known_options.get(run, set())
        for key in with_raw:
            _require(
                key in valid,
                f"{loc}: unknown option '{key}' for `{run}`. "
                f"Valid options: {sorted(valid)}.",
            )
    with_: dict[str, Any] = {
        key: _interp_with_value(value, resolved_vars, f"{loc}, option '{key}'")
        for key, value in with_raw.items()
    }

    step_id = raw.get("id", f"{run}-{index}")
    _require(
        isinstance(step_id, str) and bool(step_id.strip()),
        f"{loc}: `id` must be a non-empty string.",
    )
    step_id = cast(str, step_id)
    _require(
        step_id not in seen_ids,
        f"{loc}: duplicate step id '{step_id}' within flow '{flow_name}'.",
    )
    seen_ids.add(step_id)

    return Step(run=run, with_=with_, id=step_id, on_error=on_error)


def _parse_flow(
    raw: Any,
    index: int,
    resolved_vars: dict[str, str],
    known_options: Optional[dict[str, set[str]]],
) -> Flow:
    """Validate one raw flow mapping into a :class:`Flow`.

    Args:
        raw: The raw flow mapping.
        index: 1-based position of the flow in the file.
        resolved_vars: Fully-resolved variables.
        known_options: Optional map of command -> valid ``with`` keys.

    Returns:
        Flow: The validated flow.

    Raises:
        FlowError: On any structural problem.
    """
    loc = f"flow {index}"
    _require(isinstance(raw, dict), f"{loc}: each flow must be a mapping.")
    unknown = set(raw) - _FLOW_KEYS
    _require(
        not unknown,
        f"{loc}: unknown key(s) {sorted(unknown)}; valid keys are "
        f"{sorted(_FLOW_KEYS)}.",
    )

    name = raw.get("name")
    _require(
        isinstance(name, str) and bool(name.strip()),
        f"{loc}: `name` must be a non-empty string.",
    )
    name = cast(str, name)

    on_error = raw.get("on_error", "abort")
    _require(
        on_error in VALID_ON_ERROR,
        f"flow '{name}': `on_error` must be one of {list(VALID_ON_ERROR)}, "
        f"got {on_error!r}.",
    )

    steps_raw = raw.get("steps")
    _require(
        isinstance(steps_raw, list) and len(steps_raw) > 0,
        f"flow '{name}': `steps` must be a non-empty list.",
    )
    steps_raw = cast("list[Any]", steps_raw)

    seen_ids: set[str] = set()
    steps = [
        _parse_step(step_raw, i, name, on_error, resolved_vars, seen_ids, known_options)
        for i, step_raw in enumerate(steps_raw, start=1)
    ]
    return Flow(name=name, on_error=on_error, steps=steps)


def parse_flow_file(
    path: str,
    *,
    cli_vars: Optional[dict[str, str]] = None,
    known_options: Optional[dict[str, set[str]]] = None,
) -> FlowFile:
    """Parse and validate a ``flows.yml`` into a resolved :class:`FlowFile`.

    Args:
        path: Path to the flow file.
        cli_vars: Overrides from ``--var NAME=VALUE`` (highest precedence).
        known_options: Optional map of command name -> the set of valid ``with``
            option keys (dashes replaced with underscores). When supplied, unknown
            options are rejected with a clear error; when omitted, option-key
            validation is skipped (the executor supplies the real map).

    Returns:
        FlowFile: The validated flow file with variables fully resolved.

    Raises:
        FlowError: If the file cannot be read or is invalid in any way.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise FlowError(f"Could not read flow file '{path}': {exc}") from exc

    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FlowError(f"Invalid YAML in flow file '{path}':\n{exc}") from exc

    _require(
        isinstance(data, dict),
        f"Flow file '{path}' must be a mapping with a `version` and `flows`.",
    )
    unknown = set(data) - _TOP_KEYS
    _require(
        not unknown,
        f"Flow file '{path}': unknown top-level key(s) {sorted(unknown)}; "
        f"valid keys are {sorted(_TOP_KEYS)}.",
    )

    version = data.get("version")
    _require(
        version is not None,
        f"Flow file '{path}': missing `version` (expected {SUPPORTED_VERSION}).",
    )
    _require(
        isinstance(version, int) and not isinstance(version, bool),
        f"Flow file '{path}': `version` must be an integer, got {version!r}.",
    )
    _require(
        version == SUPPORTED_VERSION,
        f"Flow file '{path}': unsupported version {version} "
        f"(this fluvo supports version {SUPPORTED_VERSION}).",
    )

    vars_block = _parse_vars(data.get("vars"))
    resolved_vars = _resolve_vars(_effective_raw_vars(vars_block, cli_vars or {}))

    flows_raw = data.get("flows")
    _require(
        isinstance(flows_raw, list) and len(flows_raw) > 0,
        f"Flow file '{path}': `flows` must be a non-empty list.",
    )
    flows_raw = cast("list[Any]", flows_raw)

    flows = [
        _parse_flow(flow_raw, i, resolved_vars, known_options)
        for i, flow_raw in enumerate(flows_raw, start=1)
    ]

    seen_names: set[str] = set()
    for flow in flows:
        _require(
            flow.name not in seen_names,
            f"Flow file '{path}': duplicate flow name '{flow.name}'.",
        )
        seen_names.add(flow.name)

    return FlowFile(version=SUPPORTED_VERSION, vars=resolved_vars, flows=flows)


def select_flows(flow_file: FlowFile, names: Optional[list[str]]) -> list[Flow]:
    """Return the flows to run, in file order.

    Args:
        flow_file: The parsed flow file.
        names: Flow names requested via ``--run`` (comma-split by the CLI), or
            ``None``/empty to run every flow.

    Returns:
        list[Flow]: The selected flows, preserving file order.

    Raises:
        FlowError: If a requested name does not exist in the file.
    """
    if not names:
        return flow_file.flows
    available = {flow.name: flow for flow in flow_file.flows}
    unknown = [name for name in names if name not in available]
    _require(
        not unknown,
        f"--run: no such flow {unknown} in the file. "
        f"Available flows: {sorted(available)}.",
    )
    requested = set(names)
    return [flow for flow in flow_file.flows if flow.name in requested]
