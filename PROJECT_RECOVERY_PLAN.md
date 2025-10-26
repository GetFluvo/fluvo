# Project Recovery Plan - Restore Stability

## Current Status
- ❌ **51 failing tests** (regression from 693/693 to 642/693)
- ❌ **Broken nox sessions** (linting, type checking, etc.)
- ❌ **Critical import issues** (500 valid records incorrectly flagged as failed)
- ❌ **Unstable codebase** preventing further development

## Priority Recovery Points

### 🔴 **CRITICAL (Must Fix Immediately)**
1. **Restore Test Suite** - Fix all 51 failing tests to get back to 693/693 passing
2. **Fix Critical Import Bug** - Resolve why 500 valid records are incorrectly failing
3. **Restore Nox Sessions** - Get all linting, type checking, and CI sessions working

### 🟡 **HIGH (Should Fix Next)**
4. **Architecture Preservation** - Maintain flexible external ID handling improvements
5. **Code Quality Maintenance** - Keep MYPY, Ruff, and other linters passing
6. **Documentation Updates** - Ensure all changes are properly documented

### 🟢 **MEDIUM (Nice to Have Later)**
7. **Further Refactoring** - Simplify complex functions and reduce duplication
8. **Performance Optimization** - Fine-tune critical paths
9. **Enhanced Error Handling** - Improve user feedback and debugging

## Detailed Recovery Steps

### Phase 1: Restore Test Suite Stability (⏰ 2-4 hours)

#### Step 1: Identify Root Cause of Test Failures
```
❌ 51 failing tests due to patching functions that were moved during refactoring
✅ Fix: Update test patches to point to new module locations
```

#### Step 2: Update Test Patches Systematically
All failing tests patch functions that moved during architectural refactoring:
- `_resolve_related_ids` → moved from `relational_import` to `relational_import_strategies.direct`
- `_prepare_link_dataframe` → moved from `relational_import` to `relational_import_strategies.write_tuple`
- `_handle_m2m_field` → moved from `relational_import` to `relational_import_strategies.write_tuple`
- Other functions similarly relocated

**Fix Implementation:**
1. For each failing test, identify what function it's trying to patch
2. Update patch decorator to point to new location
3. Verify test passes after patch update

#### Step 3: Fix Behavioral Test Expectations
Some tests expect old rigid behavior, but new architecture is more flexible:
- Non-self-referencing m2m fields no longer deferred by default
- External ID pattern detection instead of hardcoded dependencies
- Enhanced numeric field safety (0/0.0 for invalid values)

**Fix Implementation:**
1. Update test assertions to match new flexible behavior
2. Preserve core functionality while adapting to architectural improvements
3. Add comments explaining architectural rationale

### Phase 2: Fix Critical Import Bug (⏰ 3-6 hours)

#### Step 1: Reproduce the Issue
```
❌ 500 valid records incorrectly flagged as failed
✅ Debug: Trace exactly why valid records go to _fail.csv
```

#### Step 2: Identify Root Cause
Common causes:
- Over-aggressive error sanitization converting valid data to empty strings
- Incorrect field validation rejecting valid values
- Premature deferral of non-self-referencing fields
- External ID pattern detection falsely flagging valid records

#### Step 3: Implement Targeted Fix
1. Analyze `_failed` file generation logic
2. Identify where valid records are incorrectly flagged
3. Fix specific validation logic without breaking safety features
4. Test with actual data to verify fix

### Phase 3: Restore Nox Sessions (⏰ 1-2 hours)

#### Step 1: Fix Linting Issues
```bash
# Run pre-commit to identify issues
pre-commit run --all-files

# Common fixes:
# - Line length violations (E501)
# - Missing docstrings (D100-D107)
# - Import order issues (I001)
# - Unused imports/variables (F401, F841)
```

#### Step 2: Fix Type Checking Issues
```bash
# Run MyPy to identify type issues
mypy src tests docs/conf.py --python-executable=/usr/bin/python

# Common fixes:
# - Missing type annotations
# - Incompatible types
# - Import-related type errors
```

#### Step 3: Fix Formatting Issues
```bash
# Run Ruff format to fix code style
ruff format src tests

# Run Ruff check to fix remaining issues
ruff check src tests --fix
```

## Specific Implementation Instructions

### Task 1: Fix Test Patches (🔴 CRITICAL)

#### File: `tests/test_relational_import.py`
**Issues:**
- Patching `odoo_data_flow.lib.relational_import._resolve_related_ids` (function moved)
- Patching `odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_config` (module moved)

**Fixes:**
```python
# BEFORE (broken):
@patch("odoo_data_flow.lib.relational_import._resolve_related_ids")
@patch("odoo_data_flow.lib.relational_import.conf_lib.get_connection_from_config")

# AFTER (fixed):
@patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
```

#### File: `tests/test_relational_import_edge_cases.py`
**Same pattern** - update all patch decorators to point to new module locations.

#### File: `tests/test_relational_import_focused.py`  
**Same pattern** - update all patch decorators.

### Task 2: Fix Behavioral Test Expectations (🔴 CRITICAL)

#### Update Test Logic for New Architecture:
```python
# BEFORE (expects old behavior):
assert "category_id" in import_plan["deferred_fields"]

# AFTER (expects new flexible behavior):
# According to the new architecture, only self-referencing fields are deferred
# Since category_id is not self-referencing (relation: res.partner.category vs model: res.partner),
# it should NOT be in deferred_fields
if "deferred_fields" in import_plan:
    assert "category_id" not in import_plan["deferred_fields"]
# But strategies should still be calculated for proper import handling
assert "category_id" in import_plan.get("strategies", {})
```

### Task 3: Fix Critical Import Bug (🔴 CRITICAL)

#### Debug Steps:
1. **Trace `_failed` file generation** - Find where records are written to fail files
2. **Identify false positives** - See what valid records are incorrectly flagged
3. **Check field validation logic** - Ensure valid data isn't rejected
4. **Verify deferral decisions** - Confirm only truly self-referencing fields are deferred

#### File: `src/odoo_data_flow/import_threaded.py`
**Check functions:**
- `_safe_convert_field_value` - Ensure it doesn't over-sanitize valid data
- `_handle_field_deferral` - Confirm selective deferral logic is correct
- `_prepare_batch_data` - Verify batch preparation doesn't reject valid records
- `_handle_fallback_create` - Ensure fallback doesn't incorrectly flag records

### Task 4: Restore Nox Sessions (🔴 CRITICAL)

#### Run All Checks:
```bash
# 1. Pre-commit hooks
pre-commit run --all-files

# 2. MyPy type checking
mypy src tests docs/conf.py --python-executable=/usr/bin/python

# 3. Ruff linting
ruff check src tests

# 4. Ruff formatting  
ruff format src tests

# 5. Pydoclint
pydoclint src tests

# 6. Full test suite
PYTHONPATH=src python -m pytest --tb=no -q
```

#### Fix Issues as Found:
1. **Address all linting errors** systematically
2. **Resolve type checking issues** without breaking functionality
3. **Fix formatting violations** with automated tools
4. **Update documentation** to meet style requirements

## Risk Mitigation Strategy

### Preserve Core Architectural Improvements
✅ **DO NOT UNDO:**
- Selective field deferral (only self-referencing fields deferred)
- External ID flexibility (no hardcoded dependencies)
- Enhanced numeric safety (0/0.0 for invalid values)
- XML ID pattern detection (direct resolution)

❌ **AVOID:**
- Reverting to old rigid behavior that made tool inflexible
- Reintroducing hardcoded external ID dependencies
- Removing safety features that prevent server errors

### Ensure Backward Compatibility
✅ **DO MAINTAIN:**
- CLI interface compatibility
- Configuration file compatibility
- Core import/export functionality
- Existing user workflows

### Test-Driven Recovery Approach
✅ **VERIFY EACH CHANGE:**
1. Run specific failing test before change
2. Apply targeted fix
3. Run same test after change to verify fix
4. Run full test suite to check for regressions
5. Run linters to ensure code quality maintained

## Success Verification

### Immediate Goals (Within 24 hours):
- ✅ **693/693 tests passing** (restore full test suite)
- ✅ **All nox sessions working** (linting, type checking, CI)
- ✅ **Critical import bug fixed** (500 records no longer incorrectly failing)
- ✅ **All architectural improvements preserved**

### Quality Metrics to Monitor:
- **Test Pass Rate**: 100% (693/693)
- **Linting Score**: 0 errors/warnings
- **Type Safety**: MyPy 0 errors
- **Code Complexity**: <50 lines average per function
- **Code Duplication**: <5%

### Verification Commands:
```bash
# Test suite
PYTHONPATH=src python -m pytest --tb=no -q | tail -3

# Linting
pre-commit run --all-files

# Type checking
mypy src tests docs/conf.py --python-executable=/usr/bin/python

# Code quality
ruff check src tests
ruff format --check src tests
```

## Timeline and Milestones

### Day 1 (8-12 hours):
- **Hours 1-4**: Fix all test patching issues (51 failing → 0 failing)
- **Hours 5-8**: Fix critical import bug (500 records incorrectly failing)
- **Hours 9-12**: Restore all nox sessions (pre-commit, mypy, ruff, etc.)

### Day 2 (4-6 hours):
- **Hours 1-2**: Run comprehensive validation (full test suite + all linters)
- **Hours 3-4**: Address any remaining edge cases or regressions
- **Hours 5-6**: Document changes and create final summary

## Emergency Recovery Option

If systematic fixes prove too time-consuming, emergency recovery:

### Fast Recovery Steps:
1. **Revert to stable commit** `706af79` (693/693 tests passing)
2. **Reapply architectural improvements** selectively without breaking tests
3. **Update tests incrementally** to match new behavior
4. **Verify stability at each step** to prevent regressions

### Command Sequence:
```bash
# 1. Revert to known stable state
git reset --hard 706af79

# 2. Reapply architectural improvements one by one
# 3. Update corresponding tests after each change
# 4. Verify stability continuously
```

## Conclusion

The project recovery requires a focused, systematic approach prioritizing:
1. **Restore test suite stability** (51 failing tests)
2. **Fix critical import functionality** (500 valid records failing)
3. **Restore development environment** (broken nox sessions)

The architectural improvements should be preserved throughout the recovery process to maintain the enhanced flexibility and safety features that were implemented.

This recovery plan provides clear, actionable steps that can be followed by any implementing agent to restore the project to a stable, production-ready state.