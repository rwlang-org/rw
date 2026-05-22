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


def test_string_equality_ok():
    src = "def main() -> int:\n    b: bool = \"a\" == \"b\"\n    return 0\n"
    check(src)


def test_string_neq_ok():
    src = "def main() -> int:\n    b: bool = \"a\" != \"b\"\n    return 0\n"
    check(src)


def test_string_concat_ok():
    src = (
        "def main() -> int:\n"
        "    s: string = \"a\" + \"b\"\n"
        "    print(s)\n"
        "    return 0\n"
    )
    check(src)


def test_len_returns_int():
    src = (
        "def main() -> int:\n"
        "    n: int = len(\"hello\")\n"
        "    return n\n"
    )
    check(src)


def test_string_plus_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    s: string = \"a\" + 1\n"
        "    return 0\n"
    )
    e = err(src)
    assert "+" in e.diagnostic.message


def test_string_eq_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    b: bool = \"a\" == 1\n"
        "    return 0\n"
    )
    e = err(src)
    assert "same type" in e.diagnostic.message


def test_len_wrong_arg_type():
    src = (
        "def main() -> int:\n"
        "    n: int = len(1)\n"
        "    return n\n"
    )
    e = err(src)
    assert "len argument must be string" in e.diagnostic.message


def test_len_wrong_arity():
    src = (
        "def main() -> int:\n"
        "    n: int = len(\"a\", \"b\")\n"
        "    return n\n"
    )
    e = err(src)
    assert "len takes exactly 1 argument" in e.diagnostic.message


def test_cannot_spawn_len():
    src = (
        "def main() -> int:\n"
        "    f: Future[int] = spawn len(\"x\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "cannot spawn the builtin `len`" in e.diagnostic.message


def test_bytes_type_annotation_parses():
    # Declaring a Bytes parameter and using it should parse and resolve.
    # We don't yet have a way to *produce* a Bytes value, so use a
    # function parameter (the only way to introduce a Bytes binding
    # before bytes_from_str lands).
    src = (
        "def takes_bytes(b: Bytes) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    res = check(src)
    assert "takes_bytes" in res.functions
    assert res.functions["takes_bytes"].params[0][1] is T.BYTES


# ---- Bytes builtin positive cases ----

def test_bytes_from_str_returns_bytes():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"hi\")\n"
        "    return 0\n"
    )
    check(src)


def test_len_bytes_returns_int():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"hello\")\n"
        "    n: int = len(b)\n"
        "    return n\n"
    )
    check(src)


def test_bytes_equality_ok():
    src = (
        "def main() -> int:\n"
        "    a: Bytes = bytes_from_str(\"x\")\n"
        "    b: Bytes = bytes_from_str(\"y\")\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    check(src)


def test_str_from_bytes_returns_string():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"x\")\n"
        "    s: string = str_from_bytes(b)\n"
        "    print(s)\n"
        "    return 0\n"
    )
    check(src)


def test_future_bytes_ok():
    src = (
        "def make() -> Bytes:\n"
        "    return bytes_from_str(\"x\")\n"
        "def main() -> int:\n"
        "    f: Future[Bytes] = spawn make()\n"
        "    b: Bytes = await f\n"
        "    return len(b)\n"
    )
    check(src)


# ---- Bytes negative cases ----

def test_print_bytes_is_type_error():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"x\")\n"
        "    print(b)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "print" in e.diagnostic.message


def test_bytes_plus_bytes_is_type_error():
    src = (
        "def main() -> int:\n"
        "    a: Bytes = bytes_from_str(\"x\")\n"
        "    b: Bytes = bytes_from_str(\"y\")\n"
        "    c: Bytes = a + b\n"
        "    return 0\n"
    )
    e = err(src)
    # `+` falls through string-only special-case into the numeric check,
    # which rejects with "int or float".
    assert "+" in e.diagnostic.message


def test_bytes_eq_string_is_type_error():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(\"x\")\n"
        "    if b == \"x\":\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    assert "same type" in e.diagnostic.message


def test_bytes_from_str_wrong_arg_type():
    src = (
        "def main() -> int:\n"
        "    b: Bytes = bytes_from_str(1)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "bytes_from_str argument must be string" in e.diagnostic.message


def test_cannot_spawn_bytes_from_str():
    src = (
        "def main() -> int:\n"
        "    f: Future[Bytes] = spawn bytes_from_str(\"x\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "cannot spawn the builtin `bytes_from_str`" in e.diagnostic.message


# ---- List[int] type annotation ----

def test_list_int_type_annotation_parses():
    # Declaring a List[int] parameter and using it should parse and
    # resolve, even before list_new / list_push are wired up.
    src = (
        "def takes_list(l: List[int]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    res = check(src)
    assert "takes_list" in res.functions
    assert res.functions["takes_list"].params[0][1] is T.LIST_INT


def test_list_with_non_int_param_is_parser_error():
    # only List[int] supported.
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    src = (
        "def f(l: List[string]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "List[int]" in str(ei.value) or "only List" in str(ei.value)


# ---- List[int] builtin positive cases ----

def test_list_new_returns_list_int():
    src = (
        "def main() -> int:\n"
        "    l: List[int] = list_new()\n"
        "    return 0\n"
    )
    check(src)


def test_list_push_returns_list_int():
    src = (
        "def main() -> int:\n"
        "    l: List[int] = list_new()\n"
        "    l = list_push(l, 5)\n"
        "    return 0\n"
    )
    check(src)


def test_list_at_returns_int():
    src = (
        "def main() -> int:\n"
        "    l: List[int] = list_new()\n"
        "    l = list_push(l, 7)\n"
        "    x: int = list_at(l, 0)\n"
        "    return x\n"
    )
    check(src)


def test_len_list_int_returns_int():
    src = (
        "def main() -> int:\n"
        "    l: List[int] = list_new()\n"
        "    l = list_push(l, 1)\n"
        "    n: int = len(l)\n"
        "    return n\n"
    )
    check(src)


# ---- List[int] negative cases ----

def test_print_list_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    l: List[int] = list_new()\n"
        "    print(l)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "print" in e.diagnostic.message


def test_list_int_plus_list_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    a: List[int] = list_new()\n"
        "    b: List[int] = list_new()\n"
        "    c: List[int] = a + b\n"
        "    return 0\n"
    )
    e = err(src)
    assert "+" in e.diagnostic.message


def test_list_int_eq_list_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    a: List[int] = list_new()\n"
        "    b: List[int] = list_new()\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    # equality falls through to the generic "no comparison for this
    # type" path; we just check it's flagged on `==`.
    assert "==" in e.diagnostic.message or "compare" in e.diagnostic.message


def test_list_push_wrong_value_type():
    src = (
        "def main() -> int:\n"
        "    l: List[int] = list_new()\n"
        "    l = list_push(l, \"hi\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "list_push second argument must be int" in e.diagnostic.message


def test_list_at_wrong_index_type():
    src = (
        "def main() -> int:\n"
        "    l: List[int] = list_new()\n"
        "    x: int = list_at(l, \"a\")\n"
        "    return x\n"
    )
    e = err(src)
    assert "list_at second argument must be int" in e.diagnostic.message


def test_list_new_wrong_arity():
    src = (
        "def main() -> int:\n"
        "    l: List[int] = list_new(1)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "list_new takes no arguments" in e.diagnostic.message


def test_cannot_spawn_list_new():
    src = (
        "def main() -> int:\n"
        "    f: Future[List[int]] = spawn list_new()\n"
        "    return 0\n"
    )
    e = err(src)
    assert "cannot spawn the builtin `list_new`" in e.diagnostic.message


# ---- Option[int] type annotation ----

def test_option_int_type_annotation_parses():
    src = (
        "def takes_opt(o: Option[int]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    res = check(src)
    assert "takes_opt" in res.functions
    assert res.functions["takes_opt"].params[0][1] is T.OPTION_INT


def test_option_with_non_int_param_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    src = (
        "def f(o: Option[string]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "Option[int]" in str(ei.value) or "only Option" in str(ei.value)


def test_match_with_missing_arm_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    # only Some arm — parser must reject
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = None\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            return x\n"
        "    return 0\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "must cover both" in str(ei.value)


