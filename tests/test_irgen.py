"""IRGen smoke and structural tests.

We avoid fragile string-equality snapshots (LLVM IR formatting may shift
across llvmlite versions). Instead we check structural invariants of the
generated IR — function names, the presence of runtime calls, etc.
"""

from __future__ import annotations

import re

from rwc.desugar import desugar_module
from rwc.irgen import generate
from rwc.lexer import tokenize
from rwc.loader import LoadedProgram
from rwc.parser import parse
from rwc.sema import analyze_program


def ir_for(src: str) -> str:
    ast = desugar_module(parse(tokenize(src)))
    program = LoadedProgram(root=ast, root_name="<root>")
    res = analyze_program(program)
    mod = generate(program, res)
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
    assert "rw_gc_alloc" in ir_text  # spawn arguments live on the GC heap (spec 67)


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


def test_file_io_emits_runtime_calls():
    src = (
        "def main() -> int:\n"
        "    fd: int = file_open(\"/tmp/x\", \"w\")\n"
        "    n: int = write(fd, bytes_from_str(\"hi\"))\n"
        "    rfd: int = file_open(\"/tmp/x\", \"r\")\n"
        "    b: Bytes = read(rfd, 16)\n"
        "    rc: int = close(rfd)\n"
        "    return 0\n"
    )
    ir_text = ir_for(src)
    assert "rw_file_open" in ir_text
    assert "rw_read" in ir_text
    assert "rw_write" in ir_text
    assert "rw_close" in ir_text


def test_qualified_call_emits_namespaced_symbol(tmp_path):
    from rwc.loader import load_program
    (tmp_path / "lib.rw").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
    )
    entry_src = "import lib\n\ndef main() -> int:\n    return lib.add(1, 2)\n"
    entry_path = tmp_path / "main.rw"
    entry_path.write_text(entry_src, encoding="utf-8")
    program = load_program(entry_src, str(entry_path))
    res = analyze_program(program, filename=str(entry_path))
    ir_text = str(generate(program, res))
    assert "rw_user_lib.add" in ir_text
    assert "rw_user_main" in ir_text


def _ir_for_program(tmp_path, entry_src: str, **libs: str) -> str:
    from rwc.loader import load_program
    for name, src in libs.items():
        (tmp_path / f"{name}.rw").write_text(src, encoding="utf-8")
    entry_path = tmp_path / "main.rw"
    entry_path.write_text(entry_src, encoding="utf-8")
    program = load_program(entry_src, str(entry_path))
    res = analyze_program(program, filename=str(entry_path))
    return str(generate(program, res))


def test_entry_func_named_like_qualified_symbol_does_not_clash(tmp_path):
    # Entry `lib_add` and `lib.add` used to both mangle to `rw_user_lib_add`.
    ir_text = _ir_for_program(
        tmp_path,
        "import lib\n\ndef lib_add(a: int, b: int) -> int:\n    return 100\n\n"
        "def main() -> int:\n    return lib.add(1, 2) + lib_add(1, 2)\n",
        lib="def add(a: int, b: int) -> int:\n    return a + b\n",
    )
    assert "rw_user_lib_add" in ir_text
    assert "rw_user_lib.add" in ir_text


def test_underscore_module_names_do_not_clash(tmp_path):
    # Module `a_b` func `c` vs module `a` func `b_c`.
    ir_text = _ir_for_program(
        tmp_path,
        "import a_b\nimport a\n\ndef main() -> int:\n"
        "    f1: Future[int] = spawn a_b.c()\n    f2: Future[int] = spawn a.b_c()\n"
        "    return await f1 + await f2\n",
        a_b="def c() -> int:\n    return 1\n",
        a="def b_c() -> int:\n    return 2\n",
    )
    assert "rw_user_a_b.c" in ir_text
    assert "rw_user_a.b_c" in ir_text
    assert "rw_trampoline_a_b.c" in ir_text
    assert "rw_trampoline_a.b_c" in ir_text


def test_string_search_builtins_emit_runtime_calls():
    ir_text = ir_for(
        "def main() -> int:\n"
        '    s: string = "GET / HTTP/1.1"\n'
        '    i: int = str_find(s, " ")\n'
        "    p: string = str_slice(s, 0, i)\n"
        '    b: bool = str_starts_with(s, "GET")\n'
        "    n: string = str_from_int(i)\n"
        "    o: Option[int] = str_to_int(n)\n"
        "    return 0\n"
    )
    for sym in ("rw_str_find", "rw_str_slice", "rw_str_starts_with", "rw_str_from_int", "rw_str_to_int"):
        assert re.search(rf'call .*@"{sym}"\(', ir_text), sym


def test_statement_spawn_is_detached():
    # A spawn whose Future is discarded must be detached (spec 19).
    ir_text = ir_for(
        "def work() -> int:\n    return 1\n\n"
        "def main() -> int:\n    spawn work()\n    return 0\n"
    )
    assert re.search(r'call void @"rw_detach"\(', ir_text)


def test_assigned_spawn_is_not_detached():
    ir_text = ir_for(
        "def work() -> int:\n    return 1\n\n"
        "def main() -> int:\n    f: Future[int] = spawn work()\n    return await f\n"
    )
    assert not re.search(r'call void @"rw_detach"\(', ir_text)


def test_record_constructor_and_fields_emit_malloc_gep():
    ir_text = ir_for(
        "class P:\n    x: int\n    s: string\n\n"
        "def bump(p: P) -> int:\n    p.x = p.x + 1\n    return p.x\n\n"
        'def main() -> int:\n    p: P = P(1, "a")\n    return bump(p)\n'
    )
    # Records are passed as i8* and allocated with malloc.
    assert re.search(r'define i64 @"rw_user_bump"\(i8\* %"p"\)', ir_text)
    assert re.search(r'call i8\* @"rw_gc_alloc"\(', ir_text)  # records on the GC heap (spec 67)
    # Field access goes through the {i64, {i64, i8*}} layout.
    assert "{i64, {i64, i8*}}*" in ir_text
    assert "getelementptr" in ir_text


def test_function_value_indirect_call():
    ir_text = ir_for(
        "def inc(x: int) -> int:\n    return x + 1\n\n"
        "def apply(f: Callable[[int], int], x: int) -> int:\n    return f(x)\n\n"
        "def main() -> int:\n    return apply(inc, 1)\n"
    )
    # A function value is {code, env} (spec 71). The call goes through the
    # code pointer with the environment first; a top-level function is
    # passed as its thunk with a null environment.
    assert re.search(r'define i64 @"rw_user_apply"\(\{i8\*, i8\*\} %"f", i64 %"x"\)', ir_text)
    assert re.search(r'bitcast i8\* %"[^"]*" to i64 \(i8\*, i64\)\*', ir_text)
    assert 'define internal i64 @"rw_fnval_inc"(i8* %".1", i64 %".2")' in ir_text
    assert re.search(r'call i64 @"rw_user_apply"\(\{i8\*, i8\*\} \{i8\* bitcast \(i64 \(i8\*, i64\)\* @"rw_fnval_inc" to i8\*\), i8\* null\}', ir_text)


def test_generic_list_uses_size_generic_helpers():
    ir_text = ir_for(
        "def main() -> int:\n"
        "    s: List[string] = list_new()\n"
        '    s = list_push(s, "a")\n'
        "    print(list_at(s, 0))\n"
        "    i: List[int] = list_new()\n"
        "    i = list_push(i, 1)\n"
        "    return len(s) + list_at(i, 0)\n"
    )
    for sym in ("rw_list_new", "rw_list_push", "rw_list_at"):
        assert re.search(rf'call void @"{sym}"\(', ir_text), sym
    # List[int] keeps its specialised helpers.
    assert re.search(r'call void @"rw_list_int_push"\(', ir_text)


def test_safepoints_at_function_entry_and_loop_head():
    ir_text = ir_for(
        "def spin(n: int) -> int:\n"
        "    i: int = 0\n"
        "    while i < n:\n"
        "        i = i + 1\n"
        "    return i\n\n"
        "def main() -> int:\n    return spin(3)\n"
    )
    loads = re.findall(r'load atomic i32, i32\* @"rw_preempt_flag" monotonic', ir_text)
    # spin: entry + loop head; main: entry.
    assert len(loads) == 3
    assert re.search(r'call void @"rw_safepoint"\(\)', ir_text)


def test_conversions_and_string_ordering_ir():
    ir_text = ir_for(
        "def main() -> int:\n"
        "    f: float = float(3)\n"
        "    s: string = str(f) + str(int(f))\n"
        '    if s < "z":\n'
        "        return 1\n"
        "    return 0\n"
    )
    assert "sitofp" in ir_text and "fptosi" in ir_text
    assert re.search(r'call .*@"rw_str_from_float"\(', ir_text)
    assert re.search(r'call i64 @"rw_str_cmp"\(', ir_text)


def test_allocas_are_all_in_the_entry_block():
    # An alloca inside a loop allocates on every iteration; fiber stacks are
    # small, so every local and temporary must be allocated once, at entry.
    ir_text = ir_for(
        "def work(n: int) -> int:\n"
        "    t: int = 0\n"
        "    while t < n:\n"
        '        s: string = "a"\n'
        "        o: Option[int] = str_to_int(s)\n"
        "        l: List[int] = list_push(list_new(), len(s))\n"
        "        t = t + list_at(l, 0)\n"
        "    return t\n\n"
        "def main() -> int:\n    return work(3)\n"
    )
    body = ir_text.split('define i64 @"rw_user_work"', 1)[1].split("\n}\n", 1)[0]
    # blocks[0] is the signature line; blocks[1] is the entry block.
    blocks = re.split(r"\n(?=[\w.\"]+:)", body)
    entry, rest = blocks[1], blocks[2:]
    assert entry.startswith("entry:")
    assert "alloca" in entry
    for blk in rest:
        assert "alloca" not in blk, blk


def test_list_syntax_lowering():
    ir_text = ir_for(
        "def main() -> int:\n"
        '    xs: List[string] = ["a", "b"]\n'
        "    t: int = 0\n"
        "    for x in xs[1:]:\n"
        "        t += len(x)\n"
        '    return t + len(xs[-1]) + len("hey"[0])\n'
    )
    for sym in ("rw_list_push", "rw_list_slice", "rw_list_at", "rw_str_index"):
        assert re.search(rf'call .*@"{sym}"\(', ir_text), sym
    assert "for.update" in ir_text


def test_integer_division_is_checked_unless_unchecked():
    from rwc.irgen import generate
    src = "def f(a: int, b: int) -> int:\n    return a / b + a % b\n\ndef main() -> int:\n    return f(1, 1)\n"
    ast = desugar_module(parse(tokenize(src)))
    program = LoadedProgram(root=ast, root_name="<root>")
    checked = str(generate(program, analyze_program(program)))
    assert "division by zero in f (line 2)" in checked
    assert re.search(r'call void @"rw_panic"\(', checked)
    ast2 = desugar_module(parse(tokenize(src)))
    program2 = LoadedProgram(root=ast2, root_name="<root>")
    unchecked = str(generate(program2, analyze_program(program2), checked=False))
    assert "division by zero" not in unchecked
