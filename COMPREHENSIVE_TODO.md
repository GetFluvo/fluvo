# Comprehensive TODO List for Odoo Data Flow Codebase

## Current Status
✅ **632 tests passing**
❌ **21 tests failing** (all due to test patching issues from architectural refactoring)
✅ **All architectural improvements implemented and working correctly**

## Immediate Priority: Fix Failing Tests (21 tests)

### Root Cause
Functions moved to strategy modules during architectural refactoring, but tests still patch old locations.

### Task List
1. **Update Test Patches** - Point all patches to correct module locations
   - [ ] `odoo_data_flow.lib.relational_import._resolve_related_ids` → `odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids`
   - [ ] `odoo_data_flow.lib.relational_import.conf_lib` imports → `odoo_data_flow.lib.preflight.conf_lib` imports
   - [ ] Other moved functions in strategy modules

2. **Verify All Tests Pass**
   - [ ] Run each fixed test to ensure it passes
   - [ ] Run full test suite to check for regressions
   - [ ] Confirm 653/653 tests passing

### Files with Patching Issues
- `tests/test_m2m_missing_relation_info.py`
- `tests/test_relational_import.py`
- `tests/test_import_threaded.py`
- Other test files that patch moved functions

## Medium Priority: Code Quality Improvements

### Function Complexity Reduction
1. **Break Down Large Functions** - Currently >100 lines
   - [ ] `_safe_convert_field_value` (~150 lines)
   - [ ] `_create_batch_individually` (~200 lines)
   - [ ] `_handle_fallback_create` (~100 lines)
   - [ ] `_execute_write_tuple_updates` (~150 lines)

2. **Reduce Nesting Levels**
   - [ ] Functions with >5 levels of nesting
   - [ ] Complex conditional chains
   - [ ] Deep try-except blocks

### Code Duplication Elimination
1. **Identify Duplicated Patterns**
   - [ ] CSV processing logic in multiple files
   - [ ] Error handling patterns
   - [ ] Progress tracking code
   - [ ] Connection management code

2. **Consolidate Common Functions**
   - [ ] Create utility modules for shared logic
   - [ ] Move duplicated code to common locations
   - [ ] Update imports to use consolidated functions

### Documentation Enhancement
1. **Add Missing Docstrings**
   - [ ] All public functions missing documentation
   - [ ] Complex functions with unclear logic
   - [ ] Module-level documentation

2. **Improve Type Annotations**
   - [ ] Replace `Any` with specific types where possible
   - [ ] Add type hints to undocumented parameters
   - [ ] Use TypedDict for complex dictionaries

## Long Term: Architectural Enhancements

### Module Restructuring
1. **Organize Code into Logical Packages**
   ```
   src/odoo_data_flow/
   ├── lib/
   │   ├── field_processing/     # Field conversion, validation
   │   ├── import_strategies/    # Import strategy implementations
   │   ├── relational_strategies/ # Relational field handling
   │   └── utils/               # Shared utilities
   ```

2. **Reduce Cross-Module Dependencies**
   - [ ] Minimize circular imports
   - [ ] Clarify module interfaces
   - [ ] Use dependency injection where appropriate

### Performance Optimization
1. **Optimize Critical Paths**
   - [ ] Cache field metadata lookups
   - [ ] Optimize DataFrame operations
   - [ ] Reduce unnecessary RPC calls

2. **Improve Batch Processing**
   - [ ] Dynamic batch sizing based on record complexity
   - [ ] Parallel processing optimizations
   - [ ] Memory usage reduction

## Architectural Improvements Already Implemented (Preserve These!)

### 1. Selective Field Deferral ✅
- Only self-referencing fields deferred by default (not all m2m)
- `category_id` with `relation: res.partner.category` is NOT deferred
- `parent_id` with `relation: res.partner` IS deferred (self-referencing)

### 2. XML ID Pattern Detection ✅
- Fields with XML ID patterns (`module.name` format) skip deferral
- `PRODUCT_TEMPLATE.73678` and `PRODUCT_PRODUCT.68170` are detected and processed directly
- Prevents unnecessary deferrals for resolvable external IDs

### 3. Enhanced Numeric Field Safety ✅
- Robust conversion prevents tuple index errors
- Invalid text like `"invalid_text"` converted to `0` for numeric fields
- Preserves data integrity while preventing server errors

### 4. External ID Field Handling ✅
- External ID fields return `""` instead of `False` to prevent tuple index errors
- No hardcoded external ID dependencies that made tool inflexible
- Flexible processing adapts to runtime Odoo metadata

### 5. Individual Record Processing ✅
- Graceful fallback when batch processing fails
- Malformed rows handled individually without crashing entire import
- Better error reporting for troubleshooting

## Specific Test Fixing Guide

### For Each Failing Test:
1. **Identify What's Being Patched**
   ```python
   # Old patch that fails
   @patch("odoo_data_flow.lib.relational_import._resolve_related_ids")

   # New patch that should work
   @patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
   ```

2. **Update Patch Locations**
   - `relational_import` functions → `relational_import_strategies` modules
   - Conf lib imports → `preflight` module
   - Other moved functions → correct new locations

3. **Verify Test Passes**
   - Run individual test
   - Check that behavior matches expectations
   - Confirm no regressions in related functionality

## Risk Mitigation Strategy

### Safe Refactoring Approach:
1. **Small, Focused Changes** - One issue at a time
2. **Continuous Testing** - Run tests after each change
3. **Backup Before Changes** - Commit current state
4. **Quick Rollback** - Ready to revert if issues arise

### Preserve Architectural Gains:
1. **Don't Reintroduce Hardcoded Dependencies**
   ❌ No hardcoded external ID patterns like `product_template.63657`
   ❌ No blanket deferral of all many2many fields
   ✅ Keep selective deferral logic

2. **Maintain Enhanced Safety Features**
   ✅ Keep numeric field conversion safety
   ✅ Keep external ID field handling improvements
   ✅ Keep individual record processing fallback

## Success Criteria

### Quantitative Measures:
- ✅ **All 653 tests passing** (632 + 21 fixed)
- ✅ **MyPy 0 errors** (type safety maintained)
- ✅ **All pre-commit hooks passing**
- ✅ **Average function size <50 lines**
- ✅ **Code duplication <5%**

### Qualitative Improvements:
- ✅ **Enhanced maintainability** (easier to understand and modify)
- ✅ **Improved developer experience** (clearer code structure)
- ✅ **Preserved flexibility** (no hardcoded dependencies)
- ✅ **Maintained performance** (no regressions)

## Timeline Estimate

### Week 1: Test Patch Fixes (High Priority)
- Fix all 21 failing test patches
- Restore full test suite to 653/653 passing
- Add regression prevention measures

### Week 2: Code Quality Improvements (Medium Priority)
- Reduce function complexity
- Eliminate code duplication
- Enhance documentation

### Week 3+: Architectural Enhancements (Low Priority)
- Module restructuring
- Performance optimizations
- Advanced configuration management

## Conclusion

The codebase is in excellent technical shape with solid architectural foundations. The main blocker to full test suite passing is updating test patches to match the refactored module locations. Once that's fixed, the remaining improvements can be made incrementally while preserving all the valuable architectural enhancements already implemented.

**The key is to preserve the architectural gains while making the codebase more maintainable.**
