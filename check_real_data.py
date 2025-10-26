#!/usr/bin/env python3
"""Check real data in CSV to understand what's happening."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import polars as pl


def check_optional_product_data(csv_file_path):
    """Check the actual optional_product_ids data in the CSV."""
    print(f"Checking CSV file: {csv_file_path}")

    try:
        # Read the CSV
        df = pl.read_csv(csv_file_path, separator=";", infer_schema_length=0)
        print(f"Total records: {len(df)}")
        print(f"Columns: {list(df.columns)}")

        # Check for optional_product_ids columns
        opt_cols = [col for col in df.columns if "optional_product" in col.lower()]
        print(f"\nOptional product columns found: {opt_cols}")

        for col in opt_cols:
            print(f"\n=== Analyzing column: {col} ===")
            col_data = df[col]

            # Basic statistics
            total_count = len(col_data)
            null_count = col_data.is_null().sum()
            non_null_count = total_count - null_count

            print(f"Total records: {total_count}")
            print(f"Null values: {null_count}")
            print(f"Non-null values: {non_null_count}")

            # For non-null values, check if they're empty or have content
            if non_null_count > 0:
                non_null_data = col_data.drop_nulls()
                # Use pl.lit("") for comparison
                non_empty_count = non_null_data.filter(pl.col(col) != pl.lit("")).len()
                empty_count = non_null_count - non_empty_count

                print(f"Non-empty values: {non_empty_count}")
                print(f"Empty string values: {empty_count}")

                # Show sample non-empty values
                if non_empty_count > 0:
                    sample_non_empty = non_null_data.filter(
                        pl.col(col) != pl.lit("")
                    ).head(10)
                    print("Sample non-empty values:")
                    for i, val in enumerate(sample_non_empty.to_list()):
                        print(f"  {i + 1}: {val!r}")

                        # Check if this looks like valid external ID data
                        if val and isinstance(val, str) and "," in str(val):
                            ids = [x.strip() for x in str(val).split(",") if x.strip()]
                            print(
                                f"    -> Appears to contain {len(ids)} external IDs: {ids}"
                            )

            # Show some rows that have data in this column
            if non_empty_count > 0:
                rows_with_data = df.filter(
                    pl.col(col).is_not_null() & (pl.col(col) != pl.lit(""))
                )
                print(f"\nFirst 3 rows with data in {col}:")
                for i in range(min(3, len(rows_with_data))):
                    row = rows_with_data.row(i)
                    # Find the column index
                    col_idx = list(rows_with_data.columns).index(col)
                    print(f"  Row {i + 1}: {col} = {row[col_idx]!r}")

    except Exception as e:
        print(f"Error reading CSV: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python check_real_data.py <path_to_csv_file>")
        sys.exit(1)

    csv_file = sys.argv[1]
    if not os.path.exists(csv_file):
        print(f"File not found: {csv_file}")
        sys.exit(1)

    check_optional_product_data(csv_file)
