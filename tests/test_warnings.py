"""Warnings: unused-variable, -W, and --warn-as-error (spec 73)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from rwc.desugar import desugar_module
from rwc.lexer import tokenize
from rwc.parser import parse
from rwc.sema import analyze

REPO = Path(__file__).resolve().parent.parent


def _analyze(src: str):
    return analyze(desugar_module(parse(tokenize(src))))


def _warnings(body: str) -> list:
    src = "def main() -> int:\n" + body + "    return 0\n"
    return [(w.message, w.line, w.col) for w in _analyze(src).warnings]


def test_unused_declarations():
    assert _warnings("    a: int = 1\n    b = 2\n") == [
        ("unused variable `a`", 2, 5),
        ("unused variable `b`", 3, 5),
    ]


def test_assigned_but_never_read():
    assert _warnings("    a = 1\n    a = 2\n") == [("unused variable `a`", 2, 5)]


def test_used_variables_are_quiet():
    body = (
        "    a = 1\n"
        "    b: int = a + 1\n"
        '    s = f"{b}"\n'
        "    print(s)\n"
        "    n = 0\n"
        "    n += 1\n"
    )
    # `n += 1` reads n before writing it.
    assert _warnings(body) == []


def test_underscore_params_and_loop_vars_are_quiet():
    src = (
        "def f(x: int) -> int:\n"
        "    _tmp = 3\n"
        "    for i in range(2):\n"
        '        print("x")\n'
        "    return 0\n"
        "def main() -> int:\n"
        "    return f(1)\n"
    )
    assert _analyze(src).warnings == []


def test_use_inside_closure_counts():
    body = "    k = 2\n    g: Callable[[int], int] = lambda x: x * k\n    print(g(3))\n"
    assert _warnings(body) == []


def test_unused_inside_closure():
    body = "    def h() -> int:\n        t = 1\n        return 0\n    print(h())\n"
    assert _warnings(body) == [("unused variable `t`", 3, 9)]


def _check(tmp_path, *args: str) -> subprocess.CompletedProcess:
    rw = tmp_path / "w.rw"
    rw.write_text("def main() -> int:\n    unused = 1\n    return 0\n")
    return subprocess.run(
        [sys.executable, "-m", "rwc.cli", "check", *args, str(rw)], capture_output=True, text=True, cwd=REPO
    )


def test_cli_reports_warning_and_passes(tmp_path):
    r = _check(tmp_path)
    assert r.returncode == 0
    assert "warning[unused-variable]: unused variable `unused`" in r.stderr
    assert "w.rw:2:5" in r.stderr


def test_cli_disable_rule(tmp_path):
    r = _check(tmp_path, "-W", "no-unused-variable")
    assert r.returncode == 0
    assert r.stderr == ""


def test_cli_warn_as_error(tmp_path):
    r = _check(tmp_path, "--warn-as-error")
    assert r.returncode == 1
    assert "error[unused-variable]" in r.stderr


def test_cli_disabled_rule_does_not_fail_warn_as_error(tmp_path):
    r = _check(tmp_path, "--warn-as-error", "-W", "no-unused-variable")
    assert r.returncode == 0


def test_cli_unknown_rule(tmp_path):
    r = _check(tmp_path, "-W", "no-such-rule")
    assert r.returncode == 2
    assert "unknown warning" in r.stderr


def test_build_fails_under_warn_as_error(tmp_path):
    rw = tmp_path / "w.rw"
    rw.write_text("def main() -> int:\n    unused = 1\n    return 0\n")
    r = subprocess.run(
        [sys.executable, "-m", "rwc.cli", "build", "--warn-as-error", str(rw)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert r.returncode == 1
    assert not (tmp_path / "w").exists()
