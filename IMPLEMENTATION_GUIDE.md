# Implementation Guide - Fix Project Stability Issues

## Overview
This guide provides step-by-step instructions to restore the Odoo Data Flow project to full stability with all tests passing and all nox sessions working.

## Current Status
- **✅ 634 tests passing**
- **❌ 59 tests failing**
- **❌ Nox sessions failing**
- **❌ Critical import bug** (500 valid records incorrectly failing)

## Phase 1: Restore Test Suite (2-4 hours)

### Step 1: Identify All Failing Tests
```bash
cd /home/bosd/git/odoo-data-flow
PYTHONPATH=src python -m pytest --tb=no -q | grep FAILED | wc -l
# Should show 59 failing tests
```

### Step 2: Categorize Failing Tests
Most failing tests fall into these categories:
1. **Patch location issues** (functions moved to new modules)
2. **Behavioral expectation issues** (tests expect old rigid behavior)
3. **Implementation detail issues** (tests depend on internal structure)

### Step 3: Fix Patch Location Issues

#### Pattern: Update Mock Patches
Many tests mock functions that were moved during refactoring.

**Common Issue:**
```python
# BROKEN - Function moved to new location
@patch("odoo_data_flow.lib.relational_import._resolve_related_ids")
```

**Fix:**
```python
# CORRECT - Point to new location
@patch("odoo_data_flow.lib.relational_import_strategies.direct._resolve_related_ids")
```

#### Files to Fix:
1. `tests/test_relational_import.py` (16 failing tests)
2. `tests/test_relational_import_edge_cases.py` (25 failing tests)
3. `tests/test_relational_import_focused.py` (6 failing tests)
4. `tests/test_m2m_missing_relation_info.py` (8 failing tests)
5. `tests/test_failure_handling.py` (4 failing tests)

#### Specific Fixes:
1. **Update `relational_import` patches** → `relational_import_strategies.direct`
2. **Update `conf_lib` patches** → `lib.conf_lib` (moved from `lib.relational_import.conf_lib`)
3. **Update `cache` patches** → `lib.cache` (moved from `lib.relational_import.cache`)

### Step 4: Fix Behavioral Expectations

#### Pattern: Adapt Tests to New Flexible Architecture
The new architecture is more flexible than the old one:
- **Old**: All many2many fields deferred by default
- **New**: Only self-referencing fields deferred by default

**Example Fix:**
```python
# OLD TEST EXPECTATION (broken)
assert "category_id" in import_plan["deferred_fields"]

# NEW TEST EXPECTATION (fixed)
# category_id relates to res.partner.category, not res.partner (not self-referencing)
# So it should NOT be in deferred_fields according to new architecture
if "deferred_fields" in import_plan:
    assert "category_id" not in import_plan["deferred_fields"]
# But strategies should still be calculated for proper import handling
assert "category_id" in import_plan.get("strategies", {})
```

## Phase 2: Fix Critical Import Bug (3-6 hours)

### Step 1: Reproduce the Issue
The bug manifests as 500 valid records being incorrectly written to `_fail.csv` files.

### Step 2: Debug the Root Cause
The issue is likely in one of these areas:

#### Area 1: Field Value Conversion
```python
# Check _safe_convert_field_value function in import_threaded.py
# Ensure it's not over-sanitizing valid data
```

#### Area 2: Deferral Logic
```python
# Check if non-self-referencing fields are incorrectly deferred
# Look at _handle_field_deferral in preflight.py
```

#### Area 3: Error Message Sanitization
```python
# Check _sanitize_error_message in import_threaded.py
# Ensure it's not corrupting valid error messages
```

### Step 3: Trace the Fail File Generation
Find where records are written to `_fail.csv`:
```bash
grep -r "_fail.*csv\|fail.*csv" src/ --include="*.py" | head -10
```

### Step 4: Fix the Specific Logic
Once root cause is identified, apply targeted fix that:
- ✅ Preserves architectural improvements
- ✅ Fixes the false positive failures
- ✅ Maintains data safety features

## Phase 3: Restore Nox Sessions (1-2 hours)

### Step 1: Run Pre-commit Hooks
```bash
cd /home/bosd/git/odoo-data-flow
pre-commit run --all-files
```

Fix any issues highlighted:
- **Line length violations** - Break long lines or increase limit
- **Import order issues** - Use `ruff check --fix`
- **Missing docstrings** - Add appropriate documentation
- **Unused imports/variables** - Remove dead code

### Step 2: Run MyPy Type Checking
```bash
cd /home/bosd/git/odoo-data-flow
mypy src tests docs/conf.py --python-executable=/usr/bin/python
```

Fix any type errors:
- **Missing type annotations** - Add appropriate hints
- **Incompatible types** - Adjust function signatures
- **Import-related issues** - Fix module imports

### Step 3: Run Ruff Linting
```bash
cd /home/bosd/git/odoo-data-flow
ruff check src tests
ruff format src tests
```

Fix formatting and style issues automatically where possible.

## Detailed Task Breakdown

### Task 1: Fix Test Patch Locations (🔴 CRITICAL)

#### Subtasks:
1. **Update `relational_import` patches** in all test files
2. **Update `conf_lib` patches** to point to `lib.conf_lib`
3. **Update `cache` patches** to point to `lib.cache`
4. **Update strategy function patches** to point to correct modules

#### Priority Files:
1. `tests/test_relational_import.py` - 16 failing tests
2. `tests/test_relational_import_edge_cases.py` - 25 failing tests
3. `tests/test_relational_import_focused.py` - 6 failing tests
4. `tests/test_m2m_missing_relation_info.py` - 8 failing tests
5. `tests/test_failure_handling.py` - 4 failing tests

### Task 2: Fix Behavioral Test Expectations (🔴 CRITICAL)

#### Subtasks:
1. **Update deferral logic expectations** - Only self-referencing fields deferred
2. **Update external ID handling expectations** - Flexible pattern detection
3. **Update numeric field expectations** - Enhanced safety features

### Task 3: Fix Critical Import Bug (🔴 CRITICAL)

#### Subtasks:
1. **Reproduce 500-record failure** with sample data
2. **Trace record processing flow** to identify failure point
3. **Apply targeted fix** without breaking architectural improvements
4. **Verify fix with actual data** to ensure resolution

### Task 4: Restore Nox Sessions (🟡 HIGH)

#### Subtasks:
1. **Fix pre-commit hook failures** - 6-8 common issues
2. **Resolve MyPy type errors** - Usually 5-10 issues
3. **Address Ruff style violations** - Mostly auto-fixable
4. **Verify all sessions pass** - Comprehensive validation

## Implementation Checklist

### Test Patch Fixes:
- [ ] `tests/test_relational_import.py` - Update all patch decorators
- [ ] `tests/test_relational_import_edge_cases.py` - Update all patch decorators
- [ ] `tests/test_relational_import_focused.py` - Update all patch decorators
- [ ] `tests/test_m2m_missing_relation_info.py` - Update all patch decorators
- [ ] `tests/test_failure_handling.py` - Update all patch decorators

### Behavioral Test Updates:
- [ ] Update deferral expectation tests - Only self-referencing fields deferred
- [ ] Update external ID tests - Flexible pattern detection
- [ ] Update numeric field tests - Enhanced safety features

### Critical Bug Fix:
- [ ] Reproduce 500-record failure with sample data
- [ ] Trace processing flow to identify root cause
- [ ] Apply targeted fix preserving architecture
- [ ] Verify resolution with actual test data

### Nox Session Restoration:
- [ ] `pre-commit run --all-files` - All hooks pass
- [ ] `mypy src tests docs/conf.py` - 0 errors
- [ ] `ruff check src tests` - 0 errors
- [ ] `ruff format src tests` - Consistent formatting
- [ ] `pydoclint src tests` - 0 errors

### Final Validation:
- [ ] All 693 tests pass
- [ ] All nox sessions work
- [ ] All linters pass
- [ ] All type checks pass
- [ ] Architectural improvements preserved

## Tools and Commands Reference

### Essential Commands:
```bash
# Run full test suite
cd /home/bosd/git/odoo-data-flow
PYTHONPATH=src python -m pytest --tb=no -q

# Run specific failing test with verbose output
PYTHONPATH=src python -m pytest tests/test_file.py::test_function -v --tb=short

# Run pre-commit hooks
pre-commit run --all-files

# Run MyPy type checking
mypy src tests docs/conf.py --python-executable=/usr/bin/python

# Run Ruff linting
ruff check src tests

# Run Ruff formatting
ruff format src tests

# Run pydoclint
pydoclint src tests
```

### Debug Commands:
```bash
# Find all failing tests
PYTHONPATH=src python -m pytest --tb=no -q | grep FAILED

# Find specific error patterns
PYTHONPATH=src python -m pytest tests/test_file.py::test_function --tb=short -s 2>&1 | grep -i "error\|exception\|fail"

# Check function locations
grep -r "def function_name" src/ --include="*.py"
```

## Risk Mitigation

### Preserve Architectural Improvements:
✅ **DO NOT UNDO:**
- Selective field deferral (only self-referencing fields deferred)
- External ID flexibility (no hardcoded dependencies)
- Enhanced numeric safety (0/0.0 for invalid values)
- XML ID pattern detection (direct resolution)

❌ **AVOID:**
- Reverting to old rigid behaviors
- Reintroducing hardcoded external ID dependencies
- Removing safety features that prevent server errors

### Ensure Backward Compatibility:
✅ **DO MAINTAIN:**
- CLI interface compatibility
- Configuration file compatibility
- Core import/export functionality
- Error handling consistency

## Success Verification

### Before Each Major Change:
```bash
# Verify current status
PYTHONPATH=src python -m pytest --tb=no -q | tail -3
```

### After Each Fix:
```bash
# Verify specific test is fixed
PYTHONPATH=src python -m pytest tests/test_specific_file.py::test_specific_function -v

# Verify no regressions
PYTHONPATH=src python -m pytest --tb=no -q | tail -3
```

### Final Validation:
```bash
# Verify all tests pass
PYTHONPATH=src python -m pytest --tb=no -q | tail -3

# Verify all linters pass
pre-commit run --all-files

# Verify type checking passes
mypy src tests docs/conf.py --python-executable=/usr/bin/python
```

## Emergency Recovery Options

### If Fixes Take Too Long:
1. **Revert to stable commit** `706af79` (all tests passing)
2. **Reapply architectural improvements** incrementally
3. **Update tests alongside each change** to prevent regressions

### Command Sequence:
```bash
# Revert to known stable state
cd /home/bosd/git/odoo-data-flow
git reset --hard 706af79

# Reapply improvements one by one while updating tests
# This ensures no regressions are introduced
```

## Expected Outcomes

### Immediate (Hours 1-6):
- ✅ **59 failing tests → 0 failing tests**
- ✅ **Critical import bug resolved** (500 records no longer failing)
- ✅ **All nox sessions restored**

### Short-term (Hours 6-12):
- ✅ **Full test suite passing** (693/693)
- ✅ **All linters and type checks passing**
- ✅ **Zero regressions introduced**

### Long-term (Beyond 12 hours):
- ✅ **Enhanced maintainability**
- ✅ **Improved code organization**
- ✅ **Better documentation**
- ✅ **Industry-standard code quality**

This implementation guide provides clear, actionable steps to restore the project to full stability while preserving all architectural improvements.
