"""Command line for running this package in a child Python process.

The app runs the embedded interpreter as `python -I -B`: `-I` ignores `PYTHON*` variables
and the user site directory, `-B` never writes `.pyc` files (the app bundle must stay
unchanged, or its signature breaks). Children get the same flags as the parent.
"""

from __future__ import annotations

import sys


def interpreter_flags() -> list[str]:
    flags = []
    if sys.flags.isolated:
        flags.append("-I")
    if sys.flags.dont_write_bytecode:
        flags.append("-B")
    return flags


def python_command(*args: str) -> list[str]:
    """`python [-I] [-B] -m transcribator <args>` with this process's interpreter."""
    return [sys.executable, *interpreter_flags(), "-m", "transcribator", *args]
