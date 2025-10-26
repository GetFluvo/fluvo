#!/usr/bin/env python3
"""Debug test to understand what's happening."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Import the module to see if functions are available
try:
    from odoo_data_flow.lib import relational_import

    print("Import successful!")
    print(
        f"Available functions: {[attr for attr in dir(relational_import) if not attr.startswith('_')]}"
    )

    # Check if the function exists
    if hasattr(relational_import, "run_write_tuple_import"):
        print("Function 'run_write_tuple_import' exists!")
        func = relational_import.run_write_tuple_import
        print(f"Function: {func}")
    else:
        print("Function 'run_write_tuple_import' NOT FOUND!")

except Exception as e:
    print(f"Import failed: {e}")
    import traceback

    traceback.print_exc()
