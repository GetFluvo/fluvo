"""Idempotent import module for skip-unchanged functionality.

This module provides functionality to detect unchanged records and skip
them during import, making imports idempotent and more efficient.
"""

from dataclasses import dataclass
from typing import Any, Optional

from ..logging_config import log


@dataclass
class IdempotentStats:
    """Statistics for idempotent import operations."""

    total_records: int = 0
    unchanged_records: int = 0
    changed_records: int = 0
    new_records: int = 0
    skipped_records: int = 0
    fields_compared: int = 0
    comparison_errors: int = 0

    @property
    def skip_rate(self) -> float:
        """Calculate the skip rate percentage."""
        if self.total_records == 0:
            return 0.0
        return (self.skipped_records / self.total_records) * 100


def normalize_value(value: Any) -> Any:
    """Normalize a value for comparison.

    Handles various Odoo value formats:
    - False/None -> None
    - Empty strings -> None
    - Many2one tuples -> just the ID
    - Strips whitespace from strings
    """
    if value is False or value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return None
        if len(value) == 2 and isinstance(value[0], int):
            # Many2one tuple (id, name)
            return value[0]
        return value
    return value


def compare_values(source_value: Any, target_value: Any) -> bool:
    """Compare two values for equality after normalization.

    Args:
        source_value: Value from CSV/source data.
        target_value: Value from Odoo.

    Returns:
        True if values are equal, False otherwise.
    """
    norm_source = normalize_value(source_value)
    norm_target = normalize_value(target_value)

    # Both None/empty
    if norm_source is None and norm_target is None:
        return True

    # One is None
    if norm_source is None or norm_target is None:
        return False

    # Compare as strings for flexibility
    return str(norm_source) == str(norm_target)


def get_existing_records(
    connection: Any,
    model: str,
    external_ids: list[str],
    fields: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch existing records from Odoo by external IDs.

    Args:
        connection: Odoo connection object.
        model: Model name.
        external_ids: List of external IDs to look up.
        fields: Fields to fetch for comparison.

    Returns:
        Dict mapping external ID to record data.
    """
    result: dict[str, dict[str, Any]] = {}

    if not external_ids:
        return result

    try:
        ir_model_data = connection.get_model("ir.model.data")
        model_obj = connection.get_model(model)

        # Build lookup for external IDs
        ext_id_to_res_id: dict[str, int] = {}

        for ext_id in external_ids:
            if "." not in ext_id:
                continue

            module, name = ext_id.split(".", 1)
            records = ir_model_data.search_read(
                [
                    ("module", "=", module),
                    ("name", "=", name),
                    ("model", "=", model),
                ],
                ["res_id"],
                limit=1,
            )
            if records:
                ext_id_to_res_id[ext_id] = records[0]["res_id"]

        if not ext_id_to_res_id:
            return result

        # Fetch the actual records with the requested fields
        res_ids = list(ext_id_to_res_id.values())
        records = model_obj.search_read(
            [("id", "in", res_ids)],
            fields,
        )

        # Build reverse lookup
        res_id_to_ext_id = {v: k for k, v in ext_id_to_res_id.items()}

        for record in records:
            ext_id = res_id_to_ext_id.get(record["id"])
            if ext_id:
                result[ext_id] = record

    except Exception as e:
        log.warning(f"Error fetching existing records: {e}")

    return result


def find_unchanged_records(
    csv_data: list[dict[str, Any]],
    existing_records: dict[str, dict[str, Any]],
    id_field: str = "id",
    compare_fields: Optional[list[str]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], IdempotentStats]:
    """Identify unchanged records that can be skipped.

    Args:
        csv_data: List of records from CSV (as dicts).
        existing_records: Dict of existing records keyed by external ID.
        id_field: Field containing the external ID.
        compare_fields: Fields to compare. If None, compares all fields.

    Returns:
        Tuple of (changed_records, unchanged_records, stats).
    """
    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    stats = IdempotentStats()

    for record in csv_data:
        stats.total_records += 1
        ext_id = record.get(id_field, "")

        if not ext_id or ext_id not in existing_records:
            # New record - always include
            stats.new_records += 1
            changed.append(record)
            continue

        existing = existing_records[ext_id]
        fields_to_compare = compare_fields or [
            k for k in record.keys() if k != id_field
        ]

        is_changed = False
        for field_name in fields_to_compare:
            if field_name not in record:
                continue

            # Handle subfield notation (partner_id/id -> partner_id)
            base_field = field_name.split("/")[0]
            if base_field not in existing:
                continue

            stats.fields_compared += 1

            try:
                if not compare_values(record[field_name], existing[base_field]):
                    is_changed = True
                    break
            except Exception:
                stats.comparison_errors += 1
                is_changed = True  # If we can't compare, assume changed
                break

        if is_changed:
            stats.changed_records += 1
            changed.append(record)
        else:
            stats.unchanged_records += 1
            stats.skipped_records += 1
            unchanged.append(record)

    return changed, unchanged, stats


def filter_unchanged_rows(  # noqa: C901
    rows: list[list[Any]],
    header: list[str],
    existing_records: dict[str, dict[str, Any]],
    id_field: str = "id",
    compare_fields: Optional[list[str]] = None,
) -> tuple[list[list[Any]], IdempotentStats]:
    """Filter out unchanged rows from import data.

    This is the main entry point for idempotent import filtering.

    Args:
        rows: List of data rows (as lists).
        header: Column headers.
        existing_records: Dict of existing records keyed by external ID.
        id_field: Field containing the external ID.
        compare_fields: Fields to compare. If None, compares all fields.

    Returns:
        Tuple of (filtered_rows, stats).
    """
    stats = IdempotentStats()

    if not existing_records:
        stats.total_records = len(rows)
        stats.new_records = len(rows)
        return rows, stats

    # Find id field index
    try:
        id_index = header.index(id_field)
    except ValueError:
        log.warning(f"ID field '{id_field}' not in header, cannot filter unchanged")
        stats.total_records = len(rows)
        return rows, stats

    # Determine which fields to compare
    if compare_fields is None:
        compare_fields = [h for h in header if h != id_field]

    # Build field index mapping
    field_indices = {}
    for field_name in compare_fields:
        if field_name in header:
            field_indices[field_name] = header.index(field_name)

    filtered_rows: list[list[Any]] = []

    for row in rows:
        stats.total_records += 1

        if id_index >= len(row):
            filtered_rows.append(row)
            continue

        ext_id = str(row[id_index]).strip()

        if not ext_id or ext_id not in existing_records:
            stats.new_records += 1
            filtered_rows.append(row)
            continue

        existing = existing_records[ext_id]
        is_changed = False

        for field_name, field_idx in field_indices.items():
            if field_idx >= len(row):
                continue

            base_field = field_name.split("/")[0]
            if base_field not in existing:
                continue

            stats.fields_compared += 1

            try:
                if not compare_values(row[field_idx], existing[base_field]):
                    is_changed = True
                    break
            except Exception:
                stats.comparison_errors += 1
                is_changed = True
                break

        if is_changed:
            stats.changed_records += 1
            filtered_rows.append(row)
        else:
            stats.unchanged_records += 1
            stats.skipped_records += 1

    return filtered_rows, stats


def display_idempotent_stats(stats: IdempotentStats, model: str) -> None:
    """Display idempotent import statistics."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    lines = [
        f"Total records: {stats.total_records}",
        f"New records: {stats.new_records}",
        f"Changed records: {stats.changed_records}",
        f"Unchanged (skipped): {stats.skipped_records}",
        f"Skip rate: {stats.skip_rate:.1f}%",
    ]

    if stats.comparison_errors > 0:
        lines.append(f"Comparison errors: {stats.comparison_errors}")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[bold cyan]Idempotent Import Stats for {model}[/bold cyan]",
            expand=False,
        )
    )
