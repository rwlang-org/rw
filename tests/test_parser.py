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


def test_parse_break_in_while():
    src = (
        "def f() -> void:\n"
        "    while i < 10:\n"
        "        break\n"
    )
    mod = parse_src(src)
    w = mod.functions[0].body[0]
    assert isinstance(w, A.While)
    assert isinstance(w.body[0], A.Break)


def test_parse_continue_in_while():
    src = (
        "def f() -> void:\n"
        "    while i < 10:\n"
        "        continue\n"
    )
    mod = parse_src(src)
    w = mod.functions[0].body[0]
    assert isinstance(w, A.While)
    assert isinstance(w.body[0], A.Continue)


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


def test_parse_bitwise_precedence_or_below_and():
    # 1 | 2 & 3  ==  1 | (2 & 3)  ( & binds tighter than | )
    src = "def f() -> int:\n    return 1 | 2 & 3\n"
    ret = parse_src(src).functions[0].body[0]
    assert isinstance(ret, A.Return)
    top = ret.value
    assert isinstance(top, A.BinOp) and top.op == "|"
    assert isinstance(top.left, A.IntLit) and top.left.value == 1
    assert isinstance(top.right, A.BinOp) and top.right.op == "&"
    assert isinstance(top.right.left, A.IntLit) and top.right.left.value == 2
    assert isinstance(top.right.right, A.IntLit) and top.right.right.value == 3


def test_parse_shift_binds_looser_than_add():
    # 1 + 2 << 3  ==  (1 + 2) << 3  ( + binds tighter than << )
    src = "def f() -> int:\n    return 1 + 2 << 3\n"
    ret = parse_src(src).functions[0].body[0]
    assert isinstance(ret, A.Return)
    top = ret.value
    assert isinstance(top, A.BinOp) and top.op == "<<"
    assert isinstance(top.left, A.BinOp) and top.left.op == "+"
    assert isinstance(top.left.left, A.IntLit) and top.left.left.value == 1
    assert isinstance(top.left.right, A.IntLit) and top.left.right.value == 2
    assert isinstance(top.right, A.IntLit) and top.right.value == 3


def test_parse_unary_tilde():
    src = "def f() -> int:\n    return ~0\n"
    ret = parse_src(src).functions[0].body[0]
    assert isinstance(ret, A.Return)
    e = ret.value
    assert isinstance(e, A.UnaryOp) and e.op == "~"
    assert isinstance(e.operand, A.IntLit) and e.operand.value == 0


def test_parse_type_alias():
    src = (
        "type Foo = int\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    mod = parse_src(src)
    assert len(mod.type_aliases) == 1
    alias = mod.type_aliases[0]
    assert isinstance(alias, A.TypeAlias)
    assert alias.name == "Foo"
    assert isinstance(alias.target, A.TypeName)
    assert alias.target.name == "int"


def test_parse_assert_without_message():
    src = "def f() -> void:\n    assert x\n"
    stmt = parse_src(src).functions[0].body[0]
    assert isinstance(stmt, A.Assert)
    assert isinstance(stmt.cond, A.Name) and stmt.cond.name == "x"
    assert stmt.msg is None


def test_parse_assert_with_message():
    src = "def f() -> void:\n    assert x, \"msg\"\n"
    stmt = parse_src(src).functions[0].body[0]
    assert isinstance(stmt, A.Assert)
    assert isinstance(stmt.cond, A.Name) and stmt.cond.name == "x"
    assert isinstance(stmt.msg, A.StringLit) and stmt.msg.value == "msg"


def test_parse_import_statement():
    src = "import math_lib\n\ndef main() -> int:\n    return 0\n"
    mod = parse_src(src)
    assert len(mod.imports) == 1
    imp = mod.imports[0]
    assert isinstance(imp, A.Import)
    assert imp.module == "math_lib"
    assert imp.alias is None and imp.names is None


def test_parse_qualified_call():
    src = "def main() -> int:\n    return math_lib.add(1, 2)\n"
    mod = parse_src(src)
    ret = mod.functions[0].body[0]
    assert isinstance(ret, A.Return)
    call = ret.value
    assert isinstance(call, A.Call)
    assert call.module == "math_lib"
    assert call.callee == "add"
    assert len(call.args) == 2


def test_unqualified_call_has_no_module():
    src = "def main() -> int:\n    return add(1, 2)\n"
    mod = parse_src(src)
    call = mod.functions[0].body[0].value
    assert isinstance(call, A.Call) and call.module is None


def test_import_after_def_is_error():
    src = "def main() -> int:\n    return 0\n\nimport math_lib\n"
    with pytest.raises(ParserError):
        parse_src(src)


def test_parse_import_as_alias():
    src = "import math_lib as m\n\ndef main() -> int:\n    return m.add(1, 2)\n"
    mod = parse_src(src)
    assert len(mod.imports) == 1
    imp = mod.imports[0]
    assert imp.module == "math_lib"
    assert imp.alias == "m"
    assert imp.names is None
    call = mod.functions[0].body[0].value
    assert isinstance(call, A.Call) and call.module == "m" and call.callee == "add"


def test_parse_from_import_single():
    src = "from math_lib import add\n\ndef main() -> int:\n    return add(1, 2)\n"
    mod = parse_src(src)
    assert len(mod.imports) == 1
    imp = mod.imports[0]
    assert imp.module == "math_lib"
    assert imp.alias is None
    assert imp.names == [("add", None)]


def test_parse_from_import_multiple_with_alias():
    src = "from math_lib import add, mul as m\n\ndef main() -> int:\n    return add(1, m(2, 3))\n"
    mod = parse_src(src)
    imp = mod.imports[0]
    assert imp.names == [("add", None), ("mul", "m")]


def test_from_import_after_def_is_error():
    src = "def main() -> int:\n    return 0\n\nfrom lib import add\n"
    with pytest.raises(ParserError):
        parse_src(src)


# ----- records (spec 20) -----

def test_parse_class_def():
    mod = parse_src("class Point:\n    x: int\n    label: string\n\ndef main() -> int:\n    return 0\n")
    assert len(mod.classes) == 1
    c = mod.classes[0]
    assert c.name == "Point"
    assert [(f.name, f.type_expr.name) for f in c.fields] == [("x", "int"), ("label", "string")]


def test_parse_class_requires_fields():
    with pytest.raises(ParserError):
        parse_src("class Empty:\n\ndef main() -> int:\n    return 0\n")


def test_parse_import_after_class_is_error():
    with pytest.raises(ParserError):
        parse_src("class P:\n    x: int\nimport lib\n")


def test_parse_field_access_chain():
    mod = parse_src("def main() -> int:\n    return a.b.c\n")
    expr = mod.functions[0].body[0].value
    assert isinstance(expr, A.FieldAccess) and expr.field == "c"
    assert isinstance(expr.obj, A.FieldAccess) and expr.obj.field == "b"
    assert isinstance(expr.obj.obj, A.Name) and expr.obj.obj.name == "a"


def test_parse_field_assign():
    mod = parse_src("def main() -> int:\n    p.x = 1\n    p.q.y = 2\n    return 0\n")
    s0, s1 = mod.functions[0].body[0], mod.functions[0].body[1]
    assert isinstance(s0, A.FieldAssign) and s0.field == "x"
    assert isinstance(s0.obj, A.Name) and s0.obj.name == "p"
    assert isinstance(s1, A.FieldAssign) and s1.field == "y"
    assert isinstance(s1.obj, A.FieldAccess)


def test_parse_qualified_call_still_call():
    mod = parse_src("def main() -> int:\n    return lib.f(1).x\n")
    expr = mod.functions[0].body[0].value
    assert isinstance(expr, A.FieldAccess)
    assert isinstance(expr.obj, A.Call) and expr.obj.module == "lib" and expr.obj.callee == "f"


def test_parse_assign_to_call_is_error():
    with pytest.raises(ParserError):
        parse_src("def main() -> int:\n    f() = 1\n    return 0\n")


def test_parse_qualified_type_name():
    mod = parse_src("def f(r: web.Request) -> web.Response:\n    return r\n")
    fn = mod.functions[0]
    assert (fn.params[0].type_expr.module, fn.params[0].type_expr.name) == ("web", "Request")
    assert (fn.return_type.module, fn.return_type.name) == ("web", "Response")


# ----- function values (spec 21) -----

def test_parse_callable_type():
    mod = parse_src("def f(h: Callable[[int, string], bool], g: Callable[[], void]) -> int:\n    return 0\n")
    h, g = mod.functions[0].params
    assert isinstance(h.type_expr, A.TypeCallable)
    assert [p.name for p in h.type_expr.params] == ["int", "string"]
    assert h.type_expr.ret.name == "bool"
    assert g.type_expr.params == [] and g.type_expr.ret.name == "void"


def test_parse_call_on_field_and_call_result():
    mod = parse_src("def main() -> int:\n    a.b.c(1)\n    pick()(2)\n    return 0\n")
    e0 = mod.functions[0].body[0].expr
    e1 = mod.functions[0].body[1].expr
    assert isinstance(e0, A.CallValue) and isinstance(e0.callee, A.FieldAccess)
    assert isinstance(e1, A.CallValue) and isinstance(e1.callee, A.Call)


def test_parse_name_dot_call_stays_qualified_call():
    mod = parse_src("def main() -> int:\n    r.handler(1)\n    return 0\n")
    e = mod.functions[0].body[0].expr
    assert isinstance(e, A.Call) and e.module == "r" and e.callee == "handler"


# ----- List[T] (spec 22) -----

def test_parse_generic_list_types():
    mod = parse_src("def f(a: List[string], b: List[List[Route]], c: List[int]) -> int:\n    return 0\n")
    a, b, c = (p.type_expr for p in mod.functions[0].params)
    assert isinstance(a, A.TypeList) and a.elem.name == "string"
    assert isinstance(b, A.TypeList) and isinstance(b.elem, A.TypeList) and b.elem.elem.name == "Route"
    assert isinstance(c, A.TypeName) and c.name == "List[int]"  # unchanged
