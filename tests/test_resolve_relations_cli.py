"""Tests for the --resolve-relation CLI flag and its parser."""

from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from fluvo import __main__
from fluvo.__main__ import _parse_resolve_relation_specs


def test_parse_four_part_spec() -> None:
    """A 4-part spec parses to a dict without a 'to' key."""
    out = _parse_resolve_relation_specs(("country:res.country:code:country_id",))
    assert out == [
        {
            "source_column": "country",
            "model": "res.country",
            "key_field": "code",
            "relation_field": "country_id",
        }
    ]


def test_parse_five_part_spec_includes_to() -> None:
    """A 5-part spec carries the 'to' target."""
    out = _parse_resolve_relation_specs(("c:res.country:code:country_id:dbid",))
    assert out[0]["to"] == "dbid"


def test_parse_multiple_specs() -> None:
    """Several specs produce several dicts."""
    out = _parse_resolve_relation_specs(
        (
            "country:res.country:code:country_id",
            "parent:res.partner:ref:parent_id:xmlid",
        )
    )
    assert len(out) == 2


def test_parse_rejects_wrong_part_count() -> None:
    """A spec with too few parts is rejected."""
    with pytest.raises(click.BadParameter):
        _parse_resolve_relation_specs(("country:res.country:code",))


def test_parse_rejects_invalid_to() -> None:
    """An invalid 'to' value is rejected."""
    with pytest.raises(click.BadParameter):
        _parse_resolve_relation_specs(("c:res.country:code:country_id:nope",))


@patch("fluvo.__main__.run_import")
def test_cli_resolve_relation_flows_to_run_import(mock_run_import: MagicMock) -> None:
    """--resolve-relation is parsed and passed to run_import as resolve_relations."""
    mock_run_import.return_value = {"x": 1}
    runner = CliRunner()
    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "import",
                "--connection-file",
                "conn.conf",
                "--file",
                "my.csv",
                "--model",
                "res.partner",
                "--resolve-relation",
                "country:res.country:code:country_id",
            ],
        )
    assert result.exit_code == 0
    call_kwargs = mock_run_import.call_args.kwargs
    assert call_kwargs["resolve_relations"] == [
        {
            "source_column": "country",
            "model": "res.country",
            "key_field": "code",
            "relation_field": "country_id",
        }
    ]
