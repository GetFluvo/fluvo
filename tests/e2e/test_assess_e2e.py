"""`fluvo assess` against a real Odoo (PLAN 2.2).

Proves the assessment runs end-to-end: real row counts, a real field inventory,
and risk flags derived from live ``fields_get`` metadata.
"""

from __future__ import annotations

import json
from typing import Any

from fluvo.lib import assess


def test_assess_reports_real_inventory(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any
) -> None:
    """Assessing base models yields real volumes, field counts and risk flags."""
    out_file = str(tmp_path / "report.json")
    result = assess.run_assess(
        conn_config,
        models=["res.partner", "res.users", "ghost.model_xyz"],
        output=out_file,
        fmt="json",
    )

    models = {a["model"]: a for a in result}
    # The unreadable model is skipped, not fatal.
    assert set(models) == {"res.partner", "res.users"}

    partner = models["res.partner"]
    assert isinstance(partner["row_count"], int) and partner["row_count"] >= 1
    assert partner["field_total"] > 20  # res.partner is a wide model
    # res.partner has relational fields (country_id, state_id, ...).
    assert partner["risks"]["relational"]["many2one"] > 0

    # The written handout is valid JSON with the same content.
    doc = json.loads(open(out_file, encoding="utf-8").read())
    assert {a["model"] for a in doc["assessment"]} == {"res.partner", "res.users"}


def test_assess_discovers_default_models(conn_config: dict[str, Any], rpc: Any) -> None:
    """With no --models, the curated defaults present on the DB are discovered."""
    result = assess.run_assess(conn_config, models=None, fmt="table")
    found = {a["model"] for a in result}
    # res.partner and res.users ship in base, so they must be discovered.
    assert {"res.partner", "res.users"} <= found


def test_assess_reports_company_distribution(
    conn_config: dict[str, Any], rpc: Any
) -> None:
    """A company-aware model's assessment includes its records-per-company audit."""
    result = assess.run_assess(conn_config, models=["res.partner"], fmt="table")
    partner = result[0]
    assert "company_distribution" in partner, "expected a company audit"
    dist = partner["company_distribution"]
    assert dist and all(
        {"company_id", "company_name", "count"} <= d.keys() for d in dist
    )
    # The per-company counts reconcile to the model's total row count.
    assert sum(d["count"] for d in dist) == partner["row_count"]
