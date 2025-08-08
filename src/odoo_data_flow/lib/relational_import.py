"""Handles the 'Direct Relational Table Import' strategy."""

import tempfile
from pathlib import Path
from typing import Any, Optional

import polars as pl
from rich.progress import Progress, TaskID

from .. import import_threaded
from ..logging_config import log
from . import cache, conf_lib


def _resolve_related_ids(
    config: str, related_model: str, external_ids: pl.Series
) -> Optional[pl.DataFrame]:
    """Resolve related ids.

    Resolves external IDs for a related model, trying cache first,
    then falling back to XML-ID resolution.
    """
    # 1. Try to load from cache
    related_model_cache = cache.load_id_map(config, related_model)
    if related_model_cache is not None:
        log.info(f"Cache hit for related model '{related_model}'.")
        return related_model_cache

    # 2. Fallback to XML-ID resolution
    log.warning(
        f"Cache miss for related model '{related_model}'. "
        f"Falling back to slow XML-ID resolution."
    )
    connection = conf_lib.get_connection_from_config(config_file=config)
    if not connection.is_connected():
        log.error("Cannot perform XML-ID lookup: Odoo connection failed.")
        return None

    resolved_ids = {}
    id_list = external_ids.drop_nulls().unique().to_list()
    total_ids = len(id_list)
    log.info(f"Resolving {total_ids} unique external IDs for '{related_model}'...")

    # Odoo's `execute` for `ir.model.data` `xmlid_to_res_id` is not bulk
    # We can loop, but it will be slow. A better way might be to search `ir.model.data`
    # but let's stick to the public API for now.
    for external_id in id_list:
        try:
            # The `.` in external_id is the separator for module and identifier
            if "." not in external_id:
                log.warning(
                    f"Skipping invalid external_id '{external_id}' for "
                    f"model '{related_model}'. It must be in the format "
                    "'module.identifier'."
                )
                continue
            res_id = connection.execute(
                "ir.model.data", "xmlid_to_res_id", external_id, True
            )
            if res_id:
                resolved_ids[external_id] = res_id
        except Exception as e:
            log.error(
                f"Error resolving external_id '{external_id}' for model "
                f"'{related_model}': {e}"
            )

    if not resolved_ids:
        log.error(f"XML-ID resolution failed for all IDs in model '{related_model}'.")
        return None

    log.info(
        f"Successfully resolved {len(resolved_ids)} out of {total_ids} "
        f"external IDs for model '{related_model}'."
    )
    return pl.DataFrame(
        {"external_id": resolved_ids.keys(), "db_id": resolved_ids.values()}
    )


def run_direct_relational_import(
    config: str,
    model: str,
    field: str,
    strategy_details: dict[str, Any],
    source_df: pl.DataFrame,
    id_map: dict[str, int],
    worker: int,
    batch_size: int,
    progress: Progress,
    task_id: TaskID,
) -> bool:
    """Orchestrates the high-speed direct relational import."""
    progress.update(
        task_id,
        description=f"Pass 2/2: Updating relations for [bold]{field}[/bold]",
    )
    log.info(f"Running 'Direct Relational Import' for field '{field}'...")
    relational_table = strategy_details["relation_table"]
    owning_model_fk = strategy_details["relation_field"]
    related_model_fk = strategy_details["relation"]

    # 1. Prepare the owning model's IDs
    owning_df = pl.DataFrame({"external_id": id_map.keys(), "db_id": id_map.values()})

    # 2. Prepare the related model's IDs using the resolver
    all_related_ext_ids = source_df.get_column(field).str.split(",").explode()
    related_model_df = _resolve_related_ids(
        config, related_model_fk, all_related_ext_ids
    )
    if related_model_df is None:
        log.error(f"Could not resolve IDs for related model '{related_model_fk}'.")
        return False

    # 3. Create the link table DataFrame
    link_df = source_df.select(["id", field]).rename({"id": "external_id"})
    link_df = link_df.with_columns(pl.col(field).str.split(",")).explode(field)

    # Join to get DB IDs for the owning model
    link_df = link_df.join(owning_df, on="external_id", how="inner").rename(
        {"db_id": owning_model_fk}
    )

    # Join to get DB IDs for the related model
    link_df = link_df.join(
        related_model_df.rename({"external_id": field}), on=field, how="inner"
    ).rename({"db_id": f"{related_model_fk}/id"})

    final_df = link_df.select([owning_model_fk, f"{related_model_fk}/id"])

    # 4. Write to a temporary file and import
    with tempfile.NamedTemporaryFile(
        mode="w+", delete=False, suffix=".csv", newline=""
    ) as tmp:
        final_df.write_csv(tmp.name)
        tmp_path = tmp.name

    success, _ = import_threaded.import_data(
        config_file=config,
        model=relational_table,
        unique_id_field=owning_model_fk,
        file_csv=tmp_path,
        max_connection=worker,
        batch_size=batch_size,
    )

    Path(tmp_path).unlink()
    return success


def run_write_tuple_import(
    config: str,
    model: str,
    field: str,
    strategy_details: dict[str, Any],
    source_df: pl.DataFrame,
    id_map: dict[str, int],
    worker: int,
    batch_size: int,
    progress: Progress,
    task_id: TaskID,
) -> bool:
    """Orchestrates the 'write_tuple' import for relational fields."""
    progress.update(
        task_id,
        description=f"Pass 2/2: Updating relations for [bold]{field}[/bold]",
    )
    log.info(f"Running 'Write Tuple' for field '{field}'...")
    relational_table = strategy_details["relation_table"]
    owning_model_fk = strategy_details["relation_field"]
    related_model_fk = strategy_details["relation"]

    # 1. Prepare the owning model's IDs
    owning_df = pl.DataFrame({"external_id": id_map.keys(), "db_id": id_map.values()})

    # 2. Prepare the related model's IDs using the resolver
    all_related_ext_ids = source_df.get_column(field).str.split(",").explode()
    related_model_df = _resolve_related_ids(
        config, related_model_fk, all_related_ext_ids
    )
    if related_model_df is None:
        log.error(f"Could not resolve IDs for related model '{related_model_fk}'.")
        return False

    # 3. Create the link table DataFrame
    link_df = source_df.select(["id", field]).rename({"id": "external_id"})
    link_df = link_df.with_columns(pl.col(field).str.split(",")).explode(field)

    # Join to get DB IDs for the owning model
    link_df = link_df.join(owning_df, on="external_id", how="inner").rename(
        {"db_id": owning_model_fk}
    )

    # Join to get DB IDs for the related model
    link_df = link_df.join(
        related_model_df.rename({"external_id": field}), on=field, how="inner"
    ).rename({"db_id": f"{related_model_fk}/id"})

    final_df = link_df.select([owning_model_fk, f"{related_model_fk}/id"])

    # 4. Create records in the relational table
    connection = conf_lib.get_connection_from_config(config_file=config)
    rel_model = connection.get_model(relational_table)

    vals_list = final_df.to_dicts()
    successful_creates = 0
    failed_creates = 0

    for vals in vals_list:
        try:
            rel_model.create(vals)
            successful_creates += 1
        except Exception as e:
            log.error(
                f"Failed to create record for '{relational_table}' with values {vals}. "
                f"Reason: {e}"
            )
            failed_creates += 1

    log.info(
        f"Finished 'Write Tuple' for '{field}': "
        f"{successful_creates} successful, {failed_creates} failed."
    )

    return successful_creates > 0
