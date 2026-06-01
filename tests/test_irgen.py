"""IRGen smoke and structural tests.

We avoid fragile string-equality snapshots (LLVM IR formatting may shift
across llvmlite versions). Instead we check structural invariants of the
generated IR — function names, the presence of runtime calls, etc.
"""

from __future__ import annotations

from rwc.irgen import generate
from rwc.lexer import tokenize
from rwc.parser import parse
from rwc.sema import analyze


def ir_for(src: str) -> str:
    ast = parse(tokenize(src))
    res = analyze(ast)
    mod = generate(ast, res)
    return str(mod)


def test_hello_world_ir_has_print_str_call():
    src = 'def main() -> int:\n    print("hello")\n    return 0\n'
    ir_text = ir_for(src)
    assert "rw_user_main" in ir_text
    assert 'define i32 @"main"' in ir_text
    assert "rw_print_str" in ir_text
    assert "rw_init" in ir_text and "rw_shutdown" in ir_text
    assert "hello" in ir_text


def test_integer_arith_ir():
    src = (
        "def main() -> int:\n"
        "    x: int = 1 + 2 * 3\n"
        "    print(x)\n"
        "    return 0\n"
    )
    ir_text = ir_for(src)
    assert "rw_print_i64" in ir_text
    # Contains add and mul instructions on i64.
    assert "add i64" in ir_text or "add" in ir_text
    assert "mul" in ir_text


def test_if_else_emits_branches():
    src = (
        "def main() -> int:\n"
        "    if 1 < 2:\n"
        "        return 1\n"
        "    else:\n"
        "        return 0\n"
    )
    ir_text = ir_for(src)
    assert "then" in ir_text and "else" in ir_text


def test_while_loop_emits_condition_block():
    src = (
        "def main() -> int:\n"
        "    i: int = 0\n"
        "    while i < 3:\n"
        "        i = i + 1\n"
        "    print(i)\n"
        "    return 0\n"
    )
    ir_text = ir_for(src)
    assert "while.cond" in ir_text and "while.body" in ir_text


def test_spawn_creates_trampoline_and_uses_runtime():
    src = (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "def main() -> int:\n"
        "    fu: Future[int] = spawn add(3, 4)\n"
        "    r: int = await fu\n"
        "    print(r)\n"
        "    return 0\n"
    )
    ir_text = ir_for(src)
    assert "rw_trampoline_add" in ir_text
    assert "rw_spawn_i64" in ir_text
    assert "rw_await_i64" in ir_text
    assert "malloc" in ir_text


def test_function_symbols_are_prefixed_with_rw_user():
    src = (
        "def helper(x: int) -> int:\n"
        "    return x\n"
        "def main() -> int:\n"
        "    return helper(1)\n"
    )
    ir_text = ir_for(src)
    assert "rw_user_helper" in ir_text
    assert "rw_user_main" in ir_text


def test_bool_lowered_to_i8():
    src = (
        "def main() -> int:\n"
        "    b: bool = true\n"
        "    if b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    ir_text = ir_for(src)
    # The boolean local should be i8.
    assert "alloca i8" in ir_text


def test_ternary_emits_branches_and_phi():
    src = (
        "def main() -> int:\n"
        "    x: int = 10 if true else 20\n"
        "    return x\n"
    )
    ir_text = ir_for(src)
    # Lowered to conditional branch + phi, like short-circuit and/or.
    assert "br i1" in ir_text
    assert "phi" in ir_text
    assert "10" in ir_text and "20" in ir_text
