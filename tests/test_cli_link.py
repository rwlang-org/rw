"""extern "c" with a user C library: rwc run -L DIR -l LIB (spec 74)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

C_SRC = """
#include <stdint.h>
int32_t mix(int32_t a, uint8_t b, int8_t c) { return a * 1000 + b * 10 + c; }
uint8_t low_byte(uint32_t x) { return (uint8_t)x; }
double scale(double x, int64_t k) { return x * (double)k; }
"""

RW_SRC = """
extern "c" def mix(a: i32, b: u8, c: i8) -> i32
extern "c" def low_byte(x: u32) -> u8
extern "c" def scale(x: float, k: int) -> float

def main() -> int:
    print(mix(7, 250, -3))
    print(low_byte(0x12345678))
    print(scale(1.5, 4))
    return 0
"""


@pytest.mark.skipif(shutil.which("clang") is None and shutil.which("cc") is None, reason="needs a C compiler")
def test_link_a_user_library(tmp_path):
    cc = shutil.which("clang") or shutil.which("cc")
    (tmp_path / "mylib.c").write_text(C_SRC)
    subprocess.run([cc, "-c", "-fPIC", "-o", str(tmp_path / "mylib.o"), str(tmp_path / "mylib.c")], check=True)
    subprocess.run(["ar", "rcs", str(tmp_path / "libmylib.a"), str(tmp_path / "mylib.o")], check=True)
    src = tmp_path / "p.rw"
    src.write_text(RW_SRC)
    r = subprocess.run([sys.executable, "-m", "rwc.cli", "run", str(src), "-L", str(tmp_path), "-l", "mylib"],
                       capture_output=True, text=True, cwd=REPO, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["9497", "120", "6"]


def test_missing_library_is_a_linker_error(tmp_path):
    src = tmp_path / "p.rw"
    src.write_text('extern "c" def nope_rw_test() -> int\n\ndef main() -> int:\n    print(nope_rw_test())\n    return 0\n')
    r = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(src), "-o", str(tmp_path / "p")],
                       capture_output=True, text=True, cwd=REPO, timeout=120)
    assert r.returncode != 0
    assert "linker error" in r.stderr and "nope_rw_test" in r.stderr
