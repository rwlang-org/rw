# Option[int] + match Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rw 言語に `Option[int]` 型と最小 `match` 文 (Python 3.10 風) を追加し、`safe_div(10, 0)` のような関数が `None` を返して `match` で分解できるコードを動かす。あわせて `list_at_opt(l, i) -> Option[int]` をランタイムに追加して、範囲外を `abort` でなく `None` で返せるようにする。

**Architecture:** `Option[int]` は 2 ワード fat struct `{i64 tag, i64 payload}` (tag=1 = Some, 0 = None) として LLVM IR で表現。サイズ 16 バイトなので arm64 / x86_64 SysV どちらもレジスタで返せる (pointer-out ABI 不要)。新キーワード `Option` / `match` / `case` / `Some` / `None` を導入、parser は `match` を statement として 2 アーム必須でパースし、irgen は `switch` 命令で lowering する。

**Tech Stack:** C11 (ランタイム)、Python 3.12 + llvmlite (コンパイラ)、pytest (テスト)。

**Spec:** `docs/specs/10-option-type.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `runtime/runtime.h` | ABI 宣言 | `rw_option_int` struct + `rw_list_int_at_opt` プロトタイプ |
| `runtime/runtime.c` | helper 実装 | `rw_list_int_at_opt` 追加 |
| `runtime/fiber/test_option.c` | C 単体テスト | 新規 |
| `.gitignore` | テストバイナリ | 1 行追加 |
| `rwc/types.py` | プリミティブ型定義 | `OPTION_INT` 追加 |
| `rwc/lexer.py` | キーワード認識 | `KW_OPTION` / `KW_MATCH` / `KW_CASE` / `KW_SOME` / `KW_NONE` |
| `rwc/parser.py` | 型 + 式 + 文のパース | `parse_type` の Option 分岐、Some/None 式、match 文 |
| `rwc/ast_nodes.py` | AST ノード | `SomeExpr` / `NoneExpr` / `MatchStmt` 追加 |
| `rwc/sema.py` | 型解決 + 式/文 Sema + return coverage | 4 ヶ所修正 |
| `rwc/irgen.py` | LLVM IR 生成 | `RW_OPTION_INT_TY` / Some/None/match の emit |
| `tests/test_sema.py` | 型検査 positive/negative | テスト追加 |
| `tests/test_e2e.py` | parametrize に option_basic | 1 行追加 |
| `examples/option_basic.rw` | デモ | 新規 |
| `examples/option_basic.rw.expected` | 期待出力 | 新規 |

---

## Task 1: lexer / parser / types / AST で `Option[int]` 構文を認識

このタスクのゴールは、`Option[int]` の型注釈、`Some(e)` / `None` の式、`match v: case Some(x): ... case None: ...` の文が **AST まで構築できる** こと。Sema / irgen は未実装のままで、`Some` を実際に評価しようとするとエラーになる。

**Files:**
- Modify: `rwc/types.py`
- Modify: `rwc/lexer.py`
- Modify: `rwc/ast_nodes.py`
- Modify: `rwc/parser.py`
- Modify: `rwc/sema.py` (`_resolve_type` だけ)
- Modify: `tests/test_sema.py`

- [ ] **Step 1.1: `rwc/types.py` に `OPTION_INT` を追加**

`rwc/types.py` で `LIST_INT = _Primitive("List[int]")` の **直下** に追加:

```python
OPTION_INT = _Primitive("Option[int]")
```

`is_printable` / `is_numeric` には**含めない**。

- [ ] **Step 1.2: `rwc/lexer.py` に 5 つのキーワードを追加**

`TokenKind` enum の `KW_LIST = auto()` の **直下** に:

```python
    KW_OPTION = auto()
    KW_MATCH = auto()
    KW_CASE = auto()
    KW_SOME = auto()
    KW_NONE = auto()
```

`KEYWORDS` dict の `"List": TokenKind.KW_LIST,` の **直下** に:

```python
    "Option": TokenKind.KW_OPTION,
    "match":  TokenKind.KW_MATCH,
    "case":   TokenKind.KW_CASE,
    "Some":   TokenKind.KW_SOME,
    "None":   TokenKind.KW_NONE,
```

- [ ] **Step 1.3: `rwc/ast_nodes.py` に新ノードを追加**

`ast_nodes.py` を Read してファイル末尾の構造を確認 (`SpawnExpr` / `AwaitExpr` の定義場所を探す)。それらの **直後** に追加:

```python
@dataclass
class SomeExpr(Expr):
    arg: Expr


@dataclass
class NoneExpr(Expr):
    pass


@dataclass
class MatchStmt(Stmt):
    target: Expr
    some_var: str
    some_block: List[Stmt]
    none_block: List[Stmt]
```

`SomeExpr` / `NoneExpr` は `Expr` を継承、`MatchStmt` は `Stmt` を継承する。
継承クラス名と既存 `Stmt`/`Expr` のフィールド (line, col 等の dataclass field
があるか) を Read で確認し、必要なら `line: int = 0`, `col: int = 0` を加える。
**Read で見たフォーマットに合わせる**。

- [ ] **Step 1.4: `rwc/parser.py` の `parse_type` に Option 分岐**

`parse_type` メソッド内、`List` 分岐の **直下** に追加:

```python
        if t.kind == TokenKind.KW_OPTION:
            self.i += 1
            self.eat(TokenKind.LBRACK, "'[' after Option")
            inner_tok = self.cur
            if inner_tok.kind != TokenKind.KW_INT:
                raise ParserError(
                    "only Option[int] is supported in this version of rw",
                    inner_tok.line, inner_tok.col, max(1, len(inner_tok.value)),
                )
            self.i += 1
            self.eat(TokenKind.RBRACK, "']' to close Option[int]")
            return A.TypeName("Option[int]", t.line, t.col)
```

- [ ] **Step 1.5: `parser.py` で `Some(e)` と `None` を式として受ける**

primary expression を扱う場所 (リテラル / IDENT / `spawn` / `await` などを
処理しているメソッド) を Read で特定する。探す目印: `KW_TRUE` / `KW_FALSE` /
`KW_SPAWN` / `KW_AWAIT` の分岐。同じレベルに追加する形:

```python
        if t.kind == TokenKind.KW_SOME:
            kw = t
            self.i += 1
            self.eat(TokenKind.LPAREN, "'(' after Some")
            arg = self.parse_expr()
            self.eat(TokenKind.RPAREN, "')' to close Some(...)")
            return A.SomeExpr(arg=arg, line=kw.line, col=kw.col)
        if t.kind == TokenKind.KW_NONE:
            kw = t
            self.i += 1
            return A.NoneExpr(line=kw.line, col=kw.col)
```

`A.SomeExpr` / `A.NoneExpr` のコンストラクタが `line`/`col` を引数で受けるか
は Step 1.3 で決めた dataclass の形に依存する。`SpawnExpr` の生成方法と
揃える。

- [ ] **Step 1.6: `parser.py` の `parse_stmt` に `match` 分岐**

`parse_stmt` メソッド (parser.py:207 付近) の `if t.kind == TokenKind.KW_RETURN:` / `KW_IF:` / `KW_WHILE:` と同レベルに追加:

```python
        if t.kind == TokenKind.KW_MATCH:
            return self._parse_match()
```

`_parse_match` メソッドを `parser.py` の `_parse_while` のすぐ後に追加:

```python
    def _parse_match(self) -> A.MatchStmt:
        kw = self.eat(TokenKind.KW_MATCH)
        target = self.parse_expr()
        self.eat(TokenKind.COLON, "':' after match target")
        self.eat(TokenKind.NEWLINE, "newline before match body")
        self.eat(TokenKind.INDENT, "indented match body")

        some_var: Optional[str] = None
        some_block: Optional[List[A.Stmt]] = None
        none_block: Optional[List[A.Stmt]] = None

        while self.cur.kind == TokenKind.KW_CASE:
            self.eat(TokenKind.KW_CASE)
            arm_tok = self.cur
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
                self.eat(TokenKind.NEWLINE, "newline before case body")
                self.eat(TokenKind.INDENT, "indented case body")
                some_var = ident.value
                some_block = []
                while self.cur.kind != TokenKind.DEDENT:
                    some_block.append(self.parse_stmt())
                self.eat(TokenKind.DEDENT, "dedent to close case body")
            elif arm_tok.kind == TokenKind.KW_NONE:
                if none_block is not None:
                    raise ParserError(
                        "duplicate `case None` arm in match",
                        arm_tok.line, arm_tok.col, 4,
                    )
                self.eat(TokenKind.KW_NONE)
                self.eat(TokenKind.COLON, "':' after case pattern")
                self.eat(TokenKind.NEWLINE, "newline before case body")
                self.eat(TokenKind.INDENT, "indented case body")
                none_block = []
                while self.cur.kind != TokenKind.DEDENT:
                    none_block.append(self.parse_stmt())
                self.eat(TokenKind.DEDENT, "dedent to close case body")
            else:
                raise ParserError(
                    "match case must be `Some(x)` or `None`",
                    arm_tok.line, arm_tok.col,
                    max(1, len(arm_tok.value)),
                )

        self.eat(TokenKind.DEDENT, "dedent to close match body")

        if some_block is None or none_block is None:
            raise ParserError(
                "match on Option[int] must cover both Some and None",
                kw.line, kw.col, 5,
            )
        return A.MatchStmt(
            target=target,
            some_var=some_var,
            some_block=some_block,
            none_block=none_block,
            line=kw.line, col=kw.col,
        )
```

実際に rw の lexer が NEWLINE / INDENT / DEDENT をどう生成するか (parser が
`if` や `while` を読むときと同じシーケンスか) は parser.py:236-280 (`_parse_if`
/ `_parse_while`) を Read して合わせる。**この plan のスケルトンを必ず
既存 `_parse_if` のシーケンスに揃える**。

- [ ] **Step 1.7: `rwc/sema.py` の `_resolve_type` に `Option[int]` を追加**

`_resolve_type` 関数の `m` dict に 1 行追加:

```python
        m = {
            "int": T.INT,
            "float": T.FLOAT,
            "bool": T.BOOL,
            "string": T.STRING,
            "Bytes": T.BYTES,
            "List[int]": T.LIST_INT,
            "Option[int]": T.OPTION_INT,
            "void": T.VOID,
        }
```

- [ ] **Step 1.8: ビルドと既存テスト緑を確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: `101 passed`。Sema / irgen が新 AST を扱わないので、`Some(1)` を
**実際に使う** コードは Sema で fall through するが、まだそういうコードは
書いてない。

- [ ] **Step 1.9: 型注釈と match だけ parse 通るテスト**

`tests/test_sema.py` の末尾に追加:

```python
def test_option_int_type_annotation_parses():
    src = (
        "def takes_opt(o: Option[int]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    res = check(src)
    assert "takes_opt" in res.functions
    assert res.functions["takes_opt"].params[0][1] is T.OPTION_INT


def test_option_with_non_int_param_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    src = (
        "def f(o: Option[string]) -> int:\n"
        "    return 0\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "Option[int]" in str(ei.value) or "only Option" in str(ei.value)


def test_match_with_missing_arm_is_parser_error():
    import pytest
    from rwc.lexer import tokenize
    from rwc.parser import parse, ParserError
    # only Some arm — parser must reject
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = None\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            return x\n"
        "    return 0\n"
    )
    with pytest.raises(ParserError) as ei:
        parse(tokenize(src))
    assert "must cover both" in str(ei.value)
```

- [ ] **Step 1.10: pytest を走らせる**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -q 2>&1 | tail -5
```

Expected: 既存 38 + 新規 3 = `41 passed`。`test_match_with_missing_arm` で
ParserError を期待しているが、parser が match の中で `Some(x)` の式 (= まだ
Sema 未実装) を許容する場合、別経路でエラーになる可能性がある。その場合は
代わりに `parse(tokenize(...))` で `ParserError` を期待するように形を
合わせる。

- [ ] **Step 1.11: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/types.py rwc/lexer.py rwc/ast_nodes.py rwc/parser.py rwc/sema.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: introduce Option[int] type and match syntax in lexer / parser

Adds the primitive type T.OPTION_INT, five new lexer keywords
(Option / match / case / Some / None), parser branches for the
Option[int] type annotation, Some(e) / None expressions, and the
match statement with two-arm requirement (Some(x) and None,
order-independent, both mandatory).

AST gets three new nodes: SomeExpr, NoneExpr, MatchStmt. Sema's
_resolve_type recognizes Option[int]. With this commit, the type
annotation `o: Option[int]` parses, but actually constructing or
matching on a value will fall through sema and crash at irgen —
that lands in the next two commits.

Parser-level negatives covered: Option[T] for T != int rejects,
match with only one arm rejects.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Sema で `Some` / `None` / `MatchStmt` を型検査

このタスクで Sema が新 AST を理解する。`Some(e)` の引数型、`MatchStmt` の
ターゲット型と bound 変数のスコープ、両 arm が return しているかの判定
(return-coverage) を実装する。

**Files:**
- Modify: `rwc/sema.py`
- Modify: `tests/test_sema.py`

- [ ] **Step 2.1: Sema の `_check_expr` で `SomeExpr` / `NoneExpr` を扱う**

`rwc/sema.py` の `_check_expr` メソッド (sema.py:298 付近、`isinstance(expr,
A.BinOp)` の分岐があるあたり) を Read。`isinstance(expr, A.SpawnExpr)` の
**直下** に追加:

```python
        if isinstance(expr, A.SomeExpr):
            at = self._check_expr(fn, expr.arg, locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 4,
                    f"Some argument must be int, found `{at}`",
                ))
            return T.OPTION_INT
        if isinstance(expr, A.NoneExpr):
            return T.OPTION_INT
```

`expr.line` / `expr.col` のフィールド名は Step 1.3 の AST 定義に合わせる。

- [ ] **Step 2.2: Sema の `_check_stmt` で `MatchStmt` を扱う**

`rwc/sema.py:153` 付近の `_check_stmt` メソッドを Read。`if isinstance(stmt,
A.WhileStmt):` の **直下** に追加:

```python
        if isinstance(stmt, A.MatchStmt):
            tt = self._check_expr(fn, stmt.target, locals_)
            if tt is not T.OPTION_INT:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 5,
                    f"match target must be Option[int], found `{tt}`",
                ))
            # Some arm: bind some_var as int in a new locals scope
            some_locals = dict(locals_)
            some_locals[stmt.some_var] = T.INT
            some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
            # None arm: no binding
            none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
            # match terminates in return iff both arms do
            return some_ret and none_ret
```

`_check_block` の戻り値は「block の末尾に到達せず return で抜けたら True」と
いう既存パターン。`if/elif/else` (sema.py:230 付近) で `then_ret` /
`else_ret` を使っている形に揃える。

- [ ] **Step 2.3: Positive テストを追加**

`tests/test_sema.py` の末尾に追加:

```python
# ---- Option[int] positive cases ----

def test_some_int_returns_option_int():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(5)\n"
        "    return 0\n"
    )
    check(src)


def test_none_returns_option_int():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = None\n"
        "    return 0\n"
    )
    check(src)


def test_function_returning_option_int_with_both_arms():
    src = (
        "def f(b: int) -> Option[int]:\n"
        "    if b == 0:\n"
        "        return None\n"
        "    return Some(1)\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    check(src)


def test_match_two_arms_ok():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(7)\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            print(x)\n"
        "        case None:\n"
        "            print(-1)\n"
        "    return 0\n"
    )
    check(src)


def test_match_some_bound_var_is_int():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(7)\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            y: int = x + 1\n"
        "            print(y)\n"
        "        case None:\n"
        "            print(-1)\n"
        "    return 0\n"
    )
    check(src)


def test_match_terminates_via_both_arms_return():
    # main has no `return` after match because match itself terminates
    # in both arms.
    src = (
        "def pick(b: int) -> int:\n"
        "    o: Option[int] = None\n"
        "    if b == 0:\n"
        "        o = None\n"
        "    else:\n"
        "        o = Some(b)\n"
        "    match o:\n"
        "        case Some(x):\n"
        "            return x\n"
        "        case None:\n"
        "            return -1\n"
        "def main() -> int:\n"
        "    return pick(7)\n"
    )
    check(src)
```

- [ ] **Step 2.4: Negative テストを追加**

`tests/test_sema.py` に続けて追加:

```python
# ---- Option[int] negative cases ----

def test_some_string_is_type_error():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(\"hi\")\n"
        "    return 0\n"
    )
    e = err(src)
    assert "Some argument must be int" in e.diagnostic.message


def test_match_on_int_is_type_error():
    src = (
        "def main() -> int:\n"
        "    x: int = 5\n"
        "    match x:\n"
        "        case Some(v):\n"
        "            print(v)\n"
        "        case None:\n"
        "            print(0)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "match target must be Option[int]" in e.diagnostic.message


def test_print_option_is_type_error():
    src = (
        "def main() -> int:\n"
        "    o: Option[int] = Some(1)\n"
        "    print(o)\n"
        "    return 0\n"
    )
    e = err(src)
    assert "print" in e.diagnostic.message


def test_option_eq_is_type_error():
    # Option[int] is intentionally not on the == whitelist
    src = (
        "def main() -> int:\n"
        "    a: Option[int] = Some(1)\n"
        "    b: Option[int] = None\n"
        "    if a == b:\n"
        "        return 0\n"
        "    return 1\n"
    )
    e = err(src)
    assert "compare" in e.diagnostic.message or "==" in e.diagnostic.message
```

- [ ] **Step 2.5: pytest を走らせる**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest tests/test_sema.py -q 2>&1 | tail -5
```

Expected: 既存 41 + positive 6 + negative 4 = `51 passed`。
`test_match_terminates_via_both_arms_return` は return-coverage が match の
両 arm を見る実装が必要。失敗するなら Step 2.2 の `_check_stmt` の戻り値
ロジックを再確認。

`test_print_option` のメッセージは現状の Sema が「print does not support
`Option[int]`」を返す前提。実際の文字列が違ったら assert を緩める。

- [ ] **Step 2.6: 既存テスト一式も緑か**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 既存 101 + Task 1 で 3 + Task 2 で 10 = `114 passed`。

- [ ] **Step 2.7: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add rwc/sema.py tests/test_sema.py
git commit -m "$(cat <<'EOF'
rwc: sema for Some / None / match

Sema:
  - SomeExpr requires its arg to be int, returns Option[int].
  - NoneExpr returns Option[int].
  - MatchStmt requires target to be Option[int], binds the
    Some(x) pattern's identifier as int in the Some arm's scope,
    and treats the whole statement as "terminates in return" iff
    both arms terminate (same pattern as if/else return coverage).
  - Option[int] is deliberately absent from the == whitelist, so
    `a == None` style comparisons get caught at sema.

Tests: 10 new in test_sema.py covering Some/None typing, both-arms
match, Some-bound-as-int, return coverage via match, and four
negatives (Some("hi"), match on int, print(Option), Option == ).

irgen doesn't know how to emit Some/None/MatchStmt yet — code that
actually executes those forms will still crash at irgen until the
next commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: ランタイム + irgen を実装し、Option 値が実行できるようにする

このタスクで Some / None / match / list_at_opt の IR を出せるようになる。
ここで初めて rw コードが実際に Option を扱って動く。

**Files:**
- Modify: `runtime/runtime.h`
- Modify: `runtime/runtime.c`
- Create: `runtime/fiber/test_option.c`
- Modify: `.gitignore`
- Modify: `rwc/sema.py` (`list_at_opt` の組込み追加)
- Modify: `rwc/irgen.py`

### Runtime

- [ ] **Step 3.1: `runtime.h` に `rw_option_int` struct と `rw_list_int_at_opt` プロトタイプ**

`runtime/runtime.h` の `rw_list_int` struct の定義の **直下**、`rw_list_int_new` 系プロトタイプの前に追加:

```c
/* Option[int] type. Two-word fat struct: tag (0=None, 1=Some) +
 * payload (int value when Some). 16 bytes — fits in two registers,
 * so value-return ABI is safe (no pointer-out needed). */
typedef struct {
    int64_t tag;       /* 0 = None, 1 = Some */
    int64_t payload;
} rw_option_int;
```

`rw_list_int_len` プロトタイプの **直下** に追加:

```c
/* List[int]: range-checked accessor returning Option[int]. */
void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i);
```

`rw_list_int_at` は変更しない (`abort` のままで残す)。

- [ ] **Step 3.2: `runtime.c` に `rw_list_int_at_opt` 実装を追加**

`runtime/runtime.c` の `rw_list_int_len` の **直下** に追加:

```c
void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i) {
    if (i < 0 || i >= l->len) {
        out->tag = 0;
        out->payload = 0;
        return;
    }
    out->tag = 1;
    out->payload = l->data[i];
}
```

- [ ] **Step 3.3: C 単体テスト `test_option.c` を新規作成**

`/Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime/fiber/test_option.c`:

```c
/*
 * Unit test for rw_list_int_at_opt and the rw_option_int struct.
 */

#include "../runtime.h"

#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    /* Build a small list: [10, 20, 30] */
    rw_list_int l, l1, l2;
    rw_list_int_new(&l);
    rw_list_int_push(&l1, &l, 10);
    rw_list_int_push(&l2, &l1, 20);
    rw_list_int l3;
    rw_list_int_push(&l3, &l2, 30);

    /* In-range: Some */
    rw_option_int o;
    rw_list_int_at_opt(&o, &l3, 0);
    assert(o.tag == 1);
    assert(o.payload == 10);

    rw_list_int_at_opt(&o, &l3, 2);
    assert(o.tag == 1);
    assert(o.payload == 30);

    /* Out-of-range: None */
    rw_list_int_at_opt(&o, &l3, 3);
    assert(o.tag == 0);

    rw_list_int_at_opt(&o, &l3, -1);
    assert(o.tag == 0);

    /* Empty list: None for any index */
    rw_list_int empty;
    rw_list_int_new(&empty);
    rw_list_int_at_opt(&o, &empty, 0);
    assert(o.tag == 0);

    printf("all option tests passed\n");
    return 0;
}
```

- [ ] **Step 3.4: `.gitignore` にバイナリを追加**

`/Users/ryuichi/ghq/github.com/ryuichi1208/rw/.gitignore` の `runtime/fiber/test_list_int` の **直下** に:

```
runtime/fiber/test_option
```

- [ ] **Step 3.5: ランタイムをビルドして C テストを走らせる**

```sh
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime clean
make -C /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_option.c librw.a -o fiber/test_option && ./fiber/test_option
```

Expected: 警告なしビルド + `all option tests passed`。

- [ ] **Step 3.6: 既存 C テストが緑か念のため確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_list_int.c librw.a -o fiber/test_list_int && ./fiber/test_list_int
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c librw.a -o fiber/test_sched && ./fiber/test_sched
```

Expected: `all list_int tests passed` / `total = 333833500`。

### Sema for list_at_opt

- [ ] **Step 3.7: `list_at_opt` 組込みを Sema に追加**

`rwc/sema.py` の `list_at` 組込み (`_check_call` 内) の **直下** に追加:

```python
        # Builtin: list_at_opt(List[int], int) -> Option[int].
        if call.callee == "list_at_opt":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at_opt takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.LIST_INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at_opt first argument must be List[int], found `{t0}`",
                ))
            if t1 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at_opt second argument must be int, found `{t1}`",
                ))
            return T.OPTION_INT
```

SpawnExpr 禁止リストに追加 (`list_at` 禁止分岐の隣):

```python
                if call.callee == "list_at_opt":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `list_at_opt`",
                    ))
```

### irgen

- [ ] **Step 3.8: irgen に `RW_OPTION_INT_TY` を定義**

`rwc/irgen.py` 上部、`RW_LIST_INT_TY = ...` の **直下** に:

```python
RW_OPTION_INT_TY = ir.LiteralStructType([I64, I64])  # {tag, payload}
```

- [ ] **Step 3.9: `llvm_type_of` に Option[int] を追加**

`llvm_type_of` で `if t is T.LIST_INT:` の **直下** に:

```python
    if t is T.OPTION_INT:
        return RW_OPTION_INT_TY
```

- [ ] **Step 3.10: `_declare_runtime` に `rw_list_int_at_opt` を追加**

`_declare_runtime` 内で List ops の宣言の **直下** に:

```python
        # Option[int] ops — pointer-out for the output struct, matching
        # the List helpers' calling convention.
        option_ptr = RW_OPTION_INT_TY.as_pointer()
        self._rw_list_int_at_opt = ir.Function(
            m, ir.FunctionType(ir.VoidType(),
                               [option_ptr, RW_LIST_INT_TY.as_pointer(), I64]),
            "rw_list_int_at_opt")
```

- [ ] **Step 3.11: irgen の `_emit_expr` で `SomeExpr` / `NoneExpr` を扱う**

`_emit_expr` で既存の `if isinstance(expr, A.SpawnExpr):` 等と並列に追加:

```python
        if isinstance(expr, A.SomeExpr):
            v = self._emit_expr(expr.arg, ctx)
            base = ir.Constant(RW_OPTION_INT_TY,
                               [ir.Constant(I64, 1), ir.Constant(I64, 0)])
            return ctx.builder.insert_value(base, v, 1)
        if isinstance(expr, A.NoneExpr):
            return ir.Constant(RW_OPTION_INT_TY,
                               [ir.Constant(I64, 0), ir.Constant(I64, 0)])
```

- [ ] **Step 3.12: irgen の `_emit_stmt` で `MatchStmt` を扱う**

`_emit_stmt` (irgen.py:217 付近) の `if isinstance(stmt, A.WhileStmt):` の
**直下** に追加:

```python
        if isinstance(stmt, A.MatchStmt):
            self._emit_match(stmt, ctx)
            return
```

`_emit_match` を `_emit_while` の隣に追加:

```python
    def _emit_match(self, stmt: A.MatchStmt, ctx: "FunctionCtx") -> None:
        b = ctx.builder
        v = self._emit_expr(stmt.target, ctx)
        tag = b.extract_value(v, 0)
        payload = b.extract_value(v, 1)

        some_bb = ctx.function.append_basic_block("match.some")
        none_bb = ctx.function.append_basic_block("match.none")
        end_bb = ctx.function.append_basic_block("match.end")

        sw = b.switch(tag, none_bb)
        sw.add_case(ir.Constant(I64, 1), some_bb)

        # Some arm
        b.position_at_end(some_bb)
        slot = b.alloca(I64, name=stmt.some_var)
        b.store(payload, slot)
        saved = ctx.locals.get(stmt.some_var)
        ctx.locals[stmt.some_var] = slot
        self._emit_block(stmt.some_block, ctx)
        if not b.block.is_terminated:
            b.branch(end_bb)
        if saved is not None:
            ctx.locals[stmt.some_var] = saved
        else:
            ctx.locals.pop(stmt.some_var, None)

        # None arm
        b.position_at_end(none_bb)
        self._emit_block(stmt.none_block, ctx)
        if not b.block.is_terminated:
            b.branch(end_bb)

        b.position_at_end(end_bb)
```

`ctx.builder.block.is_terminated` の取得方法は llvmlite のバージョンに依存
する。既存の `_emit_if` (もしあれば) でどう判定しているかを Read で確認し、
同じ書き方に揃える。無ければ `b.block.is_terminated` を試して動かす。

- [ ] **Step 3.13: irgen の `_emit_call` で `list_at_opt` を扱う**

`_emit_call` 内、既存の `list_at` 分岐の **直下** に追加:

```python
        if call.callee == "list_at_opt":
            lv = self._emit_expr(call.args[0], ctx)
            iv = self._emit_expr(call.args[1], ctx)
            in_slot = ctx.builder.alloca(RW_LIST_INT_TY)
            ctx.builder.store(lv, in_slot)
            out_slot = ctx.builder.alloca(RW_OPTION_INT_TY)
            ctx.builder.call(self._rw_list_int_at_opt, [out_slot, in_slot, iv])
            return ctx.builder.load(out_slot)
```

### Smoke

- [ ] **Step 3.14: 単独で IR + 実行 smoke**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
cat > /tmp/option_smoke.rw <<'EOF'
def safe_div(a: int, b: int) -> Option[int]:
    if b == 0:
        return None
    return Some(a / b)

def main() -> int:
    r: Option[int] = safe_div(10, 2)
    match r:
        case Some(x):
            print(x)
        case None:
            print(-1)
    e: Option[int] = safe_div(10, 0)
    match e:
        case Some(x):
            print(x)
        case None:
            print(-1)
    return 0
EOF
uv run rwc emit-ir /tmp/option_smoke.rw 2>&1 | grep -E "switch|insertvalue|extractvalue" | head -10
echo "---"
uv run rwc run /tmp/option_smoke.rw
```

Expected:

IR に `switch`, `insertvalue`, `extractvalue` の各命令が含まれる。

実行結果:
```
5
-1
```

`exit != 0` (特に 139 = SIGSEGV) なら ABI 問題か match の lowering バグ。
`Option[int]` は 16 バイトなので pointer-out 不要のはずだが、念のため
`llvm-ir-c-abi` skill の検証手順を参照。

- [ ] **Step 3.15: pytest 全件**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 既存 114 + (このタスクでテスト追加なしなので) `114 passed`。

- [ ] **Step 3.16: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add runtime/runtime.h runtime/runtime.c runtime/fiber/test_option.c .gitignore rwc/sema.py rwc/irgen.py
git commit -m "$(cat <<'EOF'
rwc + runtime: emit IR for Some / None / match, add list_at_opt

Runtime:
  - Add rw_option_int struct {tag, payload} (16 bytes, value-return
    safe — no pointer-out ABI quirks like the 24-byte rw_list_int).
  - Add rw_list_int_at_opt(out*, l*, i): returns Some(value) when
    in range, None when out of bounds. The old rw_list_int_at is
    preserved unchanged (still aborts) for backward compatibility.
  - C unit test covers in-range Some, out-of-range / negative /
    empty-list None.

Sema:
  - Add list_at_opt(List[int], int) -> Option[int] builtin and
    forbid spawn of it.

irgen:
  - RW_OPTION_INT_TY = {i64 tag, i64 payload}.
  - llvm_type_of(T.OPTION_INT) -> RW_OPTION_INT_TY.
  - SomeExpr: insertvalue {1, 0} with arg into payload slot.
  - NoneExpr: constant {0, 0}.
  - MatchStmt: extract_value tag + payload, switch on tag to the
    Some / None basic blocks, alloca-and-store for the bound var,
    branch to a common end block for falls-through arms.
  - list_at_opt call: pointer-out shim (alloca input list, alloca
    output option, call, load result).

Smoke:
  RW_WORKERS=1 uv run rwc run examples/option_basic.rw
  -> 5\n-1\n (matches .expected from next commit)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: example + e2e

**Files:**
- Create: `examples/option_basic.rw`
- Create: `examples/option_basic.rw.expected`
- Modify: `tests/test_e2e.py`

- [ ] **Step 4.1: `examples/option_basic.rw` を書く**

`/Users/ryuichi/ghq/github.com/ryuichi1208/rw/examples/option_basic.rw`:

```rw
def safe_div(a: int, b: int) -> Option[int]:
    if b == 0:
        return None
    return Some(a / b)

def main() -> int:
    r: Option[int] = safe_div(10, 2)
    match r:
        case Some(x):
            print(x)
        case None:
            print(-1)
    e: Option[int] = safe_div(10, 0)
    match e:
        case Some(x):
            print(x)
        case None:
            print(-1)
    return 0
```

- [ ] **Step 4.2: `examples/option_basic.rw.expected` を書く**

```
5
-1
```

(末尾改行ありで保存。)

- [ ] **Step 4.3: 手元で byte 一致を確認**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
diff <(RW_WORKERS=1 uv run rwc run examples/option_basic.rw 2>&1) examples/option_basic.rw.expected && echo OK
```

Expected: `OK` だけが表示される。

- [ ] **Step 4.4: `tests/test_e2e.py` の parametrize に `option_basic` を追加**

`tests/test_e2e.py:45` の以下の行:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic"],
```

を以下に変更:

```python
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic"],
```

- [ ] **Step 4.5: 全 pytest**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw && uv run pytest -q 2>&1 | tail -5
```

Expected: 114 + 1 = `115 passed`。

- [ ] **Step 4.6: 既存 example 回帰**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
RW_WORKERS=1 uv run rwc run examples/hello.rw
RW_WORKERS=1 uv run rwc run examples/list_basic.rw
RW_WORKERS=1 uv run rwc run examples/string_ops.rw
RW_WORKERS=1 uv run rwc run examples/bytes_basic.rw
RW_WORKERS=1 uv run rwc run examples/spawn_many.rw
```

Expected:
```
hello
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

- [ ] **Step 4.7: ランタイム単体テストも緑か**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw/runtime
make clean && make
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_option.c librw.a -o fiber/test_option && ./fiber/test_option
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_list_int.c librw.a -o fiber/test_list_int && ./fiber/test_list_int
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_sched.c librw.a -o fiber/test_sched && ./fiber/test_sched
```

Expected: `all option tests passed` / `all list_int tests passed` /
`total = 333833500`。

- [ ] **Step 4.8: Commit**

```sh
cd /Users/ryuichi/ghq/github.com/ryuichi1208/rw
git add examples/option_basic.rw examples/option_basic.rw.expected tests/test_e2e.py
git commit -m "$(cat <<'EOF'
examples: add option_basic exercising Some / None / match

examples/option_basic.rw exercises the full Option[int] surface
in a single program: a safe_div helper that returns Some(quotient)
when b != 0 and None when b == 0, plus two match statements that
print the value (Some arm) or a sentinel -1 (None arm).

The .expected captures the byte-for-byte stdout (5 then -1), and
tests/test_e2e.py picks it up via the existing parametrize list,
so any regression in lexer / parser / sema / irgen / runtime
flows that touch Some / None / match / list_at_opt will fail the
suite.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

### Spec coverage

| Spec 要求 | カバーするタスク |
|---|---|
| 新型 `Option[int]` (キーワード `Option`、parser が `[int]` 強制) | Task 1.1 / 1.2 / 1.4 |
| `Some(int) -> Option[int]` 式 | Task 1.5 (parser), 2.1 (sema), 3.11 (irgen) |
| `None` リテラル | Task 1.2 (lexer KW_NONE), 1.5 (parser), 2.1 (sema), 3.11 (irgen) |
| `match v: case Some(x): ... case None: ...` 構文 | Task 1.6 (parser), 2.2 (sema), 3.12 (irgen) |
| 2 アーム必須 (parser) | Task 1.6 (両 arm 必須チェック) + test 1.9 |
| Some arm の bound 変数が int | Task 2.2 (sema scope) + test 2.3 |
| return-coverage が match 両 arm を見る | Task 2.2 (`some_ret and none_ret`) + test 2.3 |
| `list_at_opt(List[int], int) -> Option[int]` 組込み | Task 3.1 / 3.2 (runtime), 3.7 (sema), 3.10 / 3.13 (irgen) |
| 範囲外で None を返す runtime helper | Task 3.2 + C test 3.3 |
| 内部表現は 16 バイト value 返し | Task 3.1 (struct), 3.8 (`RW_OPTION_INT_TY`) — pointer-out 不要 |
| `Option[string]` 等は parser エラー | Task 1.4 + test 1.9 |
| `match` の片側欠落は parser エラー | Task 1.6 + test 1.9 |
| `print(Option[int])` は型エラー | `is_printable` を変えないことで自動 + test 2.4 |
| `Option == Option` は型エラー | == whitelist を変えないことで自動 + test 2.4 |
| `Some(string)` は型エラー | Task 2.1 + test 2.4 |
| `match` ターゲットが Option[int] でないと型エラー | Task 2.2 + test 2.4 |
| `Future[Option[int]]` 禁止 | spec で記載、`_decl_spawn`/`_decl_await` に LIST_INT を追加しないのと同じく OPTION_INT も追加しない (Task 3 全体で「敢えて触らない」) |
| 既存 101 テスト緑 | Task 1.8, 2.6, 3.15, 4.5 |

すべての spec 要求にタスクがある。

### Placeholder スキャン

「TBD」「TODO」「(要確認)」「fill in」「Add appropriate」「Similar to Task N」は plan 内 0 件。Step 1.10 末尾の「ParserError を期待するように形を合わせる」、Step 2.5 の「実際の文字列が違ったら assert を緩める」、Step 3.12 の「llvmlite のバージョンに依存」はそれぞれ **代替手順を plan 内に明示している** ので、placeholder ではない。

### Type consistency

- `T.OPTION_INT` は Task 1.1 / 1.7 / 2.1 / 2.2 / 3.7 / 3.9 で完全一致
- LLVM 表現 `RW_OPTION_INT_TY = LiteralStructType([I64, I64])` は Task 3.8 / 3.9 / 3.10 / 3.11 / 3.12 / 3.13 で揃っている
- ランタイム関数: `rw_list_int_at_opt(out*, l*, i)` を Task 3.1 (宣言) / 3.2 (実装) / 3.3 (C test) / 3.10 (irgen 宣言) / 3.13 (irgen 呼び出し) で完全一致
- AST ノード名: `SomeExpr` / `NoneExpr` / `MatchStmt` を Task 1.3 (定義) / 1.5 / 1.6 (parser) / 2.1 / 2.2 (sema) / 3.11 / 3.12 (irgen) で揃って使用
- Sema 型エラーメッセージ: `"Some argument must be int"` / `"match target must be Option[int]"` / `"list_at_opt first argument must be List[int]"` / `"list_at_opt second argument must be int"` を実装 (Task 2.1, 2.2, 3.7) と negative テスト assert (Task 2.4) で完全一致
- Match の Some arm bound 変数管理: Task 2.2 で `some_locals[stmt.some_var] = T.INT`、Task 3.12 で `ctx.locals[stmt.some_var] = slot`。同じフィールド名 `some_var` を一貫使用
