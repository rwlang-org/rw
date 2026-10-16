"""Metadata lives in pyproject.toml. This file only adds the C runtime, the
standard library, and the helper scripts to the wheel, under rwc/_data/
(see rwc/resources.py), since they sit outside the `rwc` package."""

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent
DATA_DIRS = ("runtime", "stdlib", "scripts")
# Build outputs and C test programs stay out of the wheel; the runtime is
# compiled on first use (rwc/driver.py).
IGNORE = shutil.ignore_patterns("*.o", "*.a", "test_*", "__pycache__")


class build_py_with_data(build_py):
    def run(self) -> None:
        super().run()
        dest = Path(self.build_lib) / "rwc" / "_data"
        for name in DATA_DIRS:
            src = ROOT / name
            if src.is_dir():
                shutil.copytree(src, dest / name, dirs_exist_ok=True, ignore=IGNORE)


setup(cmdclass={"build_py": build_py_with_data})
