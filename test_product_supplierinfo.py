#!/usr/bin/env python3
"""Simple test script to replicate the product.supplierinfo import issue."""

import os
import sys
import tempfile

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Import the main module
# Mock the connection to avoid actual Odoo calls
from unittest.mock import MagicMock, patch

from odoo_data_flow import import_threaded


def test_product_supplierinfo_import():
    """Test product.supplierinfo import with problematic external ID."""
    print("Testing product.supplierinfo import with problematic external ID...")

    # Mock the connection setup
    with patch(
        "odoo_data_flow.import_threaded.conf_lib.get_connection_from_config"
    ) as mock_get_conn:
        mock_model = MagicMock()
        mock_get_conn.return_value.get_model.return_value = mock_model

        # Mock the load method to fail with external ID error (like the original issue)
        def load_side_effect(header, lines, context=None):
            print(f"Load called with header: {header}")
            print(f"Load called with lines: {lines}")
            raise Exception(
                "No matching record found for external id "
                "'PRODUCT_TEMPLATE.63657' in field 'Product Template'"
            )

        mock_model.load.side_effect = load_side_effect

        # Mock the create method to handle individual record creation
        def create_side_effect(vals, context=None):
            print(f"Creating record with vals: {vals}")
            # Check if this contains the problematic external ID
            external_id_in_vals = any(
                "product_template.63657" in str(v).lower() for v in vals.values()
            )
            if external_id_in_vals:
                print(
                    "This record contains the problematic external ID "
                    "'product_template.63657'"
                )
                # Simulate the error that would occur during individual processing
                raise Exception(
                    "Tuple index out of range error when processing "
                    "external ID reference"
                )
            mock_record = MagicMock()
            mock_record.id = 101
            print("Record created successfully")
            return mock_record

        mock_model.create.side_effect = create_side_effect

        # Mock the ref method to handle external ID resolution
        def ref_side_effect(external_id, raise_if_not_found=True):
            print(f"Resolving external ID: {external_id}")
            if "product_template.6357" in external_id.lower():
                if raise_if_not_found:
                    raise Exception(
                        f"No matching record found for external id '{external_id}'"
                    )
                else:
                    return None
            else:
                mock_ref = MagicMock()
                mock_ref.id = 999
                return mock_ref

        mock_model.env.ref.side_effect = ref_side_effect

        # Test with data that contains the problematic external ID
        test_data = """id;product_tmpl_id/id;name;min_qty;price
PRODUCT_SUPPLIERINFO.321933;product_template.63657;Test Supplier;1;100.0"""

        # Write test data to temporary file
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv") as f:
            f.write(test_data)
            temp_file = f.name

        try:
            # Run the import
            result, stats = import_threaded.import_data(
                config={
                    "hostname": "test",
                    "database": "test",
                    "login": "test",
                    "password": "test",
                },
                model="product.supplierinfo",
                unique_id_field="id",
                file_csv=temp_file,
                fail_file=temp_file.replace(".csv", "_fail.csv"),
            )
            print(f"Import result: {result}")
            print(f"Stats: {stats}")
        except Exception as e:
            print(f"Import failed with error: {e}")
            import traceback

            traceback.print_exc()
        finally:
            # Clean up
            os.unlink(temp_file)
            fail_file = temp_file.replace(".csv", "_fail.csv")
            if os.path.exists(fail_file):
                os.unlink(fail_file)


if __name__ == "__main__":
    test_product_supplierinfo_import()
