"""Closures: nested def and lambda, capture by value (spec 71)."""

from __future__ import annotations

import pytest

from rwc.diagnostics import CompileError
from rwc.driver import check_source


def ok(body: str, extra: str = "") -> None:
    check_source(extra + "def main() -> int:\n" + body + "    return 0\n", "t.rw")


def err(body: str, extra: str = "") -> str:
    with pytest.raises(CompileError) as ei:
        ok(body, extra)
    return ei.value.diagnostic.message


def test_captures_are_found_and_propagate():
    from rwc.loader import load_program
    from rwc.sema import analyze_program
    src = ("def main() -> int:\n    a = 1\n    b = \"x\"\n    unused = 3.0\n"
           "    def outer() -> Callable[[], int]:\n        return lambda: a + len(b)\n"
           "    return outer()()\n")
    prog = load_program(src, "t.rw")
    res = analyze_program(prog, filename="t.rw")
    caps = sorted([c.captures for c in res.closures.values()], key=len)
    # The lambda captures a and b; outer captures them too (to build it),
    # and nobody captures `unused`.
    assert [[n for n, _ in cs] for cs in caps] == [["a", "b"], ["a", "b"]]


def test_assigning_a_captured_variable_is_an_error():
    msg = err("    n = 0\n    def bump() -> void:\n        n += 1\n")
    assert "captures it by value and cannot assign to it" in msg
    assert "belongs to the enclosing function" in err("    n = 0\n    def f() -> void:\n        n = 5\n")
    loop = "    i = 0\n    def f() -> void:\n        for i in range(0, 3):\n            print(i)\n"
    assert "cannot assign" in err(loop)


def test_record_fields_may_be_mutated():
    ok("    c = Box(0)\n    def inc() -> void:\n        c.v += 1\n    inc()\n", "class Box:\n    v: int\n\n")


def test_locals_of_the_closure_are_its_own():
    ok("    def f() -> int:\n        t = 2\n        return t\n    t = 1\n    print(f() + t)\n")


def test_later_variables_are_not_visible():
    assert "undefined variable: later" in err("    def f() -> int:\n        return later\n    later = 1\n")


def test_lambda_needs_a_callable_context():
    assert "cannot infer the parameter types" in err("    f = lambda x: x\n")
    assert "lambda takes 2 parameter(s)" in err("    f: Callable[[int], int] = lambda a, b: a\n")


def test_lambda_body_is_type_checked():
    assert "return type mismatch" in err("    f: Callable[[int], string] = lambda x: x + 1\n")


def test_break_does_not_cross_into_a_closure():
    msg = err("    while true:\n        def f() -> void:\n            break\n        break\n")
    assert "break" in msg


def test_nested_def_defaults_and_redefinition():
    assert "default arguments" in err("    def f(x: int = 1) -> int:\n        return x\n")
    assert "already defined" in err("    f = 1\n    def f() -> void:\n        print(1)\n")


def test_spawning_a_closure_is_rejected():
    msg = err("    def f() -> int:\n        return 1\n    fut = spawn f()\n")
    assert "cannot spawn a function value" in msg
