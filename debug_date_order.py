#!/usr/bin/env python3
"""Debug script to check what data Odoo returns for sale.order date_order field."""

import sys

sys.path.insert(0, "/home/bosd/git/odoo-data-flow/src")

from odoo_data_flow.lib import conf_lib

# Load connection
connection = conf_lib.get_connection_from_config(
    "/home/bosd/doodba/sps_12_18_so/conf/source_12_prod.conf"
)
connection.check_login()

# Get sale.order model
sale_order = connection.get_model("sale.order")

# Get field metadata for date_order
fields_info = sale_order.fields_get(["date_order"])
print("=== Field Metadata ===")
print(f"date_order field info: {fields_info}")
print()

# Search for some sale orders
ids = sale_order.search([("state", "!=", "cancel")], limit=5)
print(f"=== Found {len(ids)} sale orders ===")
print(f"IDs: {ids[:5]}")
print()

# Read the date_order field
if ids:
    records = sale_order.read(ids[:5], ["id", "name", "date_order", "company_id"])
    print("=== Raw Data from Odoo (using read()) ===")
    for record in records:
        print(f"ID: {record.get('id')}")
        print(f"  name: {record.get('name')}")
        print(
            f"  date_order: {record.get('date_order')} (type: {type(record.get('date_order'))})"
        )
        print(f"  company_id: {record.get('company_id')}")
        print()
