#!/usr/bin/env python3
"""Debug script to reproduce supplierinfo partner_id issue."""

import os
import sys

import polars as pl

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from odoo_data_flow.lib.relational_import_strategies.direct import _resolve_related_ids
from odoo_data_flow.lib.relational_import_strategies.write_tuple import (
    _prepare_link_dataframe,
)


def test_partner_id_processing():
    """Test partner_id field processing to see where value is lost."""
    print("🔍 Testing partner_id field processing...")

    # Create a simple DataFrame that mimics supplierinfo data
    df = pl.DataFrame(
        {
            "id": ["sup1", "sup2"],
            "name": ["Supplier 1", "Supplier 2"],
            "partner_id": [
                "res_partner_1",
                "res_partner_2",
            ],  # This should NOT be deferred
            "delay": [1, 2],
            "min_qty": [10.0, 20.0],
        }
    )

    print("📊 Original DataFrame:")
    print(df)
    print(f"partner_id column dtype: {df['partner_id'].dtype}")
    print(f"partner_id values: {df['partner_id'].to_list()}")

    # Test _prepare_link_dataframe to see what happens to the field
    print("\n🔧 Testing _prepare_link_dataframe...")
    try:
        result = _prepare_link_dataframe(
            config={
                "hostname": "localhost",
                "database": "test",
                "login": "admin",
                "password": "admin",
                "port": 8069,
            },
            model="res.partner",
            field="partner_id",
            source_df=df,
            id_map={"res_partner_1": 101, "res_partner_2": 102},
            batch_size=1000,
        )
        print(f"✅ _prepare_link_dataframe result: {result}")
        if result is not None:
            print(f"Result type: {type(result)}")
            if hasattr(result, "shape"):
                print(f"Result shape: {result.shape}")
                if result.shape[0] > 0:
                    print("Result data:")
                    print(result)
    except Exception as e:
        print(f"❌ _prepare_link_dataframe failed: {e}")
        import traceback

        traceback.print_exc()

    # Test _resolve_related_ids to see what happens to the field values
    print("\n🔧 Testing _resolve_related_ids...")
    try:
        result = _resolve_related_ids(
            config={
                "hostname": "localhost",
                "database": "test",
                "login": "admin",
                "password": "admin",
                "port": 8069,
            },
            related_model="res.partner",
            external_ids=pl.Series(["res_partner_1", "res_partner_2"]),
        )
        print(f"✅ _resolve_related_ids result: {result}")
        if result is not None:
            print(f"Result type: {type(result)}")
            if hasattr(result, "shape"):
                print(f"Result shape: {result.shape}")
                if result.shape[0] > 0:
                    print("Result data:")
                    print(result)
    except Exception as e:
        print(f"❌ _resolve_related_ids failed: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    test_partner_id_processing()
