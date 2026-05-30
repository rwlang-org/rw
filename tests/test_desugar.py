from __future__ import annotations

from rwc import ast_nodes as A
from rwc.desugar import desugar_module
from rwc.lexer import tokenize
from rwc.parser import parse


def desugar_src(src: str) -> A.Module:
    return desugar_module(parse(tokenize(src)))


def test_for_expands_to_vardecl_and_while():
    src = "def main() -> int:\n    for i in range(0, 3):\n        return i\n"
    mod = desugar_src(src)
    body = mod.functions[0].body
    # No For node remains anywhere.
    assert all(not isinstance(s, A.For) for s in body)
    # Expect: temp decls + loop var decl + a While.
    whiles = [s for s in body if isinstance(s, A.While)]
    assert len(whiles) == 1
    # The loop variable `i` is declared as a VarDecl before the while.
    vardecls = [s for s in body if isinstance(s, A.VarDecl)]
    assert any(v.name == "i" for v in vardecls)


def test_for_while_condition_uses_or_of_two_comparisons():
    src = "def main() -> int:\n    for i in range(0, 3):\n        return i\n"
    mod = desugar_src(src)
    w = [s for s in mod.functions[0].body if isinstance(s, A.While)][0]
    # cond is: (step>0 and i<stop) or (step<0 and i>stop)
    assert isinstance(w.cond, A.BinOp) and w.cond.op == "or"


def test_for_body_ends_with_increment():
    src = "def main() -> int:\n    for i in range(0, 3):\n        return i\n"
    mod = desugar_src(src)
    w = [s for s in mod.functions[0].body if isinstance(s, A.While)][0]
    last = w.body[-1]
    # i = i + __step
    assert isinstance(last, A.Assign) and last.name == "i"
    assert isinstance(last.value, A.BinOp) and last.value.op == "+"
