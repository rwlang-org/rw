"""`rwc test` and assert_eq (spec 68)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _rwc_test(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "rwc.cli", "test", *args],
                          capture_output=True, text=True, cwd=REPO, timeout=300)


def _write(d: Path, name: str, src: str) -> None:
    (d / name).write_text(src)


def test_runs_tests_and_reports_failures(tmp_path):
    _write(tmp_path, "mathlib.rw", "def add(a: int, b: int) -> int:\n    return a + b\n")
    _write(tmp_path, "test_math.rw",
           "import mathlib\n\n"
           "def test_add() -> void:\n    assert_eq(mathlib.add(2, 3), 5)\n\n"
           "def test_wrong() -> void:\n    print(\"before\")\n    assert_eq(mathlib.add(2, 2), 5)\n\n"
           "def test_text() -> void:\n    assert_eq(\"one\\ntwo\\n\", \"one\\nthree\\n\")\n\n"
           "def test_float() -> void:\n    assert_eq(0.5 + 0.25, 0.75)\n\n"
           "def not_a_test() -> void:\n    assert_eq(1, 2)\n\n"
           "def test_takes_args(x: int) -> void:\n    assert_eq(1, 2)\n")
    sub = tmp_path / "pkg"
    sub.mkdir()
    _write(sub, "edge_test.rw", "def test_crash() -> void:\n    xs = [1]\n    print(xs[3])\n")
    _write(sub, "helper.rw", "def test_not_collected() -> void:\n    assert_eq(1, 2)\n")
    r = _rwc_test(str(tmp_path))
    assert r.returncode == 1
    out = r.stdout
    assert "PASS test_add" in out and "PASS test_float" in out
    assert "FAIL test_wrong" in out and "    before" in out
    assert "rw: assert_eq failed in test_wrong (line 8)" in out
    assert "      left:  4\n      right: 5" in out
    assert "first difference at line 2:" in out and '- "two"' in out and '+ "three"' in out
    assert "FAIL test_crash" in out
    assert "not_a_test" not in out and "test_takes_args" not in out and "test_not_collected" not in out
    assert out.strip().splitlines()[-1].startswith("2 passed, 3 failed")


def test_filter_and_success_exit(tmp_path):
    _write(tmp_path, "test_a.rw", "def test_one() -> void:\n    assert_eq(1, 1)\n\n"
                                  "def test_two() -> void:\n    assert_eq(1, 2)\n")
    r = _rwc_test("-k", "one", str(tmp_path / "test_a.rw"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS test_one" in r.stdout and "test_two" not in r.stdout


def test_build_errors_and_no_tests(tmp_path):
    _write(tmp_path, "test_bad.rw", "def test_x() -> void:\n    assert_eq(1, \"one\")\n")
    r = _rwc_test(str(tmp_path))
    assert r.returncode == 1
    assert "assert_eq arguments differ in type" in r.stderr
    assert "did not build" in r.stdout
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _rwc_test(str(empty))
    assert r.returncode == 1
    assert "no test files found" in r.stdout


def test_timeout(tmp_path):
    _write(tmp_path, "test_slow.rw", "def test_hang() -> void:\n    sleep(10000)\n")
    r = _rwc_test("--timeout", "0.5", str(tmp_path))
    assert r.returncode == 1
    assert "FAIL test_hang" in r.stdout and "timed out after 0.5 s" in r.stdout
