<!--
Thanks for contributing to fluvo! Keep this short but concrete.
The `all-checks` gate must be green before a maintainer can merge.
-->

## What & why

<!-- What does this change, and what problem does it solve? -->

Closes #

## How it's verified

<!--
Point at the tests that prove it. "Done = tests pass" is the standard here.
e.g. tests/test_importer.py::test_x, or e2e scenario tests/e2e/test_integrity_*.py
-->

- Tests added/updated:
- Commands run locally: `uv run nox -s tests pre-commit mypy`

## Checklist

- [ ] I have signed the **CLA** (the bot prompts on your first PR — comment to sign).
- [ ] New/changed behaviour is covered by tests; the coverage gate stays green.
- [ ] `uv run nox -s pre-commit` passes — including **pydoclint**. If I intentionally
      changed docstrings, I refreshed `pydoclint-baseline.txt`
      (`uv run pydoclint --generate-baseline=True $(git ls-files '*.py')`).
- [ ] Docs updated if behaviour or the CLI changed.
- [ ] The `all-checks` gate is green.
