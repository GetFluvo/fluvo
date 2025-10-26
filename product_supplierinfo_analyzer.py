#!/usr/bin/env python3
"""Specific analyzer for product_supplierinfo import failures."""

import csv
import os
import sys
from collections import Counter
from typing import Optional


def create_sample_product_supplierinfo_fail_file():
    """Create a sample fail file with tuple index errors for testing."""
    print("📝 Creating sample product_supplierinfo fail file...")

    sample_data = [
        ["id", "name", "product_tmpl_id/id", "partner_id/id", "_ERROR_REASON"],
        [
            "SUPP001",
            "Supplier 1",
            "product_template.63657",
            "res_partner.123",
            "Tuple index out of range error for record SUPP001: This is often caused by sending incorrect data types to Odoo fields. Check your data types.",
        ],
        [
            "SUPP002",
            "Supplier 2",
            "product_template.456",
            "res_partner.456",
            "Tuple index out of range error for record SUPP002: This indicates the RPC call structure is incompatible with this server version or the record has unresolvable references.",
        ],
        [
            "SUPP003",
            "Supplier 3",
            "product_template.789",
            "res_partner.789",
            "IndexError: tuple index out of range in odoo/api.py:525",
        ],
        [
            "SUPP004",
            "Supplier 4",
            "product_template.63657",
            "res_partner.101",
            "External ID resolution error for record SUPP004: ValueError('does not seem to be an integer for field partner_id'). Original error typically caused by missing external ID references.",
        ],
        [
            "SUPP005",
            "Supplier 5",
            "product_template.63657",
            "res_partner.102",
            "Database serialization error for record SUPP005: TransactionRollbackError('could not serialize access due to concurrent update'). This may indicate a temporary server overload.",
        ],
    ]

    with open("product_supplierinfo_fail.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_ALL)
        writer.writerows(sample_data)

    print("✅ Created sample product_supplierinfo_fail.csv with tuple index errors")
    return "product_supplierinfo_fail.csv"


def analyze_product_supplierinfo_fail_file(fail_file: Optional[str] = None):
    """Analyze a product_supplierinfo fail file for tuple index errors."""
    if not fail_file:
        # Try to find existing product_supplierinfo fail files
        possible_files = [
            "product_supplierinfo_fail.csv",
            "product_supplier_fail.csv",
            "supplierinfo_fail.csv",
            "product.supplierinfo_fail.csv",
        ]

        for filename in possible_files:
            if os.path.exists(filename):
                fail_file = filename
                break

        if not fail_file:
            print("❌ No product_supplierinfo fail file found")
            print("💡 Creating sample file for demonstration...")
            fail_file = create_sample_product_supplierinfo_fail_file()

    print(f"🔍 Analyzing product_supplierinfo fail file: {fail_file}")
    print("=" * 60)

    try:
        with open(fail_file, encoding="utf-8") as f:
            # Detect delimiter
            sample = f.read(1024)
            f.seek(0)

            if ";" in sample and "," not in sample:
                delimiter = ";"
            elif "," in sample and ";" not in sample:
                delimiter = ","
            else:
                delimiter = ";"  # Default

            reader = csv.reader(f, delimiter=delimiter)
            header = next(reader, [])

            print(f"📋 Header: {header}")

            # Find error reason column
            error_col_index = -1
            for i, col in enumerate(header):
                if "_ERROR_REASON" in col:
                    error_col_index = i
                    break

            if error_col_index == -1:
                print("❌ No _ERROR_REASON column found")
                return

            print(
                f"📝 Error reason column: {header[error_col_index]} (index {error_col_index})"
            )

            # Analyze the data
            total_records = 0
            tuple_index_errors = 0
            external_id_errors = 0
            serialization_errors = 0
            other_errors = 0

            error_reasons = Counter()

            for row in reader:
                total_records += 1

                if error_col_index < len(row):
                    error_reason = row[error_col_index].strip()
                    if error_reason:
                        error_reasons[error_reason] += 1

                        error_lower = error_reason.lower()
                        if (
                            "tuple index out of range" in error_lower
                            or "indexerror" in error_lower
                        ):
                            tuple_index_errors += 1
                        elif (
                            "external id" in error_lower
                            or "reference" in error_lower
                            or "does not exist" in error_lower
                        ):
                            external_id_errors += 1
                        elif "serialize" in error_lower or "concurrent" in error_lower:
                            serialization_errors += 1
                        else:
                            other_errors += 1

            print("\n📊 ANALYSIS RESULTS:")
            print(f"   Total records: {total_records}")
            print(f"   Tuple index errors: {tuple_index_errors}")
            print(f"   External ID errors: {external_id_errors}")
            print(f"   Serialization errors: {serialization_errors}")
            print(f"   Other errors: {other_errors}")

            if total_records > 0:
                print("\n📈 ERROR BREAKDOWN:")
                print(
                    f"   Tuple index errors: {(tuple_index_errors / total_records) * 100:.1f}%"
                )
                print(
                    f"   External ID errors: {(external_id_errors / total_records) * 100:.1f}%"
                )
                print(
                    f"   Serialization errors: {(serialization_errors / total_records) * 100:.1f}%"
                )
                print(f"   Other errors: {(other_errors / total_records) * 100:.1f}%")

            if error_reasons:
                print("\n📋 TOP ERROR PATTERNS:")
                for reason, count in error_reasons.most_common(5):
                    percentage = (
                        (count / total_records) * 100 if total_records > 0 else 0
                    )
                    print(
                        f"   {percentage:5.1f}% ({count:3d}): {reason[:100]}{'...' if len(reason) > 100 else ''}"
                    )

            # Specific recommendations based on error types
            print("\n💡 RECOMMENDATIONS:")

            if tuple_index_errors > 0:
                print(f"🔧 TUPLE INDEX ERRORS ({tuple_index_errors} records):")
                print(
                    "   - These are typically caused by sending wrong data types to Odoo fields"
                )
                print("   - Check that numeric fields receive numbers, not strings")
                print("   - Verify that external ID fields contain valid references")
                print(
                    "   - Consider using --deferred-fields for self-referencing fields"
                )

            if external_id_errors > 0:
                print(f"\n🔗 EXTERNAL ID ERRORS ({external_id_errors} records):")
                print(
                    "   - Verify all external ID references exist in the target database"
                )
                print("   - Check for typos in external ID names")
                print(
                    "   - Ensure referenced records are imported before dependent records"
                )

            if serialization_errors > 0:
                print(f"\n🔄 SERIALIZATION ERRORS ({serialization_errors} records):")
                print("   - These indicate server overload or concurrent updates")
                print("   - Reduce worker count to decrease server load")
                print("   - Retry failed records in a subsequent run")

    except FileNotFoundError:
        print(f"❌ File not found: {fail_file}")
    except Exception as e:
        print(f"❌ Error analyzing file: {e}")
        import traceback

        traceback.print_exc()


def main():
    """Main function."""
    print("🔍 PRODUCT_SUPPLIERINFO FAIL FILE ANALYZER")
    print("=" * 60)

    fail_file = sys.argv[1] if len(sys.argv) > 1 else None
    analyze_product_supplierinfo_fail_file(fail_file)


if __name__ == "__main__":
    main()
