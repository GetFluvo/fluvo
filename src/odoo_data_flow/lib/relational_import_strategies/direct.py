"""Handles direct relational import strategy."""

import tempfile
from typing import Any, Optional, Union

import polars as pl
from rich.progress import Progress, TaskID

from ...logging_config import log
from .. import cache, conf_lib


def _resolve_related_ids(
    config: Union[str, dict[str, Any]], related_model: str, external_ids: pl.Series
) -> Optional[pl.DataFrame]:
    """Resolve related ids.

    Resolves external IDs for a related model, trying cache first,
    then falling back to a bulk XML-ID resolution.
    """
    # 1. Try to load from cache
    if isinstance(config, str):
        related_model_cache = cache.load_id_map(config, related_model)
        if related_model_cache is not None:
            log.info(f"Cache hit for related model '{related_model}'.")
            return related_model_cache

    # 2. Fallback to bulk XML-ID resolution
    log.warning(
        f"Cache miss for related model '{related_model}'. "
        f"Falling back to bulk XML-ID resolution for {len(external_ids)} IDs."
    )

    # 2a. Connect to Odoo
    try:
        if isinstance(config, dict):
            connection = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config_file=config)
    except Exception as e:
        log.error(f"Could not connect to Odoo: {e}")
        return None

    # 2b. Resolve the external IDs using ir.model.data
    tmp_csv_path = None
    try:
        # Create a temporary CSV file with the external IDs, one per line
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, newline="", suffix=".csv"
        ) as tmp_csv:
            tmp_csv.write("id\n")
            for ext_id in external_ids:
                if ext_id and str(ext_id).strip():
                    tmp_csv.write(f"{ext_id}\n")
            tmp_csv_path = tmp_csv.name

        # Read the temporary CSV file to get the data frame
        tmp_df = pl.read_csv(tmp_csv_path)
        tmp_df = tmp_df.filter(pl.col("id").is_not_null() & (pl.col("id") != ""))
        external_ids_clean = tmp_df["id"]

        if len(external_ids_clean) == 0:
            log.info("No valid external IDs to resolve after cleaning.")
            return pl.DataFrame(schema={"id": pl.Utf8, "res_id": pl.Int64})

        # Prepare the data for the search_read call
        domain = [
            ("model", "=", related_model),
            ("name", "in", external_ids_clean.to_list()),
        ]
        fields = ["name", "res_id"]

        # Perform the search_read
        model_data = connection.get_model("ir.model.data")
        result = model_data.search_read(domain=domain, fields=fields)

        # Convert the result to a DataFrame
        if result:
            df_result = pl.DataFrame(result)
            df_result = df_result.select(["name", "res_id"])
            df_result = df_result.rename({"name": "id"})

            # Save to cache if config is a string (indicating a config file path)
            if isinstance(config, str):
                id_map_dict = dict(zip(df_result["id"], df_result["res_id"]))
                cache.save_id_map(config, related_model, id_map_dict)

            return df_result
        else:
            log.info(
                f"No matching records found for {len(external_ids_clean)} external IDs."
            )
            return pl.DataFrame(schema={"id": pl.Utf8, "res_id": pl.Int64})

    except Exception as e:
        log.error(f"Failed to resolve external IDs for model '{related_model}': {e}")
        return None
    finally:
        # Clean up the temporary file
        if tmp_csv_path:
            try:
                import os

                os.unlink(tmp_csv_path)
            except Exception as e:
                # Silently ignore cleanup errors to avoid interrupting the main process
                # This is acceptable since temporary files will eventually be cleaned by OS
                import logging

                logging.getLogger(__name__).debug(
                    f"Ignoring cleanup error for temporary file: {e}"
                )


def _derive_missing_relation_info(
    config: Union[str, dict[str, Any]],
    model: str,
    field: str,
    field_type: Optional[str],
    relation: Optional[str],
    source_df: pl.DataFrame,
) -> tuple[pl.DataFrame, str, str]:
    """Derive missing relation information from Odoo.

    Args:
        config: Path to connection file or connection dict.
        model: The name of the Odoo model.
        field: The name of the field.
        field_type: The type of the field (e.g., 'many2one', 'many2many').
        relation: The related model name.
        source_df: The source DataFrame.

    Returns:
        A tuple containing:
            - DataFrame with relation information.
            - The derived field type.
            - The derived relation model.
    """
    # Derive missing information from Odoo if needed
    if field_type is None or relation is None:
        try:
            result = _query_relation_info_from_odoo(config, model, field)
            if result is not None:
                field_type, relation = result
            else:
                field_type = field_type or ""
                relation = relation or ""
        except Exception as e:
            log.error(f"Could not query relation info from Odoo: {e}")
            return pl.DataFrame(), field_type or "", relation or ""

    # Connect to Odoo to get detailed field information
    try:
        if isinstance(config, dict):
            connection = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config_file=config)
        model_obj = connection.get_model(model)
    except Exception as e:
        log.error(f"Could not connect to Odoo to derive relation info: {e}")
        return pl.DataFrame(), field_type or "", relation or ""

    try:
        # Get the field information from Odoo
        fields_info = model_obj.fields_get([field])
        if field in fields_info:
            field_info = fields_info[field]
            derived_type = field_info.get("type", field_type or "")
            derived_relation = field_info.get("relation", relation or "")

            log.info(
                f"Derived field info for '{field}': type={derived_type}, relation={derived_relation}"
            )

            # If we have a relation, resolve the external IDs
            if derived_relation and field_type in ["many2one", "many2many"]:
                external_ids_series = source_df[field]
                relation_df = _resolve_related_ids(
                    config, derived_relation, external_ids_series
                )
                return relation_df, derived_type, derived_relation
            else:
                return pl.DataFrame(), derived_type, derived_relation
        else:
            log.warning(f"Field '{field}' not found in model '{model}'")
            return pl.DataFrame(), field_type or "", relation or ""

    except Exception as e:
        log.error(f"Failed to derive relation info for field '{field}': {e}")
        return pl.DataFrame(), field_type or "", relation or ""


def _query_relation_info_from_odoo(
    config: Union[str, dict[str, Any]], model: str, field: str
) -> Optional[tuple[str, str]]:
    """Query relation info from Odoo for a specific field.

    Args:
        config: Connection configuration (file path or dict).
        model: Odoo model name.
        field: Field name to query.

    Returns:
        A tuple of (field_type, relation_model), or None on exception.
    """
    # Handle self-referencing models to avoid constraint errors
    if model == field:
        log.debug(
            f"Self-referencing model detected: {model}.{field}. Returning None to skip."
        )
        return None

    try:
        if isinstance(config, dict):
            connection = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config_file=config)
        model_obj = connection.get_model(model)

        fields_info = model_obj.fields_get([field])
        if field in fields_info:
            field_info = fields_info[field]
            field_type = field_info.get("type", "unknown")
            relation_model = field_info.get("relation", "")
            return field_type, relation_model
        else:
            return None  # Return None when field not found
    except Exception as e:
        log.error(f"Failed to query relation info from Odoo for {model}.{field}: {e}")
        return None  # Return None on exception


def _derive_relation_info(
    config: Union[str, dict[str, Any]],
    model: str,
    field: str,
    source_df: pl.DataFrame,
    field_type: Optional[str] = None,
    relation: Optional[str] = None,
) -> tuple[pl.DataFrame, str, str]:
    """Derive relation information for a field, using cached data when available.

    Args:
        config: Path to connection file or connection dict.
        model: The name of the Odoo model.
        field: The name of the field.
        source_df: The source DataFrame.
        field_type: The type of the field (optional).
        relation: The related model name (optional).

    Returns:
        A tuple containing:
            - DataFrame with relation information.
            - The field type.
            - The relation model.
    """
    # Try to load cached relation info first
    if isinstance(config, str):
        cached_info = cache.load_relation_info(config, model, field)
        if cached_info is not None:
            log.info(f"Cached relation info found for {model}.{field}")
            cached_df, cached_type, cached_relation = cached_info
            return cached_df, cached_type, cached_relation

    # If no cache or cache miss, derive from Odoo
    if field_type is None or relation is None:
        result = _query_relation_info_from_odoo(config, model, field)
        if result is not None:
            field_type, relation = result
        else:
            field_type = field_type or ""
            relation = relation or ""

    # Derive missing information
    relation_df, derived_type, derived_relation = _derive_missing_relation_info(
        config, model, field, field_type, relation, source_df
    )

    # Cache the results if using file-based config
    if isinstance(config, str):
        cache.save_relation_info(
            config, model, field, relation_df, derived_type, derived_relation
        )

    return relation_df, derived_type, derived_relation


def run_direct_relational_import(
    config: Union[str, dict[str, Any]],
    model: str,
    field: str,
    strategy_info: dict[str, Any],
    source_df: pl.DataFrame,
    id_map: dict[str, int],
    max_connection: int,
    batch_size: int,
    progress: Progress,
    task_id: TaskID,
    filename: str,
    context: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Run the direct relational import strategy.

    This strategy processes relational data by directly linking records using resolved IDs.

    Args:
        config: Path to connection file or connection dict.
        model: The Odoo model to import into.
        field: The field to update.
        strategy_info: Strategy information from preflight.
        source_df: Source DataFrame containing the data.
        id_map: Map of source IDs to database IDs.
        max_connection: Maximum number of concurrent connections.
        batch_size: Size of each processing batch.
        progress: Rich progress instance.
        task_id: Task ID for progress tracking.
        filename: Source filename.
        context: Context dictionary for Odoo operations.

    Returns:
        Optional dict with import details for chained imports, or None.
    """
    log.info(f"Starting direct relational import for {model}.{field}")

    # Get field information
    field_type = strategy_info.get("type", "many2one")
    relation = strategy_info.get("relation", "")

    # Derive relation information
    relation_df, derived_type, derived_relation = _derive_relation_info(
        config, model, field, source_df, field_type, relation
    )

    if derived_type != field_type or derived_relation != relation:
        log.info(
            f"Field info updated: type {field_type}->{derived_type}, "
            f"relation {relation}->{derived_relation}"
        )
        field_type, relation = derived_type, derived_relation

    # Validate we have the relation information we need
    if not relation:
        log.error(f"Could not determine relation model for field {field}")
        return None

    if relation_df.height == 0:
        log.warning(f"No relation data found for {model}.{field}")
        return None

    # Merge relation data with source data
    try:
        # Create a mapping from external ID to database ID
        relation_map = dict(
            zip(relation_df["id"].to_list(), relation_df["res_id"].to_list())
        )

        # Get the field values from the source DataFrame
        field_values = source_df[field].to_list()

        # Resolve the field values to database IDs
        resolved_ids = []
        for ext_id in field_values:
            if ext_id and str(ext_id).strip():
                db_id = relation_map.get(str(ext_id).strip())
                if db_id:
                    resolved_ids.append(db_id)
                else:
                    resolved_ids.append(None)
            else:
                resolved_ids.append(None)

        # Update the records using the resolved IDs
        success_count = 0
        if isinstance(config, dict):
            connection = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config_file=config)
        model_obj = connection.get_model(model)

        # Process in batches
        total_records = len(resolved_ids)
        for i in range(0, total_records, batch_size):
            batch_end = min(i + batch_size, total_records)
            batch_ids = resolved_ids[i:batch_end]

            # Filter out None values
            valid_updates = [
                (source_id, db_id)
                for source_id, db_id in zip(list(id_map.keys())[i:batch_end], batch_ids)
                if db_id is not None
            ]

            if valid_updates:
                try:
                    # Prepare the update data
                    update_data = [
                        {"id": db_id, field: related_id}
                        for source_id, (db_id, related_id) in zip(
                            list(id_map.keys())[i:batch_end],
                            [
                                (id_map[source_id], db_id)
                                for source_id, db_id in valid_updates
                            ],
                        )
                    ]

                    # Perform the write operation
                    if context:
                        model_obj.with_context(**context).write(update_data)
                    else:
                        model_obj.write(update_data)
                    success_count += len(valid_updates)
                except Exception as e:
                    log.error(f"Failed to update batch {i // batch_size + 1}: {e}")

            # Update progress
            progress.update(task_id, advance=len(batch_ids))

        log.info(
            f"Direct relational import completed for {model}.{field}: "
            f"{success_count}/{total_records} records updated"
        )
        return {"model": model, "field": field, "updates": success_count}

    except Exception as e:
        log.error(f"Direct relational import failed for {model}.{field}: {e}")
        return None
