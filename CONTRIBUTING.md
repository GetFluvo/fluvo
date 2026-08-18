# Contributor Guide

Thank you for your interest in improving this project.
This project is open-source under the [LGPL license] and
welcomes contributions in the form of bug reports, feature requests, and pull requests.

Here is a list of important resources for contributors:

- [Source Code]
- [Documentation]
- [Issue Tracker]
- [Code of Conduct]

[lgpl 3.0 license]: https://www.gnu.org/licenses/lgpl-3.0
[source code]: https://github.com/getfluvo/fluvo
[documentation]: https://fluvo.readthedocs.io/en/latest/
[issue tracker]: https://github.com/getfluvo/fluvo/issues

## Contributor License Agreement

Fluvo is open-core: the LGPL-3.0 library is the permanent core, and some
components are offered under a separate commercial license. To keep that
possible, every contributor agrees to our [Contributor License Agreement](CLA.md)
(CLA) before their first pull request is merged. It confirms your contribution is
yours to give and lets the maintainer license it under both LGPL-3.0 and
commercial terms; you keep full ownership of your work.

You don't need to do anything up front. When you open your first PR, the **CLA
Assistant** bot comments with a link to the CLA. To agree, post this comment on
the PR:

> I have read the CLA Document and I hereby sign the CLA

Your signature is recorded, the `license/cla` check goes green, and it's
remembered for all future PRs. Maintainers and bots (e.g. Dependabot) are exempt.

## How to report a bug

Report bugs on the [Issue Tracker].

When filing an issue, make sure to answer these questions:

- Which operating system and Python version are you using?
- Which version of this project are you using?
- What did you do?
- What did you expect to see?
- What did you see instead?

The best way to get your bug fixed is to provide a test case,
and/or steps to reproduce the issue.

## How to request a feature

Request features on the [Issue Tracker].

## How to set up your development environment

You need Python 3.10+ and the following tools:

- [uv]
- [Nox]

Install the package with all development requirements from the frozen lockfile:

```console
$ uv sync --all-groups --frozen
```

You can now run an interactive Python session,
or the command-line interface:

```console
$ uv run python
$ uv run fluvo
```

[uv]: https://docs.astral.sh/uv/
[nox]: https://nox.thea.codes/en/stable/

## How to test the project

Run the full test suite:

```console
$ nox
```

List the available Nox sessions:

```console
$ nox --list-sessions
```

You can also run a specific Nox session.
For example, invoke the unit test suite like this:

```console
$ nox --session=tests
```

Unit tests are located in the _tests_ directory,
and are written using the [pytest] testing framework.

[pytest]: https://docs.pytest.org/en/stable/

## How to submit changes

Open a [pull request] to submit changes to this project.

Your pull request needs to meet the following guidelines for acceptance:

- The `all-checks` gate must be green. It aggregates the full CI matrix
  (pre-commit, mypy, tests, typeguard, xdoctest, docs-build) and is a required
  status check on `master`.
- Include tests. Coverage is gated, so new code needs to be covered.
- Docstrings are checked by [pydoclint] against a committed baseline
  (`pydoclint-baseline.txt`). Write complete Google-style docstrings; if you
  legitimately need to refresh the baseline, run
  `uv run pydoclint --generate-baseline=True $(git ls-files '*.py')` and commit
  the result.
- If you agree to the [CLA](#contributor-license-agreement) (see above) and your
  changes add functionality, update the documentation accordingly.

Feel free to submit early, though—we can always iterate on this.

[pydoclint]: https://github.com/jsh9/pydoclint

To run linting and code formatting checks before committing your change, you can install pre-commit as a Git hook by running the following command:

```console
$ nox --session=pre-commit -- install
```

It is recommended to open an issue before starting work on anything.
This will allow a chance to talk it over with the owners and validate your approach.

[pull request]: https://github.com/getfluvo/fluvo/pulls

<!-- github-only -->

[code of conduct]: CODE_OF_CONDUCT.md
