from __future__ import annotations

import pytest

from rwc import types as T
from rwc.desugar import desugar_module
from rwc.diagnostics import CompileError
from rwc.lexer import tokenize
from rwc.parser import parse
from rwc.sema import analyze


def check(src: str):
    return analyze(desugar_module(parse(tokenize(src))), filename="test.rw")


def err(src: str) -> CompileError:
    with pytest.raises(CompileError) as ei:
        check(src)
    return ei.value


# ---- positive cases ----

def test_minimal_main_ok():
    res = check("def main() -> int:\n    return 0\n")
    assert (None, "main") in res.functions
    assert res.functions[(None, "main")].return_type is T.INT


def test_function_call_and_return():
    src = (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "def main() -> int:\n"
        "    return add(3, 4)\n"
    )
    res = check(src)
    assert res.functions[(None, "add")].return_type is T.INT


def test_print_string_ok():
    res = check("def main() -> int:\n    print(\"hi\")\n    return 0\n")
    assert (None, "main") in res.functions


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
    assert (None, "add") in res.functions
    assert (None, "main") in res.functions


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
    assert (None, "takes_bytes") in res.functions
    assert res.functions[(None, "takes_bytes")].params[0][1] is T.BYTES


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
    assert (None, "takes_list") in res.functions
    assert res.functions[(None, "takes_list")].params[0][1] is T.LIST_INT




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
    assert (None, "takes_opt") in res.functions
    assert res.functions[(None, "takes_opt")].params[0][1] is T.OPTION_INT


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


# ---- Option[int] positive cases ----

def test_some_int_returns_option_int():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(5)\n"
        "    return 0\n"
    )
    check(src)


def test_none_returns_option_int():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = None\n"
        "    return 0\n"
    )
    check(src)


def test_function_returning_option_int_with_both_arms():
    src = (
        "def f(b: int) -> Option[int]:\n"
        "    if b == 0:\n"
        "        return None\n"
        "    return Some(1)\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    check(src)


def test_match_two_arms_ok():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(7)\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            print(x)\n"
        "        case None:\n"
        "            print(-1)\n"
        "    return 0\n"
    )
    check(src)


def test_match_some_bound_var_is_int():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(7)\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            y: int = x + 1\n"
        "            print(y)\n"
        "        case None:\n"
        "            print(-1)\n"
        "    return 0\n"
    )
    check(src)


def test_match_terminates_via_both_arms_return():
    # pick has no `return` after match because match itself terminates
    # in both arms.
    src = (
        "def pick(b: int) -> int:\n"
        "    o: Option[int] = None\n"
        "    if b == 0:\n"
        "        o = None\n"
        "    else:\n"
        "        o = Some(b)\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            return x\n"
        "        case None:\n"
        "            return -1\n"
        "def main() -> int:\n"
        "    return pick(7)\n"
    )
    check(src)


# ---- Option[int] negative cases ----

def test_some_string_is_type_error():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(\"hi\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "Some argument must be int" in e.diagnostic.message


def test_match_on_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    x: int = 5\n"
        "    match x:\n"
        "        case Some(v):\n"
        "            print(v)\n"
        "        case None:\n"
        "            print(0)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "match target must be an Option, found `int`" in e.diagnostic.message


def test_print_option_is_type_error():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(1)\n"
        "    print(o)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "print" in e.diagnostic.message


def test_option_eq_is_type_error():
    # Option[int] is intentionally not on the == whitelist
    src = (
        "def main() -> int:\n"
        "    a: Option[int] = Some(1)\n"
        "    b: Option[int] = None\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    assert "compare" in e.diagnostic.message or "==" in e.diagnostic.message


# ---- Result[int, int] type annotation + parser ----

def test_result_int_int_type_annotation_parses():
    src = (
        "def takes_res(r: Result[int, int]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    res = check(src)
    assert (None, "takes_res") in res.functions
    assert res.functions[(None, "takes_res")].params[0][1] is T.RESULT_INT_INT


def test_match_with_mixed_arms_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = None\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            return x\n"
        "        case Err(e):\n"
        "            return e\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "mixed match arms" in str(ei.value)


# ---- Result[int, int] positive cases ----

def test_ok_int_returns_result_int_int():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(5)\n"
        "    return 0\n"
    )
    check(src)


def test_err_int_returns_result_int_int():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Err(0)\n"
        "    return 0\n"
    )
    check(src)


def test_function_returning_result_with_both_arms():
    src = (
        "def f(b: int) -> Result[int, int]:\n"
        "    if b == 0:\n"
        "        return Err(0)\n"
        "    return Ok(1)\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    check(src)


def test_match_result_two_arms_ok():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(7)\n"
        "    match r:\n"
        "        case Ok(x):\n"
        "            print(x)\n"
        "        case Err(e):\n"
        "            print(e)\n"
        "    return 0\n"
    )
    check(src)


def test_match_result_bound_vars_are_int():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(7)\n"
        "    match r:\n"
        "        case Ok(x):\n"
        "            y: int = x + 1\n"
        "            print(y)\n"
        "        case Err(e):\n"
        "            z: int = e * 2\n"
        "            print(z)\n"
        "    return 0\n"
    )
    check(src)


def test_match_result_terminates_via_both_arms_return():
    src = (
        "def pick(b: int) -> int:\n"
        "    r: Result[int, int] = Err(0)\n"
        "    if b == 0:\n"
        "        r = Err(0)\n"
        "    else:\n"
        "        r = Ok(b)\n"
        "    match r:\n"
        "        case Ok(x):\n"
        "            return x\n"
        "        case Err(e):\n"
        "            return e\n"
        "def main() -> int:\n"
        "    return pick(7)\n"
    )
    check(src)


# ---- Result[int, int] negative cases ----

def test_ok_string_is_type_error():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(\"hi\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "Ok argument must be int" in e.diagnostic.message


def test_err_string_is_type_error():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Err(\"hi\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "Err argument must be int" in e.diagnostic.message


def test_match_result_on_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    x: int = 5\n"
        "    match x:\n"
        "        case Ok(v):\n"
        "            print(v)\n"
        "        case Err(e):\n"
        "            print(e)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "match target must be a Result, found `int`" in e.diagnostic.message


def test_print_result_is_type_error():
    src = (
        "def main() -> int:\n"
        "    r: Result[int, int] = Ok(1)\n"
        "    print(r)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "print" in e.diagnostic.message


def test_result_eq_is_type_error():
    src = (
        "def main() -> int:\n"
        "    a: Result[int, int] = Ok(1)\n"
        "    b: Result[int, int] = Err(0)\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    assert "compare" in e.diagnostic.message or "==" in e.diagnostic.message


def test_ok_eq_some_is_type_error():
    src = (
        "def main() -> int:\n"
        "    a: Result[int, int] = Ok(1)\n"
        "    b: Option[int] = Some(1)\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    assert "same type" in e.diagnostic.message


# ---- TCP builtins positive cases ----

def test_tcp_listen_returns_int():
    src = (
        "def main() -> int:\n"
        "    fd: int = tcp_listen(8080)\n"
        "    return fd\n"
    )
    check(src)


def test_tcp_accept_returns_int():
    src = (
        "def main() -> int:\n"
        "    lfd: int = tcp_listen(8080)\n"
        "    cfd: int = tcp_accept(lfd)\n"
        "    return cfd\n"
    )
    check(src)


def test_read_returns_bytes():
    res = check(
        "def main() -> int:\n"
        "    fd: int = file_open(\"/tmp/x\", \"r\")\n"
        "    b: Bytes = read(fd, 4096)\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_write_returns_int():
    res = check(
        "def main() -> int:\n"
        "    fd: int = file_open(\"/tmp/x\", \"w\")\n"
        "    n: int = write(fd, bytes_from_str(\"hi\"))\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_close_returns_int():
    res = check(
        "def main() -> int:\n"
        "    fd: int = file_open(\"/tmp/x\", \"r\")\n"
        "    rc: int = close(fd)\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_file_open_returns_int():
    res = check(
        "def main() -> int:\n"
        "    fd: int = file_open(\"/tmp/x\", \"w\")\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_read_wrong_max_type():
    e = err(
        "def main() -> int:\n"
        "    b: Bytes = read(3, \"big\")\n"
        "    return 0\n"
    )
    assert "read second argument must be int" in e.diagnostic.message


def test_write_wrong_buffer_type():
    e = err(
        "def main() -> int:\n"
        "    n: int = write(3, \"hi\")\n"
        "    return 0\n"
    )
    assert "write second argument must be Bytes" in e.diagnostic.message


def test_file_open_wrong_path_type():
    e = err(
        "def main() -> int:\n"
        "    fd: int = file_open(3, \"r\")\n"
        "    return 0\n"
    )
    assert "file_open first argument must be string" in e.diagnostic.message


def test_file_open_wrong_arity():
    e = err(
        "def main() -> int:\n"
        "    fd: int = file_open(\"/tmp/x\")\n"
        "    return 0\n"
    )
    assert "file_open takes 2 arguments" in e.diagnostic.message


# ---- TCP builtins negative cases ----

def test_tcp_listen_wrong_arg_type():
    src = (
        "def main() -> int:\n"
        "    fd: int = tcp_listen(\"8080\")\n"
        "    return fd\n"
    )
    e = err(src)
    assert "tcp_listen argument must be int" in e.diagnostic.message


def test_tcp_listen_wrong_arity():
    src = (
        "def main() -> int:\n"
        "    fd: int = tcp_listen()\n"
        "    return fd\n"
    )
    e = err(src)
    assert "tcp_listen takes 1 argument" in e.diagnostic.message


def test_cannot_spawn_tcp_accept():
    src = (
        "def main() -> int:\n"
        "    f: Future[int] = spawn tcp_accept(3)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "cannot spawn the builtin `tcp_accept`" in e.diagnostic.message


# ---- for ... in range(...) ----

def test_for_loop_int_args_ok():
    src = (
        "def main() -> int:\n"
        "    total: int = 0\n"
        "    for i in range(0, 5):\n"
        "        total = total + i\n"
        "    return total\n"
    )
    res = check(src)
    assert (None, "main") in res.functions


def test_for_loop_non_int_stop_is_error():
    src = (
        "def main() -> int:\n"
        '    for i in range(0, "x"):\n'
        "        return i\n"
        "    return 0\n"
    )
    err(src)


def test_range_outside_for_is_error():
    # `range` is only meaningful inside a for-header; used as a value it
    # resolves to an unknown builtin call and must be rejected.
    src = (
        "def main() -> int:\n"
        "    x: int = range(0, 5)\n"
        "    return x\n"
    )
    err(src)




# ---- break / continue ----

def test_break_outside_loop_is_error():
    src = (
        "def main() -> int:\n"
        "    break\n"
        "    return 0\n"
    )
    e = err(src)
    assert "break" in e.diagnostic.message and "loop" in e.diagnostic.message


def test_continue_outside_loop_is_error():
    src = (
        "def main() -> int:\n"
        "    continue\n"
        "    return 0\n"
    )
    e = err(src)
    assert "continue" in e.diagnostic.message and "loop" in e.diagnostic.message


def test_break_inside_while_ok():
    src = (
        "def main() -> int:\n"
        "    i: int = 0\n"
        "    while i < 10:\n"
        "        i = i + 1\n"
        "        if i > 3:\n"
        "            break\n"
        "    return i\n"
    )
    check(src)


def test_continue_inside_while_ok():
    src = (
        "def main() -> int:\n"
        "    i: int = 0\n"
        "    while i < 10:\n"
        "        i = i + 1\n"
        "        if i == 2:\n"
        "            continue\n"
        "    return i\n"
    )
    check(src)


def test_break_after_loop_is_error():
    # depth must return to 0 after the while body closes.
    src = (
        "def main() -> int:\n"
        "    i: int = 0\n"
        "    while i < 10:\n"
        "        i = i + 1\n"
        "    break\n"
        "    return i\n"
    )
    e = err(src)
    assert "break" in e.diagnostic.message and "loop" in e.diagnostic.message


# ---- conditional (ternary) expression ----

def test_ternary_int_branches_ok():
    res = check(
        "def main() -> int:\n"
        "    x: int = 1 if true else 2\n"
        "    return x\n"
    )
    assert res.functions[(None, "main")].return_type is T.INT


def test_ternary_string_branches_ok():
    res = check(
        "def main() -> int:\n"
        '    s: string = "yes" if true else "no"\n'
        "    print(s)\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_ternary_non_bool_cond_is_error():
    e = err(
        "def main() -> int:\n"
        "    x: int = 1 if 7 else 2\n"
        "    return x\n"
    )
    assert "bool" in e.diagnostic.message.lower()


def test_ternary_branch_type_mismatch_is_error():
    e = err(
        "def main() -> int:\n"
        '    x: int = 1 if true else "no"\n'
        "    return x\n"
    )
    assert "same type" in e.diagnostic.message.lower()


def test_bitwise_int_ok():
    res = check(
        "def main() -> int:\n"
        "    return (5 & 3) | (1 << 4) ^ ~0\n"
    )
    assert res.functions[(None, "main")].return_type is T.INT


def test_bitwise_on_float_is_error():
    e = err(
        "def main() -> int:\n"
        "    x: int = 1.0 & 2.0\n"
        "    return x\n"
    )
    assert "int" in e.diagnostic.message.lower()


def test_bitnot_on_float_is_error():
    e = err(
        "def main() -> int:\n"
        "    x: int = ~1.0\n"
        "    return x\n"
    )
    assert "int" in e.diagnostic.message.lower()


# ---- math builtins ----

def test_sqrt_returns_float():
    res = check(
        "def main() -> int:\n"
        "    x: float = sqrt(2.0)\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_pow_returns_float():
    res = check(
        "def main() -> int:\n"
        "    x: float = pow(2.0, 3.0)\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_sqrt_int_arg_is_type_error():
    e = err(
        "def main() -> int:\n"
        "    x: float = sqrt(2)\n"
        "    return 0\n"
    )
    assert "sqrt argument must be float" in e.diagnostic.message


def test_pow_wrong_arity_is_error():
    e = err(
        "def main() -> int:\n"
        "    x: float = pow(2.0)\n"
        "    return 0\n"
    )
    assert "pow takes 2 arguments" in e.diagnostic.message


def test_floor_ceil_fabs_return_float():
    res = check(
        "def main() -> int:\n"
        "    a: float = floor(3.7)\n"
        "    b: float = ceil(3.2)\n"
        "    c: float = fabs(0.0 - 1.0)\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_cannot_spawn_sqrt():
    e = err(
        "def main() -> int:\n"
        "    f: Future[float] = spawn sqrt(2.0)\n"
        "    return 0\n"
    )
    assert "cannot spawn the builtin `sqrt`" in e.diagnostic.message


# ---- type aliases ----

def test_type_alias_used_as_int():
    src = (
        "type UserId = int\n"
        "def get(id: UserId) -> int:\n"
        "    return id + 1\n"
        "def main() -> int:\n"
        "    u: UserId = 41\n"
        "    return get(u)\n"
    )
    res = check(src)
    assert res.functions[(None, "get")].params[0][1] is T.INT
    assert res.functions[(None, "get")].return_type is T.INT
    assert res.local_types[(None, "main", "u")] is T.INT


def test_type_alias_of_alias():
    src = (
        "type A = int\n"
        "type B = A\n"
        "def main() -> int:\n"
        "    x: B = 5\n"
        "    return x\n"
    )
    res = check(src)
    assert res.local_types[(None, "main", "x")] is T.INT


def test_type_alias_unknown_target_is_error():
    e = err(
        "type Bad = NoSuchType\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    assert "unknown type" in e.diagnostic.message.lower()


def test_type_alias_duplicate_is_error():
    e = err(
        "type X = int\n"
        "type X = int\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    assert "duplicate" in e.diagnostic.message.lower()


def test_type_alias_cannot_use_builtin_name():
    # Built-in type names are lexer keywords, so `type int = ...` is rejected
    # at parse time (an alias name must be a plain identifier).
    from rwc.parser import ParserError

    with pytest.raises(ParserError):
        check(
            "type int = float\n"
            "def main() -> int:\n"
            "    return 0\n"
        )


# ---- assert ----

def test_assert_bool_ok():
    res = check(
        "def main() -> int:\n"
        "    assert 1 == 1\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_assert_with_message_ok():
    res = check(
        "def main() -> int:\n"
        "    assert true, \"oops\"\n"
        "    return 0\n"
    )
    assert (None, "main") in res.functions


def test_assert_non_bool_cond_is_error():
    e = err(
        "def main() -> int:\n"
        "    assert 1\n"
        "    return 0\n"
    )
    assert "bool" in e.diagnostic.message.lower()


def test_assert_non_string_message_is_error():
    e = err(
        "def main() -> int:\n"
        "    assert true, 5\n"
        "    return 0\n"
    )
    assert "string" in e.diagnostic.message.lower()


# ----- namespaced imports (spec 17) -----

from rwc.loader import load_program
from rwc.sema import analyze_program


def _check_program(tmp_path, entry_src: str, **libs: str):
    for name, src in libs.items():
        (tmp_path / f"{name}.rw").write_text(src, encoding="utf-8")
    entry_path = tmp_path / "main.rw"
    entry_path.write_text(entry_src, encoding="utf-8")
    prog = load_program(entry_src, str(entry_path))
    return analyze_program(prog, filename=str(entry_path))


def test_qualified_call_resolves(tmp_path):
    res = _check_program(
        tmp_path,
        "import lib\n\ndef main() -> int:\n    return lib.add(1, 2)\n",
        lib="def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert (None, "main") in res.functions
    assert ("lib", "add") in res.functions


def test_call_to_unimported_module_errors(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "def main() -> int:\n    return lib.add(1, 2)\n",
            lib="def add(a: int, b: int) -> int:\n    return a + b\n",
        )
    assert "not imported" in ei.value.diagnostic.message


def test_qualified_call_unknown_func_errors(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "import lib\n\ndef main() -> int:\n    return lib.nope(1)\n",
            lib="def add(a: int, b: int) -> int:\n    return a + b\n",
        )
    assert "no function" in ei.value.diagnostic.message


def test_same_name_functions_coexist(tmp_path):
    # Both entry and lib define `helper`; they must not collide.
    res = _check_program(
        tmp_path,
        "import lib\n\ndef helper() -> int:\n    return 1\n\ndef main() -> int:\n    return helper() + lib.helper()\n",
        lib="def helper() -> int:\n    return 2\n",
    )
    assert (None, "helper") in res.functions
    assert ("lib", "helper") in res.functions


def test_qualified_arg_type_mismatch_errors(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            'import lib\n\ndef main() -> int:\n    return lib.add("x", 2)\n',
            lib="def add(a: int, b: int) -> int:\n    return a + b\n",
        )
    assert "argument 1" in ei.value.diagnostic.message


def test_same_name_type_aliases_in_different_modules_coexist(tmp_path):
    res = _check_program(
        tmp_path,
        "import lib\n\ntype Num = int\n\ndef main() -> Num:\n    return lib.f()\n",
        lib="type Num = int\n\ndef f() -> Num:\n    return 1\n",
    )
    assert ("lib", "f") in res.functions


def test_type_alias_is_not_visible_across_modules(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "import lib\n\ndef main() -> Num:\n    return lib.f()\n",
            lib="type Num = int\n\ndef f() -> Num:\n    return 1\n",
        )
    assert "unknown type: Num" in ei.value.diagnostic.message


def test_diagnostic_in_imported_module_names_that_file(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "import lib\n\ndef main() -> int:\n    return lib.f()\n",
            lib='def f() -> int:\n    return "x"\n',
        )
    diag = ei.value.diagnostic
    assert diag.file == str(tmp_path / "lib.rw")
    assert diag.line == 2


# ----- from-imports (spec 17, PR2) -----

def test_from_import_resolves_unqualified(tmp_path):
    res = _check_program(
        tmp_path,
        "from lib import add\n\ndef main() -> int:\n    return add(1, 2)\n",
        lib="def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert (None, "main") in res.functions
    assert ("lib", "add") in res.functions


def test_from_import_alias(tmp_path):
    res = _check_program(
        tmp_path,
        "from lib import add as plus\n\ndef main() -> int:\n    return plus(1, 2)\n",
        lib="def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert ("lib", "add") in res.functions


def test_from_import_unknown_name_errors(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "from lib import nope\n\ndef main() -> int:\n    return nope()\n",
            lib="def add(a: int, b: int) -> int:\n    return a + b\n",
        )
    assert "no function" in ei.value.diagnostic.message


def test_from_import_collides_with_local(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "from lib import add\n\ndef add() -> int:\n    return 9\n\ndef main() -> int:\n    return add(1, 2)\n",
            lib="def add(a: int, b: int) -> int:\n    return a + b\n",
        )
    assert "collides with a local function" in ei.value.diagnostic.message


def test_from_import_collides_with_builtin(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "from lib import print\n\ndef main() -> int:\n    return 0\n",
            lib="def print(a: int) -> int:\n    return a\n",
        )
    assert "collides with a builtin" in ei.value.diagnostic.message


# ----- import-as aliases (spec 17, PR3) -----

def test_import_as_resolves_via_alias(tmp_path):
    res = _check_program(
        tmp_path,
        "import lib as m\n\ndef main() -> int:\n    return m.add(1, 2)\n",
        lib="def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert ("lib", "add") in res.functions


def test_import_as_original_name_not_visible(tmp_path):
    # After `import lib as m`, the bare `lib.` qualifier is no longer valid.
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "import lib as m\n\ndef main() -> int:\n    return lib.add(1, 2)\n",
            lib="def add(a: int, b: int) -> int:\n    return a + b\n",
        )
    assert "not imported" in ei.value.diagnostic.message


def test_duplicate_import_qualifier_errors(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "import lib\nimport other as lib\n\ndef main() -> int:\n    return lib.add(1, 2)\n",
            lib="def add(a: int, b: int) -> int:\n    return a + b\n",
            other="def add(a: int, b: int) -> int:\n    return a - b\n",
        )
    assert "duplicate import qualifier" in ei.value.diagnostic.message


# ----- string search / conversion builtins (spec 18) -----

def test_string_search_builtins_ok():
    check(
        "def main() -> int:\n"
        '    s: string = "GET /x HTTP/1.1"\n'
        '    i: int = str_find(s, " ")\n'
        "    p: string = str_slice(s, 0, i)\n"
        '    b: bool = str_starts_with(s, "GET")\n'
        "    n: string = str_from_int(42)\n"
        '    o: Option[int] = str_to_int("12")\n'
        "    return i\n"
    )


@pytest.mark.parametrize(
    "call, msg",
    [
        ('str_find("a")', "str_find takes 2 arguments"),
        ('str_find("a", 1)', "str_find argument 2 must be string"),
        ('str_slice("a", 0)', "str_slice takes 3 arguments"),
        ('str_slice("a", "0", 1)', "str_slice argument 2 must be int"),
        ("str_starts_with(1, \"a\")", "str_starts_with argument 1 must be string"),
        ('str_from_int("1")', "str_from_int argument 1 must be int"),
        ("str_to_int(1)", "str_to_int argument 1 must be string"),
        ("str_to_int()", "str_to_int takes 1 argument"),
    ],
)
def test_string_search_builtins_reject_bad_args(call, msg):
    e = err(f"def main() -> int:\n    x: int = 0\n    {call}\n    return 0\n")
    assert msg in e.diagnostic.message



# ----- records (spec 20) -----

RECORDS_OK = (
    "class Request:\n    method: string\n    path: string\n\n"
    "class Response:\n    status: int\n    req: Request\n    body: string\n\n"
    "def handle(r: Request) -> Response:\n"
    '    return Response(200, r, "hi " + r.path)\n\n'
    "def main() -> int:\n"
    '    req: Request = Request("GET", "/x")\n'
    "    res: Response = handle(req)\n"
    '    res.body = res.body + "!"\n'
    '    res.req.path = "/y"\n'
    "    print(res.req.path)\n"
    "    return res.status\n"
)


def test_records_ok():
    res = check(RECORDS_OK)
    assert (None, "handle") in res.functions


def test_record_forward_reference_ok():
    check(
        "class A:\n    b: B\n\nclass B:\n    n: int\n\n"
        "def main() -> int:\n    a: A = A(B(1))\n    return a.b.n\n"
    )


def _rec(body: str, extra: str = "") -> CompileError:
    return err(
        "class P:\n    x: int\n    s: string\n\n" + extra
        + "def main() -> int:\n    p: P = P(1, \"a\")\n" + body + "    return 0\n"
    )


@pytest.mark.parametrize(
    "body, msg",
    [
        ("    q: P = P(1)\n", "P takes 2 arguments"),
        ('    q: P = P("1", "a")\n', "field 1 (`x`) of P must be int"),
        ("    print(p.nope)\n", "P has no field `nope`"),
        ("    p.nope = 1\n", "P has no field `nope`"),
        ('    p.x = "a"\n', "cannot assign `string` to field `x`"),
        ("    n: int = 3\n    print(n.x)\n", "`int` has no fields"),
        ("    print(p == p)\n", "cannot compare `P`"),
    ],
)
def test_record_errors(body, msg):
    assert msg in _rec(body).diagnostic.message


@pytest.mark.parametrize(
    "src, msg",
    [
        ("class P:\n    x: int\n\nclass P:\n    y: int\n\n", "duplicate class: P"),
        ("class P:\n    x: int\n    x: int\n\n", "duplicate field `x` in class P"),
        ("class P:\n    x: void\n\n", "field `x` cannot be void"),
        ("class P:\n    x: Nope\n\n", "unknown type: Nope"),
        ("class P:\n    x: int\n\ntype P = int\n\n", "`P` is already a class"),
        ("class P:\n    x: int\n\ndef P() -> int:\n    return 1\n\n", "`P` is already a class"),
        ("class len:\n    x: int\n\n", "class name `len` collides with a builtin"),
    ],
)
def test_record_declaration_errors(src, msg):
    e = err(src + "def main() -> int:\n    return 0\n")
    assert msg in e.diagnostic.message


def test_spawn_returning_record_is_error():
    e = err(
        "class P:\n    x: int\n\ndef mk() -> P:\n    return P(1)\n\n"
        "def main() -> int:\n    f: Future[P] = spawn mk()\n    return 0\n"
    )
    assert "cannot spawn a function returning a record" in e.diagnostic.message


def test_record_needs_import_or_qualifier(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "import lib\n\ndef main() -> int:\n    p: P = lib.mk()\n    return 0\n",
            lib="class P:\n    x: int\n\ndef mk() -> P:\n    return P(1)\n",
        )
    assert "unknown type: P" in ei.value.diagnostic.message


def test_same_class_name_in_two_modules_are_distinct(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "import lib\n\nclass P:\n    x: int\n\ndef take(p: P) -> int:\n    return p.x\n\n"
            "def main() -> int:\n    return take(lib.mk())\n",
            lib="class P:\n    x: int\n\ndef mk() -> P:\n    return P(1)\n",
        )
    assert "argument 1" in ei.value.diagnostic.message


def test_from_import_collides_with_local_class(tmp_path):
    with pytest.raises(CompileError) as ei:
        _check_program(
            tmp_path,
            "from lib import P\n\nclass P:\n    x: int\n\ndef main() -> int:\n    return 0\n",
            lib="def P() -> int:\n    return 1\n",
        )
    assert "collides with a local function or class" in ei.value.diagnostic.message


# ----- importing records (spec 20, "Records across modules") -----

LIB_WEB = (
    "class Request:\n    path: string\n\n"
    "class Response:\n    status: int\n    body: string\n\n"
    "def ok(body: string) -> Response:\n    return Response(200, body)\n"
)


def test_from_import_class(tmp_path):
    _check_program(
        tmp_path,
        "from web import Request, Response, ok\n\n"
        "def hello(req: Request) -> Response:\n    return ok(req.path)\n\n"
        'def main() -> int:\n    r: Response = hello(Request("/x"))\n'
        '    r2: Response = Response(404, "nf")\n    return r.status + r2.status\n',
        web=LIB_WEB,
    )


def test_qualified_class_type_and_constructor(tmp_path):
    _check_program(
        tmp_path,
        "import web\n\n"
        "def hello(req: web.Request) -> web.Response:\n    return web.ok(req.path)\n\n"
        'def main() -> int:\n    r: web.Response = hello(web.Request("/x"))\n    return r.status\n',
        web=LIB_WEB,
    )


def test_aliased_qualified_class(tmp_path):
    _check_program(
        tmp_path,
        'import web as w\n\ndef main() -> int:\n    r: w.Response = w.Response(1, "a")\n    return r.status\n',
        web=LIB_WEB,
    )


@pytest.mark.parametrize(
    "entry, msg",
    [
        ("import web\n\ndef main() -> int:\n    r: web.Nope = web.ok(\"a\")\n    return 0\n",
         "module 'web' has no class `Nope`"),
        ("def main() -> int:\n    r: web.Response = 1\n    return 0\n",
         "module 'web' is not imported here"),
        ("import web\n\ndef main() -> int:\n    r: web.Response = web.Nope(1)\n    return 0\n",
         "module 'web' has no function `Nope`"),
        ("from web import Nope\n\ndef main() -> int:\n    return 0\n",
         "has no function or class `Nope`"),
        ("from web import Request\n\nclass Request:\n    x: int\n\ndef main() -> int:\n    return 0\n",
         "collides with a local function or class"),
        ("from web import Request\n\ndef Request() -> int:\n    return 0\n\ndef main() -> int:\n    return 0\n",
         "collides with a local function or class"),
    ],
)
def test_record_import_errors(tmp_path, entry, msg):
    with pytest.raises(CompileError) as ei:
        _check_program(tmp_path, entry, web=LIB_WEB)
    assert msg in ei.value.diagnostic.message


# ----- function values (spec 21) -----

FV_PRELUDE = (
    "class Request:\n    path: string\n\n"
    "class Response:\n    status: int\n\n"
    "class Route:\n    path: string\n    handler: Callable[[Request], Response]\n\n"
    "def hello(req: Request) -> Response:\n    return Response(200)\n\n"
    "def nf(req: Request) -> Response:\n    return Response(404)\n\n"
)


def test_function_values_ok():
    check(
        FV_PRELUDE
        + "def pick(found: bool) -> Callable[[Request], Response]:\n"
        "    return hello if found else nf\n\n"
        "def apply(h: Callable[[Request], Response], r: Request) -> int:\n"
        "    return h(r).status\n\n"
        "def main() -> int:\n"
        '    req: Request = Request("/")\n'
        '    rt: Route = Route("/", hello)\n'
        "    a: int = rt.handler(req).status\n"
        "    rt.handler = nf\n"
        "    h: Callable[[Request], Response] = pick(true)\n"
        "    b: int = h(req).status + pick(false)(req).status\n"
        "    return a + b + apply(nf, req)\n"
    )


@pytest.mark.parametrize(
    "body, msg",
    [
        ("    h: Callable[[Request], Response] = len\n", "cannot use builtin `len` as a value"),
        ("    h: Callable[[Request], int] = hello\n", "type mismatch"),
        ("    h: Callable[[Request], Response] = hello\n    h(1)\n", "argument 1"),
        ("    h: Callable[[Request], Response] = hello\n    h()\n", "expects 1 argument"),
        ('    rt: Route = Route("/", hello)\n    rt.nope(Request("/"))\n', "Route has no field `nope`"),
        ('    rt: Route = Route("/", hello)\n    rt.path(Request("/"))\n', "`string` is not callable"),
        ("    n: int = 1\n    n.f(1)\n", "`int` has no fields"),
        ("    h: Callable[[Request], Response] = hello\n    print(h == h)\n", "cannot compare"),
        ('    h: Callable[[Request], Response] = hello\n    f: Future[Response] = spawn h(Request("/"))\n',
         "cannot spawn a function value"),
    ],
)
def test_function_value_errors(body, msg):
    e = err(FV_PRELUDE + "def main() -> int:\n" + body + "    return 0\n")
    assert msg in e.diagnostic.message


def test_function_value_from_other_module(tmp_path):
    _check_program(
        tmp_path,
        "import lib\nfrom lib import twice\n\n"
        "def main() -> int:\n"
        "    f: Callable[[int], int] = lib.inc\n"
        "    g: Callable[[int], int] = twice\n"
        "    return f(1) + g(2)\n",
        lib="def inc(x: int) -> int:\n    return x + 1\n\ndef twice(x: int) -> int:\n    return x * 2\n",
    )


# ----- List[T] (spec 22) -----

def test_generic_list_ok():
    check(
        "class Route:\n    path: string\n    tags: List[string]\n\n"
        "def names() -> List[string]:\n    return list_new()\n\n"
        "def first(xs: List[Route]) -> Route:\n    return list_at(xs, 0)\n\n"
        "def main() -> int:\n"
        "    s: List[string] = list_new()\n"
        '    s = list_push(s, "a")\n'
        "    print(list_at(s, 0))\n"
        "    rs: List[Route] = list_new()\n"
        '    rs = list_push(rs, Route("/", list_new()))\n'
        '    first(rs).tags = list_push(first(rs).tags, "x")\n'
        "    nested: List[List[string]] = list_new()\n"
        "    nested = list_push(nested, list_new())\n"
        "    n: int = len(s) + len(rs) + len(list_at(nested, 0)) + len(names())\n"
        "    ints: List[int] = list_new()\n"
        "    legacy: int = len(list_push(list_new(), 1))\n"
        "    return n + legacy\n"
    )


@pytest.mark.parametrize(
    "body, msg",
    [
        ('    s: List[string] = list_new()\n    s = list_push(s, 1)\n',
         "list_push second argument must be string"),
        ("    s: List[string] = list_new()\n    n: int = list_at(s, 0)\n", "type mismatch"),
        ("    s: List[string] = list_new()\n    i: List[int] = s\n", "type mismatch"),
        ("    s: List[string] = list_new()\n    print(s == s)\n", "cannot compare"),
        ("    s: List[string] = list_new()\n    o: Option[int] = list_at_opt(s, 0)\n",
         "list_at_opt first argument must be List[int]"),
        ("    v: List[void] = list_new()\n", "List element type cannot be void"),
    ],
)
def test_generic_list_errors(body, msg):
    e = err("def main() -> int:\n" + body + "    return 0\n")
    assert msg in e.diagnostic.message


# ----- conversions and string ordering (spec 26) -----

def test_conversions_ok():
    check(
        "def main() -> int:\n"
        "    f: float = float(3) + float(2.5)\n"
        "    i: int = int(f) + int(true) + int(7)\n"
        '    s: string = str(i) + str(f) + str(false) + str("x")\n'
        '    b: bool = "a" < "b" and "b" >= "b" and s > "" and "z" <= "zz"\n'
        "    return i\n"
    )


@pytest.mark.parametrize(
    "expr, msg",
    [
        ('int("1")', "int argument must be int, float or bool"),
        ("float(true)", "float argument must be int or float"),
        ("str(bytes_from_str(\"a\"))", "str argument must be int, float, bool or string"),
        ("int(1, 2)", "int takes 1 argument"),
        ('"a" < 1', "requires int or float on both sides, or two strings"),
    ],
)
def test_conversion_errors(expr, msg):
    e = err(f"def main() -> int:\n    print({expr})\n    return 0\n")
    assert msg in e.diagnostic.message


# ----- string builtins (spec 28) -----

def test_string_builtins_ok():
    check(
        "def main() -> int:\n"
        '    parts: List[string] = str_split("a,b", ",")\n'
        '    s: string = str_join(parts, "-") + str_strip("  x ") + str_replace("aa", "a", "b")\n'
        "    s = str_lower(s) + str_upper(s)\n"
        '    b: bool = str_contains(s, "x") and str_ends_with(s, "X")\n'
        "    return len(parts)\n"
    )


@pytest.mark.parametrize(
    "expr, msg",
    [
        ('str_split("a")', "str_split takes 2 arguments"),
        ('str_join("a", ",")', "str_join argument 1 must be List[string]"),
        ("str_lower(1)", "str_lower argument 1 must be string"),
        ('str_replace("a", "b")', "str_replace takes 3 arguments"),
    ],
)
def test_string_builtin_errors(expr, msg):
    e = err(f"def main() -> int:\n    print(len({expr}))\n    return 0\n")
    assert msg in e.diagnostic.message


# ----- list syntax (spec 29) -----

def test_list_syntax_ok():
    check(
        "class P:\n    name: string\n\n"
        "def main() -> int:\n"
        '    names: List[string] = ["a", "b"]\n'
        "    empty: List[string] = []\n"
        "    grid: List[List[int]] = [[], [1, 2]]\n"
        '    ps: List[P] = [P("x")]\n'
        "    n: int = len(names[1:]) + len(names[:-1]) + grid[1][0]\n"
        '    s: string = names[0] + names[-1] + "word"[1:3] + "word"[0] + ps[0].name\n'
        "    for name in names:\n"
        "        print(name)\n"
        '    for ch in "abc":\n'
        "        print(ch)\n"
        "    for p in ps:\n"
        "        print(p.name)\n"
        "    return n\n"
    )


@pytest.mark.parametrize(
    "body, msg",
    [
        ('    xs: List[int] = [1, "a"]\n', "list element 2 must be int"),
        ("    n: int = 5\n    print(n[0])\n", "cannot index `int`"),
        ('    xs: List[int] = [1]\n    print(xs["a"])\n', "index must be int"),
        ("    xs: List[int] = [1]\n    print(xs[0:\"a\"])\n", "slice bounds must be int"),
        ("    n: int = 5\n    for x in n:\n        print(x)\n", "cannot iterate over `int`"),
        ("    xs: List[int] = [1]\n    x: int = 0\n    for x in xs:\n        print(x)\n", "already declared"),
    ],
)
def test_list_syntax_errors(body, msg):
    e = err("def main() -> int:\n" + body + "    return 0\n")
    assert msg in e.diagnostic.message



# ----- generic Option[T] / Result[T, E] (spec 30) -----

def test_generic_option_result_ok():
    check(
        "class User:\n    name: string\n\n"
        "def find(name: string) -> Option[User]:\n"
        '    if name == "ann":\n'
        "        return Some(User(name))\n"
        "    return None\n\n"
        "def parse(s: string) -> Result[int, string]:\n"
        "    match str_to_int(s):\n"
        "        case Some(n):\n"
        "            return Ok(n)\n"
        "        case None:\n"
        '            return Err("not a number: " + s)\n\n'
        "def main() -> int:\n"
        '    u: Option[User] = find("ann")\n'
        "    match u:\n"
        "        case Some(found):\n"
        "            print(found.name)\n"
        "        case None:\n"
        '            print("none")\n'
        '    match parse("12"):\n'
        "        case Ok(v):\n"
        "            print(v + 1)\n"
        "        case Err(msg):\n"
        "            print(msg)\n"
        '    names: List[Option[string]] = [Some("a"), None]\n'
        '    o: Option[string] = Some("x") if true else None\n'
        "    r: Result[float, bool] = Err(false)\n"
        "    legacy: Option[int] = None\n"
        "    return 0\n"
    )


@pytest.mark.parametrize(
    "body, msg",
    [
        ('    o: Option[string] = Some(1)\n', "Some argument must be string"),
        ('    r: Result[string, int] = Ok(1)\n', "Ok argument must be string"),
        ('    r: Result[string, int] = Err("x")\n', "Err argument must be int"),
        ("    o: Option[string] = None\n    p: Option[int] = o\n", "type mismatch"),
        ("    v: Option[void] = None\n", "Option payload cannot be void"),
    ],
)
def test_generic_option_result_errors(body, msg):
    e = err("def main() -> int:\n" + body + "    return 0\n")
    assert msg in e.diagnostic.message


@pytest.mark.parametrize("ret", ["Option[int]", "Result[string, int]", "List[int]"])
def test_spawn_with_unsupported_return_type_is_error(ret):
    e = err(f"def f() -> {ret}:\n    return list_new()\n\ndef main() -> int:\n    spawn f()\n    return 0\n"
            if ret.startswith("List") else
            f"def f() -> {ret}:\n    return None\n\ndef main() -> int:\n    spawn f()\n    return 0\n"
            if ret.startswith("Option") else
            f"def f() -> {ret}:\n    return Err(1)\n\ndef main() -> int:\n    spawn f()\n    return 0\n")
    assert f"cannot spawn a function returning `{ret}`" in e.diagnostic.message


# ----- Dict[K, V] (spec 31) -----

def test_dict_ok():
    check(
        "class User:\n    name: string\n\n"
        "def count(words: List[string]) -> Dict[string, int]:\n"
        "    c: Dict[string, int] = dict_new()\n"
        "    for w in words:\n"
        "        if dict_has(c, w):\n"
        "            c[w] += 1\n"
        "        else:\n"
        "            c[w] = 1\n"
        "    return c\n\n"
        "def main() -> int:\n"
        '    c: Dict[string, int] = count(["a", "b"])\n'
        "    users: Dict[int, User] = dict_new()\n"
        '    users[1] = User("ann")\n'
        "    n: int = len(c) + len(dict_keys(c)) + len(dict_values(users))\n"
        "    for k in c:\n"
        "        print(k + str(c[k]))\n"
        "    match dict_get(users, 1):\n"
        "        case Some(u):\n"
        "            print(u.name)\n"
        "        case None:\n"
        '            print("none")\n'
        "    removed: bool = dict_remove(users, 1)\n"
        "    return n\n"
    )


@pytest.mark.parametrize(
    "body, msg",
    [
        ("    d: Dict[float, int] = dict_new()\n", "Dict keys must be int or string"),
        ("    d: Dict[string, int] = dict_new()\n    d[1] = 2\n", "Dict[string, int] key must be string"),
        ('    d: Dict[string, int] = dict_new()\n    d["a"] = "b"\n', "cannot assign `string` to an element of Dict[string, int]"),
        ("    xs: List[int] = [1]\n    xs[0] = 2\n", "lists and strings are immutable"),
        ("    print(len(dict_new()))\n", "cannot infer the type of dict_new()"),
        ('    d: Dict[string, int] = dict_new()\n    print(dict_get(d, 1))\n', "dict_get argument 2 must be string"),
        ("    n: int = 3\n    print(dict_has(n, 1))\n", "dict_has argument 1 must be a Dict"),
    ],
)
def test_dict_errors(body, msg):
    e = err("def main() -> int:\n" + body + "    return 0\n")
    assert msg in e.diagnostic.message


def test_user_function_named_like_dict_builtin_prefix():
    check("def dict_size(n: int) -> int:\n    return n\n\ndef main() -> int:\n    return dict_size(3)\n")
