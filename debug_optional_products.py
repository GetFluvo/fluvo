#!/usr/bin/env python3
"""Debug script to analyze optional_product_ids data."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import polars as pl


def analyze_csv_data(csv_file_path):
    """Analyze the optional_product_ids/id column in CSV."""
    print(f"Analyzing CSV file: {csv_file_path}")

    # Read the CSV with polars
    try:
        df = pl.read_csv(csv_file_path, separator=";", infer_schema_length=0)
        print(f"Total rows: {len(df)}")
        print(f"Columns: {list(df.columns)}")

        # Check if optional_product_ids/id column exists
        if "optional_product_ids/id" in df.columns:
            print("\n=== optional_product_ids/id Analysis ===")
            opt_col = df["optional_product_ids/id"]

            # Count non-null values
            non_null_count = opt_col.drop_nulls().len()
            print(f"Non-null values: {non_null_count}")

            # Count non-empty values
            non_empty_count = opt_col.filter(
                pl.col("optional_product_ids/id").is_not_null()
                & (pl.col("optional_product_ids/id") != "")
            ).len()
            print(f"Non-empty values: {non_empty_count}")

            # Show sample of non-empty values
            sample_non_empty = opt_col.filter(
                pl.col("optional_product_ids/id").is_not_null()
                & (pl.col("optional_product_ids/id") != "")
            ).head(10)
            print("Sample non-empty values:")
            for i, val in enumerate(sample_non_empty.to_list()):
                print(f"  {i + 1}: {val!r}")

            # Count empty/null values
            empty_count = len(opt_col) - non_empty_count
            print(f"Empty/null values: {empty_count}")

        else:
            print("Column 'optional_product_ids/id' not found in CSV")

        # Also check for base column
        if "optional_product_ids" in df.columns:
            print("\n=== optional_product_ids Analysis ===")
            base_col = df["optional_product_ids"]
            non_empty_base = base_col.filter(
                pl.col("optional_product_ids").is_not_null()
                & (pl.col("optional_product_ids") != "")
            ).len()
            print(f"Non-empty base values: {non_empty_base}")

    except Exception as e:
        print(f"Error reading CSV: {e}")
        # Try with pandas as fallback
        try:
            import pandas as pd

            df = pd.read_csv(csv_file_path, sep=";")
            print(f"Pandas - Total rows: {len(df)}")
            print(f"Pandas - Columns: {list(df.columns)}")

            if "optional_product_ids/id" in df.columns:
                non_empty = df["optional_product_ids/id"].dropna()
                non_empty = non_empty[non_empty != ""]
                print(f"Pandas - Non-empty values: {len(non_empty)}")
                print("Sample values:")
                for i, val in enumerate(non_empty.head(10)):
                    print(f"  {i + 1}: {val!r}")
        except Exception as e2:
            print(f"Pandas also failed: {e2}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python debug_optional_products.py <path_to_csv_file>")
        sys.exit(1)

    csv_file = sys.argv[1]
    if not os.path.exists(csv_file):
        print(f"File not found: {csv_file}")
        sys.exit(1)

    analyze_csv_data(csv_file)
