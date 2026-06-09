"""Data validation module for dry-run imports.

This module provides functionality to validate import data before
actually writing to Odoo, catching issues early.
"""

import csv
from dataclasses import dataclass, field
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel

from ..logging_config import log


@dataclass
class ValidationError:
    """Represents a single validation error."""

    row_number: int
    column: str
    value: str
    error_type: str
    message: str


@dataclass
class ValidationResult:
    """Results of a validation run."""

    total_rows: int = 0
    valid_rows: int = 0
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    missing_references: dict[str, set[str]] = field(default_factory=dict)
    invalid_selections: dict[str, set[str]] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """Returns True if no errors were found."""
        return len(self.errors) == 0

    @property
    def error_count(self) -> int:
        """Returns the total number of errors."""
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        """Returns the total number of warnings."""
        return len(self.warnings)


def _get_selection_values(fields_info: dict[str, Any], field_name: str) -> set[str]:
    """Extract valid selection values for a field."""
    field_info = fields_info.get(field_name, {})
    if field_info.get("type") != "selection":
        return set()

    selection = field_info.get("selection", [])
    if isinstance(selection, list):
        return {str(item[0]) for item in selection if isinstance(item, (list, tuple))}
    return set()


def _get_required_fields(fields_info: dict[str, Any]) -> set[str]:
    """Get list of required fields from fields_info."""
    required = set()
    for name, info in fields_info.items():
        if info.get("required", False) and not info.get("readonly", False):
            required.add(name)
    return required


def _get_relational_fields(
    fields_info: dict[str, Any], header: list[str]
) -> dict[str, dict[str, Any]]:
    """Get relational fields that need reference validation.

    Returns dict mapping column name to field info.
    """
    relational = {}
    for col in header:
        # Handle subfield notation like "partner_id/id"
        base_field = col.split("/")[0]
        field_info = fields_info.get(base_field, {})
        field_type = field_info.get("type", "")

        if field_type in ("many2one", "many2many"):
            relational[col] = {
                "field_name": base_field,
                "relation": field_info.get("relation", ""),
                "type": field_type,
            }
    return relational


def validate_csv_data(  # noqa: C901
    file_path: str,
    model: str,
    fields_info: dict[str, Any],
    connection: Any,
    separator: str = ";",
    encoding: str = "utf-8",
    ignore: Optional[list[str]] = None,
) -> ValidationResult:
    """Validate CSV data without importing.

    Args:
        file_path: Path to the CSV file.
        model: Odoo model name.
        fields_info: Field definitions from fields_get().
        connection: Odoo connection object.
        separator: CSV separator.
        encoding: File encoding.
        ignore: Columns to ignore.

    Returns:
        ValidationResult with all validation errors and warnings.
    """
    result = ValidationResult()
    ignore = ignore or []

    try:
        with open(file_path, encoding=encoding, newline="") as f:
            reader = csv.reader(f, delimiter=separator)
            header = next(reader)

            # Filter ignored columns
            col_indices = {
                i: col for i, col in enumerate(header) if col not in ignore and col
            }
            filtered_header = [col for col in header if col not in ignore and col]

            # Get field metadata
            required_fields = _get_required_fields(fields_info)
            relational_fields = _get_relational_fields(fields_info, filtered_header)
            selection_fields = {
                col: _get_selection_values(fields_info, col.split("/")[0])
                for col in filtered_header
                if fields_info.get(col.split("/")[0], {}).get("type") == "selection"
            }

            # Cache for reference lookups
            reference_cache: dict[str, dict[str, bool]] = {}

            for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is 1)
                result.total_rows += 1
                row_has_error = False

                # Build row dict
                row_data = {}
                for i, col in col_indices.items():
                    if i < len(row):
                        row_data[col] = row[i]

                # Check required fields
                for req_field in required_fields:
                    if req_field in filtered_header:
                        value = row_data.get(req_field, "").strip()
                        if not value:
                            result.errors.append(
                                ValidationError(
                                    row_number=row_num,
                                    column=req_field,
                                    value="",
                                    error_type="required_field",
                                    message=f"Required field '{req_field}' is empty",
                                )
                            )
                            row_has_error = True

                # Check selection field values
                for col, valid_values in selection_fields.items():
                    value = row_data.get(col, "").strip()
                    if value and value not in valid_values:
                        result.errors.append(
                            ValidationError(
                                row_number=row_num,
                                column=col,
                                value=value,
                                error_type="invalid_selection",
                                message=f"Invalid selection value '{value}'. "
                                f"Valid values: {', '.join(sorted(valid_values))}",
                            )
                        )
                        row_has_error = True

                        # Track for summary
                        if col not in result.invalid_selections:
                            result.invalid_selections[col] = set()
                        result.invalid_selections[col].add(value)

                # Check relational references
                for col, rel_info in relational_fields.items():
                    value = row_data.get(col, "").strip()
                    if not value:
                        continue

                    relation_model = rel_info["relation"]
                    if not relation_model:
                        continue

                    # Initialize cache for this model
                    if relation_model not in reference_cache:
                        reference_cache[relation_model] = {}

                    # Handle multiple values for m2m
                    if rel_info["type"] == "many2one":
                        values = [value]
                    else:
                        values = value.split(",")

                    for ref_value in values:
                        ref_value = ref_value.strip()
                        if not ref_value:
                            continue

                        # Check cache first
                        if ref_value in reference_cache[relation_model]:
                            if not reference_cache[relation_model][ref_value]:
                                # Already know it's missing
                                if col not in result.missing_references:
                                    result.missing_references[col] = set()
                                result.missing_references[col].add(ref_value)
                            continue

                        # Check if reference exists in Odoo
                        exists = _check_reference_exists(
                            connection, relation_model, ref_value
                        )
                        reference_cache[relation_model][ref_value] = exists

                        if not exists:
                            result.errors.append(
                                ValidationError(
                                    row_number=row_num,
                                    column=col,
                                    value=ref_value,
                                    error_type="missing_reference",
                                    message=f"Reference '{ref_value}' not found "
                                    f"in {relation_model}",
                                )
                            )
                            row_has_error = True

                            if col not in result.missing_references:
                                result.missing_references[col] = set()
                            result.missing_references[col].add(ref_value)

                if not row_has_error:
                    result.valid_rows += 1

    except FileNotFoundError:
        result.errors.append(
            ValidationError(
                row_number=0,
                column="",
                value=file_path,
                error_type="file_not_found",
                message=f"File not found: {file_path}",
            )
        )
    except Exception as e:
        result.errors.append(
            ValidationError(
                row_number=0,
                column="",
                value="",
                error_type="validation_error",
                message=f"Validation failed: {e}",
            )
        )

    return result


def _check_reference_exists(connection: Any, model: str, ref_value: str) -> bool:
    """Check if a reference exists in Odoo.

    Handles both external IDs (module.xml_id) and database IDs.
    """
    try:
        # Check if it's an external ID
        if "." in ref_value:
            ir_model_data = connection.get_model("ir.model.data")
            module, name = ref_value.split(".", 1)
            count = ir_model_data.search_count(
                [("module", "=", module), ("name", "=", name), ("model", "=", model)]
            )
            return bool(count > 0)

        # Check if it's a database ID
        try:
            db_id = int(ref_value)
            model_obj = connection.get_model(model)
            count = model_obj.search_count([("id", "=", db_id)])
            return bool(count > 0)
        except ValueError:
            # Not a valid ID format
            return False

    except Exception as e:
        log.debug(f"Error checking reference {ref_value} in {model}: {e}")
        return False


def display_validation_results(result: ValidationResult, model: str) -> None:
    """Display validation results in a formatted panel."""
    console = Console()

    if result.is_valid:
        console.print(
            Panel(
                f"[green]✓[/green] All {result.total_rows} rows validated "
                f"successfully.\nNo errors found. Data is ready for import.",
                title=f"[bold green]Validation Passed for {model}[/bold green]",
                expand=False,
            )
        )
        return

    # Build error summary
    lines = []
    lines.append(f"[red]✗[/red] Validation found {result.error_count} errors")
    lines.append(f"   Valid rows: {result.valid_rows}/{result.total_rows}")
    lines.append("")

    # Missing references summary
    if result.missing_references:
        lines.append("[bold]Missing References:[/bold]")
        for col, refs in result.missing_references.items():
            lines.append(f"  • {col}: {len(refs)} missing")
            # Show first few examples
            examples = list(refs)[:3]
            lines.append(f"    Examples: {', '.join(examples)}")
        lines.append("")

    # Invalid selections summary
    if result.invalid_selections:
        lines.append("[bold]Invalid Selection Values:[/bold]")
        for col, values in result.invalid_selections.items():
            lines.append(f"  • {col}: {', '.join(sorted(values))}")
        lines.append("")

    # Show first few detailed errors
    if result.errors:
        lines.append("[bold]First 10 Errors:[/bold]")
        for error in result.errors[:10]:
            if error.row_number > 0:
                lines.append(
                    f"  Row {error.row_number}, {error.column}: {error.message}"
                )
            else:
                lines.append(f"  {error.message}")

        if len(result.errors) > 10:
            lines.append(f"  ... and {len(result.errors) - 10} more errors")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[bold red]Validation Failed for {model}[/bold red]",
            expand=False,
        )
    )
