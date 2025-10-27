# Plan to Fix Test Patching Issues

## Overview
There are 21 failing tests due to patching functions that were moved during architectural refactoring. All tests need to be updated to point to the correct module locations.

## Step 1: Identify Moved Functions

### Functions Moved During Refactoring:
1. `_resolve_related_ids` → `odoo_data_flow.lib.relational_import_strategies.direct`
2. `_handle_m2m_field` → `odoo_data_flow.lib.relational_import_strategies.write_tuple`
3. `_has_xml_id_pattern` → `odoo_data_flow.lib.relational_import_strategies.write_tuple`
4. `_create_batch_individually` → `odoo_data_flow.lib.import_threaded`
5. `_handle_create_error` → `odoo_data_flow.lib.import_threaded`
6. `cache` → `odoo_data_flow.lib.cache`
7. Various other helper functions moved to strategy modules

## Step 2: Update Test Patches

### Strategy:
1. For each failing test, identify what patches are failing
2. Update patches to point to new module locations
3. Run test to verify fix
4. Repeat for all 21 failing tests

### Example Fix:
```python
# Before (failing):
@patch("odoo_data_flow.lib.relational_import._resolve_related_ids")

# After (fixed):
@patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
```

## Step 3: Prioritized Fix Order

### High Priority Tests (blocking core functionality):
1. `tests/test_m2m_missing_relation_info.py::test_run_write_tuple_import_derives_missing_info`
2. `tests/test_m2m_missing_relation_info.py::test_run_direct_relational_import_derives_missing_info`
3. `tests/test_relational_import.py::test_run_direct_relational_import`
4. `tests/test_relational_import.py::test_run_write_tuple_import`

### Medium Priority Tests (strategy handling):
5. `tests/test_relational_import.py::test_resolve_related_ids_failure`
6. `tests/test_relational_import.py::test_resolve_related_ids_with_dict`
7. `tests/test_relational_import.py::test_resolve_related_ids_connection_error`
8. `tests/test_relational_import.py::test_run_write_o2m_tuple_import`

### Low Priority Tests (utility functions):
9-21. Various tests in `TestQueryRelationInfoFromOdoo`, `TestDeriveMissingRelationInfo`, `TestDeriveRelationInfo` classes

## Step 4: Implementation Approach

### Automated Patch Updates:
1. Create script to identify common patch patterns
2. Generate list of old→new patch mappings
3. Apply bulk updates where safe
4. Manually verify complex cases

### Manual Verification:
1. Run each test individually after patch update
2. Verify test behavior matches expectations
3. Check for any side effects or new issues

## Step 5: Validation

### Success Criteria:
- ✅ All 21 previously failing tests now pass
- ✅ All 632 previously passing tests still pass
- ✅ No new test failures introduced
- ✅ Full test suite passes (653/653)

### Rollback Plan:
If issues arise:
1. Revert patch changes for specific test
2. Investigate root cause
3. Apply corrected patch
4. Continue with remaining tests

## Timeline

### Day 1:
- Fix high priority tests (4 tests)
- Verify no regressions in existing tests

### Day 2:
- Fix medium priority tests (4 tests)
- Address any complications from strategy module patches

### Day 3:
- Fix remaining low priority tests (13 tests)
- Final validation of full test suite
- Document any lessons learned

## Risk Mitigation

### Potential Issues:
1. **Incorrect patch targets** - May cause tests to fail differently
2. **Missing imports** - New modules may need additional imports
3. **Function signature changes** - Moved functions may have different parameters
4. **Side effect changes** - Different module contexts may affect behavior

### Mitigation Strategies:
1. **Gradual rollout** - Fix tests one by one, verify each
2. **Thorough validation** - Run full test suite after each change
3. **Detailed logging** - Capture any unexpected behavior
4. **Quick rollback** - Ready to revert if issues arise
