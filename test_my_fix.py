#!/usr/bin/env python3
"""Test script to validate the write_tuple.py fix."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import polars as pl

from odoo_data_flow.lib.relational_import_strategies.write_tuple import (
    _prepare_link_dataframe,
)


def test_field_mapping():
    """Test that field mapping works correctly."""
    print("Testing field mapping fix...")

    # Create a mock source DataFrame with /id fields
    source_df = pl.DataFrame(
        {
            "id": ["REC1", "REC2", "REC3"],
            "optional_product_ids/id": ["PROD1,PROD2", "PROD3", ""],
            "name": ["Product 1", "Product 2", "Product 3"],
        }
    )

    # Mock id_map
    id_map = {"REC1": 1, "REC2": 2, "REC3": 3}

    print(f"Source DataFrame columns: {list(source_df.columns)}")

    # Test the case where we're looking for 'optional_product_ids'
    # but the actual field is 'optional_product_ids/id'
    result = _prepare_link_dataframe(
        config={"dummy": "config"},
        model="product.template",
        field="optional_product_ids",  # This is what the system looks for
        source_df=source_df,
        id_map=id_map,
        batch_size=10,
    )

    if result is not None:
        print("✅ SUCCESS: Field mapping worked correctly!")
        print(f"Result DataFrame shape: {result.shape}")
        print(f"Result columns: {list(result.columns)}")
        print("First few rows:")
        print(result.head())
        return True
    else:
        print("❌ FAILURE: Field mapping failed")
        return False


if __name__ == "__main__":
    success = test_field_mapping()
    sys.exit(0 if success else 1)
