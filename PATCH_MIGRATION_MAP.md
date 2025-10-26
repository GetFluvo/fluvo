# Patch Migration Map - Fix Test Mock Locations

## Overview
This document maps the old function locations to new locations that need to be updated in test patches.

## Common Patch Migration Patterns

### 1. Relational Import Functions (🔴 MOST COMMON - ~40 failing tests)

#### Functions Moved to Strategy Modules:
```
# BEFORE (Old Location)
odoo_data_flow.lib.relational_import._resolve_related_ids
odoo_data_flow.lib.relational_import._prepare_link_dataframe
odoo_data_flow.lib.relational_import._handle_m2m_field
odoo_data_flow.lib.relational_import._has_xml_id_pattern
odoo_data_flow.lib.relational_import._derive_missing_relation_info
odoo_data_flow.lib.relational_import._query_relation_info_from_odoo
odoo_data_flow.lib.relational_import.run_direct_relational_import
odoo_data_flow.lib.relational_import.run_write_tuple_import
odoo_data_flow.lib.relational_import.run_write_o2m_tuple_import

# AFTER (New Location)
odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids
odoo_data_flow.lib.relational_import_strategies.write_tuple._prepare_link_dataframe
odoo_data_flow.lib.relational_import_strategies.write_tuple._handle_m2m_field
odoo_data_flow.lib.relational_import_strategies.write_tuple._has_xml_id_pattern
odoo_data_flow.lib.relational_import_strategies.direct._derive_missing_relation_info
odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo
odoo_data_flow.lib.relational_import_strategies.direct.run_direct_relational_import
odoo_data_flow.lib.relational_import_strategies.write_tuple.run_write_tuple_import
odoo_data_flow.lib.relational_import_strategies.write_o2m_tuple.run_write_o2m_tuple_import
```

### 2. Configuration Library Functions (🟡 MODERATE - ~10 failing tests)

#### Functions Moved to Lib Module:
```
# BEFORE (Old Location)
odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_config
odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_dict
odoo_data_flow.lib.relational_import.cache.load_id_map

# AFTER (New Location)
odoo_data_flow.lib.conf_lib.get_connection_from_config
odoo_data_flow.lib.conf_lib.get_connection_from_dict
odoo_data_flow.lib.cache.load_id_map
```

### 3. Preflight Functions (🟢 LESS COMMON - ~5 failing tests)

#### Functions Moved or Restructured:
```
# BEFORE (Old Location)
odoo_data_flow.lib.relational_import._handle_field_deferral
odoo_data_flow.lib.relational_import._should_skip_deferral

# AFTER (New Location)
odoo_data_flow.lib.preflight._handle_field_deferral
odoo_data_flow.lib.preflight._should_skip_deferral
```

## Complete Patch Migration Table

| Old Location | New Location | Module Type |
|--------------|--------------|-------------|
| `odoo_data_flow.lib.relational_import._resolve_related_ids` | `odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids` | Strategy Module |
| `odoo_data_flow.lib.relational_import._prepare_link_dataframe` | `odoo_data_flow.lib.relational_import_strategies.write_tuple._prepare_link_dataframe` | Strategy Module |
| `odoo_data_flow.lib.relational_import._handle_m2m_field` | `odoo_data_flow.lib.relational_import_strategies.write_tuple._handle_m2m_field` | Strategy Module |
| `odoo_data_flow.lib.relational_import._has_xml_id_pattern` | `odoo_data_flow.lib.relational_import_strategies.write_tuple._has_xml_id_pattern` | Strategy Module |
| `odoo_data_flow.lib.relational_import._derive_missing_relation_info` | `odoo_data_flow.lib.relational_import_strategies.direct._derive_missing_relation_info` | Strategy Module |
| `odoo_data_flow.lib.relational_import._query_relation_info_from_odoo` | `odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo` | Strategy Module |
| `odoo_data_flow.lib.relational_import.run_direct_relational_import` | `odoo_data_flow.lib.relational_import_strategies.direct.run_direct_relational_import` | Strategy Module |
| `odoo_data_flow.lib.relational_import.run_write_tuple_import` | `odoo_data_flow.lib.relational_import_strategies.write_tuple.run_write_tuple_import` | Strategy Module |
| `odoo_data_flow.lib.relational_import.run_write_o2m_tuple_import` | `odoo_data_flow.lib.relational_import_strategies.write_o2m_tuple.run_write_o2m_tuple_import` | Strategy Module |
| `odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_config` | `odoo_data_flow.lib.conf_lib.get_connection_from_config` | Lib Module |
| `odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_dict` | `odoo_data_flow.lib.conf_lib.get_connection_from_dict` | Lib Module |
| `odoo_data_flow.lib.relational_import.cache.load_id_map` | `odoo_data_flow.lib.cache.load_id_map` | Lib Module |
| `odoo_data_flow.lib.relational_import._handle_field_deferral` | `odoo_data_flow.lib.preflight._handle_field_deferral` | Preflight Module |
| `odoo_data_flow.lib.relational_import._should_skip_deferral` | `odoo_data_flow.lib.preflight._should_skip_deferral` | Preflight Module |

## Bulk Replacement Commands

### Strategy Module Functions:
```bash
# Replace relational_import functions with strategy module functions
sed -i 's/odoo_data_flow\.lib\.relational_import\._resolve_related_ids/odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\._prepare_link_dataframe/odoo_data_flow.lib.relational_import_strategies.write_tuple._prepare_link_dataframe/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\._handle_m2m_field/odoo_data_flow.lib.relational_import_strategies.write_tuple._handle_m2m_field/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\._has_xml_id_pattern/odoo_data_flow.lib.relational_import_strategies.write_tuple._has_xml_id_pattern/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\._derive_missing_relation_info/odoo_data_flow.lib.relational_import_strategies.direct._derive_missing_relation_info/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\._query_relation_info_from_odoo/odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\.run_direct_relational_import/odoo_data_flow.lib.relational_import_strategies.direct.run_direct_relational_import/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\.run_write_tuple_import/odoo_data_flow.lib.relational_import_strategies.write_tuple.run_write_tuple_import/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\.run_write_o2m_tuple_import/odoo_data_flow.lib.relational_import_strategies.write_o2m_tuple.run_write_o2m_tuple_import/g' tests/*.py
```

### Configuration Library Functions:
```bash
# Replace conf_lib functions with lib module functions
sed -i 's/odoo_data_flow\.lib\.relational_import\.conf_lib\.get_connection_from_config/odoo_data_flow.lib.conf_lib.get_connection_from_config/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\.conf_lib\.get_connection_from_dict/odoo_data_flow.lib.conf_lib.get_connection_from_dict/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\.cache\.load_id_map/odoo_data_flow.lib.cache.load_id_map/g' tests/*.py
```

### Preflight Functions:
```bash
# Replace preflight functions with lib.preflight functions
sed -i 's/odoo_data_flow\.lib\.relational_import\._handle_field_deferral/odoo_data_flow.lib.preflight._handle_field_deferral/g' tests/*.py
sed -i 's/odoo_data_flow\.lib\.relational_import\._should_skip_deferral/odoo_data_flow.lib.preflight._should_skip_deferral/g' tests/*.py
```

## Verification Commands

### Check All Patch Locations:
```bash
# Find all remaining patches pointing to old locations
grep -r "odoo_data_flow\.lib\.relational_import\." tests/ --include="*.py" | grep "@patch"
```

### Validate Patch Migrations:
```bash
# Ensure no old locations are still being patched
grep -r "odoo_data_flow\.lib\.relational_import\." tests/ --include="*.py" | wc -l
# Should return 0 after all patches are fixed
```

## Common Test Fix Patterns

### Before Fixing Test:
```python
@patch("odoo_data_flow.lib.relational_import._resolve_related_ids")
@patch("odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_config")
def test_example(mock_get_conn: MagicMock, mock_resolve_ids: MagicMock) -> None:
    # Test logic...
```

### After Fixing Test:
```python
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
@patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
def test_example(mock_resolve_ids: MagicMock, mock_get_conn: MagicMock) -> None:
    # Test logic...
```

**Note**: The order of `@patch` decorators matters! They're applied bottom-to-top, so the parameters should match the reversed order.

## Test Parameter Order Correction

### Common Fix Pattern:
When patching multiple functions, ensure parameter order matches reversed patch order:

```python
# BEFORE (incorrect order)
@patch("odoo_data_flow.lib.relational_import._resolve_related_ids")
@patch("odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_config")
def test_example(
    mock_get_conn: MagicMock,  # Wrong order!
    mock_resolve_ids: MagicMock,  # Wrong order!
) -> None:
    # ...

# AFTER (correct order)
@patch("odoo_data_flow.lib.relational_import._resolve_related_ids")
@patch("odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_config")
def test_example(
    mock_resolve_ids: MagicMock,  # Correct order (bottom patch first)
    mock_get_conn: MagicMock,     # Correct order (top patch second)
) -> None:
    # ...
```

## Error Message Sanitization Fix

If tests expect specific error message formats, check `_sanitize_error_message` function:

```python
# The function may be over-sanitizing legitimate error messages
# Look for aggressive character replacements like:
error_msg = error_msg.replace(",", ";")  # This breaks legitimate commas
error_msg = error_msg.replace(";", ":")   # This breaks legitimate semicolons
```

These should be removed or made more targeted to only sanitize actual CSV-breaking characters.

## Summary

Fixing the 59 failing tests requires:
1. **✅ Update all patch decorators** to point to new module locations (45-50 tests)
2. **✅ Fix parameter ordering** for multiple patches (10-15 tests) 
3. **✅ Update behavioral expectations** to match new flexible architecture (5-10 tests)
4. **✅ Fix error message sanitization** if over-aggressive (2-5 tests)

This should restore the full 693/693 test suite to passing status while preserving all architectural improvements.