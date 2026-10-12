"""Build and run the C-level runtime tests under runtime/fiber/ (spec 19)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNTIME = REPO / "runtime"

C_TESTS = ["test_detach", "test_shutdown", "test_preempt", "test_park_race", "test_dict", "test_timer", "test_chan", "test_mutex", "test_await_any", "test_net_timeout"]


def _cc() -> str:
    for cand in (os.environ.get("CC"), "cc", "clang", "gcc"):
        if cand and shutil.which(cand):
            return cand
    pytest.skip("no C compiler found")


@pytest.fixture(scope="module")
def librw() -> Path:
    build = subprocess.run(["make", "-C", str(RUNTIME)], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    return RUNTIME / "librw.a"


@pytest.mark.parametrize("name", C_TESTS)
def test_c_runtime(librw: Path, name: str):
    src = RUNTIME / "fiber" / f"{name}.c"
    with tempfile.TemporaryDirectory() as td:
        exe = Path(td) / name
        libs = ["-lpthread", "-lm"] if sys.platform.startswith("linux") else []
        cc = subprocess.run(
            [
                _cc(),
                "-std=c11",
                "-D_GNU_SOURCE",
                "-O2",
                "-I",
                str(RUNTIME),
                str(src),
                str(librw),
                "-o",
                str(exe),
                *libs,
            ],
            capture_output=True,
            text=True,
        )
        assert cc.returncode == 0, cc.stderr
        run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
        assert run.returncode == 0, f"{name} failed (rc={run.returncode}):\n{run.stderr}"
