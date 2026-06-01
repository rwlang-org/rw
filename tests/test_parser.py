from __future__ import annotations

import pytest

from rwc import ast_nodes as A
from rwc.lexer import tokenize
from rwc.parser import ParserError, parse


def parse_src(src: str) -> A.Module:
    return parse(tokenize(src))


def test_parse_empty_module():
    mod = parse_src("")
    assert mod.functions == []


def test_parse_minimal_function():
    src = "def main() -> int:\n    return 0\n"
    mod = parse_src(src)
    assert len(mod.functions) == 1
    f = mod.functions[0]
    assert f.name == "main"
    assert f.params == []
    assert isinstance(f.return_type, A.TypeName) and f.return_type.name == "int"
    assert len(f.body) == 1
    assert isinstance(f.body[0], A.Return)
    assert isinstance(f.body[0].value, A.IntLit) and f.body[0].value.value == 0


def test_parse_function_with_params():
    src = "def add(a: int, b: int) -> int:\n    return a + b\n"
    mod = parse_src(src)
    f = mod.functions[0]
    assert [p.name for p in f.params] == ["a", "b"]
    assert all(isinstance(p.type_expr, A.TypeName) and p.type_expr.name == "int" for p in f.params)
    ret = f.body[0]
    assert isinstance(ret, A.Return)
    assert isinstance(ret.value, A.BinOp) and ret.value.op == "+"


def test_parse_var_decl_and_assign():
    src = "def f() -> void:\n    x: int = 1\n    x = x + 1\n"
    mod = parse_src(src)
    body = mod.functions[0].body
    assert isinstance(body[0], A.VarDecl)
    assert body[0].name == "x"
    assert isinstance(body[1], A.Assign)
    assert body[1].name == "x"
    assert isinstance(body[1].value, A.BinOp)


def test_parse_operator_precedence():
    # 1 + 2 * 3 should parse as 1 + (2 * 3)
    src = "def f() -> int:\n    return 1 + 2 * 3\n"
    mod = parse_src(src)
    ret = mod.functions[0].body[0]
    assert isinstance(ret, A.Return)
    e = ret.value
    assert isinstance(e, A.BinOp) and e.op == "+"
    assert isinstance(e.left, A.IntLit) and e.left.value == 1
    assert isinstance(e.right, A.BinOp) and e.right.op == "*"


def test_parse_comparison_and_logical():
    src = "def f() -> bool:\n    return a and b or not c == 0\n"
    mod = parse_src(src)
    ret = mod.functions[0].body[0]
    assert isinstance(ret, A.Return)
    # top level: or
    top = ret.value
    assert isinstance(top, A.BinOp) and top.op == "or"
    # left: a and b
    assert isinstance(top.left, A.BinOp) and top.left.op == "and"
    # right: not (c == 0)
    assert isinstance(top.right, A.UnaryOp) and top.right.op == "not"
    inner = top.right.operand
    assert isinstance(inner, A.BinOp) and inner.op == "=="


def test_parse_if_elif_else_normalized_into_nested_if():
    src = (
        "def f(x: int) -> void:\n"
        "    if x > 0:\n"
        "        return\n"
        "    elif x == 0:\n"
        "        return\n"
        "    else:\n"
        "        return\n"
    )
    mod = parse_src(src)
    body = mod.functions[0].body
    if_stmt = body[0]
    assert isinstance(if_stmt, A.If)
    # then body
    assert len(if_stmt.then_body) == 1
    # else_body should contain a nested If (from elif)
    assert len(if_stmt.else_body) == 1
    nested = if_stmt.else_body[0]
    assert isinstance(nested, A.If)
    assert len(nested.else_body) == 1
    # innermost else
    inner = nested.else_body[0]
    assert isinstance(inner, A.Return)


def test_parse_while_loop():
    src = "def f() -> void:\n    while i < 10:\n        i = i + 1\n"
    mod = parse_src(src)
    body = mod.functions[0].body
    w = body[0]
    assert isinstance(w, A.While)
    assert isinstance(w.cond, A.BinOp) and w.cond.op == "<"
    assert isinstance(w.body[0], A.Assign)


def test_parse_call_expression():
    src = "def main() -> int:\n    return add(1, 2)\n"
    mod = parse_src(src)
    ret = mod.functions[0].body[0]
    assert isinstance(ret, A.Return)
    call = ret.value
    assert isinstance(call, A.Call) and call.callee == "add"
    assert len(call.args) == 2


def test_parse_spawn_and_await():
    src = (
        "def f() -> int:\n"
        "    fu: Future[int] = spawn add(3, 4)\n"
        "    return await fu\n"
    )
    mod = parse_src(src)
    body = mod.functions[0].body
    decl = body[0]
    assert isinstance(decl, A.VarDecl)
    assert isinstance(decl.type_expr, A.TypeFuture)
    assert isinstance(decl.type_expr.inner, A.TypeName) and decl.type_expr.inner.name == "int"
    assert isinstance(decl.value, A.SpawnExpr)
    assert decl.value.call.callee == "add"

    ret = body[1]
    assert isinstance(ret, A.Return)
    assert isinstance(ret.value, A.AwaitExpr)


def test_spawn_requires_call():
    src = "def f() -> int:\n    x: Future[int] = spawn 42\n"
    with pytest.raises(ParserError):
        parse_src(src)


def test_module_must_start_with_def():
    src = "x: int = 1\n"
    with pytest.raises(ParserError):
        parse_src(src)


def test_parenthesized_expr():
    src = "def f() -> int:\n    return (1 + 2) * 3\n"
    mod = parse_src(src)
    ret = mod.functions[0].body[0]
    assert isinstance(ret, A.Return)
    top = ret.value
    assert isinstance(top, A.BinOp) and top.op == "*"
    assert isinstance(top.left, A.BinOp) and top.left.op == "+"


def test_unary_minus():
    src = "def f() -> int:\n    return -1 + -2\n"
    mod = parse_src(src)
    ret = mod.functions[0].body[0]
    assert isinstance(ret, A.Return)
    top = ret.value
    assert isinstance(top, A.BinOp) and top.op == "+"
    assert isinstance(top.left, A.UnaryOp) and top.left.op == "-"
    assert isinstance(top.right, A.UnaryOp) and top.right.op == "-"


def test_parse_for_two_args():
    src = "def main() -> int:\n    for i in range(0, 10):\n        return i\n"
    mod = parse_src(src)
    f = mod.functions[0]
    loop = f.body[0]
    assert isinstance(loop, A.For)
    assert loop.var == "i"
    assert isinstance(loop.start, A.IntLit) and loop.start.value == 0
    assert isinstance(loop.stop, A.IntLit) and loop.stop.value == 10
    # step defaults to literal 1
    assert isinstance(loop.step, A.IntLit) and loop.step.value == 1


def test_parse_for_one_arg():
    src = "def main() -> int:\n    for i in range(5):\n        return i\n"
    loop = parse_src(src).functions[0].body[0]
    assert isinstance(loop, A.For)
    assert isinstance(loop.start, A.IntLit) and loop.start.value == 0
    assert isinstance(loop.stop, A.IntLit) and loop.stop.value == 5
    assert isinstance(loop.step, A.IntLit) and loop.step.value == 1


def test_parse_for_three_args():
    src = "def main() -> int:\n    for i in range(10, 0, -1):\n        return i\n"
    loop = parse_src(src).functions[0].body[0]
    assert isinstance(loop, A.For)
    assert isinstance(loop.stop, A.IntLit) and loop.stop.value == 0
    # step is unary minus on 1
    assert isinstance(loop.step, A.UnaryOp) and loop.step.op == "-"


def test_parse_for_zero_args_is_error():
    src = "def main() -> int:\n    for i in range():\n        return i\n"
    with pytest.raises(ParserError):
        parse_src(src)


def test_parse_for_four_args_is_error():
    src = "def main() -> int:\n    for i in range(0, 1, 2, 3):\n        return i\n"
    with pytest.raises(ParserError):
        parse_src(src)


def test_parse_ternary_expr():
    src = "def main() -> int:\n    x: int = 1 if true else 2\n    return x\n"
    decl = parse_src(src).functions[0].body[0]
    assert isinstance(decl, A.VarDecl)
    e = decl.value
    assert isinstance(e, A.IfExpr)
    assert isinstance(e.then, A.IntLit) and e.then.value == 1
    assert isinstance(e.cond, A.BoolLit) and e.cond.value is True
    assert isinstance(e.els, A.IntLit) and e.els.value == 2


def test_parse_ternary_is_right_associative():
    # a if p else b if q else c  ==  a if p else (b if q else c)
    src = "def main() -> int:\n    x: int = 1 if true else 2 if false else 3\n    return x\n"
    e = parse_src(src).functions[0].body[0].value
    assert isinstance(e, A.IfExpr)
    assert isinstance(e.then, A.IntLit) and e.then.value == 1
    # else branch is itself a conditional expression
    assert isinstance(e.els, A.IfExpr)
    assert isinstance(e.els.then, A.IntLit) and e.els.then.value == 2
    assert isinstance(e.els.els, A.IntLit) and e.els.els.value == 3


def test_parse_ternary_missing_else_is_error():
    src = "def main() -> int:\n    x: int = 1 if true\n    return x\n"
    with pytest.raises(ParserError):
        parse_src(src)
