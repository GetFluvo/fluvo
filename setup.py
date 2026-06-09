"""This file handles the mypyc compilation settings."""

import os

from mypyc.build import mypycify
from setuptools import setup


def get_ext_modules():
    """Conditionally builds mypyc extensions.

    To compile with mypyc, set the FLUVO_COMPILE_MYPYC=1 environment variable:
        FLUVO_COMPILE_MYPYC=1 python setup.py build_ext --inplace

    Note: mapper.py is excluded because it contains callable objects that
    lose their signature when compiled, breaking introspection-based tests.
    """
    # If the environment variable is set, compile performance-critical modules
    if os.environ.get("FLUVO_COMPILE_MYPYC") == "1":
        print("Compiling import/export modules with mypyc...")
        return mypycify(
            [
                "src/fluvo/import_threaded.py",
                "src/fluvo/importer.py",
                "src/fluvo/export_threaded.py",
                "src/fluvo/write_threaded.py",
                "src/fluvo/lib/internal/tools.py",
            ]
        )

    # Otherwise, return an empty list to build a pure Python package
    print("Skipping mypyc compilation. Building pure Python package...")
    return []


# This minimalist setup.py provides ONLY the C extension build instructions
# and the entry point for the command-line script.
setup(
    # Your other setup arguments like name, version, packages, etc., go here.
    # setuptools will automatically find most of this from pyproject.toml
    # if you have it configured.
    ext_modules=get_ext_modules(),
)
