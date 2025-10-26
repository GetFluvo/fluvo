#!/usr/bin/env python3
"""Helper script to find and analyze product_supplierinfo fail files."""

import csv
import os
from collections import Counter
from pathlib import Path


def find_potential_fail_files():
    """Find all potential fail files in the project."""
    print("🔍 Searching for potential fail files...")
    print("=" * 50)

    # Common fail file patterns
    patterns = [
        "*fail*.csv",
        "*_fail*",
        "*failed*.csv",
        "*error*.csv",
        "fail.csv",
        "failed.csv",
        "*_fail.csv",
    ]

    found_files = []

    # Search in current directory and subdirectories
    for pattern in patterns:
        for file_path in Path(".").rglob(pattern):
            if file_path.is_file() and not any(
                exclude in str(file_path)
                for exclude in [".git", "__pycache__", ".mypy_cache", ".pytest_cache"]
            ):
                try:
                    size = file_path.stat().st_size
                    found_files.append((str(file_path), size))
                except OSError:
                    continue

    if found_files:
        print(f"📁 Found {len(found_files)} potential fail files:")
        for file_path, size in sorted(found_files, key=lambda x: x[1], reverse=True):
            print(f"  {size:>10} bytes: {file_path}")
    else:
        print("❌ No potential fail files found")

    return found_files


def analyze_file_content(file_path: str, sample_lines: int = 10):
    """Analyze the content of a potential fail file."""
    print(f"\n📄 Analyzing file: {file_path}")
    print("-" * 50)

    try:
        # Check file size
        size = os.path.getsize(file_path)
        print(f"📏 File size: {size:,} bytes")

        if size == 0:
            print("📭 File is empty")
            return

        # Try to read the file
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            # Read first few lines to check format
            lines = []
            for i, line in enumerate(f):
                if i >= sample_lines:
                    break
                lines.append(line.strip())

            if not lines:
                print("📭 File appears to be empty")
                return

            # Try to detect delimiter and format
            first_line = lines[0]
            if ";" in first_line and "," not in first_line:
                delimiter = ";"
            elif "," in first_line and ";" not in first_line:
                delimiter = ","
            else:
                delimiter = ";"  # Default

            print(f"📝 Delimiter detected: '{delimiter}'")

            # Try to parse as CSV
            try:
                with open(file_path, encoding="utf-8", errors="ignore") as f:
                    reader = csv.reader(f, delimiter=delimiter)
                    header = next(reader, [])
                    print(f"📋 Header columns: {len(header)}")
                    if header:
                        print(f"   Columns: {header}")

                    # Count total lines
                    line_count = 0
                    error_lines = 0
                    tuple_errors = 0
                    external_id_errors = 0

                    error_reasons = Counter()

                    for row in reader:
                        line_count += 1
                        if len(row) > len(header):
                            error_col_index = (
                                len(header) - 1
                            )  # Last column should be error reason
                        else:
                            error_col_index = len(row) - 1 if row else -1

                        if error_col_index >= 0 and len(row) > error_col_index:
                            error_reason = (
                                row[error_col_index].strip()
                                if row[error_col_index]
                                else ""
                            )
                            if error_reason:
                                error_lines += 1
                                error_reasons[error_reason] += 1

                                # Check for specific error patterns
                                error_lower = error_reason.lower()
                                if "tuple index out of range" in error_lower:
                                    tuple_errors += 1
                                if any(
                                    pattern in error_lower
                                    for pattern in [
                                        "external id",
                                        "reference",
                                        "does not exist",
                                        "not found",
                                    ]
                                ):
                                    external_id_errors += 1

                    print(f"📊 Total data lines: {line_count}")
                    print(f"❌ Lines with errors: {error_lines}")
                    if line_count > 0:
                        error_rate = (error_lines / line_count) * 100
                        print(f"📈 Error rate: {error_rate:.2f}%")

                    print(f"🚨 Tuple index errors: {tuple_errors}")
                    print(f"🔗 External ID errors: {external_id_errors}")

                    if error_reasons:
                        print("\n📋 Top 5 error reasons:")
                        for reason, count in error_reasons.most_common(5):
                            percentage = (
                                (count / error_lines) * 100 if error_lines > 0 else 0
                            )
                            print(
                                f"  {percentage:5.1f}% ({count:4d}): {reason[:80]}{'...' if len(reason) > 80 else ''}"
                            )

            except Exception as e:
                print(f"❌ Error parsing as CSV: {e}")
                print("🔤 First few lines as text:")
                for i, line in enumerate(lines[:5]):
                    print(
                        f"  {i + 1:2d}: {line[:100]}{'...' if len(line) > 100 else ''}"
                    )

    except Exception as e:
        print(f"❌ Error reading file: {e}")


def main():
    """Main function."""
    print("🔍 PRODUCT_SUPPLIERINFO FAIL FILE ANALYZER")
    print("=" * 60)

    # Find potential fail files
    fail_files = find_potential_fail_files()

    if not fail_files:
        print(
            "\n💡 No fail files found. Please run your import with --fail-file option:"
        )
        print(
            "   odoo-data-flow import --connection-file config.conf --file product_supplierinfo.csv --model product.supplierinfo --fail-file product_supplierinfo_fail.csv"
        )
        return

    # Analyze the largest files first
    sorted_files = sorted(fail_files, key=lambda x: x[1], reverse=True)

    print(f"\n📊 Analyzing top {min(3, len(sorted_files))} largest files...")

    for file_path, _size in sorted_files[:3]:
        analyze_file_content(file_path)
        print()


if __name__ == "__main__":
    main()
