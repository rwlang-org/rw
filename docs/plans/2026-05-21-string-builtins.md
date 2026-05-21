# String Builtins (len, ==, +) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rw 言語に最小の文字列操作 `len(s)` / `s == s` / `s + s` を追加し、後の netpoller 統合で echo server を書ける土台を作る。

**Architecture:** ランタイムに C ABI ヘルパ 3 本 (`rw_str_len` / `rw_str_eq` / `rw_str_concat`) を追加し、Sema で組込み関数テーブルと二項演算子の型ルールを拡張、irgen でこれらを呼び出す IR を出す。公開 ABI (`rw_spawn_*`, `rw_str`) は不変。

**Tech Stack:** C11 (ランタイム)、Python 3.12 + llvmlite (コンパイラ)、pytest (テスト)。

**Spec:** `docs/specs/07-string-builtins.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `runtime/runtime.h` | ABI 宣言 | 3 行追加 |
| `runtime/runtime.c` | 3 ヘルパ実装 | 関数 3 つ追加 |
| `runtime/fiber/test_str_ops.c` | C レベル単体テスト | 新規 |
| `runtime/Makefile` | (変更なし — `librw.a` は同じ OBJS) | — |
| `rwc/sema.py` | 組込み関数 / 演算子型ルール | 3 ヶ所修正 |
| `rwc/irgen.py` | ヘルパ呼び出し IR 生成 | 3 ヶ所修正 |
| `tests/test_sema.py` | 型検査の positive/negative | テスト追加 |
| `tests/test_e2e.py` | string_ops を parametrize に追加 | 1 行追加 |
| `examples/string_ops.rw` | 機能を 1 つの main で示すサンプル | 新規 |
| `examples/string_ops.rw.expected` | 期待出力 | 新規 |
| `.gitignore` | C テストバイナリ無視 | 1 行追加 |

---

## Task 1: ランタイムヘルパ 3 関数

**Files:**
- Modify: `runtime/runtime.h` (プロトタイプ追加)
- Modify: `runtime/runtime.c` (実装追加)

- [ ] **Step 1.1: `runtime.h` に 3 プロトタイプを追加**

`runtime/runtime.h` の `/* print */` ブロックの直下、`/* string helper */` セクションの末尾に追加:

```c
/* string ops (Commit added by string-builtins PR) */
int64_t rw_str_len   (rw_str s);
int8_t  rw_str_eq    (rw_str a, rw_str b);
rw_str  rw_str_concat(rw_str a, rw_str b);
```

- [ ] **Step 1.2: `runtime.c` に実装を追加**

`runtime/runtime.c` の `/* ---------- string helper ---------- */` セクションの直下、`/* ---------- lifecycle ---------- */` の上に追加:

```c
/* ---------- string ops ---------- */

int64_t rw_str_len(rw_str s) {
    return s.len;
}

int8_t rw_str_eq(rw_str a, rw_str b) {
    if (a.len != b.len) return 0;
    if (a.len == 0) return 1;  /* both empty: equal */
    return (int8_t)(memcmp(a.ptr, b.ptr, (size_t)a.len) == 0);
}

rw_str rw_str_concat(rw_str a, rw_str b) {
    rw_str out;
    out.len = a.len + b.len;
    if (out.len == 0) {
        out.ptr = NULL;
        return out;
    }
    char *p = (char *)malloc((size_t)out.len);
    if (!p) {
        /* OOM: degrade to empty string rather than crash. */
        out.len = 0;
        out.ptr = NULL;
        return out;
    }
    if (a.len > 0) memcpy(p, a.ptr, (size_t)a.len);
    if (b.len > 0) memcpy(p + a.len, b.ptr, (size_t)b.len);
    out.ptr = p;
    return out;
}
```

- [ ] **Step 1.3: 既存 C テストが緑のまま動くか確認**

```sh
make -C runtime clean && make -C runtime
```

Expected: `librw.a` がエラーなくビルドされる (新シンボル 3 つ追加、警告なし)。

```sh
cd runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c fiber/fiber.o fiber/fiber_arm64.o fiber/sched.o fiber/runq.o fiber/park.o -o fiber/test_sched && ./fiber/test_sched
```

Expected: `total = 333833500` / `expected = 333833500`。

- [ ] **Step 1.4: C テスト `test_str_ops.c` を書く**

`runtime/fiber/test_str_ops.c` を新規作成:

```c
/*
 * Unit test for rw_str_len / rw_str_eq / rw_str_concat.
 *
 * These helpers are pure functions of their inputs; we don't need
 * the scheduler at all here.
 */

#include "../runtime.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static rw_str lit(const char *s) {
    rw_str r;
    r.len = (int64_t)strlen(s);
    r.ptr = s;
    return r;
}

static rw_str empty(void) {
    rw_str r = { .len = 0, .ptr = NULL };
    return r;
}

int main(void) {
    /* len */
    assert(rw_str_len(lit("hello")) == 5);
    assert(rw_str_len(empty()) == 0);
    assert(rw_str_len(lit("")) == 0);

    /* eq */
    assert(rw_str_eq(lit("hello"), lit("hello")) == 1);
    assert(rw_str_eq(lit("hello"), lit("world")) == 0);
    assert(rw_str_eq(lit("hello"), lit("hell")) == 0);
    assert(rw_str_eq(empty(), empty()) == 1);
    assert(rw_str_eq(empty(), lit("")) == 1);
    assert(rw_str_eq(lit("hello"), empty()) == 0);

    /* concat */
    {
        rw_str c = rw_str_concat(lit("foo"), lit("bar"));
        assert(c.len == 6);
        assert(memcmp(c.ptr, "foobar", 6) == 0);
        /* leak: not freed (by design) */
    }
    {
        rw_str c = rw_str_concat(empty(), lit("x"));
        assert(c.len == 1);
        assert(c.ptr[0] == 'x');
    }
    {
        rw_str c = rw_str_concat(lit("x"), empty());
        assert(c.len == 1);
        assert(c.ptr[0] == 'x');
    }
    {
        rw_str c = rw_str_concat(empty(), empty());
        assert(c.len == 0);
        assert(c.ptr == NULL);
    }
    printf("all str_ops tests passed\n");
    return 0;
}
```

- [ ] **Step 1.5: `.gitignore` に test_str_ops を追加**

`/Users/ryuichi/ghq/github.com/ryuichi1208/rw/.gitignore` の `runtime/fiber/test_shutdown` の下に行追加:

```
runtime/fiber/test_str_ops
```

- [ ] **Step 1.6: ビルドして実行**

```sh
cd runtime && cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE fiber/test_str_ops.c runtime.o -o fiber/test_str_ops && ./fiber/test_str_ops
```

Expected: `all str_ops tests passed`。

- [ ] **Step 1.7: 既存テスト一式が緑か確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q
```

Expected: `66 passed`。

- [ ] **Step 1.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add runtime/runtime.h runtime/runtime.c runtime/fiber/test_str_ops.c .gitignore
git commit -m "$(cat <<'EOF'
runtime: add rw_str_len / rw_str_eq / rw_str_concat

Three pure helpers that operate on the existing rw_str (len, ptr)
fat pointer:
  - rw_str_len  -> int64_t                    (just returns .len)
  - rw_str_eq   -> int8_t (0/1)               (memcmp under same-length)
  - rw_str_concat -> rw_str (malloc'd buffer) (leak-allowed; see spec)

Edge cases covered in fiber/test_str_ops.c:
  - empty <-> non-empty equality
  - concat with one or both sides empty (no malloc(0))
  - NULL ptr (treated as empty)

No public ABI changes: the existing rw_spawn_* / rw_await_* / rw_str
shape is unchanged.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: コンパイラ (sema + irgen) を拡張

**Files:**
- Modify: `rwc/sema.py` (lines around 290-345 = binop check, around 378 = call check)
- Modify: `rwc/irgen.py` (lines around 68-71 = decls, 289-366 = binop, 368+ = call)
- Modify: `tests/test_sema.py`

### Sema 側

- [ ] **Step 2.1: Sema の現状を確認**

`rwc/sema.py:289-345` 付近に二項演算子の型チェックがある。`==` / `!=` の string 同士が `"string equality not supported in MVP"` で弾かれている (322 行付近)。`+` は int/float のみ許可。組込み関数は 378 行付近で `print` のみハードコード。

- [ ] **Step 2.2: Sema の `+` / `==` / `!=` を string にも許可する**

`rwc/sema.py` で「string equality not supported in MVP」のエラーを返している分岐 (sema.py:322 付近) を:

```python
                    # We could allow string equality later; disallow for MVP simplicity.
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        "string equality not supported in MVP",
                    ))
```

これを **削除** し、両辺 string の `==`/`!=` を bool として通すように直す。具体的には `==`/`!=` のハンドラを次の形に書き換える (前後文脈は実装時に sema.py を Read で開いて当てる):

```python
            # equality on int / float / bool / string
            if op in ("==", "!="):
                if lt == rt and lt in (T.INT, T.FLOAT, T.BOOL, T.STRING):
                    return T.BOOL
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, len(op),
                    f"cannot compare {lt} {op} {rt}",
                ))
```

`+` のハンドラには新しい string 分岐を 1 つ追加:

```python
            if op == "+":
                if lt == T.INT and rt == T.INT:
                    return T.INT
                if lt == T.FLOAT and rt == T.FLOAT:
                    return T.FLOAT
                if lt == T.STRING and rt == T.STRING:
                    return T.STRING
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, len(op),
                    f"cannot add {lt} + {rt}",
                ))
```

- [ ] **Step 2.3: Sema に `len` 組込みを追加**

`rwc/sema.py:378` の `if call.callee == "print":` ブロックの **直下** に、同じ高さで `len` の特例を追加:

```python
        if call.callee == "len":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"len takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self.analyze_expr(call.args[0])
            if at != T.STRING:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"len argument must be string, got `{at}`",
                ))
            return T.INT
```

`spawn` 経路で `spawn len(x)` を禁止する分岐 (`if call.callee == "print":` のところと同じ場所、sema.py:352 付近) にも `len` を加える:

```python
                if call.callee == "print":
                    raise CompileError(Diagnostic(
                        self.filename, call.line, call.col, len(call.callee),
                        "cannot spawn the builtin `print`",
                    ))
                if call.callee == "len":
                    raise CompileError(Diagnostic(
                        self.filename, call.line, call.col, len(call.callee),
                        "cannot spawn the builtin `len`",
                    ))
```

- [ ] **Step 2.4: Sema 単体テスト (positive)**

`tests/test_sema.py` に追加する関数。既存ヘルパ (`analyze_str` のような形) があればそれを使う。ヘルパが無ければ次のようなものを追加:

```python
def _analyze(src: str) -> None:
    from rwc.lexer import tokenize
    from rwc.parser import parse
    from rwc.sema import analyze
    tokens = tokenize(src, filename="<t>")
    ast = parse(tokens)
    analyze(ast, filename="<t>")
```

その上で:

```python
def test_string_len_returns_int():
    _analyze("""\
def main() -> int:
    s: string = "abc"
    n: int = len(s)
    return n
""")


def test_string_eq_returns_bool():
    _analyze("""\
def main() -> int:
    a: string = "x"
    b: string = "y"
    if a == b:
        return 1
    return 0
""")


def test_string_neq_returns_bool():
    _analyze("""\
def main() -> int:
    a: string = "x"
    b: string = "y"
    if a != b:
        return 1
    return 0
""")


def test_string_concat_returns_string():
    _analyze("""\
def main() -> int:
    a: string = "x"
    b: string = "y"
    c: string = a + b
    print(c)
    return 0
""")
```

- [ ] **Step 2.5: Sema 単体テスト (negative)**

`tests/test_sema.py` に追加:

```python
import pytest
from rwc.diagnostics import CompileError


def test_string_plus_int_is_type_error():
    with pytest.raises(CompileError) as excinfo:
        _analyze("""\
def main() -> int:
    s: string = "x" + 1
    return 0
""")
    assert "cannot add" in str(excinfo.value)


def test_string_eq_int_is_type_error():
    with pytest.raises(CompileError) as excinfo:
        _analyze("""\
def main() -> int:
    if "x" == 1:
        return 0
    return 1
""")
    assert "cannot compare" in str(excinfo.value)


def test_len_wrong_arg_type():
    with pytest.raises(CompileError) as excinfo:
        _analyze("""\
def main() -> int:
    n: int = len(1)
    return n
""")
    assert "len argument must be string" in str(excinfo.value)


def test_len_wrong_arity():
    with pytest.raises(CompileError) as excinfo:
        _analyze("""\
def main() -> int:
    n: int = len("a", "b")
    return n
""")
    assert "len takes exactly 1 argument" in str(excinfo.value)
```

- [ ] **Step 2.6: Sema テストを走らせる**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -v 2>&1 | tail -20
```

Expected: 既存テスト + 新規 8 つ全部 PASS。

`CompileError` のフォーマットや既存 test_sema.py のスタイルが違ったら、それに合わせて補正する (`from rwc.diagnostics import CompileError` の import を追加する等)。`_analyze` ヘルパが既に test_sema.py にあれば再定義せずに使う。

### irgen 側

- [ ] **Step 2.7: irgen の外部関数宣言を 3 つ追加**

`rwc/irgen.py:68-71` の `self._rw_print_*` の宣言の **直下** に追加:

```python
        self._rw_str_len = ir.Function(
            m, ir.FunctionType(I64, [RW_STR_TY]), "rw_str_len")
        self._rw_str_eq = ir.Function(
            m, ir.FunctionType(I8, [RW_STR_TY, RW_STR_TY]), "rw_str_eq")
        self._rw_str_concat = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [RW_STR_TY, RW_STR_TY]), "rw_str_concat")
```

- [ ] **Step 2.8: irgen の `_emit_call` で `len` を扱う**

`rwc/irgen.py:368` 付近の `_emit_call` で、`print` を処理しているブロックの **直下** に同じスタイルで:

```python
            if call.callee == "len":
                v = self._emit_expr(call.args[0], ctx)
                return ctx.builder.call(self._rw_str_len, [v])
```

- [ ] **Step 2.9: irgen の `_emit_binop` で string ケースを扱う**

`rwc/irgen.py:289` 付近の `_emit_binop` で、`+` / `==` / `!=` の処理に string 分岐を追加。`a.type == RW_STR_TY` または、Sema が付けた型情報を使って判別する (irgen.py を Read してどちらの方式が現状か確認してから当てる)。スケルトン:

```python
        if op == "+":
            # existing int/float branches above remain unchanged
            if isinstance(l.type, ir.LiteralStructType) and l.type == RW_STR_TY:
                return b.call(self._rw_str_concat, [l, r])
            ...

        if op in ("==", "!="):
            # existing int/float/bool branches above remain unchanged
            if isinstance(l.type, ir.LiteralStructType) and l.type == RW_STR_TY:
                eq = b.call(self._rw_str_eq, [l, r])   # i8 0/1
                eq_bool = b.icmp_unsigned("!=", eq, ir.Constant(I8, 0))  # i1
                if op == "!=":
                    eq_bool = b.not_(eq_bool)
                return eq_bool
            ...
```

実際の API 名 (`b.icmp_unsigned` か `b.icmp_signed` か、`not_` の存在等) は llvmlite の慣例に従う。既存 irgen.py で int の `==` 生成にどう書かれているかを見て、それに揃える。

- [ ] **Step 2.10: ビルドして既存 e2e が動くか確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 66 + 新規 sema 8 = 74 件全部 PASS。e2e は既存 7 件もそのまま緑。

- [ ] **Step 2.11: 単独で IR 生成して目視確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
cat > /tmp/string_smoke.rw <<'EOF'
def main() -> int:
    a: string = "foo"
    b: string = "bar"
    c: string = a + b
    print(c)
    print(len(c))
    if c == "foobar":
        print("ok")
    return 0
EOF
uv run rwc emit-ir /tmp/string_smoke.rw | grep -E "rw_str_(concat|eq|len)"
```

Expected: 3 つの `call ... @rw_str_concat`, `@rw_str_eq`, `@rw_str_len` が出力に含まれる。

- [ ] **Step 2.12: 単独でビルド + 実行**

```sh
uv run rwc run /tmp/string_smoke.rw
```

Expected:
```
foobar
6
ok
```

- [ ] **Step 2.13: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/sema.py rwc/irgen.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: allow len, string ==, and string + in sema + irgen

Sema:
  - Add `len(string) -> int` as a builtin (alongside `print`).
  - Allow `==` and `!=` between strings (was an explicit MVP block).
  - Allow `+` between strings (returns a fresh string).
  - Forbid `spawn len(...)` to match the `spawn print(...)` rule.

irgen:
  - Declare extern `rw_str_len(rw_str) -> i64`,
    `rw_str_eq(rw_str, rw_str) -> i8`,
    `rw_str_concat(rw_str, rw_str) -> rw_str`.
  - Emit calls to these for the new sema-allowed forms.

Negative tests in test_sema.py cover wrong-type and wrong-arity
diagnostics; positive tests cover the four happy paths.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: example + e2e

**Files:**
- Create: `examples/string_ops.rw`
- Create: `examples/string_ops.rw.expected`
- Modify: `tests/test_e2e.py` (parametrize list)

- [ ] **Step 3.1: `examples/string_ops.rw` を書く**

```rw
def main() -> int:
    hello: string = "hello"
    world: string = "world"
    sep: string = ", "
    greeting: string = hello + sep + world
    print(greeting)
    print(len(greeting))
    if greeting == "hello, world":
        print("eq ok")
    if hello != world:
        print("neq ok")
    return 0
```

- [ ] **Step 3.2: `examples/string_ops.rw.expected` を書く**

```
hello, world
12
eq ok
neq ok
```

(末尾改行ありで保存。`print` は自動改行を付けるので、行数と内容を一致させる。)

- [ ] **Step 3.3: 手元で実行して期待出力と一致するか確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/string_ops.rw
diff <(RW_WORKERS=1 uv run rwc run examples/string_ops.rw 2>&1) examples/string_ops.rw.expected
```

Expected: `diff` の出力なし (バイト一致)。

- [ ] **Step 3.4: `tests/test_e2e.py` の parametrize に `string_ops` を追加**

`tests/test_e2e.py` の以下の行 (37-40 付近):

```python
@pytest.mark.parametrize(
    "name",
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string"],
)
```

を:

```python
@pytest.mark.parametrize(
    "name",
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops"],
)
```

に変更。

- [ ] **Step 3.5: e2e テスト一式を走らせる**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 全部緑 (66 + sema 新規 8 + e2e 新規 1 = 75 件)。

- [ ] **Step 3.6: ランタイム単体テストも緑か確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make clean && make
# spot-check
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c fiber/fiber.o fiber/fiber_arm64.o fiber/sched.o fiber/runq.o fiber/park.o -o fiber/test_sched && ./fiber/test_sched
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE fiber/test_str_ops.c runtime.o -o fiber/test_str_ops && ./fiber/test_str_ops
```

Expected: `total = 333833500` / `all str_ops tests passed`。

- [ ] **Step 3.7: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add examples/string_ops.rw examples/string_ops.rw.expected tests/test_e2e.py
git commit -m "$(cat <<'EOF'
examples: add string_ops example exercising len, ==, +

examples/string_ops.rw uses every new operator introduced in this PR
in a single main: concat with two operands of differing literal
origin, len, ==, and != against literals. The .expected captures the
byte-for-byte stdout, and tests/test_e2e.py picks it up via the
existing parametrize list, so any later regression in sema / irgen /
runtime helpers will fail the suite.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

Spec カバレッジ確認:

- `len(s) -> int` 組込み → Task 2.3 (sema) + 2.7-2.8 (irgen) + 2.4/2.5 (test) + Task 3 (e2e)
- `==` / `!=` for string → Task 2.2 (sema) + 2.9 (irgen) + 2.4-2.5 (test) + Task 3 (e2e)
- `+` for string → Task 2.2 (sema) + 2.9 (irgen) + 2.4-2.5 (test) + Task 3 (e2e)
- ランタイムヘルパ 3 関数 → Task 1.1-1.2 + C unit test 1.4-1.6
- 空文字列 / NULL ptr の扱い → Task 1.2 (concat の `len == 0` 分岐) + 1.4 (test_str_ops の empty ケース)
- 既存 e2e が壊れない → Task 2.10 + Task 3.5 で全件回す
- 公開 ABI 不変 → 新シンボル追加のみ、既存シンボル変更なし

placeholder スキャン: 「TBD」「TODO」「(要確認)」「fill in」は plan 内に 0 件。

type consistency:
- `rw_str_len` 引数 = `rw_str`、戻り値 = `int64_t` (= LLVM `I64`) — task 1.1, 1.2, 2.7 で一貫
- `rw_str_eq` 引数 = `rw_str, rw_str`、戻り値 = `int8_t` (= LLVM `I8`) — 同上
- `rw_str_concat` 引数 = `rw_str, rw_str`、戻り値 = `rw_str` (= LLVM `RW_STR_TY`) — 同上
- Sema 内部の型定数 `T.STRING` / `T.INT` / `T.BOOL` の表記は task 2 全体で揃っている

リスク: irgen.py の `_emit_binop` の現状コード形 (どう型を判別しているか) は今は読まずに plan に書いたので、実装時に必ず Read で当てる。`isinstance(l.type, ir.LiteralStructType) and l.type == RW_STR_TY` の比較が等価で動くかは llvmlite 依存だが、既存 `rw_print_str` の呼び出し箇所と同じパターンに揃える前提。
