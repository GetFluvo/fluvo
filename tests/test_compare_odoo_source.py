"""Tests for comparing two Odoo instances — ``--against-connection`` (#350).

The comparator itself is covered in ``test_compare.py``; what is new here is the
second *source adapter*. So these tests are about the seam: which side is read
with which settings, what happens when the two instances disagree about a field,
and that the spreadsheet-only options are refused rather than quietly ignored.

No connection is ever opened: both exports and ``fields_get`` are stubbed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from fluvo.lib.compare import (
    ANGLO,
    CompareError,
    CompareOptions,
    CompareResult,
    read_source_odoo,
    run_compare,
    to_markdown,
)

_TARGET = "target.conf"
_SOURCE = "source.conf"


def _fields(**overrides: dict[str, Any]) -> dict[str, Any]:
    """A small ``fields_get`` stub, with per-test overrides."""
    base: dict[str, Any] = {
        "ref": {"type": "char"},
        "name": {"type": "char"},
        "credit_limit": {"type": "float"},
        "active": {"type": "boolean"},
        "type": {
            "type": "selection",
            "selection": [["contact", "Contact"], ["invoice", "Invoice Address"]],
        },
    }
    base.update(overrides)
    return base


@contextmanager
def _odoo(
    source: pl.DataFrame,
    target: pl.DataFrame,
    *,
    source_fields: dict[str, Any] | None = None,
    target_fields: dict[str, Any] | None = None,
) -> Iterator[MagicMock]:
    """Stub both instances: ``fields_get`` per side, and both exports.

    Yields:
        MagicMock: the patched ``export_dataframe``, so a test can assert on the
        domain and context each side was read with.
    """

    def _connect(config: Any) -> MagicMock:
        connection = MagicMock()
        wanted = source_fields if config == _SOURCE else target_fields
        connection.get_model.return_value.fields_get.return_value = (
            wanted if wanted is not None else _fields()
        )
        return connection

    def _export(config: Any, model: str, fields: list[str], **_: Any) -> pl.DataFrame:
        frame = source if config == _SOURCE else target
        return frame.select([c for c in frame.columns if c in fields])

    with (
        patch("fluvo.lib.compare._connect", side_effect=_connect),
        patch("fluvo.dataframe.export_dataframe", side_effect=_export) as export,
    ):
        yield export


def _run(**kwargs: Any) -> CompareResult:
    """``run_compare`` against the two stubbed instances."""
    kwargs.setdefault("config", _TARGET)
    kwargs.setdefault("model", "res.partner")
    kwargs.setdefault("file", None)
    kwargs.setdefault("key", "ref")
    kwargs.setdefault("against_connection", _SOURCE)
    return run_compare(**kwargs)


# ---------------------------------------------------------------------------
# Choosing a source
# ---------------------------------------------------------------------------


def test_exactly_one_source_is_required() -> None:
    """Neither source, or both, is a setup error rather than a guess."""
    with pytest.raises(CompareError, match="exactly one source"):
        run_compare(config=_TARGET, model="res.partner", file=None, key="ref")
    with pytest.raises(CompareError, match="exactly one source"):
        run_compare(
            config=_TARGET,
            model="res.partner",
            file="rows.csv",
            key="ref",
            against_connection=_SOURCE,
            fields="name",
        )


def test_fields_must_be_named_when_both_sides_are_odoo() -> None:
    """There are no file columns to infer from, and 'every field' is noise."""
    frame = pl.DataFrame({"ref": ["A"], "name": ["Acme"]})
    with _odoo(frame, frame), pytest.raises(CompareError, match="--fields is required"):
        _run()


def test_column_map_is_refused_for_an_odoo_source() -> None:
    """--map renames spreadsheet headers; an Odoo source has field names."""
    frame = pl.DataFrame({"ref": ["A"], "name": ["Acme"]})
    with _odoo(frame, frame), pytest.raises(CompareError, match="--map"):
        _run(fields="name", column_map={"Naam": "name"})


# ---------------------------------------------------------------------------
# Reading the two sides
# ---------------------------------------------------------------------------


def test_only_the_target_is_exported_with_an_addressing_column() -> None:
    """The source needs no id: a fix file addresses records in the target."""
    frame = pl.DataFrame({"ref": ["A"], "name": ["Acme"], "id": ["__e.a"]})
    with _odoo(frame, frame) as export:
        _run(fields="name")
    by_config = {call.args[0]: call.args[2] for call in export.call_args_list}
    assert by_config[_SOURCE] == ["ref", "name"]
    assert by_config[_TARGET][:2] == ["id", ".id"]


def test_the_source_domain_defaults_to_the_target_domain() -> None:
    """One --domain covers both sides, which is the usual migration case."""
    frame = pl.DataFrame({"ref": ["A"], "name": ["Acme"]})
    domain = [["active", "in", [True, False]]]
    with _odoo(frame, frame) as export:
        _run(fields="name", domain=domain)
    for call in export.call_args_list:
        assert call.kwargs["domain"] == domain


def test_the_source_domain_can_be_set_independently() -> None:
    """Including to empty, so 'no filter on the source' is expressible."""
    frame = pl.DataFrame({"ref": ["A"], "name": ["Acme"]})
    with _odoo(frame, frame) as export:
        _run(
            fields="name",
            domain=[["active", "=", True]],
            against_domain=[],
            against_context={"lang": "nl_NL"},
        )
    calls = {call.args[0]: call.kwargs for call in export.call_args_list}
    assert calls[_SOURCE]["domain"] == []
    assert calls[_SOURCE]["context"] == {"lang": "nl_NL"}
    assert calls[_TARGET]["domain"] == [["active", "=", True]]


def test_read_source_odoo_deduplicates_and_casts_to_text() -> None:
    """A field named twice is exported once, and everything comes back String."""
    frame = pl.DataFrame({"ref": ["A"], "credit_limit": [1.0]})
    with patch("fluvo.dataframe.export_dataframe", return_value=frame) as export:
        out = read_source_odoo(_SOURCE, "res.partner", ["ref", "credit_limit", "ref"])
    assert export.call_args.args[2] == ["ref", "credit_limit"]
    assert out.dtypes == [pl.String, pl.String]


def test_a_failing_source_export_says_which_side_failed() -> None:
    """Two connections means 'the export failed' is no longer enough."""
    from fluvo.dataframe import FluvoError

    frame = pl.DataFrame({"ref": ["A"], "name": ["Acme"]})

    def _export(config: Any, *args: Any, **kwargs: Any) -> pl.DataFrame:
        if config == _SOURCE:
            raise FluvoError("boom")
        return frame

    connection = MagicMock()
    connection.get_model.return_value.fields_get.return_value = _fields()
    with (
        patch("fluvo.lib.compare._connect", return_value=connection),
        patch("fluvo.dataframe.export_dataframe", side_effect=_export),
        pytest.raises(CompareError, match="source Odoo: boom"),
    ):
        _run(fields="name")


# ---------------------------------------------------------------------------
# Where the two instances disagree
# ---------------------------------------------------------------------------


def test_a_field_missing_on_the_source_is_explained_not_crashed() -> None:
    """The instances differing is the normal case, so say so clearly."""
    frame = pl.DataFrame({"ref": ["A"], "name": ["Acme"]})
    without = _fields()
    del without["credit_limit"]
    with (
        _odoo(frame, frame, source_fields=without),
        pytest.raises(CompareError, match="source Odoo's 'res.partner' has no field"),
    ):
        _run(fields="name,credit_limit")


def test_a_type_mismatch_warns_and_uses_the_targets_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An integer here and a char there is worth saying out loud."""
    source = pl.DataFrame({"ref": ["A"], "credit_limit": ["1000"]})
    target = pl.DataFrame({"ref": ["A"], "credit_limit": ["1000.0"]})
    mismatched = _fields(credit_limit={"type": "char"})
    with _odoo(source, target, source_fields=mismatched), caplog.at_level("WARNING"):
        result = _run(fields="credit_limit")
    assert "credit_limit" in caplog.text
    assert "'char'" in caplog.text
    # The target says float, so 1000 and 1000.0 are the same number.
    assert result.stats.differing_cells == 0


# ---------------------------------------------------------------------------
# The locale question does not apply
# ---------------------------------------------------------------------------


def test_both_sides_are_read_as_odoo_writes_whatever_the_options_say() -> None:
    """Odoo writes dot decimals and ISO dates in every language.

    A caller that passes a spreadsheet locale anyway must not have it applied to
    an Odoo source: under the Dutch rule the dot in ``1.000`` is a thousands
    separator, which would read Odoo's one-point-nought as a thousand.
    """
    source = pl.DataFrame({"ref": ["A"], "credit_limit": ["1.000"]})
    target = pl.DataFrame({"ref": ["A"], "credit_limit": ["1.0"]})
    with _odoo(source, target):
        result = _run(
            fields="credit_limit",
            options=CompareOptions(locale=ANGLO, date_order="mdy"),
        )
    assert result.stats.differing_cells == 0


def test_the_report_names_the_two_instances_not_a_file() -> None:
    """'Only in file' would be a lie when the source is an Odoo."""
    source = pl.DataFrame({"ref": ["A", "B"], "name": ["Acme", "Beta"]})
    target = pl.DataFrame({"ref": ["A"], "name": ["Acme"]})
    with _odoo(source, target):
        result = _run(fields="name", fmt="markdown")
    report = to_markdown(
        result, "res.partner", source_label="source Odoo", target_label="target Odoo"
    )
    assert "Only in source Odoo | 1" in report
    assert "file" not in report


# ---------------------------------------------------------------------------
# The whole point
# ---------------------------------------------------------------------------


def test_version_drift_between_two_instances_is_not_a_difference() -> None:
    """The acceptance bar: only the one real edit is reported.

    Every cell below is written differently by the two instances -- a float that
    gained a decimal, a boolean rendered as a word, a selection rendered as its
    label -- and exactly one value genuinely changed.
    """
    source = pl.DataFrame(
        {
            "ref": ["0010", "0011", "0012"],
            "name": ["Acme", "Beta", "Gamma"],
            "credit_limit": ["1000", "0", "2.5"],
            "active": ["True", "False", "1"],
            "type": ["Contact", "contact", "Invoice Address"],
        }
    )
    target = pl.DataFrame(
        {
            "id": ["__e.a", "__e.b", "__e.c"],
            "ref": ["10", "0011", "0012"],
            "name": ["Acme", "Beta", "Gamma BV"],
            "credit_limit": ["1000.0", "", "2.50"],
            "active": ["1", "0", "True"],
            "type": ["contact", "Contact", "invoice"],
        }
    )
    with _odoo(source, target):
        result = _run(fields="name,credit_limit,active,type")
    assert result.stats.matched_records == 3
    assert result.stats.differing_cells == 1
    assert result.differences["field"].to_list() == ["name"]
    assert result.differences["target_value"].to_list() == ["Gamma BV"]
