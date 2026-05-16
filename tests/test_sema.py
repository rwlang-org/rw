from __future__ import annotations

import pytest

from rwc import types as T
from rwc.diagnostics import CompileError
from rwc.lexer import tokenize
from rwc.parser import parse
from rwc.sema import analyze


def check(src: str):
    return analyze(parse(tokenize(src)), filename="test.rw")


def err(src: str) -> CompileError:
    with pytest.raises(CompileError) as ei:
        check(src)
    return ei.value


# ---- positive cases ----

def test_minimal_main_ok():
    res = check("def main() -> int:\n    return 0\n")
    assert "main" in res.functions
    assert res.functions["main"].return_type is T.INT


def test_function_call_and_return():
    src = (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "def main() -> int:\n"
        "    return add(3, 4)\n"
    )
    res = check(src)
    assert res.functions["add"].return_type is T.INT


def test_print_string_ok():
    res = check("def main() -> int:\n    print(\"hi\")\n    return 0\n")
    assert "main" in res.functions


def test_if_else_must_return_all_paths():
    src = (
        "def f(x: int) -> int:\n"
        "    if x > 0:\n"
        "        return 1\n"
        "    else:\n"
        "        return -1\n"
        "def main() -> int:\n"
        "    return f(2)\n"
    )
    check(src)


def test_spawn_returns_future():
    src = (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "def main() -> int:\n"
        "    fu: Future[int] = spawn add(1, 2)\n"
        "    r: int = await fu\n"
        "    return r\n"
    )
    res = check(src)
    assert "add" in res.functions
    assert "main" in res.functions


def test_void_return_bare():
    src = (
        "def greet() -> void:\n"
        "    print(\"hi\")\n"
        "    return\n"
        "def main() -> int:\n"
        "    greet()\n"
        "    return 0\n"
    )
    check(src)


def test_while_does_not_satisfy_return():
    # `while True` style is not yet supported and `while` doesn't guarantee return.
    src = (
        "def f() -> int:\n"
        "    while true:\n"
        "        return 1\n"
        "def main() -> int:\n"
        "    return f()\n"
    )
    e = err(src)
    assert "does not return on all paths" in e.diagnostic.message


# ---- negative cases (each is a diagnostic) ----

def test_main_missing():
    e = err("def add(a: int, b: int) -> int:\n    return a + b\n")
    assert "main" in e.diagnostic.message


def test_main_wrong_signature():
    e = err("def main(x: int) -> int:\n    return x\n")
    assert "main" in e.diagnostic.message


def test_main_wrong_return_type():
    e = err("def main() -> void:\n    return\n")
    assert "main must return int" in e.diagnostic.message


def test_duplicate_function():
    src = (
        "def main() -> int:\n    return 0\n"
        "def main() -> int:\n    return 1\n"
    )
    e = err(src)
    assert "duplicate" in e.diagnostic.message


def test_type_mismatch_var_decl():
    src = "def main() -> int:\n    x: int = \"hi\"\n    return 0\n"
    e = err(src)
    assert "type mismatch" in e.diagnostic.message


def test_undefined_variable():
    src = "def main() -> int:\n    return y\n"
    e = err(src)
    assert "undefined variable" in e.diagnostic.message


def test_call_arg_type_mismatch():
    src = (
        "def add(a: int, b: int) -> int:\n    return a + b\n"
        "def main() -> int:\n    return add(1, \"two\")\n"
    )
    e = err(src)
    assert "argument 2 of `add`" in e.diagnostic.message


def test_if_cond_must_be_bool():
    src = "def main() -> int:\n    if 1:\n        return 0\n    return 1\n"
    e = err(src)
    assert "if condition" in e.diagnostic.message


def test_arithmetic_requires_same_numeric():
    src = "def main() -> int:\n    x: int = 1 + 2.0\n    return 0\n"
    e = err(src)
    assert "operator" in e.diagnostic.message


def test_await_on_non_future():
    src = "def main() -> int:\n    x: int = 1\n    y: int = await x\n    return 0\n"
    e = err(src)
    assert "await" in e.diagnostic.message


def test_spawn_unknown_function():
    src = (
        "def main() -> int:\n"
        "    fu: Future[int] = spawn nope(1)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "undefined function" in e.diagnostic.message


def test_string_equality_not_supported():
    src = "def main() -> int:\n    b: bool = \"a\" == \"b\"\n    return 0\n"
    e = err(src)
    assert "string equality" in e.diagnostic.message
