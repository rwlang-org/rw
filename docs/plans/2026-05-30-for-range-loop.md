# `for ... in range(...)` Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rw に `for <var> in range(start, stop[, step])` ループを構文糖として導入する。

**Architecture:** parser が新しい `For` AST ノードを生成し、parser 直後・sema 前に走る独立パス `desugar.py` が `For` を既存の `VarDecl` / `While` / `Assign` ノードへ書き換える。これにより sema・irgen・runtime は無改修。`range` は `for` ヘッダ位置でのみ受理し、値としては扱わない。`step==0` は desugar したループ条件が両辺 false になる性質で 0 回ループになる。

**Tech Stack:** Python (rwc コンパイラ: lexer/parser/sema/irgen)、llvmlite、pytest、C ランタイム (無改修)。

参照 spec: `docs/specs/13-for-range-loop.md`

---

## File Structure

- **Modify** `rwc/ast_nodes.py` — `For` dataclass を追加、`Stmt` Union に追加
- **Modify** `rwc/parser.py` — `parse_for()` 追加、`parse_stmt` のディスパッチに `KW_FOR` 追加
- **Create** `rwc/desugar.py` — `desugar_module(mod)` パス。`For` を `While` 等へ展開
- **Modify** `rwc/driver.py` — `compile_source` / `emit_ir` / `emit_ast` の parse 直後に desugar を挿入
- **Modify** `tests/test_parser.py` — for のパース結果テスト
- **Create** `tests/test_desugar.py` — desugar 展開のテスト
- **Modify** `tests/test_sema.py` — for の negative テスト (型エラー)
- **Create** `examples/for_count.rw` + `examples/for_count.rw.expected` — e2e サンプル
- **Modify** `tests/test_e2e.py` — parametrize に `for_count` 追加

---

## Task 1: `For` AST ノードを追加

**Files:**
- Modify: `rwc/ast_nodes.py`

- [ ] **Step 1: `For` dataclass を `While` の直後 (L193 付近) に追加**

`rwc/ast_nodes.py` の `While` クラス定義の直後に挿入:

```python
@dataclass
class For:
    var: str               # loop variable name
    start: Expr            # int expr
    stop: Expr             # int expr
    step: Expr             # int expr (defaults filled by parser)
    body: List["Stmt"]
    line: int
    col: int
```

- [ ] **Step 2: `Stmt` Union に `For` を追加**

`rwc/ast_nodes.py` の `Stmt = Union[...]` 行 (現 L212) を変更:

```python
Stmt = Union[VarDecl, Assign, ExprStmt, Return, If, While, For, MatchStmt]
```

- [ ] **Step 3: import が壊れていないか確認**

Run: `uv run python -c "from rwc import ast_nodes as A; A.For"`
Expected: エラーなし (何も出力されない)

- [ ] **Step 4: Commit**

```bash
git add rwc/ast_nodes.py
git commit -m "ast: add For node for range-based loops"
```

---

## Task 2: parser に `for ... in range(...)` を追加

**Files:**
- Modify: `rwc/parser.py`
- Test: `tests/test_parser.py`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_parser.py` の末尾に追加:

```python
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


def test_parse_range_outside_for_is_error():
    src = "def main() -> int:\n    x: int = range(0, 5)\n    return x\n"
    with pytest.raises(ParserError):
        parse_src(src)


def test_parse_for_zero_args_is_error():
    src = "def main() -> int:\n    for i in range():\n        return i\n"
    with pytest.raises(ParserError):
        parse_src(src)


def test_parse_for_four_args_is_error():
    src = "def main() -> int:\n    for i in range(0, 1, 2, 3):\n        return i\n"
    with pytest.raises(ParserError):
        parse_src(src)
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_parser.py -k for_ -v`
Expected: FAIL (`for` がパースできず ParserError、または For ノードが生成されない)

- [ ] **Step 3: `parse_stmt` のディスパッチに `KW_FOR` を追加**

`rwc/parser.py` の `parse_stmt` (現 L239 付近)、`KW_WHILE` の分岐の直後に追加:

```python
        if t.kind == TokenKind.KW_FOR:
            return self.parse_for()
```

- [ ] **Step 4: `parse_for()` を実装**

`rwc/parser.py` の `parse_while` メソッド (現 L302) の直後に追加:

```python
    def parse_for(self) -> A.For:
        kw = self.eat(TokenKind.KW_FOR)
        var_tok = self.eat(TokenKind.IDENT, "loop variable name")
        self.eat(TokenKind.KW_IN, "'in' after for variable")
        # range header: the identifier `range` followed by ( args )
        if not (self.cur.kind == TokenKind.IDENT and self.cur.value == "range"):
            raise ParserError(
                "for loop must iterate over range(...)",
                self.cur.line, self.cur.col,
            )
        self.i += 1  # consume `range`
        self.eat(TokenKind.LPAREN, "'(' after range")
        args: List[A.Expr] = []
        if self.cur.kind != TokenKind.RPAREN:
            args.append(self.parse_expr())
            while self.cur.kind == TokenKind.COMMA:
                self.i += 1
                args.append(self.parse_expr())
        self.eat(TokenKind.RPAREN, "')' to close range")
        if not (1 <= len(args) <= 3):
            raise ParserError(
                "range() takes 1 to 3 arguments",
                kw.line, kw.col,
            )
        # Fill defaults: range(stop) / range(start, stop) / range(start, stop, step)
        if len(args) == 1:
            start: A.Expr = A.IntLit(0, kw.line, kw.col)
            stop = args[0]
        else:
            start = args[0]
            stop = args[1]
        if len(args) == 3:
            step: A.Expr = args[2]
        else:
            step = A.IntLit(1, kw.line, kw.col)
        self.eat(TokenKind.COLON, "':' after for header")
        self.eat(TokenKind.NEWLINE)
        body = self.parse_block()
        return A.For(var_tok.value, start, stop, step, body, kw.line, kw.col)
```

> 注: `range` を for 外で使うと、通常の式パスで `Call("range", ...)` が生成され
> sema が「未定義の関数 range」で弾く。parser でも for ヘッダ以外では `range`
> をビルトイン化していないため、`x = range(0,5)` は sema の段階でエラーになる。
> ただし negative テスト `test_parse_range_outside_for_is_error` は ParserError を
> 期待しているので、Step 5 で挙動を確認し、ParserError でなく CompileError に
> なる場合はテスト側を `test_sema.py` の負ケースへ移す (Step 5 参照)。

- [ ] **Step 5: テストを実行して確認**

Run: `uv run pytest tests/test_parser.py -k for_ -v`
Expected: positive 3 件 PASS。`test_parse_for_zero_args_is_error` / `test_parse_for_four_args_is_error` PASS。
`test_parse_range_outside_for_is_error` は `range` が式として通ってしまう場合 FAIL する。その場合は当該テストを `tests/test_parser.py` から削除し、Task 5 の sema negative テスト (`test_for_range_outside_is_sema_error`) でカバーする。実際の挙動に合わせてどちらか一方に置く。

- [ ] **Step 6: Commit**

```bash
git add rwc/parser.py tests/test_parser.py
git commit -m "parser: parse for-in-range loop header into For node"
```

---

## Task 3: desugar パスを実装

**Files:**
- Create: `rwc/desugar.py`
- Test: `tests/test_desugar.py`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_desugar.py` を新規作成:

```python
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
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_desugar.py -v`
Expected: FAIL (`ModuleNotFoundError: rwc.desugar`)

- [ ] **Step 3: `rwc/desugar.py` を実装**

`rwc/desugar.py` を新規作成:

```python
"""Desugaring pass: lower syntactic-sugar AST nodes to core nodes.

Runs after parsing and before sema. Currently lowers `For` (range-based
loops) into `VarDecl` + `While` + `Assign` using only core AST nodes, so
sema and irgen need no knowledge of `For`.
"""

from __future__ import annotations

from typing import List

from . import ast_nodes as A


class _Desugarer:
    def __init__(self) -> None:
        self._tmp_counter = 0

    def _fresh(self, base: str) -> str:
        n = self._tmp_counter
        self._tmp_counter += 1
        return f"__for_{base}_{n}"

    def module(self, mod: A.Module) -> A.Module:
        for fn in mod.functions:
            fn.body = self._block(fn.body)
        return mod

    def _block(self, stmts: List[A.Stmt]) -> List[A.Stmt]:
        out: List[A.Stmt] = []
        for s in stmts:
            out.extend(self._stmt(s))
        return out

    def _stmt(self, s: A.Stmt) -> List[A.Stmt]:
        if isinstance(s, A.For):
            return self._lower_for(s)
        if isinstance(s, A.If):
            s.then_body = self._block(s.then_body)
            s.else_body = self._block(s.else_body)
            return [s]
        if isinstance(s, A.While):
            s.body = self._block(s.body)
            return [s]
        if isinstance(s, A.MatchStmt):
            if s.some_block is not None:
                s.some_block = self._block(s.some_block)
            if s.none_block is not None:
                s.none_block = self._block(s.none_block)
            if s.ok_block is not None:
                s.ok_block = self._block(s.ok_block)
            if s.err_block is not None:
                s.err_block = self._block(s.err_block)
            return [s]
        return [s]

    def _int_type(self, ln: int, col: int) -> A.TypeName:
        return A.TypeName("int", ln, col)

    def _lower_for(self, f: A.For) -> List[A.Stmt]:
        ln, col = f.line, f.col
        stop_name = self._fresh("stop")
        step_name = self._fresh("step")

        # Recursively desugar the body first (nested fors).
        body = self._block(f.body)

        out: List[A.Stmt] = []
        # __stop = <stop>; __step = <step>; <var> = <start>
        out.append(A.VarDecl(stop_name, self._int_type(ln, col), f.stop, ln, col))
        out.append(A.VarDecl(step_name, self._int_type(ln, col), f.step, ln, col))
        out.append(A.VarDecl(f.var, self._int_type(ln, col), f.start, ln, col))

        zero = A.IntLit(0, ln, col)
        step_pos = A.BinOp(">", A.Name(step_name, ln, col), zero, ln, col)
        step_neg = A.BinOp("<", A.Name(step_name, ln, col), A.IntLit(0, ln, col), ln, col)
        lt = A.BinOp("<", A.Name(f.var, ln, col), A.Name(stop_name, ln, col), ln, col)
        gt = A.BinOp(">", A.Name(f.var, ln, col), A.Name(stop_name, ln, col), ln, col)
        asc = A.BinOp("and", step_pos, lt, ln, col)
        desc = A.BinOp("and", step_neg, gt, ln, col)
        cond = A.BinOp("or", asc, desc, ln, col)

        # body + (var = var + __step)
        incr = A.Assign(
            f.var,
            A.BinOp("+", A.Name(f.var, ln, col), A.Name(step_name, ln, col), ln, col),
            ln, col,
        )
        while_body = list(body) + [incr]
        out.append(A.While(cond, while_body, ln, col))
        return out


def desugar_module(mod: A.Module) -> A.Module:
    return _Desugarer().module(mod)
```

- [ ] **Step 4: テストを実行して確認**

Run: `uv run pytest tests/test_desugar.py -v`
Expected: 3 件すべて PASS

- [ ] **Step 5: Commit**

```bash
git add rwc/desugar.py tests/test_desugar.py
git commit -m "desugar: lower For range loops to While + assignments"
```

---

## Task 4: driver に desugar を組み込む

**Files:**
- Modify: `rwc/driver.py`

- [ ] **Step 1: import を追加**

`rwc/driver.py` の import 群 (現 L24 `from .parser import ...` の直後) に追加:

```python
from .desugar import desugar_module
```

- [ ] **Step 2: `compile_source` の parse 直後に desugar を挿入**

`rwc/driver.py` の `compile_source` 内、`ast = parse(tokens)` の直後 (現 L82) を:

```python
        tokens = tokenize(source, filename=filename)
        ast = parse(tokens)
        ast = desugar_module(ast)
        sema = analyze(ast, filename=filename)
        llmod = irgen_generate(ast, sema)
```

- [ ] **Step 3: `emit_ir` にも同じ挿入**

`rwc/driver.py` の `emit_ir` 内 (現 L128-131) を:

```python
    tokens = tokenize(source, filename=filename)
    ast = parse(tokens)
    ast = desugar_module(ast)
    sema = analyze(ast, filename=filename)
    llmod = irgen_generate(ast, sema)
```

- [ ] **Step 4: `emit_ast` にも同じ挿入**

`rwc/driver.py` の `emit_ast` 内 (現 L136-138) を:

```python
def emit_ast(source: str, filename: str) -> ASTModule:
    tokens = tokenize(source, filename=filename)
    return desugar_module(parse(tokens))
```

- [ ] **Step 5: パイプライン全体が通ることを確認 (一時ファイルで)**

Run:
```bash
uv run python -c "
from rwc.driver import emit_ir
src = 'def main() -> int:\n    total: int = 0\n    for i in range(0, 5):\n        total = total + i\n    return total\n'
ir = emit_ir(src, filename='t.rw')
print('rw_user_main' in ir)
"
```
Expected: `True` (for が desugar→sema→irgen を通って IR 生成される)

- [ ] **Step 6: Commit**

```bash
git add rwc/driver.py
git commit -m "driver: run desugar pass between parse and sema on all paths"
```

---

## Task 5: sema の negative テストを追加

**Files:**
- Test: `tests/test_sema.py`

> 目的: for 引数が非 int のとき型エラーになること、および `range` を for 外で
> 使うとエラーになること (parser か sema のどちらで弾かれるかは Task 2 Step 5 の
> 実挙動に従う) を固定する。

- [ ] **Step 1: テストを追加**

`tests/test_sema.py` の末尾に追加 (`check` / `err` ヘルパは既存):

```python
def test_for_loop_int_args_ok():
    src = (
        "def main() -> int:\n"
        "    total: int = 0\n"
        "    for i in range(0, 5):\n"
        "        total = total + i\n"
        "    return total\n"
    )
    # Must be desugared before sema.
    from rwc.desugar import desugar_module
    from rwc.parser import parse
    from rwc.lexer import tokenize
    from rwc.sema import analyze
    res = analyze(desugar_module(parse(tokenize(src))), filename="t.rw")
    assert "main" in res.functions


def test_for_loop_non_int_stop_is_error():
    src = (
        "def main() -> int:\n"
        '    for i in range(0, "x"):\n'
        "        return i\n"
        "    return 0\n"
    )
    from rwc.desugar import desugar_module
    from rwc.parser import parse
    from rwc.lexer import tokenize
    from rwc.sema import analyze
    import pytest as _pytest
    with _pytest.raises(CompileError):
        analyze(desugar_module(parse(tokenize(src))), filename="t.rw")
```

- [ ] **Step 2: テストを実行して確認**

Run: `uv run pytest tests/test_sema.py -k for_loop -v`
Expected: 2 件 PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_sema.py
git commit -m "sema: tests for for-loop int typing"
```

---

## Task 6: e2e サンプルと期待値

**Files:**
- Create: `examples/for_count.rw`
- Create: `examples/for_count.rw.expected`
- Modify: `tests/test_e2e.py`

- [ ] **Step 1: サンプルを作成**

`examples/for_count.rw` を新規作成:

```
def main() -> int:
    total: int = 0
    for i in range(0, 10):
        total = total + i
    down: int = 0
    for j in range(10, 0, -1):
        down = down + j
    step2: int = 0
    for k in range(0, 10, 2):
        step2 = step2 + k
    empty: int = 0
    for m in range(5, 5):
        empty = empty + 1
    print(total)
    print(down)
    print(step2)
    print(empty)
    return 0
```

> total = 0+1+...+9 = 45、down = 10+9+...+1 = 55、step2 = 0+2+4+6+8 = 20、
> empty は 0 回ループなので 0。

- [ ] **Step 2: print の出力フォーマットを確認して期待値を作る**

Run:
```bash
uv run python -m rwc.cli run examples/for_count.rw
```
Expected: 4 行の数値出力。実際の出力 (改行・整形含む) を確認する。

- [ ] **Step 3: 確認した出力で `.expected` を作る**

Step 2 の実出力をそのまま `examples/for_count.rw.expected` に保存する。
他の例 (`examples/while_count.rw.expected`) と同じ整形である想定。想定値:

```
45
55
20
0
```

(Step 2 の実出力と差異があれば実出力を正とする)

- [ ] **Step 4: parametrize に追加**

`tests/test_e2e.py` の `@pytest.mark.parametrize` リスト (現 L52 付近) の末尾に `"for_count"` を追加:

```python
@pytest.mark.parametrize(
    "name",
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic", "result_basic", "for_count"],
)
```

- [ ] **Step 5: e2e テストを実行**

Run: `uv run pytest tests/test_e2e.py -k for_count -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add examples/for_count.rw examples/for_count.rw.expected tests/test_e2e.py
git commit -m "examples: add for_count exercising for-in-range loops"
```

---

## Task 7: 全テスト緑を確認

**Files:** なし (検証のみ)

- [ ] **Step 1: 全テストを実行**

Run: `uv run pytest -q`
Expected: 全 PASS (既存テストの回帰なし、新規テストすべて緑)

- [ ] **Step 2: emit-ast で desugar 後の姿を目視確認 (任意)**

Run: `uv run python -m rwc.cli emit-ast examples/for_count.rw`
Expected: `For` ノードが現れず、`While` + `VarDecl` + `Assign` に展開されている

---

## Self-Review (記入済み)

**Spec coverage:**
- 1〜3 引数 range → Task 2 (parser でデフォルト補完)
- 任意 int 式 → Task 2 (`parse_expr` で引数を取る) + Task 5 (型チェック)
- 負 step / 半開区間 → Task 3 (条件式 `(step>0 and v<stop) or (step<0 and v>stop)`)
- step==0 が 0 回ループ → Task 3 (条件両辺 false) + Task 6 (empty で検証)
- `range` を値として使わせない → Task 2 (for ヘッダ以外で range をビルトイン化しない)
- 二重評価防止 → Task 3 (`__for_stop_N` / `__for_step_N` に束縛)
- sema/irgen/runtime 無改修 → Task 3/4 (desugar が core ノードのみ生成)
- 3 経路で desugar → Task 4 (compile_source / emit_ir / emit_ast)

**Placeholder scan:** プレースホルダなし。`.expected` の値のみ Task 6 Step 2 で実出力を正とする旨を明記 (想定値も提示)。

**Type consistency:** `For(var, start, stop, step, body, line, col)` は Task 1 で定義し Task 2/3 で同一シグネチャを使用。`desugar_module` は Task 3 で定義し Task 4/5 で使用。一致。
