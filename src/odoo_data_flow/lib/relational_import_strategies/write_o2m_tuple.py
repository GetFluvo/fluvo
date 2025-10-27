"""Handles write O2M tuple import strategy."""

from typing import Any, Optional, Union

import polars as pl
from rich.progress import Progress, TaskID

from ...logging_config import log
from .. import conf_lib, writer


def _create_relational_records(
    config: Union[str, dict[str, Any]],
    model: str,
    field: str,
    relation: str,
    parent_id: int,
    related_external_ids: list[str],
    context: Optional[dict[str, Any]] = None,
) -> tuple[list[int], list[dict[str, Any]]]:
    """Create relational records for one-to-many fields.

    Args:
        config: Connection configuration.
        model: Parent Odoo model name.
        field: Field name (e.g., 'line_ids').
        relation: Related model name (e.g., 'account.move.line').
        parent_id: Parent record database ID.
        related_external_ids: List of related external IDs.
        context: Odoo context.

    Returns:
        Tuple of (created_ids, failed_records).
    """
    created_ids = []
    failed_records = []

    try:
        # Connect to Odoo
        if isinstance(config, dict):
            connection = conf_lib.get_connection_from_dict(config)
        else:
            connection = conf_lib.get_connection_from_config(config)
        relation_model = connection.get_model(relation)

        # Process each related external ID
        for ext_id in related_external_ids:
            try:
                # Resolve the external ID to a database ID
                record_ref = relation_model.env.ref(ext_id, raise_if_not_found=False)
                if record_ref:
                    related_db_id = record_ref.id
                    created_ids.append(related_db_id)
                else:
                    failed_records.append(
                        {
                            "model": model,
                            "field": field,
                            "parent_id": parent_id,
                            "related_external_id": ext_id,
                            "error_reason": f"Related record with external ID '{ext_id}' not found",
                        }
                    )
            except Exception as e:
                failed_records.append(
                    {
                        "model": model,
                        "field": field,
                        "parent_id": parent_id,
                        "related_external_id": ext_id,
                        "error_reason": str(e),
                    }
                )

        return created_ids, failed_records

    except Exception as e:
        log.error(f"Failed to create relational records for {model}.{field}: {e}")
        # Add all records as failed
        for ext_id in related_external_ids:
            failed_records.append(
                {
                    "model": model,
                    "field": field,
                    "parent_id": parent_id,
                    "related_external_id": ext_id,
                    "error_reason": f"System error: {e}",
                }
            )
        return [], failed_records


def run_write_o2m_tuple_import(
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
    """Run the write O2M tuple import strategy.

    This strategy processes one-to-many relational data by creating command tuples for updates.

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
        context: Odoo context.

    Returns:
        True if successful, False otherwise.
    """
    log.info(f"Starting write O2M tuple import for {model}.{field}")

    try:
        # Get field information
        relation = strategy_info.get("relation", "")
        if not relation:
            log.error(
                f"Could not determine relation model for O2M field {model}.{field}"
            )
            return False

        # Process source data to extract O2M relationships
        if field not in source_df.columns:
            log.warning(f"Field '{field}' not found in source data for {model}")
            return True

        field_values = source_df[field].to_list()
        source_ids = list(id_map.keys())

        # Track progress
        total_records = len(field_values)
        successful_updates = 0
        failed_records_to_report = []

        # Process records in batches
        for i in range(0, total_records, batch_size):
            batch_end = min(i + batch_size, total_records)
            batch_values = field_values[i:batch_end]
            batch_source_ids = source_ids[i:batch_end]

            # Process each record in the batch
            for _j, (source_id, field_value) in enumerate(
                zip(batch_source_ids, batch_values)
            ):
                try:
                    # Get parent database ID
                    parent_db_id = id_map.get(source_id)
                    if not parent_db_id:
                        failed_records_to_report.append(
                            {
                                "model": model,
                                "field": field,
                                "parent_external_id": source_id,
                                "related_external_id": "N/A",
                                "error_reason": f"Parent record with external ID '{source_id}' not found in ID map",
                            }
                        )
                        continue

                    # Skip empty values
                    if not field_value or str(field_value).strip() == "":
                        continue

                    # Parse the field value - it should be a comma-separated list of external IDs
                    try:
                        related_ext_ids = [
                            ext_id.strip()
                            for ext_id in str(field_value).split(",")
                            if ext_id.strip()
                        ]
                    except (ValueError, TypeError):
                        failed_records_to_report.append(
                            {
                                "model": model,
                                "field": field,
                                "parent_external_id": source_id,
                                "related_external_id": "N/A",
                                "error_reason": f"Invalid field value format for O2M field: {field_value}",
                            }
                        )
                        continue

                    if not related_ext_ids:
                        continue

                    # Create command tuples for the O2M relationship
                    try:
                        # Connect to Odoo
                        if isinstance(config, dict):
                            connection = conf_lib.get_connection_from_dict(config)
                        else:
                            connection = conf_lib.get_connection_from_config(config)
                        model_obj = connection.get_model(model)

                        # Create the O2M command tuples
                        commands = []
                        for ext_id in related_ext_ids:
                            try:
                                record_ref = model_obj.env.ref(
                                    ext_id, raise_if_not_found=False
                                )
                                if record_ref:
                                    related_db_id = record_ref.id
                                    # (4, ID) means "link" - add existing record to the O2M field
                                    commands.append((4, related_db_id))
                                else:
                                    failed_records_to_report.append(
                                        {
                                            "model": model,
                                            "field": field,
                                            "parent_external_id": source_id,
                                            "related_external_id": ext_id,
                                            "error_reason": f"Related record with external ID '{ext_id}' not found",
                                        }
                                    )
                            except Exception as e:
                                failed_records_to_report.append(
                                    {
                                        "model": model,
                                        "field": field,
                                        "parent_external_id": source_id,
                                        "related_external_id": ext_id,
                                        "error_reason": str(e),
                                    }
                                )

                        if commands:
                            # Execute the write operation with O2M command tuples
                            write_vals = {field: commands}
                            if context:
                                model_obj.with_context(**context).write(
                                    [parent_db_id], write_vals
                                )
                            else:
                                model_obj.write([parent_db_id], write_vals)
                            successful_updates += 1

                    except Exception as e:
                        failed_records_to_report.append(
                            {
                                "model": model,
                                "field": field,
                                "parent_external_id": source_id,
                                "related_external_id": "N/A (command creation)",
                                "error_reason": str(e),
                            }
                        )

                except Exception as e:
                    failed_records_to_report.append(
                        {
                            "model": model,
                            "field": field,
                            "parent_external_id": source_id,
                            "related_external_id": "N/A (processing)",
                            "error_reason": str(e),
                        }
                    )

            # Update progress
            progress.update(task_id, advance=min(batch_size, total_records - i))

        # Report final results
        log.info(
            f"Write O2M tuple import completed for {model}.{field}: "
            f"{successful_updates} successful updates, {len(failed_records_to_report)} failures"
        )

        # Write failed records to CSV if any
        if failed_records_to_report:
            writer.write_relational_failures_to_csv(
                model, field, filename, failed_records_to_report
            )

        return successful_updates > 0 or len(failed_records_to_report) == 0

    except Exception as e:
        log.error(f"Write O2M tuple import failed for {model}.{field}: {e}")
        return False
