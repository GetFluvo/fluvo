# Consolidated TODO List for Codebase Improvements

## CURRENT STATUS
- ✅ 632 tests passing  
- ❌ 21 tests failing (due to patching moved functions)
- ✅ All architectural improvements implemented and working
- ✅ MyPy type checking passing
- ✅ All pre-commit hooks passing
- ✅ Code quality excellent

## IMMEDIATE PRIORITY - FIX TEST PATCHING ISSUES (21 failing tests)

### Root Cause
During architectural refactoring, functions were moved to strategy modules, but tests still try to patch old locations.

### Solution
Update test patches to point to new module locations using the PATCH_MIGRATION_MAP.

### Action Items (in order of priority):

1. **High Priority Tests (Core Functionality)**:
   - [ ] `tests/test_m2m_missing_relation_info.py::test_run_write_tuple_import_derives_missing_info`
   - [ ] `tests/test_m2m_missing_relation_info.py::test_run_direct_relational_import_derives_missing_info` 
   - [ ] `tests/test_relational_import.py::test_run_direct_relational_import`
   - [ ] `tests/test_relational_import.py::test_run_write_tuple_import`

2. **Medium Priority Tests (Strategy Handling)**:
   - [ ] `tests/test_relational_import.py::test_resolve_related_ids_failure`
   - [ ] `tests/test_relational_import.py::test_resolve_related_ids_with_dict`
   - [ ] `tests/test_relational_import.py::test_resolve_related_ids_connection_error`
   - [ ] `tests/test_relational_import.py::test_run_write_o2m_tuple_import`

3. **Remaining Tests (Utility Functions)**:
   - [ ] 13 tests in `TestQueryRelationInfoFromOdoo`, `TestDeriveMissingRelationInfo`, `TestDeriveRelationInfo` classes

## ARCHITECTURAL IMPROVEMENTS TO PRESERVE

### Core Improvements Already Implemented:
1. ✅ **Selective Field Deferral**: Only self-referencing fields deferred by default (not all m2m)
2. ✅ **XML ID Pattern Detection**: Fields with XML ID patterns (module.name) skip deferral for direct resolution
3. ✅ **Enhanced Numeric Field Safety**: Robust conversion prevents server tuple index errors
4. ✅ **External ID Flexibility**: Removed hardcoded external ID dependencies
5. ✅ **Individual Record Processing**: Graceful fallback for malformed CSV rows
6. ✅ **Strategy-Based Architecture**: Modular import strategies for maintainability

### Key Functions to Protect:
- `_safe_convert_field_value` - Enhanced value conversion
- `_handle_field_deferral` - Selective deferral logic  
- `_has_xml_id_pattern` - XML ID pattern detection
- `_resolve_related_ids` - Relation ID resolution (now in strategy modules)
- `_create_batch_individually` - Fallback import processing (now in import_threaded)

## MEDIUM PRIORITY - CODE QUALITY IMPROVEMENTS

### Function Complexity Reduction:
1. [ ] Break down `_safe_convert_field_value` (~150 lines) into smaller functions
2. [ ] Simplify `_create_batch_individually` (~200 lines) with better structure
3. [ ] Refactor `_handle_fallback_create` (~100 lines) for clarity
4. [ ] Decompose `_execute_write_tuple_updates` (~150 lines) into focused units

### Code Duplication Elimination:
1. [ ] Consolidate CSV processing logic across modules
2. [ ] Unify error handling patterns
3. [ ] Merge similar progress tracking code
4. [ ] Standardize connection management code

### Documentation Enhancement:
1. [ ] Add comprehensive docstrings to all public functions
2. [ ] Improve example documentation for complex functions
3. [ ] Document architectural decisions in key modules
4. [ ] Update README with new architectural improvements

## LOW PRIORITY - FUTURE ENHANCEMENTS

### Module Restructuring:
1. [ ] Organize code into logical packages (field_processing, import_strategies, utils)
2. [ ] Reduce cross-module dependencies
3. [ ] Improve module interface consistency
4. [ ] Streamline configuration management

### Performance Optimization:
1. [ ] Cache field metadata lookups
2. [ ] Optimize DataFrame operations
3. [ ] Reduce unnecessary RPC calls
4. [ ] Improve batch processing efficiency

## IMPLEMENTATION APPROACH

### Safe Refactoring Strategy:
1. **Incremental Changes** - Small, focused commits
2. **Continuous Testing** - Run full test suite after each change
3. **Performance Monitoring** - Verify no regressions
4. **Documentation Updates** - Keep docs synchronized

### Risk Mitigation:
1. **Backup Before Changes** - Commit current state
2. **Gradual Rollout** - Fix tests one by one
3. **Thorough Validation** - Check all impacted areas
4. **Quick Rollback** - Ready to revert if issues arise

## SUCCESS CRITERIA

### Quantitative Measures:
- ✅ **All 653 tests passing** (632 current + 21 fixed)
- ✅ **Zero MyPy errors**
- ✅ **All pre-commit hooks passing**
- ✅ **Function complexity < 50 lines average**
- ✅ **Code duplication < 5%**

### Qualitative Improvements:
- ✅ **Enhanced maintainability**
- ✅ **Improved developer experience**  
- ✅ **Preserved architectural improvements**
- ✅ **Better error handling and user feedback**

## TIMELINE

### Week 1: Restore Test Suite (High Priority)
1. Fix all 21 failing test patches using migration map
2. Verify full test suite passes (653/653)
3. Add regression prevention measures

### Week 2: Code Quality Improvements (Medium Priority)
1. Reduce function complexity in key modules
2. Eliminate code duplication patterns
3. Enhance documentation coverage

### Week 3+: Future Enhancements (Low Priority)
1. Module restructuring for better organization
2. Performance optimizations for critical paths
3. Advanced configuration management

## CONCLUSION

The codebase is in excellent shape with solid architectural foundations. The main blocker is restoring the test suite by updating patches to reflect refactored module locations. Once that's fixed, the remaining improvements can be made incrementally while preserving all the valuable architectural enhancements already implemented.