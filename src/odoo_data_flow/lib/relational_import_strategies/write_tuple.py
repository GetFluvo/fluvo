"""Handles write tuple import strategy."""

from typing import Any, Optional, Union

import polars as pl
from rich.progress import Progress, TaskID

from ...logging_config import log
from .. import conf_lib, writer


def _get_actual_field_name(field: str, source_df: pl.DataFrame) -> str:
    """Get the actual field name from the source data, handling external ID fields.

    Args:
        field: The base field name to look for.
        source_df: The source DataFrame containing the data.

    Returns:
        The actual field name to use (either the base field or field/id variant).
    """
    # Check if the base field exists directly
    if field in source_df.columns:
        return field

    # Check if the external ID variant (field/id) exists
    id_variant = field + "/id"
    if id_variant in source_df.columns:
        return id_variant

    # Neither exists, return the original field (will cause error downstream)
    return field


def _prepare_link_dataframe(
    config: Union[str, dict[str, Any]],
    model: str,
    field: str,
    source_df: pl.DataFrame,
    id_map: dict[str, int],
    batch_size: int,
) -> Optional[pl.DataFrame]:
    """Prepare the link dataframe for write tuple import.

    Args:
        config: Connection configuration.
        model: Odoo model name.
        field: Field name to process.
        source_df: Source data DataFrame.
        id_map: Map of source IDs to database IDs.
        batch_size: Size of processing batches.

    Returns:
        Prepared DataFrame with links or None on error.
    """
    try:
        log.debug(f"Preparing link dataframe for {model}.{field}")
        log.debug(f"Available columns in source_df: {list(source_df.columns)}")

        # Get the field info from the source data
        # Check for both base field name and /id variant for external ID fields
        actual_field_name = field
        if field not in source_df.columns:
            log.debug(
                f"Base field '{field}' not found, checking for external ID variant"
            )
            # Check if this is an external ID field (field/id format)
            id_variant = field + "/id"
            if id_variant in source_df.columns:
                actual_field_name = id_variant
                log.debug(
                    f"Using external ID field '{id_variant}' for base field '{field}'"
                )
            else:
                log.error(
                    f"Field '{field}' not found in source data (checked also for '{id_variant}')"
                )
                log.error(f"Available columns: {list(source_df.columns)}")
                return None
        elif (field + "/id") in source_df.columns:
            # Both base field and /id variant exist - prefer the /id variant for external IDs
            actual_field_name = field + "/id"
            log.debug(
                f"Using external ID field '{actual_field_name}' for base field '{field}' (both exist)"
            )

        log.debug(f"Using actual_field_name: '{actual_field_name}'")

        # Extract field values using the actual field name
        field_values = source_df[actual_field_name].to_list()

        # Debug: Show data statistics
        total_records = len(field_values)
        non_null_values = len([v for v in field_values if v is not None])
        non_empty_values = len(
            [v for v in field_values if v is not None and str(v).strip()]
        )
        log.debug(f"Field data statistics for '{actual_field_name}':")
        log.debug(f"  Total records: {total_records}")
        log.debug(f"  Non-null values: {non_null_values}")
        log.debug(f"  Non-empty values: {non_empty_values}")

        # Show detailed samples of non-empty values for debugging
        if non_empty_values > 0:
            sample_values = []
            empty_count = 0
            for v in field_values:
                if v is not None and str(v).strip():
                    sample_values.append(str(v))
                elif v is None or str(v).strip() == "":
                    empty_count += 1

            log.debug(f"  Empty/whitespace values: {empty_count}")
            shown_samples = 0
            for val in sample_values[:10]:  # Show first 10 non-empty values
                truncated = val[:100] + "..." if len(val) > 100 else val
                log.debug(f"    Sample[{shown_samples + 1}]: {truncated!r}")
                shown_samples += 1
                if shown_samples >= 5:  # Limit to 5 samples in logs
                    break
            if len(sample_values) > 5:
                log.debug(f"    ... and {len(sample_values) - 5} more non-empty values")

        # Create a list of tuples (source_id, field_value)
        link_data = []
        empty_values_debug = []
        for _i, (source_id, field_value) in enumerate(zip(id_map.keys(), field_values)):
            field_str = str(field_value) if field_value is not None else ""
            stripped_value = field_str.strip()

            if field_value is not None and stripped_value:
                link_data.append((source_id, stripped_value))
            elif field_value is not None:  # Non-null but empty after strip
                empty_values_debug.append((source_id, repr(field_str)))
            # null values are ignored entirely

        log.debug(f"Processed {len(field_values)} records:")
        log.debug(f"  Added to link_data: {len(link_data)} records")
        log.debug(f"  Skipped (empty/whitespace): {len(empty_values_debug)} records")

        if len(link_data) == 0 and non_empty_values > 0:
            log.warning(
                f"WARNING: Found {non_empty_values} non-empty values but link_data is empty!"
            )
            log.warning("  This suggests a filtering issue in the processing logic")
            # Show some of the values that should have been included
            sample_skipped = []
            for source_id, field_value in zip(id_map.keys(), field_values):
                if field_value is not None and str(field_value).strip():
                    sample_skipped.append((source_id, str(field_value)[:50]))
                    if len(sample_skipped) >= 3:
                        break
            if sample_skipped:
                log.warning(f"  Sample values that were skipped: {sample_skipped}")

        if not link_data:
            log.info(f"No valid link data found for {model}.{field}")
            if non_empty_values > 0:
                log.info(
                    f"Note: {non_empty_values} non-empty values existed but were filtered out"
                )
            return pl.DataFrame()

        # Convert to DataFrame
        link_df = pl.DataFrame(
            {
                "source_id": [item[0] for item in link_data],
                "field_value": [item[1] for item in link_data],
            }
        )

        log.debug(
            f"Prepared link DataFrame with {len(link_df)} records for {model}.{field}"
        )
        if len(link_df) > 0:
            log.debug(f"  First 3 records: {link_df.head(3).to_dicts()}")

        log.debug(f"Prepared {len(link_df)} link records for {model}.{field}")
        return link_df

    except Exception as e:
        log.error(f"Failed to prepare link dataframe for {model}.{field}: {e}")
        return None


def _execute_write_tuple_updates(
    config: Union[str, dict[str, Any]],
    model: str,
    field: str,
    link_df: pl.DataFrame,
    id_map: dict[str, int],
    batch_size: int,
    context: Optional[dict[str, Any]] = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Execute write tuple updates for a batch of records.

    Args:
        config: Connection configuration.
        model: Odoo model name.
        field: Field name to update.
        link_df: DataFrame with link data.
        id_map: Map of source IDs to database IDs.
        batch_size: Size of processing batches.

    Returns:
        Tuple of (successful_updates, failed_records).
    """
    successful_updates = 0
    failed_records = []

    try:
        # Connect to Odoo
        if isinstance(config, dict):
            connection = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config)
        model_obj = connection.get_model(model)

        # Process in batches
        total_records = len(link_df)
        for i in range(0, total_records, batch_size):
            batch_df = link_df.slice(i, min(batch_size, total_records - i))

            # Prepare update data
            update_data = []
            for row in batch_df.iter_rows(named=True):
                source_id = row["source_id"]
                field_value = row["field_value"]

                # Get the database ID for this record
                db_id = id_map.get(source_id)
                if not db_id:
                    failed_records.append(
                        {
                            "model": model,
                            "field": field,
                            "source_id": source_id,
                            "field_value": field_value,
                            "error_reason": f"Source ID '{source_id}' not found in ID map",
                        }
                    )
                    continue

                # Add to update data
                update_data.append({"id": db_id, field: field_value})

            if update_data:
                try:
                    # Execute the write operation
                    if context:
                        model_obj.with_context(**context).write(update_data)
                    else:
                        model_obj.write(update_data)
                    successful_updates += len(update_data)
                except Exception as e:
                    # Record failures for this batch
                    for row in batch_df.iter_rows(named=True):
                        failed_records.append(
                            {
                                "model": model,
                                "field": field,
                                "source_id": row["source_id"],
                                "field_value": row["field_value"],
                                "error_reason": str(e),
                            }
                        )

        return successful_updates, failed_records

    except Exception as e:
        log.error(f"Failed to execute write tuple updates for {model}.{field}: {e}")
        # Add all records as failed
        for row in link_df.iter_rows(named=True):
            failed_records.append(
                {
                    "model": model,
                    "field": field,
                    "source_id": row["source_id"],
                    "field_value": row["field_value"],
                    "error_reason": f"System error: {e}",
                }
            )
        return 0, failed_records


def run_write_tuple_import(
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
) -> bool:
    """Run the write tuple import strategy.

    This strategy processes relational data by writing tuples of (id, value) to update fields.

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

    Returns:
        True if successful, False otherwise.
    """
    log.info(f"Starting write tuple import for {model}.{field}")

    try:
        # Prepare the link dataframe
        link_df = _prepare_link_dataframe(
            config, model, field, source_df, id_map, batch_size
        )
        if link_df is None:
            log.error(f"Failed to prepare link dataframe for {model}.{field}")
            return False

        if len(link_df) == 0:
            log.info(f"No link data to process for {model}.{field}")
            return True

        # Execute the write tuple updates
        successful_updates, failed_records = _execute_write_tuple_updates(
            config, model, field, link_df, id_map, batch_size, context
        )

        # Report results
        total_records = len(link_df)
        log.info(
            f"Write tuple import completed for {model}.{field}: "
            f"{successful_updates}/{total_records} records updated"
        )

        # Handle failed records
        if failed_records:
            log.warning(
                f"{len(failed_records)} records failed during write tuple import for {model}.{field}"
            )
            writer.write_relational_failures_to_csv(
                model, field, filename, failed_records
            )

        # Update progress
        progress.update(task_id, completed=total_records)

        return successful_updates > 0 or total_records == 0

    except Exception as e:
        log.error(f"Write tuple import failed for {model}.{field}: {e}")
        return False
