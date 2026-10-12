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


def test_module_level_statement_is_error():
    # `x: int = 1` is now a constant (spec 35); other statements are not
    # allowed at module level.
    with pytest.raises(ParserError):
        parse_src("print(1)\n")


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


# ----- conversions (spec 26) -----

def test_parse_int_and_float_conversion_calls():
    mod = parse_src("def main() -> int:\n    f: float = float(1)\n    return int(f)\n")
    decl, ret = mod.functions[0].body
    assert isinstance(decl.value, A.Call) and decl.value.callee == "float"
    assert isinstance(ret.value, A.Call) and ret.value.callee == "int"


# ----- compound assignment (spec 27) -----

@pytest.mark.parametrize("op", ["+", "-", "*", "/", "%", "&", "|", "^"])
def test_parse_compound_assign_local(op):
    mod = parse_src(f"def main() -> int:\n    x: int = 1\n    x {op}= 2\n    return x\n")
    s = mod.functions[0].body[1]
    assert isinstance(s, A.Assign) and s.name == "x"
    assert isinstance(s.value, A.BinOp) and s.value.op == op
    assert isinstance(s.value.left, A.Name) and s.value.left.name == "x"


def test_parse_compound_assign_field():
    mod = parse_src("def main() -> int:\n    a.b.total -= 3\n    return 0\n")
    s = mod.functions[0].body[0]
    assert isinstance(s, A.FieldAssign) and s.field == "total"
    assert isinstance(s.value, A.BinOp) and s.value.op == "-"
    assert isinstance(s.value.left, A.FieldAccess) and s.value.left.field == "total"


def test_parse_compound_assign_through_call_is_error():
    with pytest.raises(ParserError):
        parse_src("def main() -> int:\n    f().x += 1\n    return 0\n")


# ----- list syntax (spec 29) -----

def test_parse_list_literal_index_slice():
    mod = parse_src(
        "def main() -> int:\n"
        "    xs: List[int] = [1, 2, 3]\n"
        "    e: List[int] = []\n"
        "    a: int = xs[0]\n"
        "    b: List[int] = xs[1:]\n"
        "    c: List[int] = xs[:-1]\n"
        "    d: List[int] = xs[:]\n"
        "    return xs[1:2][0]\n"
    )
    body = mod.functions[0].body
    assert isinstance(body[0].value, A.ListLit) and len(body[0].value.elems) == 3
    assert isinstance(body[1].value, A.ListLit) and body[1].value.elems == []
    assert isinstance(body[2].value, A.Index)
    s1, s2, s3 = body[3].value, body[4].value, body[5].value
    assert isinstance(s1, A.Slice) and s1.start is not None and s1.stop is None
    assert isinstance(s2, A.Slice) and s2.start is None and isinstance(s2.stop, A.UnaryOp)
    assert isinstance(s3, A.Slice) and s3.start is None and s3.stop is None
    ret = body[6].value
    assert isinstance(ret, A.Index) and isinstance(ret.obj, A.Slice)


def test_parse_for_each():
    mod = parse_src("def main() -> int:\n    for x in xs:\n        print(x)\n    return 0\n")
    loop = mod.functions[0].body[0]
    assert isinstance(loop, A.ForEach) and loop.var == "x"
    assert isinstance(loop.iterable, A.Name) and loop.iterable.name == "xs"


def test_parse_index_assignment():
    # Parsed for every object; sema accepts it only for dicts (spec 31).
    mod = parse_src("def main() -> int:\n    d[k] = 1\n    d[\"x\"] += 2\n    return 0\n")
    s0, s1 = mod.functions[0].body[0], mod.functions[0].body[1]
    assert isinstance(s0, A.IndexAssign) and isinstance(s0.index, A.Name)
    assert isinstance(s1, A.IndexAssign) and isinstance(s1.value, A.BinOp)
    assert isinstance(s1.value.left, A.Index)


def test_parse_dict_type():
    mod = parse_src("def f(d: Dict[string, List[string]]) -> int:\n    return 0\n")
    t = mod.functions[0].params[0].type_expr
    assert isinstance(t, A.TypeDict) and t.key.name == "string"
    assert isinstance(t.value, A.TypeList)


def test_parse_slice_assignment_is_error():
    with pytest.raises(ParserError):
        parse_src("def main() -> int:\n    xs[0:1] = 1\n    return 0\n")


# ----- methods (spec 33) -----

def test_parse_class_methods_lowered_to_functions():
    mod = parse_src(
        "class Counter:\n    count: int\n\n"
        "    def inc(self, by: int) -> int:\n        return by\n\n"
        "    def reset(self) -> void:\n        return\n"
    )
    c = mod.classes[0]
    assert [f.name for f in c.fields] == ["count"]
    assert c.methods == ["inc", "reset"]
    names = [fn.name for fn in mod.functions]
    assert names == ["Counter.inc", "Counter.reset"]
    inc = mod.functions[0]
    assert [p.name for p in inc.params] == ["self", "by"]
    assert inc.params[0].type_expr.name == "Counter"


def test_parse_method_requires_self():
    with pytest.raises(ParserError):
        parse_src("class C:\n    x: int\n\n    def m(a: int) -> int:\n        return a\n")


# ----- f-strings (spec 34) -----

def test_parse_fstring_lowered_to_concat():
    mod = parse_src('def main() -> int:\n    s: string = f"x={x}!"\n    return 0\n')
    e = mod.functions[0].body[0].value
    # ("x=" + str(x)) + "!"
    assert isinstance(e, A.BinOp) and e.op == "+"
    assert isinstance(e.right, A.StringLit) and e.right.value == "!"
    assert isinstance(e.left.right, A.Call) and e.left.right.callee == "str"


def test_parse_fstring_format_spec_is_error():
    with pytest.raises(ParserError) as ei:
        parse_src('def main() -> int:\n    s: string = f"{x:.2f}"\n    return 0\n')
    assert "format specs are not supported" in str(ei.value)


# ----- module constants (spec 35) -----

def test_parse_constants():
    mod = parse_src('LIMIT: int = 10\nNEG: float = -1.5\nNAME: string = "x"\n\ndef main() -> int:\n    return LIMIT\n')
    assert [c.name for c in mod.constants] == ["LIMIT", "NEG", "NAME"]


@pytest.mark.parametrize("src", ["X: int = 1 + 2\n", "X: int = f()\n", 'X: string = -"a"\n'])
def test_parse_constant_must_be_literal(src):
    with pytest.raises(ParserError) as ei:
        parse_src(src)
    assert "must be initialised with a literal" in str(ei.value)


# ----- dict literals (spec 36) -----

def test_parse_dict_literal():
    mod = parse_src('def main() -> int:\n    d = {"a": 1, "b": 2}\n    e: Dict[string, int] = {}\n    return 0\n')
    d, e = mod.functions[0].body[0].value, mod.functions[0].body[1].value
    assert isinstance(d, A.DictLit) and len(d.keys) == 2
    assert isinstance(e, A.DictLit) and e.keys == []


# ----- tuples (spec 37) -----

def test_parse_tuples():
    mod = parse_src(
        "def f() -> tuple[int, string]:\n    return 1, \"a\"\n\n"
        "def main() -> int:\n    t: Tuple[int, bool] = (1, true)\n    a, _ = f()\n    x = (1)\n    return t[0]\n"
    )
    f, main = mod.functions
    assert isinstance(f.return_type, A.TypeTuple) and len(f.return_type.elems) == 2
    assert isinstance(f.body[0].value, A.TupleLit)
    decl, unpack, paren = main.body[0], main.body[1], main.body[2]
    assert isinstance(decl.type_expr, A.TypeTuple)
    assert isinstance(decl.value, A.TupleLit)
    assert isinstance(unpack, A.TupleAssign) and unpack.names == ["a", "_"]
    assert isinstance(paren.value, A.IntLit)


# ----- value match (spec 38) -----

def test_parse_value_match():
    mod = parse_src(
        "def main() -> int:\n    match m:\n"
        '        case "GET" | "HEAD":\n            print(1)\n'
        "        case -1:\n            print(2)\n"
        "        case other:\n            print(3)\n"
        "    return 0\n"
    )
    vm = mod.functions[0].body[0]
    assert isinstance(vm, A.ValueMatch) and len(vm.arms) == 3
    assert [p.value for p in vm.arms[0].patterns] == ["GET", "HEAD"]
    assert vm.arms[1].patterns[0].value == -1
    assert vm.arms[2].patterns == [] and vm.arms[2].bind == "other"


# ----- default and keyword arguments (spec 41) -----

def test_parse_defaults_and_keywords():
    mod = parse_src(
        'def g(name: string, greeting: string = "hi", n: int = -1) -> int:\n    return n\n\n'
        'def main() -> int:\n    return g("a", n=2)\n'
    )
    g = mod.functions[0]
    assert g.params[0].default is None and g.params[1].default.value == "hi"
    call = mod.functions[1].body[0].value
    assert isinstance(call.args[1], A.KeywordArg) and call.args[1].name == "n"


@pytest.mark.parametrize("src, msg", [
    ("def f(a: int = 1, b: int) -> int:\n    return a\n", "without a default follows"),
    ("def f(a: int = g()) -> int:\n    return a\n", "default value must be a literal"),
    ("def main() -> int:\n    return f(a=1, 2)\n", "positional argument after keyword"),
])
def test_parse_default_keyword_errors(src, msg):
    with pytest.raises(ParserError) as ei:
        parse_src(src)
    assert msg in str(ei.value)
