# rw Result[int, int] 型 + match の Ok/Err 拡張

## Context

`Option[int]` (#94) で **タグ付き共用体 + パターンマッチ** という言語機能の
土台ができた。Result はその次に置くべきもう 1 つの sum 型で、典型的な
「成功または失敗の理由付きエラー」を表現する標準形になる。

`incremental-language-extensions` skill の鉄則「ジェネリクスは 2 度目に
必要になった時」を厳密に適用するなら、Option (1 度目) + Result (2 度目)
で **真のジェネリクスを入れるタイミング** とも言える。しかし真のジェネリクス
化は `parse_match` を sema 経由にする大改造を含み、1 PR の範囲を超える。

そこで本サブプロジェクトは **依然モノモーフを維持**しつつ、match parser
だけは「Some/None ペア」か「Ok/Err ペア」のどちらかを 1 つ目のアームで
判別する形に拡張する。真のジェネリクス化は別 PR (4c) で行う。

ロードマップ:

1. 文字列 `len` / `==` / `+` (#91)
2. Bytes 型 (#92)
3. List[int] (#93)
4a. Option[int] + match (#94)
4b. **このサブプロジェクト**: Result[int, int] + match Ok/Err
4c. (将来) 真のジェネリクス化
5. (将来) netpoller + TCP API

## Goals

- 新しいプリミティブ型 `Result[int, int]` を導入 (parser は `[int, int]` 以外
  を拒否)
- 値構築: `Ok(int) -> Result[int, int]`、`Err(int) -> Result[int, int]`
- 値分解: 既存 `match` 文を「Some/None ペア」または「Ok/Err ペア」の
  どちらか 2 アームに拡張 (ペア混在は parser エラー)
- `MatchStmt` AST に `style: "option" | "result"` フィールドを足し、Sema /
  irgen が style 別に分岐
- 公開 ABI 既存部分は不変、既存テスト緑

## Non-Goals

- 汎用ジェネリック (`Result[T, E]` の T/E が int 以外): parser で
  「only `Result[int, int]` is supported」エラー
- `Result[T, E]` の真のジェネリクス化 (別 PR、4c)
- match を式として使う (`x = match v: ...`): match は statement のみ
- ネストパターン (`Ok(Some(x))`)、ガード (`case Ok(x) if x > 0`)、
  ワイルドカード (`case _`)
- `?` 演算子 (early-return)
- match のアーム順序: Some/None は順不同のままにしているので Ok/Err も
  順不同
- `Future[Result[int, int]]` (spawn 経由): Sema で禁止
- `Result` 用の組込みメソッド (`unwrap`, `is_ok`, `or_else`)
- `Option[int]` <-> `Result[int, int]` の相互変換組込み
- `print(r: Result[int, int])` (printable リストに加えない)
- `r1 == r2` (== whitelist に加えない)
- `div_checked` 等の組込み helper (Result を返す関数は **rw 言語側でユーザ
  コードとして書ける** ので、example で書いて済ます)

## 設計

### 内部表現

`Result[int, int]` は `Option[int]` と **同じ 2 ワード fat struct** で表現:

```c
typedef struct {
    int64_t tag;       /* 0 = Err, 1 = Ok */
    int64_t payload;   /* Ok のとき成功値、Err のときエラー値 */
} rw_result_int_int;
```

LLVM IR:

```
%rw_result_int_int = { i64 tag, i64 payload }
```

サイズ 16 バイト → arm64 / x86_64 SysV 両方で 2 レジスタで return できる
(pointer-out ABI 不要、`llvm-ir-c-abi` skill 参照)。

実は **Option[int] と LLVM struct の形は完全一致**しているが、Sema レベルで
別の型 (`T.OPTION_INT` vs `T.RESULT_INT_INT`) として区別する。Bytes と
string が同じ `RW_STR_TY` を共有するのと同じパターン。

タグ値の意味付け:

| tag | 意味 |
|---|---|
| 0 | Err (失敗) |
| 1 | Ok (成功) |

Option との対応関係:
- `Option.None` (tag=0) ↔ `Result.Err` (tag=0)
- `Option.Some` (tag=1) ↔ `Result.Ok` (tag=1)

これは将来 Option → Result の cast を 0-cost にする余地のためだが、本 PR
では cast は実装しない (Non-Goal)。

### 言語レベルの挙動

#### 値構築と分解

```rw
def div_checked(a: int, b: int) -> Result[int, int]:
    if b == 0:
        return Err(0)
    return Ok(a / b)

def main() -> int:
    r: Result[int, int] = div_checked(10, 2)
    match r:
        case Ok(x):
            print(x)              # => 5
        case Err(e):
            print(-1)

    e: Result[int, int] = div_checked(10, 0)
    match e:
        case Ok(x):
            print(x)
        case Err(code):
            print(code)           # => 0
    return 0
```

#### match parser の挙動

`parse_match` は 1 つ目のアームを見て **style を決定する**:

- 1 つ目が `case Some(x):` か `case None:` → Option-style
- 1 つ目が `case Ok(x):` か `case Err(e):` → Result-style

2 つ目のアームは同じ style のもう一方であることを要求する:
- Option-style で 1 つ目が Some → 2 つ目は None を要求
- Option-style で 1 つ目が None → 2 つ目は Some を要求
- Result-style で 1 つ目が Ok → 2 つ目は Err を要求
- Result-style で 1 つ目が Err → 2 つ目は Ok を要求

混在 (`case Some(x): ... case Err(e):`) は parser エラー
「expected `case None` (Option-style match)」のような明示メッセージで弾く。

#### エラーになるケース

```rw
def main() -> int:
    # parser: only Result[int, int] supported
    a: Result[string, int] = Ok(1)
    # sema: Ok argument must be int
    b: Result[int, int] = Ok("hi")
    # parser: mixed arms (Result + Option)
    c: Result[int, int] = Ok(1)
    match c:
        case Ok(x):
            print(x)
        case None:
            print(0)
    # sema: cannot compare `Result[int, int]`
    if c == Err(0):
        return 0
    # sema: print does not support `Result[int, int]`
    print(c)
    # sema: cannot spawn the builtin / Future[Result[int, int]] forbidden
    return 0
```

### コンポーネント別の変更

#### ランタイム

**変更なし**。`Result[int, int]` は LLVM IR 内 (`insertvalue` + 定数 struct)
で完結。`div_checked` は example のユーザコードとして書く、組込みではない。

#### `rwc/types.py`

```python
RESULT_INT_INT = _Primitive("Result[int, int]")
```

`is_printable` / `is_numeric` には含めない。

#### `rwc/lexer.py`

新キーワード 3 つ:

```python
KW_RESULT = auto()
KW_OK     = auto()
KW_ERR    = auto()
```

```python
KEYWORDS = {
    ...,
    "Result": KW_RESULT,
    "Ok":     KW_OK,
    "Err":    KW_ERR,
}
```

#### `rwc/parser.py`

##### `parse_type` で `Result[int, int]` を受ける

`Option` 分岐の隣に追加:

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

##### 式パーサで `Ok(e)` / `Err(e)` を受ける

`parse_unary` の `KW_SOME` / `KW_NONE` の直下に追加:

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

##### `parse_match` の拡張

現状の `parse_match` を以下の構造に置き換える:

```python
def parse_match(self) -> A.MatchStmt:
    kw = self.eat(TokenKind.KW_MATCH)
    target = self.parse_expr()
    self.eat(TokenKind.COLON, "':' after match target")
    self.eat(TokenKind.NEWLINE)
    self.eat(TokenKind.INDENT, "indented match body")

    style: Optional[str] = None  # "option" or "result"
    some_var = some_block = none_block = None
    ok_var = ok_block = err_var = err_block = None

    while self.cur.kind != TokenKind.DEDENT:
        if self.cur.kind == TokenKind.NEWLINE:
            self.i += 1; continue
        if self.cur.kind != TokenKind.KW_CASE:
            raise ParserError("expected `case` arm in match body", ...)
        self.eat(TokenKind.KW_CASE)
        arm_tok = self.cur

        if arm_tok.kind in (TokenKind.KW_SOME, TokenKind.KW_NONE):
            this_style = "option"
        elif arm_tok.kind in (TokenKind.KW_OK, TokenKind.KW_ERR):
            this_style = "result"
        else:
            raise ParserError("match case must be Some/None or Ok/Err", ...)

        if style is None:
            style = this_style
        elif style != this_style:
            expected = "Some/None" if style == "option" else "Ok/Err"
            raise ParserError(
                f"mixed match arms: expected {expected} pair, got `{arm_tok.value}`",
                arm_tok.line, arm_tok.col, max(1, len(arm_tok.value)),
            )

        # Dispatch on the four constructor cases:
        if arm_tok.kind == TokenKind.KW_SOME:
            # duplicate-check, parse Some(IDENT), parse block, set some_var/some_block
            ...
        elif arm_tok.kind == TokenKind.KW_NONE:
            # parse None, parse block, set none_block
            ...
        elif arm_tok.kind == TokenKind.KW_OK:
            # parse Ok(IDENT), parse block, set ok_var/ok_block
            ...
        elif arm_tok.kind == TokenKind.KW_ERR:
            # parse Err(IDENT), parse block, set err_var/err_block
            ...

    self.eat(TokenKind.DEDENT)

    if style == "option":
        if some_block is None or none_block is None:
            raise ParserError("match on Option[int] must cover both Some and None", ...)
    elif style == "result":
        if ok_block is None or err_block is None:
            raise ParserError("match on Result[int, int] must cover both Ok and Err", ...)
    else:
        raise ParserError("match must have at least one case arm", ...)

    return A.MatchStmt(
        target, style,
        some_var, some_block, none_block,
        ok_var, ok_block, err_var, err_block,
        kw.line, kw.col,
    )
```

#### `rwc/ast_nodes.py`

新規式ノード:

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

`Expr` Union に追加。

`MatchStmt` を style 統合形に拡張:

```python
@dataclass
class MatchStmt:
    target: Expr
    style: str                                  # "option" or "result"
    # Option-style fields
    some_var: Optional[str]
    some_block: Optional[List["Stmt"]]
    none_block: Optional[List["Stmt"]]
    # Result-style fields
    ok_var: Optional[str]
    ok_block: Optional[List["Stmt"]]
    err_var: Optional[str]
    err_block: Optional[List["Stmt"]]
    line: int
    col: int
```

style に応じて Option-style フィールドが埋まる/Result-style フィールドが
埋まる排他関係。

#### `rwc/sema.py`

`_resolve_type` の dict に `"Result[int, int]": T.RESULT_INT_INT` を追加。

`_check_expr` で `OkExpr` / `ErrExpr` を扱う:

```python
if isinstance(expr, A.OkExpr):
    at = self._check_expr(fn, expr.arg, locals_)
    if at is not T.INT:
        raise CompileError("Ok argument must be int")
    return T.RESULT_INT_INT
if isinstance(expr, A.ErrExpr):
    at = self._check_expr(fn, expr.arg, locals_)
    if at is not T.INT:
        raise CompileError("Err argument must be int")
    return T.RESULT_INT_INT
```

`_check_stmt` の `MatchStmt` を style 別に分岐:

```python
if isinstance(stmt, A.MatchStmt):
    tt = self._check_expr(fn, stmt.target, locals_)
    if stmt.style == "option":
        if tt is not T.OPTION_INT:
            raise CompileError("match target must be Option[int]")
        some_locals = dict(locals_)
        some_locals[stmt.some_var] = T.INT
        some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
        none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
        return some_ret and none_ret
    elif stmt.style == "result":
        if tt is not T.RESULT_INT_INT:
            raise CompileError("match target must be Result[int, int]")
        ok_locals = dict(locals_)
        ok_locals[stmt.ok_var] = T.INT
        ok_ret = self._check_block(fn, stmt.ok_block, ok_locals, ret_ty)
        err_locals = dict(locals_)
        err_locals[stmt.err_var] = T.INT
        err_ret = self._check_block(fn, stmt.err_block, err_locals, ret_ty)
        return ok_ret and err_ret
```

`==` whitelist に `T.RESULT_INT_INT` を加えない (= 比較不可、既存パターン)。
`Spawn` 禁止リストは特に追加なし (`Ok` / `Err` は組込みではなく式ノード
なので、`spawn Ok(1)` のような構文は parser が `Ok(1)` を Call ではなく
OkExpr として解釈するため、SpawnExpr の `Call` 制約で自動的に弾かれる)。

#### `rwc/irgen.py`

```python
RW_RESULT_INT_INT_TY = ir.LiteralStructType([I64, I64])
```

(`RW_OPTION_INT_TY` と同形だが、可読性のため別エイリアス。)

`llvm_type_of(T.RESULT_INT_INT) -> RW_RESULT_INT_INT_TY`

`_emit_expr`:

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

`_emit_stmt` の `MatchStmt`:

```python
if isinstance(stmt, A.MatchStmt):
    v = self._emit_expr(stmt.target, ctx)
    tag = b.extract_value(v, 0)
    payload = b.extract_value(v, 1)
    arm1_bb = ctx.function.append_basic_block("match.arm1")  # tag == 1
    arm0_bb = ctx.function.append_basic_block("match.arm0")  # tag == 0 (default)
    end_bb = ctx.function.append_basic_block("match.end")
    sw = b.switch(tag, arm0_bb)
    sw.add_case(ir.Constant(I64, 1), arm1_bb)
    if stmt.style == "option":
        var1, block1 = stmt.some_var, stmt.some_block   # tag=1 -> Some
        var0, block0 = None, stmt.none_block            # tag=0 -> None (no bind)
    else:  # "result"
        var1, block1 = stmt.ok_var, stmt.ok_block       # tag=1 -> Ok
        var0, block0 = stmt.err_var, stmt.err_block     # tag=0 -> Err
    # arm1 (tag == 1)
    b.position_at_end(arm1_bb)
    self._emit_arm(var1, block1, payload, ctx, end_bb)
    # arm0 (tag == 0)
    b.position_at_end(arm0_bb)
    self._emit_arm(var0, block0, payload, ctx, end_bb)
    b.position_at_end(end_bb)
    return
```

`_emit_arm` は payload を bound 変数の slot に store して block を emit、
end_bb に branch する helper (Option の None arm のような bound 変数なし
ケースは `var is None` で分岐):

```python
def _emit_arm(self, var, block, payload, ctx, end_bb):
    b = ctx.builder
    if var is not None:
        slot = b.alloca(I64, name=var)
        b.store(payload, slot)
        saved = ctx.locals.get(var)
        ctx.locals[var] = slot
        self._emit_block(block, ctx)
        if saved is not None:
            ctx.locals[var] = saved
        else:
            ctx.locals.pop(var, None)
    else:
        self._emit_block(block, ctx)
    if not b.block.is_terminated:
        b.branch(end_bb)
```

これで Option / Result どちらの match も同じ lowering ロジックを共有する。

### テスト

#### `tests/test_sema.py` (positive 6 + negative 6)

Positive:
- `Ok(5)` が `Result[int, int]`
- `Err(0)` が `Result[int, int]`
- `def f() -> Result[int, int]: return Ok(1)` が通る
- `match` の Ok/Err 両アームが揃った形が通る
- Ok arm の bound `x` が int、Err arm の bound `e` も int として使える
- Result の match が return-coverage を満たす (両 arm が return → match 全体
  が return)

Negative:
- `Ok("hi")` → 引数型エラー
- `Result[string, int]` → parser エラー
- `match` で Some + Err 混在 → parser エラー
- `match` で 1 つ目 Ok / 2 つ目 None → parser エラー
- `print(Ok(1))` → printable エラー
- `Ok(1) == Err(0)` → 比較不可エラー
- `Ok(1) == Some(1)` → 同型ではないので別エラー (おまけ)

#### `examples/result_basic.rw` + `.expected`

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

期待出力:

```
5
0
```

## ファイル別変更

### 変更

- `rwc/types.py` — `RESULT_INT_INT` プリミティブ
- `rwc/lexer.py` — `KW_RESULT` / `KW_OK` / `KW_ERR` キーワード
- `rwc/parser.py` — `parse_type` の Result 分岐、`Ok(e)`/`Err(e)` 式、
  `parse_match` を style 統合形に拡張
- `rwc/ast_nodes.py` — `OkExpr` / `ErrExpr` を Expr Union に追加、`MatchStmt`
  を style 統合形に拡張
- `rwc/sema.py` — `_resolve_type` / `OkExpr`/`ErrExpr` の Sema /
  `MatchStmt` の style 別 Sema
- `rwc/irgen.py` — `RW_RESULT_INT_INT_TY` / `Ok`/`Err` の irgen /
  `_emit_match` を style 別に分岐 (共通 helper `_emit_arm`)
- `tests/test_sema.py` — positive 6 + negative 7
- `tests/test_e2e.py` — parametrize に `result_basic`

### 新規

- `examples/result_basic.rw` + `.expected`
- `docs/specs/11-result-type.md` (本ファイル)
- `docs/plans/2026-05-23-result-type.md` (writing-plans で作成)

### 変更なし

- ランタイム (`runtime/*`)
- fiber スケジューラ
- 既存 spec docs

## 検証

```sh
# pytest
uv run pytest -q
# 期待: 既存 115 + sema positive 6 + negative 7 + e2e 1 = 129 件全緑

# 単独実行
uv run rwc run examples/result_basic.rw
# 期待出力: 5\n0\n

# 既存 example 回帰
uv run rwc run examples/option_basic.rw
uv run rwc run examples/list_basic.rw
uv run rwc run examples/string_ops.rw
uv run rwc run examples/spawn_many.rw

# ランタイム単体テストはランタイム変更なしのため触らない (回帰のみ)
make -C runtime clean && make -C runtime
```

## コミット構成

4 commits:

1. **rwc (lexer/parser/types/ast)**: `KW_RESULT/OK/ERR`、`parse_type` の
   Result 分岐、`Ok(e)`/`Err(e)` 式、`OkExpr`/`ErrExpr` AST ノード、
   `MatchStmt` を style 統合形に拡張、`parse_match` 全面書き直し、
   `T.RESULT_INT_INT`、`_resolve_type`。型注釈と AST だけ通る
2. **rwc (sema)**: `OkExpr`/`ErrExpr` の Sema、`MatchStmt` の style 別 Sema、
   既存 Option パスが影響を受けないことを確認、negative テスト一括
3. **rwc (irgen)**: `RW_RESULT_INT_INT_TY`、`Ok`/`Err` の irgen、
   `_emit_match` を `_emit_arm` 経由で style 別に分岐、smoke 動作確認
4. **examples + e2e**: `result_basic.rw` 追加、`tests/test_e2e.py` の
   parametrize 更新

## リスクと対処

| リスク | 対処 |
|---|---|
| `MatchStmt` の AST 拡張で既存 Option-style コードが壊れる | `style` フィールドのデフォルトは要らない (parser が必ず set する)。既存 `examples/option_basic.rw` を e2e に残してあるので回帰検出可能 |
| `parse_match` の全面書き直しで既存テストが落ちる | 既存テスト (`test_match_two_arms_ok` 等) を変更せずに通るよう、Option-style の挙動を保つことを Step ごとに確認 |
| match parser 内の duplicate 検出 (同じ arm 2 回) を維持する | spec の `parse_match` スケルトンに duplicate-check の TODO を入れず、コード内で `if some_block is not None: raise duplicate error` のような既存パターンを踏襲 |
| `Ok`/`Err` を変数名としてユーザコードに使っているケース | `grep -rE '\b(Ok\|Err)\b' examples/ tests/*.rw` で確認 (該当なし)。Some/None と同じく新キーワードとして導入 |
| Option と Result の LLVM struct が同形なので irgen バグで型混同 | Sema が必ず先に弾く + 名前付き alias `RW_RESULT_INT_INT_TY` で可読性を担保。実害はないが「混同が怖いから別 alias」というのは妥当 |
| 真のジェネリクスへの道筋が見えるか | `MatchStmt` の `style` フィールドは generalize するとき「constructor name list」に置き換えやすい。Sema の style 分岐は型 dispatch table 化できる。 4c での generalize に対する障害は少ない |
