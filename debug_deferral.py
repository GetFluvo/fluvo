#!/usr/bin/env python3
"""Debug script to check deferral logic for supplierinfo."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from odoo_data_flow.lib.preflight import _should_skip_deferral

# Test the deferral logic for supplierinfo partner_id field
model = "product.supplierinfo"
field_name = "partner_id"

# Check if it should be skipped for deferral
should_skip = _should_skip_deferral(model, field_name)
print(f"_should_skip_deferral('{model}', '{field_name}') = {should_skip}")

# The issue might be in the self-referencing detection logic
# For supplierinfo.partner_id, the relation should be "res.partner"
# So it should NOT be self-referencing since "res.partner" != "product.supplierinfo"
relation = "res.partner"  # This is what the field relation should be
is_self_referencing = relation == model
print(f"relation = '{relation}'")
print(f"model = '{model}'")
print(f"is_self_referencing = {is_self_referencing}")
print(f"Should be deferred: {is_self_referencing and not should_skip}")

# Check what the actual deferral check would be
field_type = "many2one"
should_be_deferred = field_type == "many2one" and is_self_referencing
print(f"field_type = '{field_type}'")
print(f"Should be deferred (many2one + self-referencing): {should_be_deferred}")
