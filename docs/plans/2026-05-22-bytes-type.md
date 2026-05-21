# Bytes Type Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rw 言語に新しいプリミティブ型 `Bytes` を導入し、`len(b)` / `Bytes == Bytes` / `bytes_from_str` / `str_from_bytes` の最小 4 操作と `Future[Bytes]` を動かす。

**Architecture:** LLVM IR レベルでは `Bytes` を既存 `string` と同じ `{i64 len, i8* ptr}` (= `RW_STR_TY`) として表現し、Sema 上だけで `T.BYTES` を別の型として区別する。ランタイムには新規関数を一切追加しない。

**Tech Stack:** Python 3.12 + llvmlite (コンパイラ)、pytest (テスト)、C11 (ランタイム — 今回は読むだけ)。

**Spec:** `docs/specs/08-bytes-type.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `rwc/types.py` | プリミティブ型定義 | `BYTES = _Primitive("Bytes")` 追加 |
| `rwc/lexer.py` | キーワード認識 | `KW_BYTES` + `KEYWORDS["Bytes"]` |
| `rwc/parser.py` | 型パース | `parse_type` の dict に 1 行 |
| `rwc/sema.py` | 型解決 + 組込み + 演算子拡張 | 4 ヶ所修正 |
| `rwc/irgen.py` | LLVM IR 生成 | 4 ヶ所修正 |
| `tests/test_sema.py` | 型検査 positive/negative | テスト追加 |
| `tests/test_e2e.py` | parametrize に bytes_basic を追加 | 1 行追加 |
| `examples/bytes_basic.rw` | 機能デモ | 新規 |
| `examples/bytes_basic.rw.expected` | 期待出力 | 新規 |

ランタイム (`runtime/*`) と既存 fiber 関連には一切触れない。

---

## Task 1: lexer / parser / types で `Bytes` 型名を認識

このタスクのゴールは「`b: Bytes = ...` の型注釈を parse + resolve できるようにする」だけ。`Bytes` を実際に使う組込みはまだ無いので、コード本体は Sema レベルで「`Bytes` という型はあるが、組み立てる手段はまだ無い」状態になる。

**Files:**
- Modify: `rwc/types.py` (BYTES 追加)
- Modify: `rwc/lexer.py` (KW_BYTES + KEYWORDS)
- Modify: `rwc/parser.py` (parse_type dict)
- Modify: `rwc/sema.py` (_resolve_type dict)

- [ ] **Step 1.1: `rwc/types.py` に `BYTES` プリミティブを追加**

`rwc/types.py` の `VOID = _Primitive("void")` の **直下** に追加:

```python
BYTES = _Primitive("Bytes")
```

`is_printable` / `is_numeric` には**含めない** (現状のままで OK)。

- [ ] **Step 1.2: `rwc/lexer.py` に `KW_BYTES` を追加**

`TokenKind` enum の `KW_STRING = auto()` の **直下** に:

```python
    KW_BYTES = auto()
```

を追加し、`KEYWORDS` dict の `"string": TokenKind.KW_STRING,` の **直下** に:

```python
    "Bytes": TokenKind.KW_BYTES,
```

を追加。

- [ ] **Step 1.3: `rwc/parser.py` の `parse_type` に Bytes を認識させる**

`parse_type` メソッド内の `kind_to_name` dict (parser.py:154 付近) を以下に変更:

```python
        kind_to_name = {
            TokenKind.KW_INT: "int",
            TokenKind.KW_FLOAT: "float",
            TokenKind.KW_BOOL: "bool",
            TokenKind.KW_STRING: "string",
            TokenKind.KW_BYTES: "Bytes",
            TokenKind.KW_VOID: "void",
        }
```

- [ ] **Step 1.4: `rwc/sema.py` の `_resolve_type` に Bytes を認識させる**

`_resolve_type` 関数 (sema.py:39 付近) の `m` dict を以下に変更:

```python
        m = {
            "int": T.INT,
            "float": T.FLOAT,
            "bool": T.BOOL,
            "string": T.STRING,
            "Bytes": T.BYTES,
            "void": T.VOID,
        }
```

- [ ] **Step 1.5: 既存テストが緑か確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `75 passed`。

- [ ] **Step 1.6: `b: Bytes = ...` の型注釈だけが parse+resolve できることを確認するテストを追加**

`tests/test_sema.py` の末尾に追加:

```python
def test_bytes_type_annotation_parses():
    # Declaring a Bytes parameter and using it should parse and resolve.
    # We don't yet have a way to *produce* a Bytes value, so use a
    # function parameter (the only way to introduce a Bytes binding
    # before Task 2 lands bytes_from_str).
    src = (
        "def takes_bytes(b: Bytes) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    res = check(src)
    assert "takes_bytes" in res.functions
    assert res.functions["takes_bytes"].params[0][1] is T.BYTES


def test_unknown_type_name_still_errors():
    # Sanity: an arbitrary unknown type name remains an error.
    src = (
        "def f(x: Nope) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    e = err(src)
    assert "unknown type" in e.diagnostic.message
```

- [ ] **Step 1.7: 上記テストを走らせる**

```sh
uv run pytest tests/test_sema.py::test_bytes_type_annotation_parses tests/test_sema.py::test_unknown_type_name_still_errors -v 2>&1 | tail -10
```

Expected: 両方 PASS。`takes_bytes` の引数型表現は `res.functions[...].params` の形式に依存するので、もし `params[0][1]` のアクセスで AttributeError が出るなら、既存テスト (`tests/test_sema.py` 内) で `params` の使われ方を確認して同じ形式に揃える。

- [ ] **Step 1.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/types.py rwc/lexer.py rwc/parser.py rwc/sema.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: introduce Bytes type name in lexer / parser / sema

Adds the primitive type T.BYTES, the lexer keyword Bytes
(TokenKind.KW_BYTES), the parser kind-to-name entry, and the sema
resolver entry. With this in place, `b: Bytes` type annotations
parse and resolve, but there is still no way to construct or
operate on a Bytes value — that lands in the next commit.

is_printable and is_numeric intentionally remain string/int/float/
bool only, so attempting to print or arithmetic on a Bytes will
fail at sema (covered by tests in the next commit).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Sema + irgen で Bytes の操作を実装

このタスクで以下を有効化する:

- `len(b: Bytes) -> int` (既存 len オーバーロード)
- `bytes_from_str(s: string) -> Bytes`
- `str_from_bytes(b: Bytes) -> string`
- `Bytes == Bytes`, `Bytes != Bytes` (既存 string `==` ルートに乗せる)
- `Future[Bytes]` (spawn / await)

`Bytes + Bytes` と `print(Bytes)` は禁止のまま (Sema エラー)。

**Files:**
- Modify: `rwc/sema.py` (_check_call で len 拡張 + bytes_from_str / str_from_bytes 追加、SpawnExpr 禁止リストに 2 つ追加)
- Modify: `rwc/irgen.py` (llvm_type_of / _decl_spawn / _decl_await / _emit_binop / _emit_call)
- Modify: `tests/test_sema.py` (positive 5 + negative 5)

### Sema

- [ ] **Step 2.1: Sema の `_check_call` で `len` の引数型を `string` または `Bytes` に拡張**

`sema.py:394` 付近の `if call.callee == "len":` ブロックの中で、引数型チェックを以下に変更:

```python
        # Builtin: len(string) -> int.  (also len(Bytes) -> int)
        if call.callee == "len":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.STRING and at is not T.BYTES:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len argument must be string or Bytes, found `{at}`",
                ))
            return T.INT
```

- [ ] **Step 2.2: Sema に `bytes_from_str` と `str_from_bytes` を追加**

`sema.py` の `len` 分岐の **直下** に追加:

```python
        # Builtin: bytes_from_str(string) -> Bytes.
        if call.callee == "bytes_from_str":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"bytes_from_str takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.STRING:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"bytes_from_str argument must be string, found `{at}`",
                ))
            return T.BYTES
        # Builtin: str_from_bytes(Bytes) -> string.
        if call.callee == "str_from_bytes":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"str_from_bytes takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.BYTES:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"str_from_bytes argument must be Bytes, found `{at}`",
                ))
            return T.STRING
```

- [ ] **Step 2.3: Sema の `SpawnExpr` 禁止リストに 2 つ追加**

`sema.py:352` 付近の `if call.callee == "print":` / `if call.callee == "len":` ブロックの **直後** に追加:

```python
                if call.callee == "bytes_from_str":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `bytes_from_str`",
                    ))
                if call.callee == "str_from_bytes":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `str_from_bytes`",
                    ))
```

### irgen

- [ ] **Step 2.4: irgen の `llvm_type_of` に Bytes を追加**

`rwc/irgen.py:40` 付近の `llvm_type_of` 関数で、`if t is T.STRING:` の **直下** に追加:

```python
    if t is T.BYTES:
        return RW_STR_TY
```

- [ ] **Step 2.5: irgen の `_decl_spawn` / `_decl_await` で T.BYTES を string と同じ経路に**

`rwc/irgen.py:93` 付近の `_decl_spawn` 内の `elif ret_ty is T.STRING:` を:

```python
        elif ret_ty is T.STRING or ret_ty is T.BYTES:
            name, ret_llvm = "rw_spawn_str", RW_STR_TY
```

に変更。`_decl_await` 内の `elif ret_ty is T.STRING:` も同様に:

```python
        elif ret_ty is T.STRING or ret_ty is T.BYTES:
            name, ret_llvm = "rw_await_str", RW_STR_TY
```

に変更。

- [ ] **Step 2.6: irgen の `_emit_binop` で `==` / `!=` が Bytes を扱えるよう拡張**

`rwc/irgen.py:340` 付近の `_emit_binop` 内、`is_str = lty is T.STRING` を以下に変更:

```python
        is_str = lty is T.STRING
        is_strlike = lty is T.STRING or lty is T.BYTES
```

そして `_emit_binop` 内の `elif is_str and op in ("==", "!="):` ブロック (irgen.py 内、xor で `!=` を反転している箇所) の条件を `is_str` から `is_strlike` に変更:

```python
            elif is_strlike and op in ("==", "!="):
                eq_i8 = b.call(self._rw_str_eq, [l, r])
                i1 = b.icmp_unsigned("!=", eq_i8, ir.Constant(I8, 0))
                if op == "!=":
                    i1 = b.xor(i1, ir.Constant(ir.IntType(1), 1))
```

`+` の string 分岐 (`if is_str and op == "+":`) は **そのまま** で OK (= Bytes には連結が無いので、`is_str` のままで Bytes は素通り = arith op 経路で型エラー、最終的に `raise RuntimeError(f"arith op {op} on {lty}")` に落ちる)。だがそれは irgen が落ちるのでまずい — **Sema で Bytes + Bytes を弾く必要がある**。これは Step 2.2 までで `_check_expr` の `+` ハンドラを通った時点で「両辺同型なら BinOp 一般のチェックに進む → string 特例 → numeric チェック (Bytes は is_numeric=False) → "operator + requires int or float" エラー」で弾かれるはず。要確認 (Step 2.10 の negative テストで)。

- [ ] **Step 2.7: irgen の `_emit_call` で `len`, `bytes_from_str`, `str_from_bytes` を扱う**

`rwc/irgen.py:386` 付近の `if call.callee == "len":` ブロックは **そのまま** で OK (= 引数の SSA 値を `rw_str_len` に渡すだけ、引数の Sema 型が string でも Bytes でも IR レベルでは同じ `RW_STR_TY`)。

`len` 分岐の **直下** に追加:

```python
        if call.callee in ("bytes_from_str", "str_from_bytes"):
            # Both are noops at the IR level: the value carries the
            # same {len, ptr} layout, only the sema type changes.
            return self._emit_expr(call.args[0], ctx)
```

### テスト

- [ ] **Step 2.8: Positive テストを追加**

`tests/test_sema.py` の末尾に追加:

```python
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
```

- [ ] **Step 2.9: Negative テストを追加**

`tests/test_sema.py` の末尾に続けて追加:

```python
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
```

- [ ] **Step 2.10: Sema テスト全件を走らせる**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -v 2>&1 | tail -30
```

Expected: 既存 27 + Task 1 で追加した 2 + ここで追加した positive 5 + negative 5 = 39 件全部 PASS。

`test_bytes_plus_bytes_is_type_error` で `+` のエラーが期待通り出るかは特に重要。`is_numeric(T.BYTES) == False` で `"operator + requires int or float"` メッセージが出るはず。出ない場合は `_check_expr` の BinOp 分岐 (`sema.py:298` 付近) を読み直して、Bytes が予想外のルートを通っていないか確認する。

- [ ] **Step 2.11: 単独で IR 生成 + 実行で smoke check**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
cat > /tmp/bytes_smoke.rw <<'EOF'
def main() -> int:
    b: Bytes = bytes_from_str("hello")
    print(len(b))
    if b == bytes_from_str("hello"):
        print("ok")
    s: string = str_from_bytes(b)
    print(s)
    return 0
EOF
uv run rwc emit-ir /tmp/bytes_smoke.rw 2>&1 | grep -E "rw_str_(eq|len)"
echo "---"
uv run rwc run /tmp/bytes_smoke.rw
```

Expected: IR に `rw_str_len`, `rw_str_eq` の `call` が出る。実行結果:

```
5
ok
hello
```

- [ ] **Step 2.12: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/sema.py rwc/irgen.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: Bytes operations in sema + irgen

Sema:
  - Overload `len` to accept Bytes (was string-only).
  - Add `bytes_from_str(string) -> Bytes` and
    `str_from_bytes(Bytes) -> string` as builtins.
  - Forbid spawn of those two new builtins.
  - `==` / `!=` on Bytes goes through the existing same-type check
    and lands on the new irgen branch below.

irgen:
  - llvm_type_of(T.BYTES) -> RW_STR_TY (shared with string).
  - _decl_spawn / _decl_await collapse T.STRING and T.BYTES onto
    rw_spawn_str / rw_await_str (same ABI shape).
  - _emit_binop ==/!= now accepts the string-like family (string or
    Bytes), routing through rw_str_eq with xor for !=.
  - _emit_call recognises bytes_from_str / str_from_bytes as
    no-ops that just thread the underlying SSA value through.

Tests (10 new): positive coverage of all four ops + Future[Bytes],
and negative coverage of print(Bytes), Bytes + Bytes, Bytes ==
string, wrong-arg-type, and spawn-of-builtin.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: example + e2e

**Files:**
- Create: `examples/bytes_basic.rw`
- Create: `examples/bytes_basic.rw.expected`
- Modify: `tests/test_e2e.py` (parametrize に bytes_basic を追加)

- [ ] **Step 3.1: `examples/bytes_basic.rw` を書く**

ファイル `examples/bytes_basic.rw`:

```rw
def main() -> int:
    b: Bytes = bytes_from_str("hello")
    print(len(b))
    if b == bytes_from_str("hello"):
        print("eq ok")
    s: string = str_from_bytes(b)
    print(s)
    return 0
```

- [ ] **Step 3.2: `examples/bytes_basic.rw.expected` を書く**

ファイル `examples/bytes_basic.rw.expected`:

```
5
eq ok
hello
```

(末尾改行ありで保存。)

- [ ] **Step 3.3: 手元で実行して期待出力と一致するか確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
diff <(RW_WORKERS=1 uv run rwc run examples/bytes_basic.rw 2>&1) examples/bytes_basic.rw.expected && echo OK
```

Expected: `OK` だけが表示される (diff 出力なし)。

- [ ] **Step 3.4: `tests/test_e2e.py` の parametrize に `bytes_basic` を追加**

`tests/test_e2e.py:45` の以下の行:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops"],
```

を:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic"],
```

に変更。

- [ ] **Step 3.5: 全 pytest を走らせる**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 既存 75 + Task 1 sema 新規 2 + Task 2 sema 新規 10 + Task 3 e2e 新規 1 = **88 件** 全緑。

- [ ] **Step 3.6: 既存 example を回帰確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/hello.rw
RW_WORKERS=1 uv run rwc run examples/string_ops.rw
RW_WORKERS=1 uv run rwc run examples/spawn_many.rw
```

Expected:
```
hello
---
hello, world
12
eq ok
neq ok
---
30
```

- [ ] **Step 3.7: ランタイム単体テストも緑か確認**

ランタイムには手を入れていないが、念のため:

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make clean && make
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c librw.a -o fiber/test_sched && ./fiber/test_sched
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_str_ops.c librw.a -o fiber/test_str_ops && ./fiber/test_str_ops
```

Expected: `total = 333833500` / `all str_ops tests passed`。

- [ ] **Step 3.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add examples/bytes_basic.rw examples/bytes_basic.rw.expected tests/test_e2e.py
git commit -m "$(cat <<'EOF'
examples: add bytes_basic exercising the new Bytes type

examples/bytes_basic.rw uses every Bytes operation introduced in
this PR in a single main:
  - bytes_from_str on a literal
  - len on Bytes
  - Bytes == Bytes
  - str_from_bytes round-trip back to string for printing

The .expected captures the byte-for-byte stdout, and
tests/test_e2e.py picks it up via the existing parametrize list.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

### Spec coverage

| Spec 要求 | カバーするタスク |
|---|---|
| プリミティブ型 Bytes (キーワード `Bytes`) | Task 1.1, 1.2, 1.3, 1.4 |
| 内部表現は string と共通 (`RW_STR_TY`) | Task 2.4 (llvm_type_of) |
| `len(b: Bytes) -> int` | Task 2.1 (sema), 2.7 (irgen 既存ルート流用) + test 2.8 + 2.10 |
| `Bytes == Bytes`, `Bytes != Bytes` | Task 2.6 (irgen is_strlike) + test 2.8 + 2.10 |
| `bytes_from_str(s: string) -> Bytes` | Task 2.2 (sema), 2.7 (irgen noop) + test 2.8, 2.9 |
| `str_from_bytes(b: Bytes) -> string` | Task 2.2 (sema), 2.7 (irgen noop) + test 2.8 |
| `Future[Bytes]` で spawn/await | Task 2.5 + test `test_future_bytes_ok` (2.8) |
| `Bytes + Bytes` は禁止 | sema 既存ルートで自動的に numeric チェックに落ちる + test `test_bytes_plus_bytes_is_type_error` (2.9) |
| `print(Bytes)` は禁止 | `is_printable` を変えないことで自動的に弾かれる + test `test_print_bytes_is_type_error` (2.9) |
| Bytes と string の混合 `==` 禁止 | sema 既存「same type」チェックで自動 + test `test_bytes_eq_string_is_type_error` (2.9) |
| `spawn bytes_from_str(...)` 禁止 | Task 2.3 + test `test_cannot_spawn_bytes_from_str` (2.9) |
| ランタイムには手を入れない | Task 3.7 で C テスト緑を確認 |
| 既存テスト緑 | Task 3.5 (pytest 全件) + Task 3.6 (example 回帰) |

すべての spec 要求にタスクが割り当てられている。

### Placeholder スキャン

「TBD」「TODO」「(要確認)」「fill in」「Add appropriate」「Similar to Task N」は plan 内 0 件。Step 2.6 末尾の「要確認 (Step 2.10 の negative テストで)」は **検証手段が plan に明示されている** ので placeholder ではない (= 同じ commit 内で test を書いて確認するという指示)。

### Type consistency

- `T.BYTES` の名前は Task 1.1, 1.4, 2.1, 2.2, 2.4, 2.5, 2.6, 2.7 で完全一致
- LLVM 表現は `RW_STR_TY` で Task 2.4, 2.5 で揃っている
- 組込み関数名: `bytes_from_str` / `str_from_bytes` / `len` で Task 2 全体で揃っている
- ランタイム呼び出し: `rw_str_len` / `rw_str_eq` / `rw_spawn_str` / `rw_await_str` を新規追加なしで流用 (irgen 既存定義をそのまま呼ぶ)
