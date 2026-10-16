"""Where rwc finds the C runtime, the standard library, and its helper
scripts. The three layouts:

- a source checkout: `runtime/`, `stdlib/`, `scripts/` next to `rwc/`
- an installed wheel: copies under `rwc/_data/` (setup.py puts them there)
- the standalone binary (PyInstaller): the bundle directory, `sys._MEIPASS`

`RW_HOME` overrides all three.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def rw_home() -> Path:
    env = os.environ.get("RW_HOME")
    if env:
        return Path(env)
    bundle = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle:
        return Path(bundle)
    pkg = Path(__file__).resolve().parent
    if (pkg / "_data").is_dir():
        return pkg / "_data"
    return pkg.parent


def cache_dir() -> Path:
    """A writable per-user directory, for building the runtime when the
    installed copy is read-only."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "rw"
