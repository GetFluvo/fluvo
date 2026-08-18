"""Tests for the declarative flow-file parser/validator (#251)."""

from pathlib import Path

import pytest

from fluvo.lib.flow import (
    FlowError,
    parse_flow_file,
    select_flows,
)

# A representative valid file used across several tests.
VALID = """
version: 1
vars:
  env: uat
  conn: "conf/{{ vars.env }}_connection.conf"
flows:
  - name: partners
    steps:
      - run: import
        with:
          connection_file: "{{ vars.conn }}"
          file: data/partners.csv
          model: res.partner
  - name: products
    on_error: continue
    steps:
      - id: create-products
        run: import
        with:
          connection_file: "{{ vars.conn }}"
          file: data/products.csv
          model: product.template
      - run: write
        with:
          connection_file: "{{ vars.conn }}"
          file: data/costs.csv
          model: product.template
"""


def _write(tmp_path: Path, text: str) -> str:
    """Write a flow file and return its path."""
    path = tmp_path / "flows.yml"
    path.write_text(text)
    return str(path)


def test_parses_valid_file(tmp_path: Path) -> None:
    """A well-formed file parses, resolves vars, and inherits on_error."""
    ff = parse_flow_file(_write(tmp_path, VALID))
    assert ff.version == 1
    assert ff.vars["conn"] == "conf/uat_connection.conf"
    assert [f.name for f in ff.flows] == ["partners", "products"]

    partners, products = ff.flows
    assert partners.on_error == "abort"
    assert products.on_error == "continue"
    # Interpolation reached into the step's `with`.
    assert partners.steps[0].with_["connection_file"] == "conf/uat_connection.conf"
    # Default vs explicit step id.
    assert partners.steps[0].id == "import-1"
    assert products.steps[0].id == "create-products"
    # Step inherits the flow's on_error default.
    assert products.steps[0].on_error == "continue"


def test_step_on_error_override(tmp_path: Path) -> None:
    """A step may override its flow's on_error."""
    text = """
version: 1
flows:
  - name: f
    on_error: continue
    steps:
      - run: import
        on_error: abort
        with: {model: res.partner}
"""
    ff = parse_flow_file(_write(tmp_path, text))
    assert ff.flows[0].steps[0].on_error == "abort"


# --- version ---------------------------------------------------------------

_ONE_STEP = "    steps:\n      - run: import\n        with: {model: x}\n"


@pytest.mark.parametrize(
    "version_line, message",
    [
        ("", "missing `version`"),
        ("version: 2", "unsupported version 2"),
        ("version: '1'", "`version` must be an integer"),
        ("version: true", "`version` must be an integer"),
    ],
)
def test_version_errors(tmp_path: Path, version_line: str, message: str) -> None:
    """Version must be present, integer, and supported."""
    text = f"{version_line}\nflows:\n  - name: f\n{_ONE_STEP}"
    with pytest.raises(FlowError, match=message):
        parse_flow_file(_write(tmp_path, text))


# --- top-level structure ---------------------------------------------------


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    """A non-mapping document is rejected."""
    with pytest.raises(FlowError, match="must be a mapping"):
        parse_flow_file(_write(tmp_path, "- just\n- a\n- list\n"))


def test_unknown_top_level_key(tmp_path: Path) -> None:
    """A typo'd top-level key (e.g. `flow:`) is caught."""
    text = "version: 1\nflow:\n  - name: f\n"
    with pytest.raises(FlowError, match="unknown top-level key"):
        parse_flow_file(_write(tmp_path, text))


def test_flows_must_be_non_empty_list(tmp_path: Path) -> None:
    """`flows` is required and must be non-empty."""
    with pytest.raises(FlowError, match="`flows` must be a non-empty list"):
        parse_flow_file(_write(tmp_path, "version: 1\nflows: []\n"))


def test_invalid_yaml(tmp_path: Path) -> None:
    """A YAML syntax error is reported as a flow error."""
    with pytest.raises(FlowError, match="Invalid YAML"):
        parse_flow_file(_write(tmp_path, "version: 1\nflows: [unclosed\n"))


def test_missing_file() -> None:
    """A missing file yields a clear error, not a traceback."""
    with pytest.raises(FlowError, match="Could not read flow file"):
        parse_flow_file("/no/such/flows.yml")


# --- flows and steps -------------------------------------------------------


def test_duplicate_flow_name(tmp_path: Path) -> None:
    """Two flows may not share a name."""
    text = """
version: 1
flows:
  - name: dup
    steps: [{run: import, with: {model: x}}]
  - name: dup
    steps: [{run: import, with: {model: y}}]
"""
    with pytest.raises(FlowError, match="duplicate flow name 'dup'"):
        parse_flow_file(_write(tmp_path, text))


def test_flow_missing_name(tmp_path: Path) -> None:
    """A flow without a name is rejected."""
    text = """
version: 1
flows:
  - steps: [{run: import, with: {model: x}}]
"""
    with pytest.raises(FlowError, match="`name` must be a non-empty string"):
        parse_flow_file(_write(tmp_path, text))


def test_unknown_flow_key(tmp_path: Path) -> None:
    """A typo'd flow key (e.g. `step:`) is caught."""
    text = "version: 1\nflows:\n  - name: f\n    step: []\n"
    with pytest.raises(FlowError, match="unknown key"):
        parse_flow_file(_write(tmp_path, text))


def test_steps_must_be_non_empty(tmp_path: Path) -> None:
    """A flow needs at least one step."""
    text = "version: 1\nflows:\n  - name: f\n    steps: []\n"
    with pytest.raises(FlowError, match="`steps` must be a non-empty list"):
        parse_flow_file(_write(tmp_path, text))


def test_bad_run_command(tmp_path: Path) -> None:
    """`run` must be one of the supported commands."""
    text = """
version: 1
flows:
  - name: f
    steps: [{run: teleport, with: {}}]
"""
    with pytest.raises(FlowError, match="`run` must be one of"):
        parse_flow_file(_write(tmp_path, text))


def test_with_must_be_mapping(tmp_path: Path) -> None:
    """`with` must be a mapping."""
    text = """
version: 1
flows:
  - name: f
    steps: [{run: import, with: nope}]
"""
    with pytest.raises(FlowError, match="`with` must be a mapping"):
        parse_flow_file(_write(tmp_path, text))


def test_bad_on_error(tmp_path: Path) -> None:
    """on_error only accepts abort/continue."""
    text = """
version: 1
flows:
  - name: f
    on_error: maybe
    steps: [{run: import, with: {model: x}}]
"""
    with pytest.raises(FlowError, match="`on_error` must be one of"):
        parse_flow_file(_write(tmp_path, text))


def test_duplicate_step_id(tmp_path: Path) -> None:
    """Step ids must be unique within a flow."""
    text = """
version: 1
flows:
  - name: f
    steps:
      - id: same
        run: import
        with: {model: x}
      - id: same
        run: write
        with: {model: y}
"""
    with pytest.raises(FlowError, match="duplicate step id 'same'"):
        parse_flow_file(_write(tmp_path, text))


def test_unknown_option_rejected_when_known_options_given(tmp_path: Path) -> None:
    """With a known-options map, an unknown `with` key is rejected."""
    text = """
version: 1
flows:
  - name: f
    steps: [{run: import, with: {bogus_flag: 1}}]
"""
    known = {"import": {"model", "file", "connection_file"}}
    with pytest.raises(FlowError, match="unknown option 'bogus_flag'"):
        parse_flow_file(_write(tmp_path, text), known_options=known)


def test_known_options_accepts_valid_keys(tmp_path: Path) -> None:
    """Valid `with` keys pass option validation."""
    text = """
version: 1
flows:
  - name: f
    steps: [{run: import, with: {model: res.partner}}]
"""
    known = {"import": {"model", "file"}}
    ff = parse_flow_file(_write(tmp_path, text), known_options=known)
    assert ff.flows[0].steps[0].with_["model"] == "res.partner"


# --- variables & interpolation ---------------------------------------------


def test_var_in_var_resolution(tmp_path: Path) -> None:
    """Vars may reference other vars, resolved to a fixed point."""
    text = """
version: 1
vars:
  a: base
  b: "{{ vars.a }}/mid"
  c: "{{ vars.b }}/leaf"
flows:
  - name: f
    steps: [{run: import, with: {file: "{{ vars.c }}"}}]
"""
    ff = parse_flow_file(_write(tmp_path, text))
    assert ff.flows[0].steps[0].with_["file"] == "base/mid/leaf"


def test_var_cycle_detected(tmp_path: Path) -> None:
    """A reference cycle among vars is reported, naming the cycle."""
    text = """
version: 1
vars:
  a: "{{ vars.b }}"
  b: "{{ vars.a }}"
flows:
  - name: f
    steps: [{run: import, with: {model: x}}]
"""
    with pytest.raises(FlowError, match="cycle detected"):
        parse_flow_file(_write(tmp_path, text))


def test_undefined_var_reference(tmp_path: Path) -> None:
    """Referencing an undefined var aborts, never silently empty."""
    text = """
version: 1
flows:
  - name: f
    steps: [{run: import, with: {file: "{{ vars.missing }}"}}]
"""
    with pytest.raises(FlowError, match="undefined variable"):
        parse_flow_file(_write(tmp_path, text))


def test_env_interpolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """{{ env.X }} reads an OS environment variable."""
    monkeypatch.setenv("FLUVO_TEST_DIR", "/data/prod")
    text = """
version: 1
flows:
  - name: f
    steps: [{run: import, with: {file: "{{ env.FLUVO_TEST_DIR }}/x.csv"}}]
"""
    ff = parse_flow_file(_write(tmp_path, text))
    assert ff.flows[0].steps[0].with_["file"] == "/data/prod/x.csv"


def test_undefined_env_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Referencing an unset env var aborts with a clear message."""
    monkeypatch.delenv("FLUVO_NOPE", raising=False)
    text = """
version: 1
flows:
  - name: f
    steps: [{run: import, with: {file: "{{ env.FLUVO_NOPE }}"}}]
"""
    with pytest.raises(FlowError, match="undefined environment variable"):
        parse_flow_file(_write(tmp_path, text))


def test_var_precedence_cli_over_env_over_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence is --var > env.* > vars block, scoped to declared names."""
    text = """
version: 1
vars:
  env: block
flows:
  - name: f
    steps: [{run: import, with: {file: "{{ vars.env }}"}}]
"""
    path = _write(tmp_path, text)

    # block only
    monkeypatch.delenv("env", raising=False)
    assert parse_flow_file(path).flows[0].steps[0].with_["file"] == "block"

    # env overrides block
    monkeypatch.setenv("env", "from_env")
    assert parse_flow_file(path).flows[0].steps[0].with_["file"] == "from_env"

    # --var overrides both
    ff = parse_flow_file(path, cli_vars={"env": "from_cli"})
    assert ff.flows[0].steps[0].with_["file"] == "from_cli"


def test_cli_var_not_declared_in_block(tmp_path: Path) -> None:
    """--var can introduce a variable not present in the vars block."""
    text = """
version: 1
flows:
  - name: f
    steps: [{run: import, with: {file: "{{ vars.extra }}"}}]
"""
    ff = parse_flow_file(_write(tmp_path, text), cli_vars={"extra": "value"})
    assert ff.flows[0].steps[0].with_["file"] == "value"


def test_bad_var_name(tmp_path: Path) -> None:
    """Variable names must be identifier-like."""
    text = """
version: 1
vars:
  "has space": x
flows:
  - name: f
    steps: [{run: import, with: {model: x}}]
"""
    with pytest.raises(FlowError, match="Invalid variable name"):
        parse_flow_file(_write(tmp_path, text))


# --- select_flows ----------------------------------------------------------


def test_select_all_by_default(tmp_path: Path) -> None:
    """No --run selects every flow in file order."""
    ff = parse_flow_file(_write(tmp_path, VALID))
    assert [f.name for f in select_flows(ff, None)] == ["partners", "products"]


def test_select_subset_preserves_file_order(tmp_path: Path) -> None:
    """--run selects the named flows, keeping file order (not argument order)."""
    ff = parse_flow_file(_write(tmp_path, VALID))
    selected = select_flows(ff, ["products", "partners"])
    assert [f.name for f in selected] == ["partners", "products"]


def test_select_unknown_flow(tmp_path: Path) -> None:
    """--run with an unknown name errors, listing what's available."""
    ff = parse_flow_file(_write(tmp_path, VALID))
    with pytest.raises(FlowError, match="no such flow"):
        select_flows(ff, ["nope"])
