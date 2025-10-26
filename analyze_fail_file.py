#!/usr/bin/env python3
"""Analyze fail files to identify patterns in error messages."""

import csv
import re
import sys
from collections import Counter
from pathlib import Path


def analyze_fail_file(fail_file_path: str) -> None:
    """Analyze a fail file and identify patterns in error messages."""
    print(f"🔍 Analyzing fail file: {fail_file_path}")
    print("=" * 60)

    if not Path(fail_file_path).exists():
        print(f"❌ File not found: {fail_file_path}")
        return

    error_reason_counter: Counter = Counter()
    error_type_counter: Counter = Counter()
    field_error_counter: Counter = Counter()
    model_counter: Counter = Counter()

    # Patterns to identify common error types
    error_patterns = {
        "tuple_index": r"tuple index out of range",
        "external_id": r"(external id|xml id|reference|does not exist|not found)",
        "type_conversion": r"(type|conversion|integer|float|string)",
        "constraint": r"(constraint|violation|unique|duplicate)",
        "serialization": r"(serialize|concurrent|deadlock)",
        "connection": r"(connection|timeout|pool)",
        "memory": r"(memory|out of memory)",
        "rpc": r"(rpc|api|call)",
        "field_validation": r"(field.*required|missing|required.*field)",
        "null_constraint": r"(null.*constraint|not-null|violates not-null)",
    }

    total_records = 0
    error_records = 0

    try:
        with open(fail_file_path, encoding="utf-8") as f:
            # Try to detect delimiter
            sample = f.read(1024)
            f.seek(0)

            if ";" in sample and "," not in sample:
                delimiter = ";"
            elif "," in sample and ";" not in sample:
                delimiter = ","
            else:
                delimiter = ";"  # Default

            reader = csv.reader(f, delimiter=delimiter)

            # Read header
            header = next(reader, [])
            if not header:
                print("❌ Empty file or invalid format")
                return

            # Find error reason column
            error_col_index = -1
            for i, col in enumerate(header):
                if (
                    "_ERROR_REASON" in col.upper()
                    or "ERROR_REASON" in col.upper()
                    or "error" in col.lower()
                ):
                    error_col_index = i
                    break

            if error_col_index == -1:
                print("❌ No error reason column found in header")
                print(f"Header columns: {header}")
                return

            print(f"📊 Header columns: {len(header)}")
            print(
                f"📝 Error reason column: {header[error_col_index] if error_col_index < len(header) else 'Unknown'}"
            )
            print("-" * 60)

            # Process each row
            for row_num, row in enumerate(reader, 1):
                total_records += 1

                if error_col_index >= len(row):
                    print(f"⚠️  Warning: Row {row_num} has fewer columns than expected")
                    continue

                error_reason = (
                    row[error_col_index].strip() if row[error_col_index] else ""
                )

                if error_reason:
                    error_records += 1
                    error_reason_counter[error_reason] += 1

                    # Classify error types
                    error_reason_lower = error_reason.lower()
                    for error_type, pattern in error_patterns.items():
                        if re.search(pattern, error_reason_lower):
                            error_type_counter[error_type] += 1

                    # Extract field names from error messages
                    field_matches = re.findall(r"'([^']+)'|\"([^\"]+)\"", error_reason)
                    for match in field_matches:
                        field_name = match[0] if match[0] else match[1]
                        if (
                            field_name and len(field_name) > 1
                        ):  # Filter out single characters
                            field_error_counter[field_name] += 1

                    # Extract model names if present
                    model_matches = re.findall(r"[a-z_]+[.][a-z_]+", error_reason_lower)
                    for model in model_matches:
                        model_counter[model] += 1

        print(f"📈 Total records analyzed: {total_records}")
        print(f"❌ Records with errors: {error_records}")
        if total_records > 0:
            error_rate = (error_records / total_records) * 100
            print(f"📊 Error rate: {error_rate:.2f}%")

        print("\n" + "=" * 60)
        print("📋 TOP 10 ERROR MESSAGES:")
        print("=" * 60)
        for error_msg, count in error_reason_counter.most_common(10):
            print(
                f"{count:5d} occurrences: {error_msg[:100]}{'...' if len(error_msg) > 100 else ''}"
            )

        print("\n" + "=" * 60)
        print("🏷️  ERROR TYPE CLASSIFICATION:")
        print("=" * 60)
        for error_type, count in error_type_counter.most_common():
            print(f"{count:5d} {error_type.replace('_', ' ').title()}")

        if field_error_counter:
            print("\n" + "=" * 60)
            print("🗃️  MOST FREQUENT FIELD NAMES IN ERRORS:")
            print("=" * 60)
            for field_name, count in field_error_counter.most_common(10):
                print(f"{count:5d} {field_name}")

        if model_counter:
            print("\n" + "=" * 60)
            print("📦 MOST FREQUENT MODELS IN ERRORS:")
            print("=" * 60)
            for model, count in model_counter.most_common(10):
                print(f"{count:5d} {model}")

        # Look for specific patterns that might indicate the tuple index issue
        print("\n" + "=" * 60)
        print("🔬 TUPLE INDEX ERROR ANALYSIS:")
        print("=" * 60)
        tuple_errors = [
            msg
            for msg in error_reason_counter.keys()
            if "tuple index out of range" in msg.lower()
        ]

        if tuple_errors:
            print("Found tuple index out of range errors:")
            for error in tuple_errors[:5]:  # Show first 5
                print(f"  • {error}")

            # Check if these are related to specific fields
            tuple_field_patterns = {}
            for error in tuple_errors:
                field_matches = re.findall(r"field.*?'([^']+)'", error.lower())
                for field in field_matches:
                    tuple_field_patterns[field] = tuple_field_patterns.get(field, 0) + 1

            if tuple_field_patterns:
                print("\nFields most associated with tuple index errors:")
                for field, count in sorted(
                    tuple_field_patterns.items(), key=lambda x: x[1], reverse=True
                ):
                    print(f"  • {field}: {count} occurrences")
        else:
            print("✅ No tuple index out of range errors found!")

        # Look for external ID resolution errors
        print("\n" + "=" * 60)
        print("🔗 EXTERNAL ID ERROR ANALYSIS:")
        print("=" * 60)
        external_id_errors = [
            msg
            for msg in error_reason_counter.keys()
            if any(
                pattern in msg.lower()
                for pattern in [
                    "external id",
                    "reference",
                    "does not exist",
                    "not found",
                    "xml id",
                ]
            )
        ]

        if external_id_errors:
            print("Found external ID resolution errors:")
            for error in external_id_errors[:5]:  # Show first 5
                print(f"  • {error[:150]}{'...' if len(error) > 150 else ''}")
        else:
            print("✅ No external ID resolution errors found!")

        # Look for type conversion errors
        print("\n" + "=" * 60)
        print("🔢 TYPE CONVERSION ERROR ANALYSIS:")
        print("=" * 60)
        type_errors = [
            msg
            for msg in error_reason_counter.keys()
            if any(
                pattern in msg.lower()
                for pattern in ["type", "conversion", "integer", "float", "string"]
            )
        ]

        if type_errors:
            print("Found type conversion errors:")
            for error in type_errors[:5]:  # Show first 5
                print(f"  • {error[:150]}{'...' if len(error) > 150 else ''}")
        else:
            print("✅ No type conversion errors found!")

    except Exception as e:
        print(f"❌ Error analyzing fail file: {e}")
        import traceback

        traceback.print_exc()


def main():
    """Main function to analyze fail files."""
    if len(sys.argv) < 2:
        print("Usage: python analyze_fail_file.py <fail_file_path>")
        print("\nExample: python analyze_fail_file.py product_supplierinfo_fail.csv")
        return

    fail_file_path = sys.argv[1]
    analyze_fail_file(fail_file_path)


if __name__ == "__main__":
    main()
