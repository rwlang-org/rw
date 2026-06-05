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
    assert "match target must be Option[int]" in e.diagnostic.message


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
    assert "takes_res" in res.functions
    assert res.functions["takes_res"].params[0][1] is T.RESULT_INT_INT


def test_result_with_non_int_param_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    src = (
        "def f(r: Result[string, int]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "Result[int, int]" in str(ei.value) or "only Result" in str(ei.value)


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
    assert "match target must be Result[int, int]" in e.diagnostic.message


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
    assert "main" in res.functions


def test_write_returns_int():
    res = check(
        "def main() -> int:\n"
        "    fd: int = file_open(\"/tmp/x\", \"w\")\n"
        "    n: int = write(fd, bytes_from_str(\"hi\"))\n"
        "    return 0\n"
    )
    assert "main" in res.functions


def test_close_returns_int():
    res = check(
        "def main() -> int:\n"
        "    fd: int = file_open(\"/tmp/x\", \"r\")\n"
        "    rc: int = close(fd)\n"
        "    return 0\n"
    )
    assert "main" in res.functions


def test_file_open_returns_int():
    res = check(
        "def main() -> int:\n"
        "    fd: int = file_open(\"/tmp/x\", \"w\")\n"
        "    return 0\n"
    )
    assert "main" in res.functions


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
    assert "main" in res.functions


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
    assert res.functions["main"].return_type is T.INT


def test_ternary_string_branches_ok():
    res = check(
        "def main() -> int:\n"
        '    s: string = "yes" if true else "no"\n'
        "    print(s)\n"
        "    return 0\n"
    )
    assert "main" in res.functions


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
