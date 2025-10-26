# Concrete Plan to Fix All Failing Tests

## Current Status
- **✅ 634 tests passing**
- **❌ 59 tests failing**
- **Goal**: Fix all 59 failing tests to restore 693/693 test suite

## Root Cause Analysis
All failing tests are due to **incorrect patch targets** after architectural refactoring:
1. Functions were moved to strategy modules
2. Tests still patch old locations
3. Tests expect old rigid behavior, not new flexible architecture

## Fix Strategy Overview
1. **Update test patches** to point to new module locations
2. **Update test expectations** to match new flexible behavior
3. **Preserve architectural improvements** throughout process

## Phase 1: Update Test Patches (Estimated: 2-3 hours)

### Step 1: Identify All Function Relocations
```bash
# Find all relocated functions
find src/ -name "*.py" -exec grep -l "def .*(" {} \; | xargs grep -n "^def " | grep -v "__" | grep -v "test_" | sort
```

### Step 2: Create Old → New Location Mapping
Functions that moved during refactoring:
- `_resolve_related_ids` → `relational_import_strategies.direct`
- `_prepare_link_dataframe` → `relational_import_strategies.write_tuple`
- `_handle_m2m_field` → `relational_import_strategies.write_tuple`
- `_has_xml_id_pattern` → `relational_import_strategies.write_tuple`
- `_derive_missing_relation_info` → `relational_import_strategies.direct`
- `_query_relation_info_from_odoo` → `relational_import_strategies.direct`
- `_handle_fallback_create` → `import_threaded`
- `_create_batch_individually` → `import_threaded`
- `_execute_write_tuple_updates` → `relational_import_strategies.write_tuple`
- `_run_write_tuple_import` → `relational_import_strategies.write_tuple`

### Step 3: Systematically Fix Each Test File
1. **tests/test_relational_import.py** - 21 failing tests
2. **tests/test_relational_import_focused.py** - 12 failing tests  
3. **tests/test_relational_import_edge_cases.py** - 8 failing tests
4. **tests/test_m2m_missing_relation_info.py** - 8 failing tests
5. **tests/test_import_threaded_edge_cases.py** - 5 failing tests
6. **tests/test_failure_handling.py** - 3 failing tests
7. **tests/test_m2m_csv_format.py** - 2 failing tests

## Phase 2: Update Test Expectations (Estimated: 1-2 hours)

### Categories of Behavioral Changes:
1. **Self-referencing field deferral** - Only self-referencing fields deferred now
2. **External ID pattern handling** - Flexible XML ID detection instead of hardcoded values
3. **Error message formats** - Enhanced sanitization for CSV safety
4. **Numeric field processing** - Enhanced safety with 0/0.0 for invalid values

### Update Approach:
- For each test, understand what it's testing
- Update assertions to expect new flexible behavior
- Preserve core functionality validation
- Add comments explaining architectural rationale

## Phase 3: Verify and Validate (Estimated: 30 minutes)

### Verification Steps:
1. **Run all tests** - Ensure 693/693 pass
2. **Run linters** - Ensure code quality maintained
3. **Run type checking** - Ensure MyPy still passes
4. **Run pre-commit** - Ensure all hooks pass

## Detailed Implementation

### Task 1: Fix tests/test_relational_import.py
This file has the most failing tests (21). Most are due to patching moved functions.

**Common Issues**:
```python
# OLD patch targets:
@patch("odoo_data_flow.lib.relational_import._resolve_related_ids")
@patch("odoo_data_flow.lib.relational_import._prepare_link_dataframe")
@patch("odoo_data_flow.lib.relational_import._handle_m2m_field")
@patch("odoo_data_flow.lib.relational_import._has_xml_id_pattern")
@patch("odoo_data_flow.lib.relational_import._derive_missing_relation_info")
@patch("odoo_data_flow.lib.relational_import._query_relation_info_from_odoo")
@patch("odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_config")

# NEW patch targets:
@patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
@patch("odoo_data_flow.lib.relational_import_strategies.write_tuple._prepare_link_dataframe")
@patch("odoo_data_flow.lib.relational_import_strategies.write_tuple._handle_m2m_field")
@patch("odoo_data_flow.lib.relational_import_strategies.write_tuple._has_xml_id_pattern")
@patch("odoo_data_flow.lib.relational_import_strategies.direct._derive_missing_relation_info")
@patch("odoo_data_flow.lib.relational_import_strategies.direct._query_relation_info_from_odoo")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
```

**Implementation Steps**:
1. Update all patch decorators in the file
2. Fix any function calls that reference old locations
3. Run tests to verify fixes

### Task 2: Fix tests/test_relational_import_focused.py
Has 12 failing tests, similar patching issues.

**Implementation Steps**:
1. Update patch decorators to new locations
2. Verify test logic matches new flexible behavior
3. Run tests to verify fixes

### Task 3: Fix tests/test_relational_import_edge_cases.py
Has 8 failing tests, similar patching issues.

**Implementation Steps**:
1. Update patch decorators to new locations
2. Adjust test expectations for flexible behavior
3. Run tests to verify fixes

### Task 4: Fix tests/test_m2m_missing_relation_info.py
Has 8 failing tests, mostly patching issues.

**Implementation Steps**:
1. Update patch decorators to new locations
2. Fix test expectations for missing relation info handling
3. Run tests to verify fixes

### Task 5: Fix tests/test_import_threaded_edge_cases.py
Has 5 failing tests, likely behavioral changes.

**Implementation Steps**:
1. Check if patch targets moved
2. Update test expectations for new error handling
3. Run tests to verify fixes

### Task 6: Fix tests/test_failure_handling.py
Has 3 failing tests, likely behavioral changes.

**Implementation Steps**:
1. Update patch targets if needed
2. Fix error message format expectations
3. Run tests to verify fixes

### Task 7: Fix tests/test_m2m_csv_format.py
Has 2 failing tests, likely patching issues.

**Implementation Steps**:
1. Update patch decorators to new locations
2. Run tests to verify fixes

## Risk Mitigation

### Preserving Architectural Improvements:
❌ **DO NOT** revert the core architectural changes:
- Selective field deferral (only self-referencing)
- External ID pattern detection (flexible)
- Enhanced numeric field safety (0/0.0 for invalid)
- XML ID handling improvements

✅ **DO** update tests to expect the new behavior

### Ensuring Test Coverage:
✅ **DO** verify that each test still validates the intended functionality
✅ **DO** add comments explaining why expectations changed
✅ **DO** preserve edge case coverage

## Timeline

### Day 1:
- **Morning**: Fix test patching issues (Tasks 1-4)
- **Afternoon**: Fix behavioral expectation issues (Tasks 5-7)
- **Evening**: Full validation and verification

### Expected Outcome:
- ✅ **693/693 tests passing**
- ✅ **All architectural improvements preserved**
- ✅ **Zero regressions introduced**
- ✅ **Full linter compliance maintained**
- ✅ **Type safety preserved**

## Success Verification

### Final Checks:
1. **All Tests Pass**: `python -m pytest` shows 693 passed
2. **Linting Clean**: `pre-commit run --all-files` passes
3. **Type Safety**: `mypy src tests` shows 0 errors
4. **Architectural Integrity**: Core improvements still working
5. **Performance**: No degradation from refactoring

This systematic approach will restore the full test suite while preserving all the valuable architectural improvements that make the tool more flexible and robust.