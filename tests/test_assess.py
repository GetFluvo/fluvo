"""Tests for the `fluvo assess` migration-assessment engine (PLAN 2.2)."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fluvo.lib import assess

FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "name": {"type": "char", "required": True, "translate": True},
    "standard_price": {"type": "float", "company_dependent": True},
    "category_id": {"type": "many2one", "relation": "res.partner.category"},
    "tag_ids": {"type": "many2many", "relation": "res.partner.category"},
    "line_ids": {"type": "one2many", "relation": "sale.order.line"},
    "image": {"type": "binary"},
    "display_name": {"type": "char", "readonly": True, "store": False},
}


def test_assess_model_counts_and_flags() -> None:
    """assess_model classifies fields by type and raises the right risk flags."""
    a = assess.assess_model("res.partner", FIELDS, row_count=1234)
    assert a["model"] == "res.partner"
    assert a["row_count"] == 1234
    assert a["field_total"] == 8
    assert a["by_type"]["many2one"] == 1
    assert a["by_type"]["char"] == 2
    risks = a["risks"]
    assert risks["company_dependent"] == ["standard_price"]
    assert risks["translated"] == ["name"]
    assert risks["required"] == ["name"]
    assert risks["binary"] == ["image"]
    # readonly + not stored -> computed, not importable.
    assert risks["readonly_computed"] == ["display_name"]
    assert risks["relational"] == {"many2one": 1, "many2many": 1, "one2many": 1}


def test_assess_model_no_risks() -> None:
    """A plain model surfaces no risk flags."""
    a = assess.assess_model("x", {"id": {"type": "integer"}}, row_count=0)
    assert assess._risk_summary(a["risks"]) == "none"


def test_risk_summary_lists_flags() -> None:
    """The one-line summary mentions each present risk."""
    a = assess.assess_model("res.partner", FIELDS, row_count=1)
    summary = assess._risk_summary(a["risks"])
    assert "1 company-dependent" in summary
    assert "1 translated" in summary
    assert "3 relational" in summary
    assert "binary" in summary


def test_to_json_and_markdown_roundtrip() -> None:
    """The JSON and Markdown renderers produce the expected shape."""
    assessments = [assess.assess_model("res.partner", FIELDS, 5)]
    doc = json.loads(assess.to_json(assessments))
    assert doc["assessment"][0]["model"] == "res.partner"
    md = assess.to_markdown(assessments)
    assert "# Fluvo migration assessment" in md
    assert "`res.partner`" in md


def _fake_conn(fields_by_model: dict[str, Any], counts: dict[str, Any]) -> MagicMock:
    """A fake Odoo connection serving fields_get/search_count per model."""
    conn = MagicMock()

    def get_model(name: str) -> MagicMock:
        m = MagicMock()
        if name in fields_by_model:
            m.fields_get.return_value = fields_by_model[name]
        else:
            m.fields_get.side_effect = Exception("no such model")
        count = counts.get(name)
        if isinstance(count, Exception):
            m.search_count.side_effect = count
        else:
            m.search_count.return_value = count
        return m

    conn.get_model.side_effect = get_model
    return conn


@patch("fluvo.lib.assess._connect")
def test_run_assess_explicit_models(mock_connect: MagicMock) -> None:
    """run_assess assesses the given models and returns their summaries."""
    mock_connect.return_value = _fake_conn(
        {"res.partner": FIELDS, "product.template": {"id": {"type": "integer"}}},
        {"res.partner": 10, "product.template": 3},
    )
    out = assess.run_assess(
        "c.conf", models=["res.partner", "product.template"], fmt="table"
    )
    assert [a["model"] for a in out] == ["res.partner", "product.template"]
    assert out[0]["row_count"] == 10


@patch("fluvo.lib.assess._connect")
def test_run_assess_skips_unreadable_and_uncounted(mock_connect: MagicMock) -> None:
    """A model whose fields can't be read is skipped; a count error -> None."""
    mock_connect.return_value = _fake_conn(
        {"res.partner": FIELDS},  # 'ghost.model' absent -> fields_get raises
        {"res.partner": Exception("access denied")},  # count fails -> None
    )
    out = assess.run_assess("c.conf", models=["res.partner", "ghost.model"])
    assert [a["model"] for a in out] == ["res.partner"]  # ghost skipped
    assert out[0]["row_count"] is None  # count failure surfaced as unknown


@patch("fluvo.lib.assess._connect")
def test_run_assess_writes_output_file(mock_connect: MagicMock, tmp_path: Any) -> None:
    """--output writes the report (JSON by default) to disk."""
    mock_connect.return_value = _fake_conn({"res.partner": FIELDS}, {"res.partner": 2})
    out_file = str(tmp_path / "report.json")
    assess.run_assess("c.conf", models=["res.partner"], output=out_file, fmt="json")
    doc = json.loads(open(out_file, encoding="utf-8").read())
    assert doc["assessment"][0]["model"] == "res.partner"


@patch("fluvo.lib.assess._connect")
def test_run_assess_no_models_aborts(mock_connect: MagicMock) -> None:
    """No readable models -> fail loud (SystemExit)."""
    mock_connect.return_value = _fake_conn({}, {})
    with pytest.raises(SystemExit):
        assess.run_assess("c.conf", models=["ghost.a", "ghost.b"])


@patch("fluvo.lib.assess._connect")
def test_run_assess_connection_failure_aborts(mock_connect: MagicMock) -> None:
    """A connection failure exits non-zero."""
    mock_connect.side_effect = Exception("refused")
    with pytest.raises(SystemExit):
        assess.run_assess("c.conf", models=["res.partner"])


def test_company_distribution_parses_read_group() -> None:
    """_company_distribution turns read_group output into per-company counts."""
    conn = MagicMock()
    model = MagicMock()
    model.read_group.return_value = [
        {"company_id": [1, "My Company"], "company_id_count": 4200},
        {"company_id": [2, "Company Two"], "company_id_count": 0},
        {"company_id": False, "company_id_count": 12},  # company-less/shared
    ]
    conn.get_model.return_value = model
    dist = assess._company_distribution(conn, "res.partner")
    assert dist == [
        {"company_id": 1, "company_name": "My Company", "count": 4200},
        {"company_id": 2, "company_name": "Company Two", "count": 0},
        {"company_id": None, "company_name": "(no company)", "count": 12},
    ]


def test_company_audit_str() -> None:
    """The audit line is compact and empty when the model isn't company-aware."""
    a = {
        "company_distribution": [
            {"company_id": 1, "company_name": "X", "count": 4200},
            {"company_id": 2, "company_name": "Y", "count": 0},
        ]
    }
    assert assess._company_audit_str(a) == "by company: 1:4200, 2:0"
    assert assess._company_audit_str({"model": "x"}) == ""


@patch("fluvo.lib.assess._connect")
def test_run_assess_includes_company_distribution(mock_connect: MagicMock) -> None:
    """A company-aware model gets its record distribution across companies."""
    company_fields = {"id": {"type": "integer"}, "company_id": {"type": "many2one"}}

    conn = MagicMock()

    def get_model(name: str) -> MagicMock:
        m = MagicMock()
        m.fields_get.return_value = company_fields
        m.search_count.return_value = 5
        m.read_group.return_value = [
            {"company_id": [1, "Main"], "company_id_count": 5},
            {"company_id": [2, "Other"], "company_id_count": 0},
        ]
        return m

    conn.get_model.side_effect = get_model
    mock_connect.return_value = conn
    out = assess.run_assess("c.conf", models=["res.partner"])
    assert out[0]["company_distribution"][1] == {
        "company_id": 2,
        "company_name": "Other",
        "count": 0,
    }
