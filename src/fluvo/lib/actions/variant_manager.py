"""Workflow to create default variants for product templates that have none.

Odoo's ORM auto-creates a default variant when a ``product.template`` is created,
but the ``load()`` import API does not. Templates imported (or migrated) without
attribute lines can therefore end up with ``product_variant_count == 0``, which
makes them unusable in sales/purchase orders, BoMs, etc. (issue #188).

This workflow finds those templates and creates the missing default variant by
creating a ``product.product`` linked to each one. ``_create_variant_ids`` is a
private method and cannot be invoked over RPC, but creating the variant record
directly achieves the same result for attribute-less templates.
"""

from typing import Any, Optional, Union

from ...lib import conf_lib
from ...logging_config import log


def run_create_missing_variants(
    config: Union[str, dict[str, Any]],
    domain: Optional[list[Any]] = None,
    batch_size: int = 200,
    dry_run: bool = False,
) -> bool:
    """Create a default variant for every product template that has none.

    Args:
        config: Connection config — a ``.conf`` file path or a connection dict.
        domain: Optional extra Odoo domain to scope which templates are checked
            (combined with the no-variant filter).
        batch_size: How many variants to create per RPC ``create`` call.
        dry_run: If True, only report how many templates would be fixed.

    Returns:
        bool: True on success (including when there is nothing to do); False if
        the connection fails or any create batch fails.
    """
    log.info("--- Starting Create Missing Variants Workflow ---")
    try:
        if isinstance(config, dict):
            connection: Any = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config_file=config)
        template_obj = connection.get_model("product.template")
        product_obj = connection.get_model("product.product")
    except Exception as e:
        log.error(f"Failed to connect to Odoo: {e}")
        return False

    search_domain: list[Any] = list(domain) if domain else []
    search_domain.append(("product_variant_count", "=", 0))

    try:
        orphan_ids = template_obj.search(search_domain)
    except Exception as e:
        log.error(f"Failed to search for templates without variants: {e}")
        return False

    if not orphan_ids:
        log.info("No product templates without variants found. Nothing to do.")
        return True

    log.info(f"Found {len(orphan_ids)} product template(s) without variants.")
    if dry_run:
        log.info("Dry run: no variants will be created.")
        return True

    created = 0
    failed = 0
    for start in range(0, len(orphan_ids), batch_size):
        batch = orphan_ids[start : start + batch_size]
        try:
            # Batch create works on Odoo >= 14. Older Odoo rejects a list of
            # dicts, so fall back to one-by-one creation if the batch call fails.
            product_obj.create([{"product_tmpl_id": tid} for tid in batch])
            created += len(batch)
        except Exception as batch_err:
            log.warning(
                f"Batch variant creation failed ({batch_err}); "
                "falling back to individual creation."
            )
            for tid in batch:
                try:
                    product_obj.create({"product_tmpl_id": tid})
                    created += 1
                except Exception as item_err:
                    failed += 1
                    log.error(
                        f"Failed to create variant for template {tid}: {item_err}"
                    )

    log.info(
        f"--- Create Missing Variants Finished: {created} created, {failed} failed ---"
    )
    return failed == 0
