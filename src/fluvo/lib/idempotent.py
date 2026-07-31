"""Idempotent import module for skip-unchanged functionality.

This module provides functionality to detect unchanged records and skip
them during import, making imports idempotent and more efficient.
"""

from dataclasses import dataclass
from typing import Any, Optional

import polars as pl

from ..logging_config import log


def _norm_str(value: Any) -> str:
    """Normalize a value to its comparison string (None/empty -> "").

    Mirrors :func:`normalize_value` + :func:`compare_values`: both-None and
    one-None cases reduce to string equality against the "" sentinel, so a single
    normalized-string comparison reproduces ``compare_values`` exactly.
    """
    norm = normalize_value(value)
    return "" if norm is None else str(norm)


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


def get_existing_records(  # noqa: C901
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

        # Build lookup for external IDs. Resolve them in bulk: group names by
        # module and issue one search_read per module (chunked), instead of one
        # RPC per external id — a 100k-row --skip-unchanged run went from 100k
        # round-trips to a handful.
        ext_id_to_res_id: dict[str, int] = {}
        names_by_module: dict[str, list[str]] = {}
        for ext_id in external_ids:
            if "." not in ext_id:
                continue
            module, name = ext_id.split(".", 1)
            names_by_module.setdefault(module, []).append(name)

        for module, names in names_by_module.items():
            for i in range(0, len(names), 2000):
                chunk = names[i : i + 2000]
                rows = ir_model_data.search_read(
                    [
                        ("module", "=", module),
                        ("name", "in", chunk),
                        ("model", "=", model),
                    ],
                    ["module", "name", "res_id"],
                )
                for row in rows:
                    ext_id_to_res_id[f"{row['module']}.{row['name']}"] = row["res_id"]

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
            record_ext_id = res_id_to_ext_id.get(record["id"])
            if record_ext_id is not None:
                result[record_ext_id] = record

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


def _filter_unchanged_pyloop(  # noqa: C901
    rows: list[list[Any]],
    header: list[str],
    existing_records: dict[str, dict[str, Any]],
    id_field: str,
    compare_fields: Optional[list[str]],
) -> tuple[list[list[Any]], IdempotentStats]:
    """Row-by-row fallback (exact reference semantics, incl. error handling)."""
    stats = IdempotentStats()
    try:
        id_index = header.index(id_field)
    except ValueError:
        log.warning(f"ID field '{id_field}' not in header, cannot filter unchanged")
        stats.total_records = len(rows)
        return rows, stats

    if compare_fields is None:
        compare_fields = [h for h in header if h != id_field]
    field_indices = {f: header.index(f) for f in compare_fields if f in header}

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


def filter_unchanged_rows(
    rows: list[list[Any]],
    header: list[str],
    existing_records: dict[str, dict[str, Any]],
    id_field: str = "id",
    compare_fields: Optional[list[str]] = None,
) -> tuple[list[list[Any]], IdempotentStats]:
    """Filter out unchanged rows via a vectorized Polars anti-join.

    Main entry point for idempotent import filtering. A row is kept if it is new
    (its external id isn't in ``existing_records``) or changed (any compared field
    differs); unchanged rows are dropped so the ORM never rewrites them. Comparison
    semantics match :func:`compare_values` (normalize, then string-equal against a
    "" sentinel for None/empty). The big incoming side is compared in Polars while
    the (small) target dict is normalized in Python; original row order/content are
    preserved. Falls back to a row-by-row pass on any unexpected value.

    Args:
        rows: List of data rows (as lists).
        header: Column headers.
        existing_records: Dict of existing records keyed by external ID.
        id_field: Field containing the external ID.
        compare_fields: Fields to compare. If None, compares all non-id fields.

    Returns:
        Tuple of (filtered_rows, stats).
    """
    stats = IdempotentStats()
    if not existing_records:
        stats.total_records = len(rows)
        stats.new_records = len(rows)
        return rows, stats
    if id_field not in header:
        log.warning(f"ID field '{id_field}' not in header, cannot filter unchanged")
        stats.total_records = len(rows)
        return rows, stats
    if not rows:
        return rows, stats

    if compare_fields is None:
        compare_fields = [h for h in header if h != id_field]
    existing_keys: set[str] = set()
    for rec in existing_records.values():
        existing_keys.update(rec.keys())
    comparable = [
        f for f in compare_fields if f in header and f.split("/")[0] in existing_keys
    ]

    try:
        # Pad ragged rows with None (absent) - distinct from "" (present-empty) so
        # a missing trailing cell is skipped, not compared (reference semantics).
        width = len(header)
        padded = [(list(r) + [None] * (width - len(r)))[:width] for r in rows]
        df = pl.DataFrame(
            padded, schema={h: pl.String for h in header}, orient="row"
        ).with_row_index("__idx")
        df = df.with_columns(
            pl.col(id_field)
            .cast(pl.String)
            .str.strip_chars()
            .fill_null("")
            .alias("__extid")
        )

        existing_rows = []
        for ext_id, rec in existing_records.items():
            entry: dict[str, Any] = {"__extid": ext_id, "__present": True}
            for i, f in enumerate(comparable):
                entry[f"__t{i}"] = _norm_str(rec.get(f.split("/")[0]))
            existing_rows.append(entry)
        existing_df = (
            pl.DataFrame(existing_rows)
            if existing_rows
            else pl.DataFrame(schema={"__extid": pl.String, "__present": pl.Boolean})
        )
        df = df.join(existing_df, on="__extid", how="left")

        # Per-field "changed": only when the source cell is present (not absent).
        df = df.with_columns(
            [pl.col(f).is_not_null().alias(f"__p{i}") for i, f in enumerate(comparable)]
            + [
                pl.col(f).str.strip_chars().fill_null("").alias(f"__s{i}")
                for i, f in enumerate(comparable)
            ]
        )

        is_matched = pl.col("__present").fill_null(False) & (pl.col("__extid") != "")
        if comparable:
            changed_expr = pl.any_horizontal(
                [
                    pl.col(f"__p{i}") & (pl.col(f"__s{i}") != pl.col(f"__t{i}"))
                    for i in range(len(comparable))
                ]
            )
        else:
            changed_expr = pl.lit(value=False)

        df = df.with_columns(
            is_matched.alias("__matched"),
            (~is_matched | changed_expr).alias("__keep"),
        )

        kept_set = set(
            df.filter(pl.col("__keep")).select("__idx").to_series().to_list()
        )
        filtered_rows = [row for i, row in enumerate(rows) if i in kept_set]

        matched_count = int(df.select(pl.col("__matched").sum()).item() or 0)
        stats.total_records = len(rows)
        stats.new_records = len(rows) - matched_count
        stats.skipped_records = len(rows) - len(filtered_rows)
        stats.unchanged_records = stats.skipped_records
        stats.changed_records = matched_count - stats.unchanged_records
        stats.fields_compared = len(comparable) * matched_count
        return filtered_rows, stats
    except Exception as e:  # pragma: no cover - defensive fallback
        log.debug(f"Vectorized idempotent filter fell back to row loop: {e}")
        return _filter_unchanged_pyloop(
            rows, header, existing_records, id_field, compare_fields
        )


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
