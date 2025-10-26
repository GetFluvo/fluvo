#!/usr/bin/env python3
"""Advanced analysis of fail files to identify specific error patterns."""

import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def advanced_error_analysis(fail_file_path: str) -> None:
    """Perform advanced analysis of error patterns in fail file."""
    print(f"🔍 Advanced Error Analysis for: {fail_file_path}")
    print("=" * 80)

    if not Path(fail_file_path).exists():
        print(f"❌ File not found: {fail_file_path}")
        return

    error_reason_counter: Counter = Counter()
    error_details: dict[str, list[str]] = defaultdict(list)
    field_usage_counter: Counter = Counter()
    model_reference_counter: Counter = Counter()

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
                f"📝 Error reason column: '{header[error_col_index] if error_col_index < len(header) else 'Unknown'}'"
            )
            print("-" * 80)

            # Process each row
            for _row_num, row in enumerate(reader, 1):
                total_records += 1

                if error_col_index >= len(row):
                    continue

                error_reason = (
                    row[error_col_index].strip() if row[error_col_index] else ""
                )

                if error_reason:
                    error_records += 1
                    error_reason_counter[error_reason] += 1

                    # Store row data for detailed analysis
                    if len(row) > 1:
                        # Store a sample of the data for this error
                        sample_data = ";".join(
                            str(cell)[:50] for cell in row[:5] if cell
                        )
                        error_details[error_reason].append(sample_data)

                    # Extract field names from error messages
                    field_matches = re.findall(r"'([^']+)'|\"([^\"]+)\"", error_reason)
                    for match in field_matches:
                        field_name = match[0] if match[0] else match[1]
                        if field_name and len(field_name) > 1 and "/" not in field_name:
                            field_usage_counter[field_name] += 1

                    # Extract model references
                    model_matches = re.findall(
                        r"([a-z_]+[.][a-z_]+)", error_reason.lower()
                    )
                    for model in model_matches:
                        model_reference_counter[model] += 1

        print(f"📈 Total records analyzed: {total_records}")
        print(f"❌ Records with errors: {error_records}")
        if total_records > 0:
            error_rate = (error_records / total_records) * 100
            print(f"📊 Error rate: {error_rate:.2f}%")

        print("\n" + "=" * 80)
        print("📋 TOP ERROR PATTERNS:")
        print("=" * 80)

        # Group similar errors
        error_groups = defaultdict(int)
        error_samples = defaultdict(list)

        for error_msg, count in error_reason_counter.most_common():
            # Create a normalized error pattern
            normalized = re.sub(r"'[^']*'|\"[^\"]*\"|[0-9]+", "XXX", error_msg.lower())
            normalized = re.sub(r"\[[^\]]*\]", "[XXX]", normalized)
            error_groups[normalized] += count
            if len(error_samples[normalized]) < 3:
                error_samples[normalized].append((error_msg, count))

        # Sort by frequency
        sorted_groups = sorted(error_groups.items(), key=lambda x: x[1], reverse=True)

        for i, (pattern, count) in enumerate(sorted_groups[:15], 1):
            percentage = (count / error_records) * 100 if error_records > 0 else 0
            print(f"\n{i:2d}. {percentage:5.1f}% ({count:4d} records): {pattern}")
            print("     Sample errors:")
            for sample_msg, _sample_count in error_samples[pattern][:2]:
                print(
                    f"       • {sample_msg[:120]}{'...' if len(sample_msg) > 120 else ''}"
                )

        print("\n" + "=" * 80)
        print("🔬 SPECIFIC TUPLE INDEX ERROR ANALYSIS:")
        print("=" * 80)

        tuple_index_errors = [
            msg
            for msg in error_reason_counter.keys()
            if "tuple index out of range" in msg.lower()
        ]

        if tuple_index_errors:
            print(
                f"🚨 Found {len(tuple_index_errors)} tuple index out of range errors:"
            )
            for error in tuple_index_errors[:10]:  # Show first 10
                print(f"  • {error[:150]}{'...' if len(error) > 150 else ''}")

            # Analyze what fields are involved in these errors
            tuple_field_analysis = defaultdict(int)
            for error in tuple_index_errors:
                # Look for field names in the error message
                field_matches = re.findall(
                    r"(?:field|column)[^']*'([^']+)'", error.lower()
                )
                for field in field_matches:
                    tuple_field_analysis[field] += 1

                # Look for any field-like references
                all_fields = re.findall(r"'([^']+/id)'", error)
                for field in all_fields:
                    tuple_field_analysis[field] += 1

            if tuple_field_analysis:
                print("\nFields most associated with tuple index errors:")
                for field, count in sorted(
                    tuple_field_analysis.items(), key=lambda x: x[1], reverse=True
                ):
                    print(f"  • {field}: {count} occurrences")
            else:
                print("\nNo specific fields identified in tuple index errors.")
        else:
            print("✅ No tuple index out of range errors found!")

        print("\n" + "=" * 80)
        print("🔗 EXTERNAL ID RELATED ERROR ANALYSIS:")
        print("=" * 80)

        external_id_patterns = [
            "external id",
            "xml id",
            "reference",
            "does not exist",
            "not found",
            "res_id not found",
            "invalid reference",
            "unknown external id",
            "missing record",
            "referenced record",
        ]

        external_id_errors = [
            msg
            for msg in error_reason_counter.keys()
            if any(pattern in msg.lower() for pattern in external_id_patterns)
        ]

        if external_id_errors:
            print(f"🚨 Found {len(external_id_errors)} external ID related errors:")
            for error in external_id_errors[:10]:  # Show first 10
                print(f"  • {error[:150]}{'...' if len(error) > 150 else ''}")
        else:
            print("✅ No external ID related errors found!")

        print("\n" + "=" * 80)
        print("🏷️  MOST REFERENCED FIELDS IN ERRORS:")
        print("=" * 80)

        if field_usage_counter:
            for field, count in field_usage_counter.most_common(15):
                percentage = (count / error_records) * 100 if error_records > 0 else 0
                print(f"{percentage:5.1f}% ({count:4d}): {field}")
        else:
            print("No field references found in errors.")

        print("\n" + "=" * 80)
        print("📦 MOST REFERENCED MODELS IN ERRORS:")
        print("=" * 80)

        if model_reference_counter:
            for model, count in model_reference_counter.most_common(15):
                percentage = (count / error_records) * 100 if error_records > 0 else 0
                print(f"{percentage:5.1f}% ({count:4d}): {model}")
        else:
            print("No model references found in errors.")

        # Recommendation section
        print("\n" + "=" * 80)
        print("💡 RECOMMENDATIONS:")
        print("=" * 80)

        if tuple_index_errors:
            print("1. 🔧 TUPLE INDEX ERRORS:")
            print("   - These suggest malformed data being sent to Odoo fields")
            print("   - Check data types in columns that appear in error messages")
            print("   - Validate that external ID columns contain valid references")
            print("   - Consider using --deferred-fields for self-referencing fields")

        if external_id_errors:
            print("\n2. 🔗 EXTERNAL ID ERRORS:")
            print("   - Verify all external ID references exist in target database")
            print("   - Check for typos in external ID names")
            print(
                "   - Ensure referenced records are imported before dependent records"
            )

        if field_usage_counter:
            print("\n3. 📊 FIELD VALIDATION:")
            top_fields = [field for field, count in field_usage_counter.most_common(5)]
            print(
                f"   - Pay special attention to these frequently problematic fields: {', '.join(top_fields)}"
            )
            print("   - Validate data types and formats for these fields")

    except Exception as e:
        print(f"❌ Error analyzing fail file: {e}")
        import traceback

        traceback.print_exc()


def main():
    """Main function to analyze fail files."""
    if len(sys.argv) < 2:
        print("Usage: python advanced_error_analysis.py <fail_file_path>")
        print(
            "\nExample: python advanced_error_analysis.py product_supplierinfo_fail.csv"
        )
        return

    fail_file_path = sys.argv[1]
    advanced_error_analysis(fail_file_path)


if __name__ == "__main__":
    main()
