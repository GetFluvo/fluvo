"""E2E: create-missing-variants restores variants for orphan templates (#188).

Reproduces the real failure mode against a live Odoo: a product.template with no
product.product variant (the state load() can produce) is unusable. We create such
an orphan template, then assert the workflow creates the missing default variant.
"""

from __future__ import annotations

from typing import Any

import pytest

from fluvo.lib.actions.variant_manager import run_create_missing_variants


@pytest.mark.usefixtures("product_module")
def test_create_missing_variants_restores_orphan_template(
    conn_config: dict[str, Any], rpc: Any
) -> None:
    """An orphan template (no variants) gets its default variant created."""
    template = rpc.get_model("product.template")

    # 1. Create an orphan template — no default variant. The presence of the
    #    'create_product_product' context key tells product.template.create to
    #    skip _create_variant_ids(), reproducing the state load() can leave behind.
    #    (Deleting an existing variant won't work: Odoo cascade-deletes a template
    #    when its last variant is removed.)
    tmpl_id = template.create(
        {"name": "variantfix widget"},
        context={"create_product_product": False},
    )
    count = template.read([tmpl_id], ["product_variant_count"])[0][
        "product_variant_count"
    ]
    assert count == 0, "template should start orphaned (no variants)"

    # 2. Run the workflow, scoped to just this template.
    ok = run_create_missing_variants(conn_config, domain=[("id", "=", tmpl_id)])
    assert ok, "create-missing-variants reported failure"

    # 3. The default variant now exists.
    count_after = template.read([tmpl_id], ["product_variant_count"])[0][
        "product_variant_count"
    ]
    assert count_after == 1, "the workflow should have created exactly one variant"
