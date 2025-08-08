"""Handles the 'Direct Relational Table Import' strategy."""

import tempfile
from pathlib import Path
from typing import Any

import polars as pl

from .. import import_threaded
from ..logging_config import log
from . import cache


def run_direct_relational_import(
    config: str,
    model: str,
    field: str,
    strategy_details: dict[str, Any],
    source_file: str,
    id_map: dict[str, int],
    worker: int,
    batch_size: int,
) -> bool:
    """Orchestrates the high-speed direct relational import.

    Args:
        config: Path to the connection configuration file.
        model: The master Odoo model (e.g., 'res.partner').
        field: The relational field to import (e.g., 'category_id').
        strategy_details: A dictionary from the import_plan.
        source_file: The path to the original source CSV file.
        id_map: The id_map for the master model.
        worker: The number of parallel workers.
        batch_size: The number of records per batch.

    Returns:
        True if the import was successful, False otherwise.
    """
    log.info(f"Running 'Direct Relational Import' for field '{field}'...")
    relational_table = strategy_details["relation_table"]
    owning_model_fk = strategy_details["relation_field"]
    related_model_fk = strategy_details["relation"]

    # 1. Prepare the owning model's IDs
    owning_df = pl.DataFrame({"external_id": id_map.keys(), "db_id": id_map.values()})

    # 2. Prepare the related model's IDs
    related_model_cache = cache.load_id_map(config, related_model_fk)
    if related_model_cache is None:
        log.warning(
            f"Could not find cache for related model '{related_model_fk}'. "
            f"Falling back to XML_ID resolution for field '{field}'."
        )
        # TODO: Implement fallback to XML_ID resolution
        return False

    # 3. Create the link table DataFrame
    source_df = pl.read_csv(source_file, truncate_ragged_lines=True)
    link_df = source_df.select(["id", field]).rename({"id": "external_id"})
    link_df = link_df.with_columns(pl.col(field).str.split(",")).explode(field)

    # Join to get DB IDs for the owning model
    link_df = link_df.join(owning_df, on="external_id", how="inner").rename(
        {"db_id": owning_model_fk}
    )

    # Join to get DB IDs for the related model
    link_df = link_df.join(
        related_model_cache.rename({"external_id": field}), on=field, how="inner"
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
    source_file: str,
    id_map: dict[str, int],
    worker: int,
    batch_size: int,
) -> bool:
    """Orchestrates the 'write_tuple' import for relational fields."""
    log.info(f"Running 'Write Tuple' for field '{field}'...")
    relational_table = strategy_details["relation_table"]
    owning_model_fk = strategy_details["relation_field"]
    related_model_fk = strategy_details["relation"]

    # 1. Prepare the owning model's IDs
    owning_df = pl.DataFrame({"external_id": id_map.keys(), "db_id": id_map.values()})

    # 2. Prepare the related model's IDs
    related_model_cache = cache.load_id_map(config, related_model_fk)
    if related_model_cache is None:
        log.warning(
            f"Could not find cache for related model '{related_model_fk}'. "
            f"Falling back to XML_ID resolution for field '{field}'."
        )
        # TODO: Implement fallback to XML_ID resolution
        return False

    # 3. Create the link table DataFrame
    source_df = pl.read_csv(source_file, truncate_ragged_lines=True)
    link_df = source_df.select(["id", field]).rename({"id": "external_id"})
    link_df = link_df.with_columns(pl.col(field).str.split(",")).explode(field)

    # Join to get DB IDs for the owning model
    link_df = link_df.join(owning_df, on="external_id", how="inner").rename(
        {"db_id": owning_model_fk}
    )

    # Join to get DB IDs for the related model
    link_df = link_df.join(
        related_model_cache.rename({"external_id": field}), on=field, how="inner"
    ).rename({"db_id": f"{related_model_fk}/id"})

    final_df = link_df.select([owning_model_fk, f"{related_model_fk}/id"])

    # 4. Create records in the relational table
    from . import conf_lib

    connection = conf_lib.get_connection_from_config(config_file=config)
    rel_model = connection.get_model(relational_table)

    vals_list = final_df.to_dicts()

    try:
        rel_model.create(vals_list)
        return True
    except Exception as e:
        log.error(f"Failed to create records for '{relational_table}': {e}")
        return False
