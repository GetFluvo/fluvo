"""Tests for multi-language export (`field@lang` / --languages, #282)."""

from typing import Any
from unittest.mock import patch

import polars as pl
import pytest

from fluvo import exporter
from fluvo.exporter import _ExportTranslationError

FIELDS = {
    "id": {"type": "char"},
    "name": {"type": "char", "translate": True},
    "description": {"type": "text", "translate": True},
    "ref": {"type": "char"},
    "standard_price": {"type": "float", "company_dependent": True},
}


def _plan(fields_list: list[str], languages: str | None, installed: Any = None) -> Any:
    installed = installed if installed is not None else {"nl_NL", "fr_FR", "de_DE"}
    with (
        patch("fluvo.exporter.preflight._get_odoo_fields", return_value=FIELDS),
        patch(
            "fluvo.exporter.preflight._get_installed_languages", return_value=installed
        ),
    ):
        return exporter._plan_translation_export("c.conf", "m", fields_list, languages)


# --- Planning -------------------------------------------------------------------


def test_no_translation_request_returns_none() -> None:
    """Plain fields with no @ token and no --languages: not a translation export."""
    assert _plan(["id", "name", "ref"], None) is None


def test_explicit_tokens_are_planned() -> None:
    """Explicit field@lang tokens are grouped by language and kept in column order."""
    plan = _plan(["id", "name", "name@nl_NL", "name@fr_FR"], None)
    assert plan["key"] == "id"
    assert plan["translations"] == {"nl_NL": ["name"], "fr_FR": ["name"]}
    assert plan["output_columns"] == ["id", "name", "name@nl_NL", "name@fr_FR"]
    assert plan["base_fields"] == ["id", "name"]


def test_languages_flag_expands_translatable_fields() -> None:
    """--languages expands every translatable plain field, leaving others alone."""
    plan = _plan(["id", "name", "description", "ref"], "nl_NL,fr_FR")
    assert plan["translations"] == {
        "nl_NL": ["description", "name"],
        "fr_FR": ["description", "name"],
    }
    # ref is not translatable -> no ref@lang columns; autos sit after their base.
    assert plan["output_columns"] == [
        "id",
        "name",
        "name@nl_NL",
        "name@fr_FR",
        "description",
        "description@nl_NL",
        "description@fr_FR",
        "ref",
    ]


def test_languages_flag_does_not_duplicate_explicit_token() -> None:
    """An explicitly-listed field@lang is not auto-added again by --languages."""
    plan = _plan(["id", "name", "name@nl_NL"], "nl_NL")
    assert plan["output_columns"] == ["id", "name", "name@nl_NL"]
    assert plan["translations"] == {"nl_NL": ["name"]}


def test_missing_key_raises() -> None:
    """Translations without an id/.id join key are refused."""
    with pytest.raises(_ExportTranslationError, match="id"):
        _plan(["name", "name@nl_NL"], None)


def test_non_translatable_token_raises() -> None:
    """An @ token on a non-translatable field is rejected."""
    with pytest.raises(_ExportTranslationError, match="not a translatable"):
        _plan(["id", "ref@nl_NL"], None)


def test_uninstalled_language_raises() -> None:
    """A language that isn't installed aborts planning."""
    with pytest.raises(_ExportTranslationError, match="not installed"):
        _plan(["id", "name@zu_ZA"], None)


def test_dot_id_key_is_accepted() -> None:
    """`.id` is a valid join key when `id` is absent."""
    plan = _plan([".id", "name", "name@nl_NL"], None)
    assert plan["key"] == ".id"


# --- Execution ------------------------------------------------------------------


def _fake_export_factory() -> Any:
    """Return an export_data stand-in that serves per-language frames by context.

    The base pass fetches the db id (``.id``) so the language passes can be scoped
    to it; each frame therefore carries ``.id`` for the join.
    """
    base = pl.DataFrame({".id": [1, 2], "id": ["a", "b"], "name": ["EN-a", "EN-b"]})
    nl = pl.DataFrame({".id": [1, 2], "name": ["NL-a", "NL-b"]})
    fr = pl.DataFrame({".id": [1, 2], "name": ["FR-a", "FR-b"]})

    def fake(**kwargs: Any) -> tuple[bool, str, int, pl.DataFrame]:
        lang = (kwargs.get("context") or {}).get("lang")
        header = kwargs["header"]
        if lang == "nl_NL":
            df = nl.select(header)
        elif lang == "fr_FR":
            df = fr.select(header)
        else:
            df = base.select(header)
        return True, "sid", df.height, df

    return fake


def test_run_translation_export_merges_languages(tmp_path: Any) -> None:
    """Base + per-language passes stitch into one file with field@lang columns."""
    plan = {
        "base_fields": ["id", "name"],
        "translations": {"nl_NL": ["name"], "fr_FR": ["name"]},
        "key": "id",
        "output_columns": ["id", "name", "name@nl_NL", "name@fr_FR"],
    }
    out = str(tmp_path / "out.csv")
    with patch(
        "fluvo.exporter.export_threaded.export_data",
        side_effect=_fake_export_factory(),
    ):
        ok, count = exporter._run_translation_export(
            config="c.conf",
            model="m",
            plan=plan,
            domain=[],
            output=out,
            context={},
            worker=1,
            batch_size=10,
            separator=",",
            encoding="utf-8",
            technical_names=False,
            sanitize_newlines=None,
        )
    assert ok and count == 2
    df = pl.read_csv(out, separator=",")
    assert df.columns == ["id", "name", "name@nl_NL", "name@fr_FR"]
    row_a = df.filter(pl.col("id") == "a").to_dicts()[0]
    assert row_a == {
        "id": "a",
        "name": "EN-a",
        "name@nl_NL": "NL-a",
        "name@fr_FR": "FR-a",
    }


def test_run_translation_export_scopes_languages_by_db_id(tmp_path: Any) -> None:
    """Language passes are scoped by db id, never by re-running the user domain.

    A domain that filters a translated field would select a different record set
    per language; the executor must instead reuse the base pass's db ids.
    """
    domains_seen: list[Any] = []

    def fake(**kwargs: Any) -> tuple[bool, str, int, pl.DataFrame]:
        domains_seen.append((kwargs.get("context") or {}).get("lang", None))
        base = pl.DataFrame({".id": [7], "id": ["a"], "name": ["EN"]})
        nl = pl.DataFrame({".id": [7], "name": ["NL"]})
        header = kwargs["header"]
        df = nl if (kwargs.get("context") or {}).get("lang") else base
        # The language pass must have been scoped to the base ids, not the user's
        # translated-field domain.
        if (kwargs.get("context") or {}).get("lang"):
            assert kwargs["domain"] == [["id", "in", [7]]]
        return True, "sid", df.height, df.select(header)

    plan = {
        "base_fields": ["id", "name"],
        "translations": {"nl_NL": ["name"]},
        "key": "id",
        "output_columns": ["id", "name", "name@nl_NL"],
    }
    out = str(tmp_path / "o.csv")
    with patch("fluvo.exporter.export_threaded.export_data", side_effect=fake):
        ok, count = exporter._run_translation_export(
            config="c.conf",
            model="m",
            plan=plan,
            domain=[["name", "like", "EN %"]],
            output=out,
            context={},
            worker=1,
            batch_size=10,
            separator=",",
            encoding="utf-8",
            technical_names=False,
            sanitize_newlines=None,
        )
    assert ok and count == 1
    df = pl.read_csv(out, separator=",")
    assert df.columns == ["id", "name", "name@nl_NL"]  # internal .id dropped
    assert df.to_dicts()[0] == {"id": "a", "name": "EN", "name@nl_NL": "NL"}


def test_languages_flag_with_no_translatable_field_errors(tmp_path: Any) -> None:
    """--languages that expands to nothing fails loud, not a silent plain export."""
    with (
        patch("fluvo.exporter.preflight._get_odoo_fields", return_value=FIELDS),
        patch(
            "fluvo.exporter.preflight._get_installed_languages",
            return_value={"nl_NL"},
        ),
        patch("fluvo.exporter.export_threaded.export_data") as mock_export,
        pytest.raises(SystemExit),
    ):
        exporter.run_export(
            config="c.conf",
            model="m",
            fields="id",  # no translatable field to expand
            output=str(tmp_path / "o.csv"),
            languages="nl_NL",
            context={},
        )
    mock_export.assert_not_called()  # never falls through to a plain export


def test_run_translation_export_empty_keeps_full_header(tmp_path: Any) -> None:
    """When no records match, the output still carries the full field@lang header."""
    empty = pl.DataFrame(schema={".id": pl.Int64, "id": pl.Utf8, "name": pl.Utf8})

    def fake(**kwargs: Any) -> tuple[bool, str, int, pl.DataFrame]:
        return True, "sid", 0, empty.select(kwargs["header"])

    plan = {
        "base_fields": ["id", "name"],
        "translations": {"nl_NL": ["name"]},
        "key": "id",
        "output_columns": ["id", "name", "name@nl_NL"],
    }
    out = str(tmp_path / "empty.csv")
    with patch("fluvo.exporter.export_threaded.export_data", side_effect=fake):
        ok, count = exporter._run_translation_export(
            config="c.conf",
            model="m",
            plan=plan,
            domain=[],
            output=out,
            context={},
            worker=1,
            batch_size=10,
            separator=",",
            encoding="utf-8",
            technical_names=False,
            sanitize_newlines=None,
        )
    assert ok and count == 0
    df = pl.read_csv(out, separator=",")
    assert df.columns == ["id", "name", "name@nl_NL"]
    assert df.height == 0


def test_run_translation_export_aborts_if_a_language_pass_fails(tmp_path: Any) -> None:
    """If a language pass fails, abort so no partial file is written."""
    base = pl.DataFrame({".id": [1], "id": ["a"], "name": ["EN"]})

    def fake(**kwargs: Any) -> tuple[bool, str, int, Any]:
        if (kwargs.get("context") or {}).get("lang"):
            return False, "sid", 0, None
        return True, "sid", 1, base.select(kwargs["header"])

    plan = {
        "base_fields": ["id", "name"],
        "translations": {"nl_NL": ["name"]},
        "key": "id",
        "output_columns": ["id", "name", "name@nl_NL"],
    }
    with (
        patch("fluvo.exporter.export_threaded.export_data", side_effect=fake),
        pytest.raises(_ExportTranslationError, match="translation pass failed"),
    ):
        exporter._run_translation_export(
            config="c.conf",
            model="m",
            plan=plan,
            domain=[],
            output=str(tmp_path / "o.csv"),
            context={},
            worker=1,
            batch_size=10,
            separator=",",
            encoding="utf-8",
            technical_names=False,
            sanitize_newlines=None,
        )
