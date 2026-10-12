"""Run-time checks for integer division (spec 42)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _run(src: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        rw = Path(td) / "p.rw"
        rw.write_text(src)
        exe = Path(td) / "p"
        build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(rw), "-o", str(exe)],
                               capture_output=True, text=True, cwd=REPO)
        assert build.returncode == 0, build.stderr
        return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("expr, msg", [
    ("a / b", "division by zero in div (line 2)"),
    ("a % b", "division by zero in div (line 2)"),
])
def test_division_by_zero_panics(expr, msg):
    src = f"def div(a: int, b: int) -> int:\n    return {expr}\n\ndef main() -> int:\n    print(div(7, 0))\n    return 0\n"
    run = _run(src)
    assert run.returncode != 0
    assert msg in run.stderr


def test_int_min_division():
    src = (
        "def div(a: int, b: int) -> int:\n    return a / b\n\n"
        "def rem(a: int, b: int) -> int:\n    return a % b\n\n"
        "def main() -> int:\n"
        "    lo = -9223372036854775807 - 1\n"
        "    print(rem(lo, -1))\n"
        "    print(div(7, -2))\n"
        "    print(div(lo, -1))\n"
        "    return 0\n"
    )
    run = _run(src)
    assert run.stdout.splitlines()[:2] == ["0", "-3"]
    assert run.returncode != 0
    assert "integer overflow in division in div (line 2)" in run.stderr
