"""Per-company import scenarios (``field@company``) against a real Odoo (#255 pt2).

Proves the company passes end-to-end: a company-dependent field imported with
``field@company`` columns lands a distinct value per company, verified by reading
the record back under each company's context.

``product.product.standard_price`` is the company-dependent field under test (on
recent Odoo the cost is company-dependent on the *variant*, not the template).
Attribute-less templates already own exactly one variant, so these seed templates,
give the auto-created variants an external id, then import by that id (an update —
no second variant, which Odoo's one-variant-per-combination rule forbids).
"""

from __future__ import annotations

from typing import Any

from . import assertions as A
from . import generators as G

VARIANT = "product.product"


def _price_in(rpc: Any, db_id: int, company_id: int) -> float:
    """Read ``standard_price`` for one variant under a given company's context."""
    ctx = {
        "allowed_company_ids": [company_id],
        "company_id": company_id,
        "force_company": company_id,
    }
    rec = rpc.get_model(VARIANT).read([db_id], ["standard_price"], context=ctx)
    rec = rec[0] if isinstance(rec, list) else rec
    return float(rec["standard_price"])


def _seed_variants(
    conn_config: dict[str, Any], rpc: Any, tmp_path: Any, prefix: str, n: int
) -> list[int]:
    """Seed ``n`` templates and give each auto-variant the external id ``prefix_vI``.

    Args:
        conn_config: Connection config dict.
        rpc: A connection for ground-truth queries and id registration.
        tmp_path: Temp directory for the seed CSV.
        prefix: Namespacing marker for the external ids.
        n: Number of templates (and variants) to seed.

    Returns:
        list[int]: The variant database ids, index-aligned (variant ``i`` ->
        ``prefix_vI``).
    """
    rows = [{"id": f"{prefix}_t{i}", "name": f"{prefix} Product {i}"} for i in range(n)]
    csv_path = G.write_csv(str(tmp_path / f"{prefix}_tmpl.csv"), ["id", "name"], rows)
    tmpl_map = A.run_full_import(
        conn_config, "product.template", csv_path, allow_default_company=True
    )
    assert tmpl_map is not None and len(tmpl_map) == n, "template seed failed"
    tmpl_ids = [tmpl_map[f"{prefix}_t{i}"] for i in range(n)]

    found = rpc.get_model(VARIANT).search_read(
        [("product_tmpl_id", "in", tmpl_ids)], ["id", "product_tmpl_id"]
    )
    tmpl_to_variant = {r["product_tmpl_id"][0]: r["id"] for r in found}

    imd = rpc.get_model("ir.model.data")
    variant_ids = []
    for i in range(n):
        vid = tmpl_to_variant[tmpl_ids[i]]
        variant_ids.append(vid)
        name = f"{prefix}_v{i}"
        if not imd.search([("module", "=", "__import__"), ("name", "=", name)]):
            imd.create(
                {
                    "module": "__import__",
                    "name": name,
                    "model": VARIANT,
                    "res_id": vid,
                }
            )
    return variant_ids


def test_company_dependent_values_land_per_company(
    conn_config: dict[str, Any],
    rpc: Any,
    tmp_path: Any,
    second_company: int,
) -> None:
    """Each field@company value lands under its own company, not shared."""
    c1, c2 = 1, second_company
    prefix = "co_dep"
    n = 3
    variant_ids = _seed_variants(conn_config, rpc, tmp_path, prefix, n)

    rows = []
    for i in range(n):
        rows.append(
            {
                "id": f"{prefix}_v{i}",
                "default_code": f"{prefix}_v{i}",
                f"standard_price@{c1}": str(10 + i),
                f"standard_price@{c2}": str(100 + i),
            }
        )
    header = ["id", "default_code", f"standard_price@{c1}", f"standard_price@{c2}"]
    csv_path = G.write_csv(str(tmp_path / "variants.csv"), header, rows)

    # Import by external id (updates the existing variants); field@company sets the
    # per-company values.
    id_map = A.run_full_import(
        conn_config, VARIANT, csv_path, allow_default_company=True
    )
    assert id_map is not None, "per-company import returned None (aborted)"

    for i in range(n):
        vid = variant_ids[i]
        v1 = _price_in(rpc, vid, c1)
        v2 = _price_in(rpc, vid, c2)
        assert v1 == float(10 + i), f"company {c1} price wrong on variant {i}: {v1}"
        assert v2 == float(100 + i), f"company {c2} price wrong on variant {i}: {v2}"
        assert v1 != v2, "per-company values must differ, not be shared"


def test_unknown_company_aborts_before_writing(
    conn_config: dict[str, Any],
    rpc: Any,
    tmp_path: Any,
    second_company: int,
) -> None:
    """A field@<unknown company> aborts in preflight with no values written."""
    prefix = "co_bad"
    variant_ids = _seed_variants(conn_config, rpc, tmp_path, prefix, 1)
    rows = [{"id": f"{prefix}_v0", "standard_price@99999": "5"}]
    header = ["id", "standard_price@99999"]
    csv_path = G.write_csv(str(tmp_path / "bad.csv"), header, rows)

    # allow_default_company gets us past the require-company guard so the abort we
    # assert is specifically the unknown-company one in company_columns_check.
    id_map = A.run_full_import(
        conn_config, VARIANT, csv_path, allow_default_company=True
    )

    assert id_map is None, "import should abort on an unknown company"
    # Nothing was written: the variant's cost is still the default 0.
    assert _price_in(rpc, variant_ids[0], 1) == 0.0
