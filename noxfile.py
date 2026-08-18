"""Nox sessions."""

import os
import shlex
import shutil
import sys
from pathlib import Path
from textwrap import dedent

import nox

# A helper command to clean up build artifacts
CLEAN_COMMAND = """
import glob, os, shutil;
shutil.rmtree('build', ignore_errors=True);
shutil.rmtree('dist', ignore_errors=True);
shutil.rmtree('src/fluvo.egg-info', ignore_errors=True);
for f in glob.glob('src/fluvo/*.so'): os.remove(f);
for f in glob.glob('src/fluvo/*.c'): os.remove(f);
"""

nox.options.default_venv_backend = "uv"

package = "fluvo"
python_versions = ["3.12", "3.13", "3.14", "3.11", "3.10"]
nox.needs_version = ">= 2021.6.6"
nox.options.sessions = (
    "pre-commit",
    "mypy",
    "tests",
    "typeguard",
    "xdoctest",
    "docs-build",
)


def activate_virtualenv_in_precommit_hooks(session: nox.Session) -> None:
    """Activate virtualenv in hooks installed by pre-commit.

    This function patches git hooks installed by pre-commit to activate the
    session's virtual environment. This allows pre-commit to locate hooks in
    that environment when invoked from git.

    Args:
        session: The Session object.
    """
    assert session.bin is not None  # nosec

    # Only patch hooks containing a reference to this session's bindir. Support
    # quoting rules for Python and bash, but strip the outermost quotes so we
    # can detect paths within the bindir, like <bindir>/python.
    bindirs = [
        bindir[1:-1] if bindir[0] in "'\"" else bindir
        for bindir in (repr(session.bin), shlex.quote(session.bin))
    ]

    virtualenv = session.env.get("VIRTUAL_ENV")
    if virtualenv is None:
        return

    headers = {
        # pre-commit < 2.16.0
        "python": f"""\
            import os
            os.environ["VIRTUAL_ENV"] = {virtualenv!r}
            os.environ["PATH"] = os.pathsep.join((
                {session.bin!r},
                os.environ.get("PATH", ""),
            ))
            """,
        # pre-commit >= 2.16.0
        "bash": f"""\
            VIRTUAL_ENV={shlex.quote(virtualenv)}
            PATH={shlex.quote(session.bin)}"{os.pathsep}$PATH"
            """,
        # pre-commit >= 2.17.0 on Windows forces sh shebang
        "/bin/sh": f"""\
            VIRTUAL_ENV={shlex.quote(virtualenv)}
            PATH={shlex.quote(session.bin)}"{os.pathsep}$PATH"
            """,
    }

    hookdir = Path(".git") / "hooks"
    if not hookdir.is_dir():
        return

    for hook in hookdir.iterdir():
        if hook.name.endswith(".sample") or not hook.is_file():
            continue

        if not hook.read_bytes().startswith(b"#!"):
            continue

        text = hook.read_text()

        if not any(
            (Path("A") == Path("a") and bindir.lower() in text.lower())
            or bindir in text
            for bindir in bindirs
        ):
            continue

        lines = text.splitlines()

        for executable, header in headers.items():
            if executable in lines[0].lower():
                lines.insert(1, dedent(header))
                hook.write_text("\n".join(lines))
                break


@nox.session(name="pre-commit", python=python_versions[0])
def precommit(session: nox.Session) -> None:
    """Lint using pre-commit."""
    args = session.posargs or [
        "run",
        "--all-files",
        "--hook-stage=manual",
        "--show-diff-on-failure",
    ]

    session.run(
        "uv",
        "sync",
        # --frozen: install exactly what uv.lock pins and never re-resolve, so CI
        # (and local nox) use a reproducible toolchain. Without this, uv may pick up
        # newer transitive versions than the lock, which silently drifts tool output
        # (e.g. pydoclint's baseline rendering) and breaks pre-commit on unrelated PRs.
        "--frozen",
        "--python",
        str(session.python),
        "--group",
        "dev",
        "--group",
        "lint",
    )
    session.run("pre-commit", *args)
    if args and args[0] == "install":
        activate_virtualenv_in_precommit_hooks(session)


@nox.session(python=python_versions)
def mypy(session: nox.Session) -> None:
    """Type-check using mypy."""
    args = session.posargs or ["src", "tests", "docs/conf.py"]

    session.run(
        "uv",
        "sync",
        # --frozen: install exactly what uv.lock pins and never re-resolve, so CI
        # (and local nox) use a reproducible toolchain. Without this, uv may pick up
        # newer transitive versions than the lock, which silently drifts tool output
        # (e.g. pydoclint's baseline rendering) and breaks pre-commit on unrelated PRs.
        "--frozen",
        "--python",
        str(session.python),
        "--group",
        "dev",
        "--group",
        "mypy",
    )

    session.install("mypy")
    session.install("pytest")
    session.install("httpx")
    # mypy runs from this session's venv, so its stub packages must be installed
    # here (the earlier `uv sync --group mypy` populates .venv, not this venv).
    session.install("types-PyYAML")
    session.install("-e", ".")
    session.run("mypy", *args)
    if not session.posargs:
        session.run("mypy", f"--python-executable={sys.executable}", "noxfile.py")


@nox.session(python=python_versions)
def tests(session: nox.Session) -> None:
    """Run the test suite."""
    session.run("python", "-c", CLEAN_COMMAND)
    session.run(
        "uv",
        "sync",
        # --frozen: install exactly what uv.lock pins and never re-resolve, so CI
        # (and local nox) use a reproducible toolchain. Without this, uv may pick up
        # newer transitive versions than the lock, which silently drifts tool output
        # (e.g. pydoclint's baseline rendering) and breaks pre-commit on unrelated PRs.
        "--frozen",
        "--python",
        str(session.python),
        "--group",
        "dev",
        "--group",
        "lint",
    )

    session.install("pytest", "coverage", "pytest-mock")
    session.install("-e", ".")
    session.run("pytest", *session.posargs)


@nox.session(python=python_versions[0])
def e2e(session: nox.Session) -> None:
    """Run the end-to-end integrity suite against a real Odoo.

    Brings up a disposable Postgres+Odoo stack via compose (podman or docker;
    see tests/e2e/conftest.py) and runs the data-integrity scenarios. Point
    FLUVO_E2E_ODOO_URL at an existing Odoo (e.g. doodba) to skip container mgmt.

    The default small tier runs here; pass ``-- -m large`` (with a larger
    FLUVO_E2E_SCALE) for the opt-in stress tier.
    """
    session.install("pytest", "pytest-mock", "pytest-cov", "coverage[toml]")
    session.install("-e", ".")
    args = session.posargs or ["-m", "not large"]
    # Override the default addopts (which ignores tests/e2e and runs doctests).
    # Also measure src coverage: the e2e suite drives the import/export engine
    # against a real Odoo, exercising error/retry/relational paths the unit tests
    # can't reach. That report is uploaded to Codecov under the `e2e` flag so those
    # genuinely-tested lines finally count (see .github/workflows/e2e.yml).
    session.run(
        "pytest",
        "tests/e2e",
        "-o",
        "addopts=",
        "--cov=src",
        "--cov-branch",
        "--cov-report=xml:coverage-e2e.xml",
        # The e2e suite only exercises the import/export engine, so it can't meet
        # the global fail_under=85 on its own — Codecov gates the merged total.
        "--cov-fail-under=0",
        *args,
    )


@nox.session(python=python_versions[0])
def tests_compiled(session: nox.Session) -> None:
    """Run tests against the compiled C extension code."""
    session.run("python", "-c", CLEAN_COMMAND)
    session.install("pytest", "pytest-mock")

    # Install the project WITH the env var to trigger mypyc compilation
    session.install("-e", ".", env={"FLUVO_COMPILE_MYPYC": "1"})

    session.run("pytest", *session.posargs)


@nox.session(python=python_versions[0])
def coverage(session: nox.Session) -> None:
    """Produce the coverage report."""
    args = session.posargs or ["report"]
    session.install(
        "pytest",
        "coverage[toml]",
        "pytest-cov",
        "pytest-mock",
        "httpx",
        "rich",
        "polars",
        "click",
        "odoo-client-lib @ git+https://github.com/odoo/odoo-client-lib.git@refs/pull/5/head",
    )
    session.install("-e", ".")
    session.log("Running pytest with coverage...")
    session.run("pytest", "--cov=src", "--cov-report=xml", "tests/")

    if not session.posargs and any(Path().glob(".coverage.*")):
        session.run("coverage", "combine")

    session.run("coverage", *args)


@nox.session(name="typeguard", python=python_versions[0])
def typeguard_tests(session: nox.Session) -> None:
    """Run tests with typeguard."""
    session.run(
        "uv",
        "sync",
        # --frozen: install exactly what uv.lock pins and never re-resolve, so CI
        # (and local nox) use a reproducible toolchain. Without this, uv may pick up
        # newer transitive versions than the lock, which silently drifts tool output
        # (e.g. pydoclint's baseline rendering) and breaks pre-commit on unrelated PRs.
        "--frozen",
        "--python",
        str(session.python),
        "--group",
        "dev",
        "--group",
        "typeguard",
    )

    session.install("typeguard", "pytest", "pytest-mock")
    session.install("-e", ".")
    session.run("pytest", "--typeguard-packages", package, *session.posargs)


@nox.session(python=python_versions)
def xdoctest(session: nox.Session) -> None:
    """Run examples with xdoctest."""
    if session.posargs:
        args = [package, *session.posargs]
    else:
        args = [f"--modname={package}", "--command=all"]
        if "FORCE_COLOR" in os.environ:
            args.append("--colored=1")
    session.run(
        "uv",
        "sync",
        # --frozen: install exactly what uv.lock pins and never re-resolve, so CI
        # (and local nox) use a reproducible toolchain. Without this, uv may pick up
        # newer transitive versions than the lock, which silently drifts tool output
        # (e.g. pydoclint's baseline rendering) and breaks pre-commit on unrelated PRs.
        "--frozen",
        "--python",
        str(session.python),
        "--group",
        "dev",
        "--group",
        "xdoctest",
    )
    session.install("xdoctest")
    session.install("-e", ".")
    session.run("python", "-m", "xdoctest", package, *args)


@nox.session(name="docs-build", python=python_versions[1])
def docs_build(session: nox.Session) -> None:
    """Build the documentation."""
    args = session.posargs or ["docs", "docs/_build"]
    if not session.posargs and "FORCE_COLOR" in os.environ:
        args.insert(0, "--color")

    session.run(
        "uv",
        "sync",
        # --frozen: install exactly what uv.lock pins and never re-resolve, so CI
        # (and local nox) use a reproducible toolchain. Without this, uv may pick up
        # newer transitive versions than the lock, which silently drifts tool output
        # (e.g. pydoclint's baseline rendering) and breaks pre-commit on unrelated PRs.
        "--frozen",
        "--python",
        str(session.python),
        "--group",
        "dev",
        "--group",
        "docs",
    )
    session.install(
        "sphinx",
        "sphinx-mermaid",
        "sphinx-click",
        "myst_parser",
        "shibuya",
        "sphinx-copybutton",
        "pygments<2.20",  # shibuya passes int linespans; pygments 2.20 escapes it
    )
    session.install("-e", ".")

    build_dir = Path("docs", "_build")
    if build_dir.exists():
        shutil.rmtree(build_dir)

    session.run("sphinx-build", *args)


@nox.session(name="docs-linkcheck", python=python_versions[1])
def docs_linkcheck(session: nox.Session) -> None:
    """Check the documentation's external links (sphinx linkcheck builder)."""
    session.run(
        "uv",
        "sync",
        # --frozen: install exactly what uv.lock pins and never re-resolve, so CI
        # (and local nox) use a reproducible toolchain. Without this, uv may pick up
        # newer transitive versions than the lock, which silently drifts tool output
        # (e.g. pydoclint's baseline rendering) and breaks pre-commit on unrelated PRs.
        "--frozen",
        "--python",
        str(session.python),
        "--group",
        "dev",
        "--group",
        "docs",
    )

    build_dir = Path("docs", "_build", "linkcheck")
    shutil.rmtree(build_dir, ignore_errors=True)

    args = ["-b", "linkcheck", "docs", str(build_dir)]
    if "FORCE_COLOR" in os.environ:
        args.insert(0, "--color")
    session.run("sphinx-build", *args, *session.posargs)


@nox.session(python=python_versions[0])
def docs(session: nox.Session) -> None:
    """Build and serve the documentation with live reloading on file changes."""
    args = session.posargs or ["--open-browser", "docs", "docs/_build"]
    session.run(
        "uv",
        "sync",
        # --frozen: install exactly what uv.lock pins and never re-resolve, so CI
        # (and local nox) use a reproducible toolchain. Without this, uv may pick up
        # newer transitive versions than the lock, which silently drifts tool output
        # (e.g. pydoclint's baseline rendering) and breaks pre-commit on unrelated PRs.
        "--frozen",
        "--python",
        str(session.python),
        "--group",
        "docs",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )

    build_dir = Path("docs", "_build")
    if build_dir.exists():
        shutil.rmtree(build_dir)

    session.run("sphinx-autobuild", *args)
