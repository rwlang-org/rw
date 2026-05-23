# Result[int, int] + match Ok/Err Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rw 言語に `Result[int, int]` 型と `Ok(e)` / `Err(e)` の値構築式を追加し、既存の `match` 文を「Some/None ペア」または「Ok/Err ペア」のどちらか 2 アームを受けるよう拡張する。`div_checked(a, b) -> Result[int, int]` のような関数を rw 側で書いて `match` で分解できる状態に持っていく。

**Architecture:** `Result[int, int]` は `{i64 tag, i64 payload}` (tag=0 = Err, 1 = Ok) で `Option[int]` と同じ LLVM struct を持つが、Sema レベルで `T.OPTION_INT` と `T.RESULT_INT_INT` を別の型として区別する。`MatchStmt` AST に `style: "option" | "result"` フィールドを追加し、parser が最初のアームを見て style を確定する。Sema / irgen は style 別に分岐し、irgen 側は `_emit_arm` 共通 helper で Option / Result lowering を共有する。

**Tech Stack:** Python 3.12 + llvmlite (コンパイラ)、pytest (テスト)。ランタイム変更なし。

**Spec:** `docs/specs/11-result-type.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `rwc/types.py` | プリミティブ型定義 | `RESULT_INT_INT` 追加 |
| `rwc/lexer.py` | キーワード認識 | `KW_RESULT` / `KW_OK` / `KW_ERR` |
| `rwc/ast_nodes.py` | AST ノード | `OkExpr` / `ErrExpr` 追加、`MatchStmt` に style と Result 用フィールド追加 |
| `rwc/parser.py` | 型 + 式 + 文のパース | `parse_type` の Result 分岐、`Ok(e)` / `Err(e)` 式、`parse_match` 全面書き直し |
| `rwc/sema.py` | 型解決 + 式/文 Sema | `_resolve_type` / `OkExpr` / `ErrExpr` Sema / `MatchStmt` style 分岐 |
| `rwc/irgen.py` | LLVM IR 生成 | `RW_RESULT_INT_INT_TY` / `Ok` / `Err` emit / `MatchStmt` を `_emit_arm` 経由で style 別 lowering |
| `tests/test_sema.py` | 型検査 positive/negative | テスト追加 |
| `tests/test_e2e.py` | parametrize に result_basic | 1 行追加 |
| `examples/result_basic.rw` | デモ | 新規 |
| `examples/result_basic.rw.expected` | 期待出力 | 新規 |

ランタイム (`runtime/*`) と fiber 関連には一切触れない。

---

## Task 1: lexer / parser / types / AST で `Result[int, int]` 構文を認識

このタスクで `Result[int, int]` の型注釈、`Ok(e)` / `Err(e)` の式、`match v: case Ok(x): ... case Err(e): ...` の文 (および既存 Some/None) が **AST まで構築できる** ようにする。`MatchStmt` の AST 構造を style 統合形に書き換える破壊的変更を含むので、既存の Option-style コード/テストが回帰しないことを確認する。

**Files:**
- Modify: `rwc/types.py`
- Modify: `rwc/lexer.py`
- Modify: `rwc/ast_nodes.py`
- Modify: `rwc/parser.py`
- Modify: `rwc/sema.py` (`_resolve_type` だけ追加 + 既存 `_check_stmt` の MatchStmt を style 分岐に対応)
- Modify: `rwc/irgen.py` (既存 `_emit_stmt` の MatchStmt を style 分岐に対応)
- Modify: `tests/test_sema.py`

### types / lexer

- [ ] **Step 1.1: `rwc/types.py` に `RESULT_INT_INT` を追加**

`OPTION_INT = _Primitive("Option[int]")` の **直下** に追加:

```python
RESULT_INT_INT = _Primitive("Result[int, int]")
```

`is_printable` / `is_numeric` には含めない。

- [ ] **Step 1.2: `rwc/lexer.py` に 3 つのキーワードを追加**

`TokenKind` enum の `KW_NONE = auto()` の **直下** に:

```python
    KW_RESULT = auto()
    KW_OK = auto()
    KW_ERR = auto()
```

`KEYWORDS` dict の `"None": TokenKind.KW_NONE,` の **直下** に:

```python
    "Result": TokenKind.KW_RESULT,
    "Ok":     TokenKind.KW_OK,
    "Err":    TokenKind.KW_ERR,
```

### AST

- [ ] **Step 1.3: `rwc/ast_nodes.py` に `OkExpr` / `ErrExpr` を追加**

既存 `NoneExpr` の **直下** に:

```python
@dataclass
class OkExpr:
    arg: "Expr"
    line: int
    col: int


@dataclass
class ErrExpr:
    arg: "Expr"
    line: int
    col: int
```

`Expr` Union に 2 つ追加:

```python
Expr = Union[
    IntLit, FloatLit, BoolLit, StringLit, Name,
    UnaryOp, BinOp, Call, SpawnExpr, AwaitExpr,
    SomeExpr, NoneExpr,
    OkExpr, ErrExpr,
]
```

- [ ] **Step 1.4: `rwc/ast_nodes.py` の `MatchStmt` を style 統合形に拡張**

現在の `MatchStmt`:

```python
@dataclass
class MatchStmt:
    target: Expr
    some_var: str
    some_block: List["Stmt"]
    none_block: List["Stmt"]
    line: int
    col: int
```

を以下に置き換え:

```python
@dataclass
class MatchStmt:
    target: Expr
    style: str                              # "option" or "result"
    # Option-style fields (None when style == "result")
    some_var: Optional[str]
    some_block: Optional[List["Stmt"]]
    none_block: Optional[List["Stmt"]]
    # Result-style fields (None when style == "option")
    ok_var: Optional[str]
    ok_block: Optional[List["Stmt"]]
    err_var: Optional[str]
    err_block: Optional[List["Stmt"]]
    line: int
    col: int
```

`Optional` を import していなければ追加。

### parser

- [ ] **Step 1.5: `parse_type` の Option 分岐の直下に Result 分岐**

`rwc/parser.py` の `parse_type` 内、Option 分岐の **直下** に追加:

```python
        if t.kind == TokenKind.KW_RESULT:
            self.i += 1
            self.eat(TokenKind.LBRACK, "'[' after Result")
            inner1 = self.cur
            if inner1.kind != TokenKind.KW_INT:
                raise ParserError(
                    "only Result[int, int] is supported in this version of rw",
                    inner1.line, inner1.col, max(1, len(inner1.value)),
                )
            self.i += 1
            self.eat(TokenKind.COMMA, "',' between Result type arguments")
            inner2 = self.cur
            if inner2.kind != TokenKind.KW_INT:
                raise ParserError(
                    "only Result[int, int] is supported in this version of rw",
                    inner2.line, inner2.col, max(1, len(inner2.value)),
                )
            self.i += 1
            self.eat(TokenKind.RBRACK, "']' to close Result[int, int]")
            return A.TypeName("Result[int, int]", t.line, t.col)
```

- [ ] **Step 1.6: `parse_unary` で `Ok(e)` / `Err(e)` を式として受ける**

`KW_NONE` 分岐の **直下** に追加:

```python
        if t.kind == TokenKind.KW_OK:
            self.i += 1
            self.eat(TokenKind.LPAREN, "'(' after Ok")
            arg = self.parse_expr()
            self.eat(TokenKind.RPAREN, "')' to close Ok(...)")
            return A.OkExpr(arg, t.line, t.col)
        if t.kind == TokenKind.KW_ERR:
            self.i += 1
            self.eat(TokenKind.LPAREN, "'(' after Err")
            arg = self.parse_expr()
            self.eat(TokenKind.RPAREN, "')' to close Err(...)")
            return A.ErrExpr(arg, t.line, t.col)
```

- [ ] **Step 1.7: `parse_match` を style 統合形に書き直す**

既存 `parse_match` メソッドを **丸ごと** 以下に置き換え:

```python
    def parse_match(self) -> A.MatchStmt:
        kw = self.eat(TokenKind.KW_MATCH)
        target = self.parse_expr()
        self.eat(TokenKind.COLON, "':' after match target")
        self.eat(TokenKind.NEWLINE)
        self.eat(TokenKind.INDENT, "indented match body")

        style: Optional[str] = None  # "option" or "result"
        some_var: Optional[str] = None
        some_block: Optional[List[A.Stmt]] = None
        none_block: Optional[List[A.Stmt]] = None
        ok_var: Optional[str] = None
        ok_block: Optional[List[A.Stmt]] = None
        err_var: Optional[str] = None
        err_block: Optional[List[A.Stmt]] = None

        while self.cur.kind != TokenKind.DEDENT:
            if self.cur.kind == TokenKind.NEWLINE:
                self.i += 1
                continue
            if self.cur.kind != TokenKind.KW_CASE:
                raise ParserError(
                    "expected `case` arm in match body",
                    self.cur.line, self.cur.col,
                    max(1, len(self.cur.value)),
                )
            self.eat(TokenKind.KW_CASE)
            arm_tok = self.cur

            # Determine this arm's style.
            if arm_tok.kind in (TokenKind.KW_SOME, TokenKind.KW_NONE):
                this_style = "option"
            elif arm_tok.kind in (TokenKind.KW_OK, TokenKind.KW_ERR):
                this_style = "result"
            else:
                raise ParserError(
                    "match case must be `Some(x)` / `None` or `Ok(x)` / `Err(e)`",
                    arm_tok.line, arm_tok.col,
                    max(1, len(arm_tok.value)),
                )

            # Lock in style on the first arm; reject mixed pairs.
            if style is None:
                style = this_style
            elif style != this_style:
                expected = "Some/None" if style == "option" else "Ok/Err"
                raise ParserError(
                    f"mixed match arms: expected `{expected}` pair, got `{arm_tok.value}`",
                    arm_tok.line, arm_tok.col,
                    max(1, len(arm_tok.value)),
                )

            if arm_tok.kind == TokenKind.KW_SOME:
                if some_block is not None:
                    raise ParserError(
                        "duplicate `case Some(...)` arm in match",
                        arm_tok.line, arm_tok.col, 4,
                    )
                self.eat(TokenKind.KW_SOME)
                self.eat(TokenKind.LPAREN, "'(' after Some")
                ident = self.eat(TokenKind.IDENT, "identifier in Some(...)")
                self.eat(TokenKind.RPAREN, "')' to close Some(...)")
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE)
                some_var = ident.value
                some_block = self.parse_block()
            elif arm_tok.kind == TokenKind.KW_NONE:
                if none_block is not None:
                    raise ParserError(
                        "duplicate `case None` arm in match",
                        arm_tok.line, arm_tok.col, 4,
                    )
                self.eat(TokenKind.KW_NONE)
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE)
                none_block = self.parse_block()
            elif arm_tok.kind == TokenKind.KW_OK:
                if ok_block is not None:
                    raise ParserError(
                        "duplicate `case Ok(...)` arm in match",
                        arm_tok.line, arm_tok.col, 2,
                    )
                self.eat(TokenKind.KW_OK)
                self.eat(TokenKind.LPAREN, "'(' after Ok")
                ident = self.eat(TokenKind.IDENT, "identifier in Ok(...)")
                self.eat(TokenKind.RPAREN, "')' to close Ok(...)")
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE)
                ok_var = ident.value
                ok_block = self.parse_block()
            elif arm_tok.kind == TokenKind.KW_ERR:
                if err_block is not None:
                    raise ParserError(
                        "duplicate `case Err(...)` arm in match",
                        arm_tok.line, arm_tok.col, 3,
                    )
                self.eat(TokenKind.KW_ERR)
                self.eat(TokenKind.LPAREN, "'(' after Err")
                ident = self.eat(TokenKind.IDENT, "identifier in Err(...)")
                self.eat(TokenKind.RPAREN, "')' to close Err(...)")
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE)
                err_var = ident.value
                err_block = self.parse_block()

        self.eat(TokenKind.DEDENT)

        if style is None:
            raise ParserError(
                "match must have at least one case arm",
                kw.line, kw.col, 5,
            )
        if style == "option":
            if some_block is None or none_block is None or some_var is None:
                raise ParserError(
                    "match on Option[int] must cover both Some and None",
                    kw.line, kw.col, 5,
                )
        else:  # "result"
            if ok_block is None or err_block is None or ok_var is None or err_var is None:
                raise ParserError(
                    "match on Result[int, int] must cover both Ok and Err",
                    kw.line, kw.col, 5,
                )

        return A.MatchStmt(
            target, style,
            some_var, some_block, none_block,
            ok_var, ok_block, err_var, err_block,
            kw.line, kw.col,
        )
```

### Sema and irgen: minimal updates to match the new AST shape

- [ ] **Step 1.8: `_resolve_type` に `Result[int, int]` を追加**

`rwc/sema.py` の `_resolve_type` の `m` dict に 1 行:

```python
        m = {
            "int": T.INT,
            "float": T.FLOAT,
            "bool": T.BOOL,
            "string": T.STRING,
            "Bytes": T.BYTES,
            "List[int]": T.LIST_INT,
            "Option[int]": T.OPTION_INT,
            "Result[int, int]": T.RESULT_INT_INT,
            "void": T.VOID,
        }
```

- [ ] **Step 1.9: 既存 Sema の `MatchStmt` 分岐を style 分岐対応に最小修正**

`rwc/sema.py` の `_check_stmt` 内の `MatchStmt` 分岐 (現状は Option-style ハードコード) を以下に置き換え。Task 2 で `style == "result"` の本格 Sema が完成するが、ここでは「既存の Option 用ロジックを `style == "option"` のときだけ走らせる」最小ガードを入れて、新 AST フィールドの存在で既存テストが死なないようにする:

```python
        if isinstance(stmt, A.MatchStmt):
            tt = self._check_expr(fn, stmt.target, locals_)
            if stmt.style == "option":
                if tt is not T.OPTION_INT:
                    raise CompileError(Diagnostic(
                        self.filename, stmt.line, stmt.col, 5,
                        f"match target must be Option[int], found `{tt}`",
                    ))
                some_locals = dict(locals_)
                some_locals[stmt.some_var] = T.INT
                some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
                none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
                return some_ret and none_ret
            # style == "result" — handled in Task 2; reject for now so
            # accidentally-constructed Result matches fail with a clear
            # message rather than crashing.
            raise CompileError(Diagnostic(
                self.filename, stmt.line, stmt.col, 5,
                "internal: Result-style match not yet implemented in sema",
            ))
```

- [ ] **Step 1.10: 既存 irgen の `MatchStmt` 分岐も同様に style ガード**

`rwc/irgen.py` の `_emit_stmt` 内の `MatchStmt` 分岐の最初に 1 行追加:

```python
        if isinstance(stmt, A.MatchStmt):
            if stmt.style != "option":
                # style == "result" lowering lands in Task 3.
                raise RuntimeError("internal: Result-style match not yet implemented in irgen")
            v = self._emit_expr(stmt.target, ctx)
            tag = b.extract_value(v, 0)
            # ... existing Option-style lowering, unchanged ...
```

(現状の Option-style 本体はそのまま `if stmt.style != "option": raise ...` の **下** に残す)

### Tests for Task 1

- [ ] **Step 1.11: 型注釈 + 構文だけ通るテスト**

`tests/test_sema.py` の末尾に追加:

```python
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
```

- [ ] **Step 1.12: 既存テストを含めて pytest 全件を回し緑か確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 既存 115 + Task 1 新規 3 = `118 passed`。

既存 Option-style の `test_match_*` 系テストは parser/Sema が新 AST を出すよう
になっても挙動同一を保つ前提。もし落ちる場合は `MatchStmt` のフィールド
順序や Optional 設定を Step 1.4 と照合。

- [ ] **Step 1.13: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/types.py rwc/lexer.py rwc/ast_nodes.py rwc/parser.py rwc/sema.py rwc/irgen.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: introduce Result[int, int] in lexer / parser / ast (parse-only)

Adds the primitive type T.RESULT_INT_INT, three new lexer keywords
(Result / Ok / Err), parser branches for the Result[int, int] type
annotation, Ok(e) / Err(e) expressions, and a major rewrite of
parse_match: the first case arm now locks in a style ("option" or
"result") and subsequent arms must match.

AST gets two new expression nodes (OkExpr, ErrExpr) and MatchStmt
grows a `style` field plus Option-style and Result-style fields
(Optional, exclusive per style).

Sema and irgen recognise the new MatchStmt shape but only handle
style == "option" for now; Result-style matches raise a clear
"not yet implemented" error so the next two commits can land them
in isolation.

Parser-level negatives covered: Result[T, E] for non-int T or E
rejects; mixed arms (Some + Err, Ok + None, etc.) reject with
"mixed match arms".

Existing Option tests stay green: the parser still locks Option
matches with Some / None as before, just via the generalised
case-lookup table.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Sema で `OkExpr` / `ErrExpr` / Result-style `MatchStmt` を完成させる

このタスクで Sema が Result の式と match を理解する。Task 1 で `"not yet implemented"` を投げていた箇所を実装で置き換える。

**Files:**
- Modify: `rwc/sema.py`
- Modify: `tests/test_sema.py`

- [ ] **Step 2.1: `_check_expr` で `OkExpr` / `ErrExpr` を扱う**

`rwc/sema.py` の `_check_expr` で、既存 `SomeExpr` / `NoneExpr` の **直下** に追加:

```python
        if isinstance(expr, A.OkExpr):
            at = self._check_expr(fn, expr.arg, locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 2,
                    f"Ok argument must be int, found `{at}`",
                ))
            return T.RESULT_INT_INT
        if isinstance(expr, A.ErrExpr):
            at = self._check_expr(fn, expr.arg, locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 3,
                    f"Err argument must be int, found `{at}`",
                ))
            return T.RESULT_INT_INT
```

- [ ] **Step 2.2: `_check_stmt` の `MatchStmt` の Result-style ガードを実装に置き換え**

Task 1 で `raise "not yet implemented"` していた箇所を以下に置き換え:

```python
            if stmt.style == "option":
                # ... existing Option-style body unchanged ...
                some_locals = dict(locals_)
                some_locals[stmt.some_var] = T.INT
                some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
                none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
                return some_ret and none_ret
            # style == "result"
            if tt is not T.RESULT_INT_INT:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 5,
                    f"match target must be Result[int, int], found `{tt}`",
                ))
            ok_locals = dict(locals_)
            ok_locals[stmt.ok_var] = T.INT
            ok_ret = self._check_block(fn, stmt.ok_block, ok_locals, ret_ty)
            err_locals = dict(locals_)
            err_locals[stmt.err_var] = T.INT
            err_ret = self._check_block(fn, stmt.err_block, err_locals, ret_ty)
            return ok_ret and err_ret
```

- [ ] **Step 2.3: Positive テスト**

`tests/test_sema.py` の末尾に追加:

```python
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
```

- [ ] **Step 2.4: Negative テスト**

`tests/test_sema.py` に続けて追加:

```python
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
```

- [ ] **Step 2.5: pytest を走らせる**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -q 2>&1 | tail -5
```

Expected: 既存 64 + Task 1 で追加した 3 + ここで positive 6 + negative 6 = `79 passed`。

irgen がまだ Result-style match を扱わないので `examples/result_basic.rw` を
実行しようとすると失敗する (Step 1.10 で `"not yet implemented"` を投げる)。
Sema レベルでは完全に通っている状態。

- [ ] **Step 2.6: 全 pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 既存 115 + Task 1 で 3 + Task 2 で 12 = `130 passed`。

- [ ] **Step 2.7: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/sema.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: sema for Ok / Err / Result-style match

Sema:
  - OkExpr / ErrExpr require their arg to be int, both return
    Result[int, int].
  - MatchStmt with style == "result" requires target to be
    Result[int, int], binds Ok(x) and Err(e) patterns' identifiers
    as int in their respective arm scopes, and treats the whole
    statement as "terminates in return" iff both arms terminate
    (same pattern as Option's match).
  - Result[int, int] is deliberately absent from the == whitelist,
    so a == b comparisons get caught at sema.
  - Mixed Ok == Some style comparisons get caught earlier by the
    existing "same type" check (covered as a bonus negative).

Tests: 12 new in test_sema.py covering Ok/Err typing, both-arms
match, bound-as-int (both Ok and Err arms), return coverage via
Result match, and six negatives (Ok("hi"), Err("hi"), match on
int, print(Result), Result == , Ok == Some).

irgen still doesn't know how to emit Result-style match — code
that actually executes those forms will still crash with the
"not yet implemented" error from Task 1 until the next commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: irgen で `Ok` / `Err` / Result-style `MatchStmt` を実装

このタスクで rw コードが実際に Result を扱って動く。Task 1 で立てた irgen の
ガード (`if stmt.style != "option": raise`) を外し、Option / Result の lowering
を共通 helper `_emit_arm` 経由で実装する。

**Files:**
- Modify: `rwc/irgen.py`

- [ ] **Step 3.1: `RW_RESULT_INT_INT_TY` を irgen に追加**

`rwc/irgen.py` 上部、`RW_OPTION_INT_TY = ...` の **直下** に:

```python
RW_RESULT_INT_INT_TY = ir.LiteralStructType([I64, I64])  # {tag, payload}
```

- [ ] **Step 3.2: `llvm_type_of` に Result を追加**

`if t is T.OPTION_INT:` の **直下** に:

```python
    if t is T.RESULT_INT_INT:
        return RW_RESULT_INT_INT_TY
```

- [ ] **Step 3.3: `_emit_expr` で `OkExpr` / `ErrExpr` を扱う**

既存 `SomeExpr` / `NoneExpr` 分岐の **直下** に追加:

```python
        if isinstance(expr, A.OkExpr):
            v = self._emit_expr(expr.arg, ctx)
            base = ir.Constant(RW_RESULT_INT_INT_TY,
                               [ir.Constant(I64, 1), ir.Constant(I64, 0)])
            return ctx.builder.insert_value(base, v, 1)
        if isinstance(expr, A.ErrExpr):
            v = self._emit_expr(expr.arg, ctx)
            base = ir.Constant(RW_RESULT_INT_INT_TY,
                               [ir.Constant(I64, 0), ir.Constant(I64, 0)])
            return ctx.builder.insert_value(base, v, 1)
```

- [ ] **Step 3.4: `_emit_arm` 共通 helper を追加**

既存 `_emit_match` の **直上** (なければ `_emit_stmt` の前) にメソッドを追加:

```python
    def _emit_arm(self, var_name, block, payload, ctx, end_bb):
        """Emit one match arm. var_name=None means no payload binding."""
        b = ctx.builder
        if var_name is not None:
            slot = b.alloca(I64, name=var_name)
            b.store(payload, slot)
            saved = ctx.locals.get(var_name)
            ctx.locals[var_name] = slot
            self._emit_block(block, ctx)
            if saved is not None:
                ctx.locals[var_name] = saved
            else:
                ctx.locals.pop(var_name, None)
        else:
            self._emit_block(block, ctx)
        if not b.block.is_terminated:
            b.branch(end_bb)
```

- [ ] **Step 3.5: `_emit_stmt` の `MatchStmt` を style 別 lowering で書き直す**

Task 1 で立てた `if stmt.style != "option": raise ...` のガードを **削除** し、
既存の Option-style 本体も含めて以下に置き換え (`_emit_match` というメソッド
名で切り出しても良いが、ここではインラインに):

```python
        if isinstance(stmt, A.MatchStmt):
            v = self._emit_expr(stmt.target, ctx)
            tag = b.extract_value(v, 0)
            payload = b.extract_value(v, 1)
            arm1_bb = ctx.function.append_basic_block("match.arm1")
            arm0_bb = ctx.function.append_basic_block("match.arm0")
            end_bb = ctx.function.append_basic_block("match.end")
            # tag == 1 -> arm1, default (tag == 0) -> arm0.
            sw = b.switch(tag, arm0_bb)
            sw.add_case(ir.Constant(I64, 1), arm1_bb)
            if stmt.style == "option":
                var1, block1 = stmt.some_var, stmt.some_block   # Some(payload)
                var0, block0 = None, stmt.none_block            # None (no bind)
            else:  # "result"
                var1, block1 = stmt.ok_var, stmt.ok_block       # Ok(payload)
                var0, block0 = stmt.err_var, stmt.err_block     # Err(payload)
            # arm1 (tag == 1)
            b.position_at_end(arm1_bb)
            self._emit_arm(var1, block1, payload, ctx, end_bb)
            # arm0 (tag == 0)
            b.position_at_end(arm0_bb)
            self._emit_arm(var0, block0, payload, ctx, end_bb)
            b.position_at_end(end_bb)
            return
```

- [ ] **Step 3.6: smoke check**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
cat > /tmp/result_smoke.rw <<'EOF'
def div_checked(a: int, b: int) -> Result[int, int]:
    if b == 0:
        return Err(0)
    return Ok(a / b)

def main() -> int:
    r: Result[int, int] = div_checked(10, 2)
    match r:
        case Ok(x):
            print(x)
        case Err(e):
            print(-1)
    e: Result[int, int] = div_checked(10, 0)
    match e:
        case Ok(x):
            print(x)
        case Err(code):
            print(code)
    return 0
EOF
uv run rwc emit-ir /tmp/result_smoke.rw 2>&1 | grep -E "switch|insertvalue" | head -10
echo "---"
uv run rwc run /tmp/result_smoke.rw
```

Expected:

IR に `switch i64` と `insertvalue` がそれぞれ含まれる (`{i64 0, i64 0}` の constant Err base と `{i64 1, i64 0}` の constant Ok base が両方出る)。

実行結果:
```
5
0
```

- [ ] **Step 3.7: 既存 Option の smoke も回帰なし**

```sh
RW_WORKERS=1 uv run rwc run examples/option_basic.rw
```

Expected:
```
5
-1
```

`_emit_arm` 経由で Option-style もこの commit で書き直されるので回帰確認は必須。

- [ ] **Step 3.8: 全 pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `130 passed` (Task 2 の数のまま、このタスクでテスト追加なし)。

- [ ] **Step 3.9: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/irgen.py
git commit -m "$(cat <<'EOF'
rwc: irgen for Ok / Err / Result-style match

irgen:
  - RW_RESULT_INT_INT_TY = {i64 tag, i64 payload} (same shape as
    RW_OPTION_INT_TY but kept as a named alias for readability).
  - llvm_type_of(T.RESULT_INT_INT) -> RW_RESULT_INT_INT_TY.
  - OkExpr: insertvalue {1, 0} (tag = 1) with the arg into payload.
  - ErrExpr: insertvalue {0, 0} (tag = 0) with the arg into payload.
  - MatchStmt: factored out an _emit_arm helper that handles
    "allocate a slot for the bound var (if any), store the payload,
    emit the block, branch to end_bb if not terminated". Both
    Option (Some/None — None has no bound var) and Result (Ok/Err
    — both have bound vars) lowering call into it.
  - The "not yet implemented" guards from Task 1 are removed.

Smoke:
  uv run rwc run /tmp/result_smoke.rw
  -> 5\n0\n
  uv run rwc run examples/option_basic.rw
  -> 5\n-1\n (Option regression check)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: example + e2e

**Files:**
- Create: `examples/result_basic.rw`
- Create: `examples/result_basic.rw.expected`
- Modify: `tests/test_e2e.py`

- [ ] **Step 4.1: `examples/result_basic.rw` を書く**

```rw
def div_checked(a: int, b: int) -> Result[int, int]:
    if b == 0:
        return Err(0)
    return Ok(a / b)

def main() -> int:
    r: Result[int, int] = div_checked(10, 2)
    match r:
        case Ok(x):
            print(x)
        case Err(e):
            print(-1)
    e: Result[int, int] = div_checked(10, 0)
    match e:
        case Ok(x):
            print(x)
        case Err(code):
            print(code)
    return 0
```

- [ ] **Step 4.2: `examples/result_basic.rw.expected`**

```
5
0
```

(末尾改行ありで保存。)

- [ ] **Step 4.3: 手元で byte 一致**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
diff <(RW_WORKERS=1 uv run rwc run examples/result_basic.rw 2>&1) examples/result_basic.rw.expected && echo OK
```

Expected: `OK` だけが表示される。

- [ ] **Step 4.4: `tests/test_e2e.py` の parametrize に `result_basic` を追加**

`tests/test_e2e.py:45` の以下の行:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic"],
```

を以下に変更:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic", "result_basic"],
```

- [ ] **Step 4.5: 全 pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 130 + 1 = `131 passed`。

- [ ] **Step 4.6: 既存 example 回帰**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/option_basic.rw
RW_WORKERS=1 uv run rwc run examples/list_basic.rw
RW_WORKERS=1 uv run rwc run examples/string_ops.rw
RW_WORKERS=1 uv run rwc run examples/bytes_basic.rw
RW_WORKERS=1 uv run rwc run examples/spawn_many.rw
```

Expected:
```
5
-1
---
3
10
30
---
hello, world
12
eq ok
neq ok
---
5
eq ok
hello
---
30
```

- [ ] **Step 4.7: ランタイム単体テストも緑か (ランタイム変更なしだが念のため)**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make clean && make
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_option.c librw.a -o fiber/test_option && ./fiber/test_option
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c librw.a -o fiber/test_sched && ./fiber/test_sched
```

Expected: `all option tests passed` / `total = 333833500`。

- [ ] **Step 4.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add examples/result_basic.rw examples/result_basic.rw.expected tests/test_e2e.py
git commit -m "$(cat <<'EOF'
examples: add result_basic exercising Ok / Err / match Result-style

examples/result_basic.rw exercises the full Result[int, int]
surface: a div_checked helper that returns Ok(quotient) when b != 0
and Err(0) when b == 0, plus two match statements that print the
value (Ok arm) or the error code (Err arm).

The .expected captures the byte-for-byte stdout (5 then 0), and
tests/test_e2e.py picks it up via the existing parametrize list,
so any regression in lexer / parser / sema / irgen flows that
touch Ok / Err / Result-style match will fail the suite.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

### Spec coverage

| Spec 要求 | カバーするタスク |
|---|---|
| 新型 `Result[int, int]` (キーワード `Result`、parser が `[int, int]` 強制) | Task 1.1 / 1.2 / 1.5 / 1.8 |
| `Ok(int) -> Result[int, int]` / `Err(int) -> Result[int, int]` 式 | Task 1.3 (AST) / 1.6 (parser) / 2.1 (sema) / 3.3 (irgen) |
| match を Option / Result の 2 style に対応 | Task 1.4 (AST style field) / 1.7 (parse_match 書き直し) / 1.9 (sema 分岐) / 2.2 (Result-style sema) / 3.5 (irgen 分岐) |
| Some/None と Ok/Err の混在禁止 (parser) | Task 1.7 (`mixed match arms`) + test 1.11 |
| Result-style match の Ok/Err bound 変数が int | Task 2.2 (sema) + test 2.3 |
| return-coverage が Result の両 arm を見る | Task 2.2 (`ok_ret and err_ret`) + test 2.3 |
| 内部表現 16 バイト value 返し | Task 3.1 (`RW_RESULT_INT_INT_TY = LiteralStructType([I64, I64])`) |
| `Result[string, int]` 等は parser エラー | Task 1.5 + test 1.11 |
| match の片側欠落は parser エラー (両 style) | Task 1.7 + test 1.11 (mixed arms と片欠落は別エラー、両方が parser でカバー) |
| `print(Result[int, int])` は型エラー | `is_printable` 不変 + test 2.4 |
| `Result == Result` は型エラー | == whitelist 不変 + test 2.4 |
| `Ok(string)` / `Err(string)` は型エラー | Task 2.1 + test 2.4 |
| match ターゲットが Result[int, int] でないと型エラー | Task 2.2 + test 2.4 |
| `Future[Result[int, int]]` 禁止 | `_decl_spawn`/`_decl_await` に追加しないことで自動禁止 (Option と同じ判断) |
| 既存 115 テスト緑 | Task 1.12, 2.6, 3.8, 4.5 |
| Option-style match の挙動回帰なし | Task 1.7 の `style == "option"` パスを保つ + Task 3.5 の `_emit_arm` 共通化後の smoke (Step 3.7) + Task 4.6 |

すべての spec 要求にタスクがある。

### Placeholder スキャン

「TBD」「TODO」「(要確認)」「fill in」「Add appropriate」「Similar to Task N」は plan 内 0 件。`raise "not yet implemented"` ガード (Task 1.9 / 1.10) は **意図的な中間状態** で、Task 2 / Task 3 で削除されることが明示されている。placeholder ではない。

### Type consistency

- `T.RESULT_INT_INT` は Task 1.1 / 1.8 / 2.1 / 2.2 / 3.2 / 3.3 で完全一致
- LLVM 表現 `RW_RESULT_INT_INT_TY = LiteralStructType([I64, I64])` は Task 3.1 / 3.2 / 3.3 で揃っている
- 新規 AST ノード: `OkExpr` / `ErrExpr` を Task 1.3 (定義) / 1.6 (parser) / 2.1 (sema) / 3.3 (irgen) で揃って使用
- `MatchStmt` の新フィールド: `style` / `ok_var` / `ok_block` / `err_var` / `err_block` を Task 1.4 (定義) / 1.7 (parser) / 1.9 + 2.2 (sema) / 3.5 (irgen) で揃って使用
- Sema 型エラーメッセージ: `"Ok argument must be int"` / `"Err argument must be int"` / `"match target must be Result[int, int]"` を実装 (Task 2.1, 2.2) と negative テスト assert (Task 2.4) で完全一致
- `_emit_arm` の引数順 `(var_name, block, payload, ctx, end_bb)` は Task 3.4 (定義) / 3.5 (呼び出し) で一致
- 既存 Option-style の `some_var` / `some_block` / `none_block` フィールド名は Task 1.4 (AST 再定義) / 1.7 (parser) / 1.9 (sema) / 3.5 (irgen) で揃って維持
