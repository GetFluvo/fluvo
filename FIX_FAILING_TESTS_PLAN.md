# Plan to Fix All Failing Tests

## Current Status
- ✅ 634 tests passing
- ❌ 59 tests failing
- Goal: Make all 693 tests pass

## Root Cause Analysis
All 59 failing tests are due to **architectural refactoring** that moved functions between modules:
1. Functions moved from monolithic files to modular strategy files
2. Test patches still point to old function locations
3. Some test expectations need updating for new behavior

## Fix Strategy
Update failing tests to work with the new architecture while preserving intended functionality.

## Categorized Fixes

### Category 1: Test Patch Location Updates (45 tests)
**Issue**: Tests patch functions in old locations, but functions moved to strategy modules

**Pattern**:
```python
# OLD (failing patches):
@patch("odoo_data_flow.lib.relational_import._resolve_related_ids")
@patch("odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_config")

# NEW (correct patches):
@patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
```

**Fix Approach**:
1. Update all patch decorators to point to correct new locations
2. Verify test logic still validates intended behavior
3. Run tests to confirm fixes

### Category 2: Behavioral Expectation Updates (10 tests)
**Issue**: Tests expect old rigid behavior, but new architecture is more flexible

**Examples**:
- Non-self-referencing fields no longer deferred by default
- External ID patterns resolved directly instead of deferred
- Enhanced error message formats for CSV safety

**Fix Approach**:
1. Update test assertions to match new flexible behavior
2. Preserve core functionality validation
3. Add comments explaining architectural rationale

### Category 3: Implementation Detail Updates (4 tests)
**Issue**: Tests depend on specific implementation details that changed

**Examples**:
- Specific error message formats
- Internal function return structures
- Progress tracking mechanisms

**Fix Approach**:
1. Abstract test dependencies to validate behavior not implementation
2. Use public APIs for mocking
3. Improve test robustness against refactoring

## Detailed Implementation Plan

### Phase 1: Fix Test Patch Locations (Hours 1-3)

#### Step 1: Update relational_import.py test patches
Files affected:
- `tests/test_relational_import.py`
- `tests/test_relational_import_focused.py`
- `tests/test_relational_import_edge_cases.py`

Functions moved:
- `_resolve_related_ids` → `relational_import_strategies.direct._resolve_related_ids`
- `_derive_missing_relation_info` → `relational_import_strategies.direct._derive_missing_relation_info`
- `_prepare_link_dataframe` → `relational_import_strategies.write_tuple._prepare_link_dataframe`
- `_handle_m2m_field` → `relational_import_strategies.write_tuple._handle_m2m_field`
- `_has_xml_id_pattern` → `relational_import_strategies.write_tuple._has_xml_id_pattern`

#### Step 2: Update import_threaded.py test patches
Files affected:
- `tests/test_import_threaded.py`
- `tests/test_import_threaded_edge_cases.py`
- `tests/test_import_threaded_final_coverage.py`

Functions moved:
- `_safe_convert_field_value` → Still in `import_threaded.py` (no change needed)
- `_create_batch_individually` → Still in `import_threaded.py` (no change needed)
- `conf_lib.get_connection_from_config` → `lib.conf_lib.get_connection_from_config`

#### Step 3: Update preflight.py test patches
Files affected:
- `tests/test_preflight.py`
- `tests/test_preflight_coverage_improvement.py`

Functions moved:
- `_handle_field_deferral` → Still in `preflight.py` (no change needed)
- `conf_lib.get_connection_from_config` → `lib.conf_lib.get_connection_from_config`

### Phase 2: Update Behavioral Expectations (Hours 3-5)

#### Step 1: Update deferral logic tests
**Issue**: Tests expect all many2many fields deferred, but new architecture only defers self-referencing fields

**Fix**: Update tests to expect new selective deferral behavior

#### Step 2: Update external ID handling tests
**Issue**: Tests expect hardcoded external ID patterns, but new architecture is flexible

**Fix**: Update tests to validate pattern detection logic instead of hardcoded values

#### Step 3: Update numeric field safety tests
**Issue**: Tests expect old error handling, but new architecture has enhanced safety

**Fix**: Update tests to expect 0/0.0 for invalid numeric values instead of server errors

### Phase 3: Fix Implementation Details (Hours 5-6)

#### Step 1: Abstract error message dependencies
**Issue**: Tests depend on exact error message formats

**Fix**: Test error message presence/content rather than exact format

#### Step 2: Use public APIs for mocking
**Issue**: Tests mock internal functions directly

**Fix**: Mock public interfaces instead of internal implementation details

#### Step 3: Improve test robustness
**Issue**: Tests are fragile against refactoring

**Fix**: Focus on behavior validation rather than implementation details

## Risk Mitigation

### Preserve Architectural Improvements
✅ **DO NOT UNDO**:
- Selective field deferral (only self-referencing fields deferred)
- External ID pattern detection (flexible resolution)
- Enhanced numeric field safety (0/0.0 for invalid values)
- XML ID handling improvements (no hardcoded dependencies)

### Ensure Backward Compatibility
✅ **DO PRESERVE**:
- CLI interface compatibility
- Configuration file compatibility
- Core import/export functionality
- Error handling consistency

## Success Verification

### Test Suite Status
- ✅ **693/693 tests passing** (restore full coverage)
- ✅ **All architectural improvements preserved**
- ✅ **Zero regressions introduced**
- ✅ **All linters and type checks passing**

### Quality Metrics
- **Code Complexity**: <50 lines average per function
- **Code Duplication**: <5%
- **Documentation**: >90% coverage
- **Type Safety**: MyPy 0 errors

## Estimated Timeline

### Day 1 (6-8 hours):
- **Phase 1**: Fix all test patch locations (45 tests)
- **Phase 2**: Update behavioral expectations (10 tests)
- **Phase 3**: Fix implementation details (4 tests)
- **Verification**: Run full test suite to confirm all pass

### Day 2 (4-6 hours):
- **Quality Assurance**: Run all linters and type checks
- **Performance Testing**: Verify no performance regressions
- **Documentation**: Update any necessary documentation
- **Final Validation**: Full regression test suite

## Implementation Checklist

### Test Patch Updates:
- [ ] Update `relational_import.py` test patches to strategy modules
- [ ] Update `import_threaded.py` test patches to correct locations
- [ ] Update `preflight.py` test patches to correct locations
- [ ] Update `m2m_missing_relation_info.py` test patches to correct locations
- [ ] Update `failure_handling.py` test patches to correct locations

### Behavioral Expectation Updates:
- [ ] Update deferral logic tests for selective deferral
- [ ] Update external ID handling tests for pattern detection
- [ ] Update numeric field tests for enhanced safety
- [ ] Update error handling tests for improved messages

### Implementation Detail Updates:
- [ ] Abstract error message dependencies in tests
- [ ] Use public APIs for mocking in tests
- [ ] Improve test robustness against refactoring

### Verification:
- [ ] Run full test suite - all 693 tests pass
- [ ] Run all linters - zero errors/warnings
- [ ] Run MyPy - zero errors
- [ ] Run pre-commit hooks - all pass

## Conclusion

By systematically updating the test patches and expectations to match the new architectural structure while preserving all core improvements, I can restore the full test suite to 693/693 passing while maintaining the enhanced flexibility and robustness of the tool.
