"""Tests for the `fluvo assess` migration-assessment engine (PLAN 2.2)."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fluvo.lib import assess
from fluvo.lib.core import ConnectError, NothingToDoError

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
def test_run_assess_no_models_raises_nothing_to_do(mock_connect: MagicMock) -> None:
    """No readable models -> a typed NothingToDoError, not a SystemExit (#363)."""
    mock_connect.return_value = _fake_conn({}, {})
    with pytest.raises(NothingToDoError):
        assess.run_assess("c.conf", models=["ghost.a", "ghost.b"])


@patch("fluvo.lib.assess._connect")
def test_run_assess_connection_failure_raises_connect_error(
    mock_connect: MagicMock,
) -> None:
    """A connection failure raises a typed ConnectError, not a SystemExit (#363)."""
    mock_connect.side_effect = Exception("refused")
    with pytest.raises(ConnectError):
        assess.run_assess("c.conf", models=["res.partner"])


@patch("fluvo.lib.assess._connect")
def test_run_assess_reports_phases_to_its_reporter(mock_connect: MagicMock) -> None:
    """run_assess drives the reporter through its phases (#363)."""
    mock_connect.return_value = _fake_conn({"res.partner": FIELDS}, {"res.partner": 3})

    class Recording:
        def __init__(self) -> None:
            self.phases: list[str] = []
            self.progress: list[tuple[int, int]] = []

        def on_phase(self, name: str) -> None:
            self.phases.append(name)

        def on_progress(self, done: int, total: int, stats: Any = None) -> None:
            self.progress.append((done, total))

        def on_message(self, level: str, text: str) -> None:
            pass

    reporter = Recording()
    assess.run_assess("c.conf", models=["res.partner"], reporter=reporter)
    assert reporter.phases == ["connecting", "discovering models", "assessing"]
    assert reporter.progress == [(1, 1)]


@patch("fluvo.lib.assess._connect")
def test_assess_cli_maps_core_error_to_panel_and_exit(
    mock_connect: MagicMock, tmp_path: Any
) -> None:
    """The CLI adapter turns a typed core error into a panel + exit 1 (#363)."""
    from click.testing import CliRunner

    from fluvo.__main__ import cli

    mock_connect.side_effect = Exception("refused")
    conf = tmp_path / "x.conf"
    conf.write_text("[x]\n")
    result = CliRunner().invoke(
        cli, ["assess", "--connection-file", str(conf), "--models", "res.partner"]
    )
    assert result.exit_code == 1
    assert "Odoo Connection Error" in result.output
    assert "Traceback" not in result.output


def test_company_distribution_counts_per_company() -> None:
    """_company_distribution counts records per company (incl. zero and shared)."""
    conn = MagicMock()
    company_model = MagicMock()
    company_model.search_read.return_value = [
        {"id": 1, "name": "My Company"},
        {"id": 2, "name": "Company Two"},
    ]
    target_model = MagicMock()
    # search_count is called per company, then once for shared (company_id = False).
    target_model.search_count.side_effect = [4200, 0, 12]

    def get_model(name: str) -> MagicMock:
        return company_model if name == "res.company" else target_model

    conn.get_model.side_effect = get_model
    dist = assess._company_distribution(conn, "res.partner")
    assert dist == [
        {"company_id": 1, "company_name": "My Company", "count": 4200},
        {"company_id": 2, "company_name": "Company Two", "count": 0},
        {"company_id": None, "company_name": "(no company)", "count": 12},
    ]
    # The shared bucket is only appended when some records have no company.
    assert target_model.search_count.call_args_list[-1][0][0] == [
        ("company_id", "=", False)
    ]


def test_company_distribution_omits_shared_bucket_when_empty() -> None:
    """No company-less records → no trailing ``(no company)`` entry."""
    conn = MagicMock()
    company_model = MagicMock()
    company_model.search_read.return_value = [{"id": 1, "name": "Solo"}]
    target_model = MagicMock()
    target_model.search_count.side_effect = [7, 0]  # company 1, then shared=0
    conn.get_model.side_effect = (
        lambda name: company_model if name == "res.company" else target_model
    )
    dist = assess._company_distribution(conn, "res.partner")
    assert dist == [{"company_id": 1, "company_name": "Solo", "count": 7}]


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
        if name == "res.company":
            m.search_read.return_value = [
                {"id": 1, "name": "Main"},
                {"id": 2, "name": "Other"},
            ]
            return m
        m.fields_get.return_value = company_fields
        # Each get_model(model) returns a fresh mock: the row-count call and the
        # per-company counts inside _company_distribution each consume this list
        # independently from index 0 — co 1 = 5, co 2 = 0, shared = 0.
        m.search_count.side_effect = [5, 0, 0]
        return m

    conn.get_model.side_effect = get_model
    mock_connect.return_value = conn
    out = assess.run_assess("c.conf", models=["res.partner"])
    assert out[0]["company_distribution"][1] == {
        "company_id": 2,
        "company_name": "Other",
        "count": 0,
    }


# --------------------------------------------------------------------------- #
# --profile: value-level profiling (issue #349)
# --------------------------------------------------------------------------- #
PROFILE_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer"},
    "name": {"type": "char"},
    "quantity": {"type": "float"},
    "sold_on": {"type": "date"},
    "state": {"type": "selection"},
    "quantity_uom_id": {"type": "many2one", "relation": "uom.uom"},
    "category_id": {"type": "many2one", "relation": "product.category"},
    "notes": {"type": "text"},
    "photo": {"type": "binary"},
    "tag_ids": {"type": "many2many", "relation": "x.tag"},
}


def test_select_profile_fields_picks_roles_and_skips_the_expensive_types() -> None:
    """Binaries, text and x2many are excluded; only qualifier m2o is read."""
    numeric, dates, enums, qualifiers = assess.select_profile_fields(PROFILE_FIELDS)
    assert numeric == ["id", "quantity"]
    assert dates == ["sold_on"]
    assert enums == ["name", "state"]
    # category_id is a many2one too, but its name does not read as a qualifier.
    assert qualifiers == ["quantity_uom_id"]


def test_rows_to_frame_flattens_false_and_many2one_tuples() -> None:
    """Odoo's False-for-unset and [id, name] many2one become plain strings."""
    rows: list[dict[str, Any]] = [
        {"quantity": 1.5, "quantity_uom_id": [7, "Units"], "name": "a"},
        {"quantity": False, "quantity_uom_id": False, "name": False},
    ]
    frame = assess.rows_to_frame(rows, ["quantity", "quantity_uom_id", "name"])
    assert frame.to_dicts() == [
        {"quantity": "1.5", "quantity_uom_id": "Units", "name": "a"},
        {"quantity": None, "quantity_uom_id": None, "name": None},
    ]


def _profile_conn(rows: list[dict[str, Any]], count: int) -> MagicMock:
    """A fake connection whose search_read serves the given rows.

    One model mock is reused across get_model calls so a test can assert on
    how search_read was called.
    """
    conn = MagicMock()
    model = MagicMock()
    model.fields_get.return_value = PROFILE_FIELDS
    model.search_count.return_value = count
    model.search_read.return_value = rows
    conn.get_model.return_value = model
    return conn


def _mixed_unit_rows() -> list[dict[str, Any]]:
    """Synthetic rows whose quantity column holds two unit populations."""
    rows: list[dict[str, Any]] = []
    for i in range(60):
        rows.append(
            {
                "id": i,
                "quantity": round(0.5 + i * 0.1, 2),
                "quantity_uom_id": False,
                "name": "alpha",
                "state": "done",
                "sold_on": "2024-01-05",
                "category_id": [1, "alpha"],
            }
        )
    for i in range(60):
        rows.append(
            {
                "id": 60 + i,
                "quantity": 500.0 + i * 50,
                "quantity_uom_id": False,
                "name": "beta",
                "state": "done",
                "sold_on": "2024-01-05",
                "category_id": [2, "beta"],
            }
        )
    return rows


@patch("fluvo.lib.assess._connect")
def test_run_assess_profile_flags_the_mixed_unit_column(
    mock_connect: MagicMock,
) -> None:
    """--profile surfaces a two-population numeric column and its empty qualifier."""
    mock_connect.return_value = _profile_conn(_mixed_unit_rows(), 120)
    out = assess.run_assess("c.conf", models=["x.model"], profile=True, fmt="table")
    profile = out[0]["profile"]
    assert profile["sampled"] == 120
    assert profile["sampled_all"] is True
    assert "quantity" in profile["flagged"]
    quantity = next(r for r in profile["columns"] if r["column"] == "quantity")
    assert "multi-population" in quantity["flags"]
    # The unit field is empty on every row the value is set.
    assert profile["qualifiers"][0]["qualifier_column"] == "quantity_uom_id"


@patch("fluvo.lib.assess._connect")
def test_run_assess_without_profile_reads_no_values(mock_connect: MagicMock) -> None:
    """Profiling is opt-in: the default run never calls search_read."""
    conn = _profile_conn(_mixed_unit_rows(), 120)
    mock_connect.return_value = conn
    out = assess.run_assess("c.conf", models=["x.model"], fmt="table")
    assert "profile" not in out[0]
    assert not conn.get_model("x.model").search_read.called


@patch("fluvo.lib.assess._connect")
def test_run_assess_profile_respects_the_row_limit(mock_connect: MagicMock) -> None:
    """--profile-limit is passed to search_read and recorded as a sample."""
    conn = _profile_conn(_mixed_unit_rows()[:50], 5000)
    mock_connect.return_value = conn
    out = assess.run_assess(
        "c.conf", models=["x.model"], profile=True, profile_limit=50
    )
    profile = out[0]["profile"]
    assert profile["sampled"] == 50
    assert profile["total"] == 5000
    # A sampled profile must say so; a fill rate from 1% of rows is an estimate.
    assert profile["sampled_all"] is False
    _, kwargs = conn.get_model("x.model").search_read.call_args
    assert kwargs["limit"] == 50


@patch("fluvo.lib.assess._connect")
def test_run_assess_profile_group_by_is_dropped_when_absent(
    mock_connect: MagicMock,
) -> None:
    """A --group-by field the model lacks is ignored rather than failing the run."""
    mock_connect.return_value = _profile_conn(_mixed_unit_rows(), 120)
    out = assess.run_assess(
        "c.conf", models=["x.model"], profile=True, group_by="no_such_field"
    )
    assert out[0]["profile"]["group_by"] is None


@patch("fluvo.lib.assess._connect")
def test_run_assess_profile_survives_an_unreadable_model(
    mock_connect: MagicMock,
) -> None:
    """A search_read failure degrades to no profile, not a failed assessment."""
    conn = MagicMock()

    def get_model(name: str) -> MagicMock:
        m = MagicMock()
        m.fields_get.return_value = PROFILE_FIELDS
        m.search_count.return_value = 10
        m.search_read.side_effect = Exception("access denied")
        return m

    conn.get_model.side_effect = get_model
    mock_connect.return_value = conn
    out = assess.run_assess("c.conf", models=["x.model"], profile=True)
    assert "profile" not in out[0]
    assert out[0]["model"] == "x.model"


@patch("fluvo.lib.assess._connect")
def test_run_assess_profile_reaches_the_markdown_report(
    mock_connect: MagicMock,
) -> None:
    """The profiling findings render into the Markdown handout."""
    mock_connect.return_value = _profile_conn(_mixed_unit_rows(), 120)
    out = assess.run_assess("c.conf", models=["x.model"], profile=True, fmt="markdown")
    md = assess.to_markdown(out)
    assert "## Value profiling" in md
    assert "multi-population" in md
    assert "`quantity`" in md


@patch("fluvo.lib.assess._connect")
def test_run_assess_profile_reaches_the_json_report(mock_connect: MagicMock) -> None:
    """The profile is JSON-serialisable, so --output json carries it."""
    mock_connect.return_value = _profile_conn(_mixed_unit_rows(), 120)
    out = assess.run_assess("c.conf", models=["x.model"], profile=True, fmt="json")
    doc = json.loads(assess.to_json(out))
    assert doc["assessment"][0]["profile"]["flagged"] == ["quantity"]


def test_profile_findings_is_empty_without_a_profile() -> None:
    """An assessment with no profile contributes no findings."""
    assert assess.profile_findings([{"model": "x", "risks": {}}]) == []


def test_profile_markdown_is_empty_without_a_profile() -> None:
    """No profile means no section, rather than an empty heading."""
    assert assess.profile_markdown([{"model": "x", "risks": {}}]) == ""
