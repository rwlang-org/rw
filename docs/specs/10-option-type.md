# rw Option[int] 型 + match 構文 (モノモーフ最小形)

## Context

ここまでに rw 言語へ入れたプリミティブ:

- `string` の `len` / `==` / `+` (#91)
- `Bytes` (#92)
- `List[int]` (#93)

`List[int]` で踏んだ最後の壁が、`list_at(l, i)` の **範囲外アクセスを言語で
表現できない** 問題だった。現状はランタイムで `abort()` するしかなく、これは
学習用言語としては仕方ないが、 echo server 直前にもう一段踏み込むなら
**「失敗を値として返す」表現** が要る。

タグ付き共用体 (sum type) を **本気で入れる** には:

- 真のジェネリクス (`Option[T]`)
- `Result[T, E]`
- パターンマッチ (構文、exhaustiveness、ネスト、ガード)
- 既存組込み (`list_at` 等) のシグネチャ書き換え

がすべて絡む。これは間違いなく 1 PR の範囲を超える。`incremental-language-
extensions` の鉄則「ジェネリクスは 2 度目に必要になった時」「1 PR で触る
レイヤーは多くて 4 つまで」に従い、**`Option[int]` 1 種類 + 最小 match
構文** だけを本サブプロジェクトに切り出す。

長期計画は spec の外で進める:

1. 文字列 `len` / `==` / `+` (済)
2. Bytes 型 (済)
3. List[int] (済)
4. **このサブプロジェクト**: Option[int] + match (4a)
5. (将来) Result[int, int] + match の 2 アーム確立 (4b)
6. (将来) 真のジェネリクス化 (4c)
7. (将来) netpoller + TCP API

## Goals

- 新しいプリミティブ型 `Option[int]` を導入 (parser は `[int]` 以外を拒否)
- 値構築: `Some(int) -> Option[int]`, `None: Option[int]`
- 値分解: 最小 `match` 文 (Python 3.10 風の `case` キーワード + ブロック)
  - `case Some(x): <block>` (`x` は IDENT 1 個、bound として block 内で
    `int` 型)
  - `case None: <block>`
  - 2 アーム必須、順不同
- `list_at_opt(l: List[int], i: int) -> Option[int]` を追加して、範囲外
  アクセスを `None` で返せるようにする (`list_at` は abort のまま残す、後方
  互換)
- 公開 ABI 既存部分は不変、既存テスト緑

## Non-Goals

- 汎用ジェネリック (`Option[T]` の T が int 以外): parser で「only
  `Option[int]` is supported」エラー
- `Result[T, E]` (別 PR、4b)
- match を式として使う (`x = match v: ...`): match は statement のみ
- ネストパターン (`Some(Some(x))`)、ガード (`case Some(x) if x > 0`)、
  ワイルドカード (`case _`)
- match の 1 アームのみ / 3 アーム以上 (`Option[int]` は 2 値しか持たないので
  ちょうど 2 アームを要求)
- `Future[Option[int]]` (spawn 経由): Sema で禁止
- `Option` 用の組込みメソッド (`unwrap`, `is_some`, `or_else`)
- `print(opt)` (printable リストに加えない、`match` で `Some(x): print(x)`
  と書かせる)
- `opt == opt` (== whitelist に加えない、`match` で分解させる)
- `list_at` を `list_at_opt` に置き換える破壊的変更 (既存 example が回帰する
  ので並置)
- 範囲外を `Result[int, IndexError]` で表現 (IndexError 型がまだ無いので
  None で代用)

## 設計

### 内部表現

`Option[int]` は 2 ワード fat struct:

```c
typedef struct {
    int64_t tag;       /* 0 = None, 1 = Some */
    int64_t payload;   /* Some のとき int 値、None のとき未定義 */
} rw_option_int;
```

LLVM IR:

```
%rw_option_int = { i64 tag, i64 payload }
```

サイズ 16 バイト → arm64 / x86_64 SysV 両方で **2 レジスタで return できる**。
`List[int]` で踏んだ pointer-out ABI 問題は **発生しない** (16 バイト以下なら
value 返し OK、`llvm-ir-c-abi` skill 参照)。

ランタイム関数の追加は **`list_at_opt` だけ**:

```c
void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i);
```

`rw_option_int` も pointer-out にしたほうが対称的で安全 (将来 24 バイト
超の型に拡張するときも統一できる) なので、`list_at_opt` も pointer-out
にする。`Some` / `None` の構築自体は LLVM IR 内 (`insertvalue` /
constant struct) で完結し、ランタイムを介さない。

### 言語レベルの挙動

#### 値構築

```rw
def safe_div(a: int, b: int) -> Option[int]:
    if b == 0:
        return None
    return Some(a / b)
```

- `Some(<int 式>)`: 式として評価され、`{tag=1, payload=<int>}` を返す
- `None`: リテラルとして評価され、`{tag=0, payload=0}` を返す
  - `None` は新しい予約語 (lexer の `KW_NONE`)。変数名としては使えなくなる

#### 値分解

```rw
def main() -> int:
    r: Option[int] = safe_div(10, 2)
    match r:
        case Some(x):
            print(x)             # => 5
        case None:
            print(-1)
    e: Option[int] = safe_div(10, 0)
    match e:
        case Some(x):
            print(x)
        case None:
            print(-1)            # => -1
    return 0
```

- `match <expr>:` の式は `Option[int]` 型でなければエラー
- 続くインデント内に `case Some(<IDENT>):` と `case None:` が **両方** 必要
  (順不同)
- 各 case の右にブロック (1 つ以上の statement)
- match 全体は **statement**。式としては使えない (現状の rw に式形 if が
  無いのと揃える)

#### 範囲外を None で返す

```rw
def main() -> int:
    l: List[int] = list_new()
    l = list_push(l, 42)
    safe: Option[int] = list_at_opt(l, 0)
    match safe:
        case Some(x):
            print(x)          # => 42
        case None:
            print(-1)
    oob: Option[int] = list_at_opt(l, 5)
    match oob:
        case Some(x):
            print(x)
        case None:
            print(-1)         # => -1
    return 0
```

### コンポーネント別の変更

#### ランタイム

新規 1 関数のみ:

```c
void rw_list_int_at_opt(rw_option_int *out, const rw_list_int *l, int64_t i);
```

実装は `rw_list_int_at` のロジックを `abort` の代わりに `out->tag = 0` を
書くように変えた版。範囲内なら `{tag=1, payload=l->data[i]}` を out に
書き込む。

`rw_option_int` の C 型定義も追加 (`runtime.h` のトップに):

```c
typedef struct {
    int64_t tag;
    int64_t payload;
} rw_option_int;
```

C 単体テスト `runtime/fiber/test_option.c` (新規):
- `rw_list_int_at_opt` が範囲内で Some、範囲外で None を返すこと
- list が空のときも None を返すこと

#### `rwc/types.py`

```python
OPTION_INT = _Primitive("Option[int]")
```

`is_printable` / `is_numeric` には含めない。

#### `rwc/lexer.py`

新キーワード 5 つ:

```python
KW_OPTION = auto()
KW_MATCH  = auto()
KW_CASE   = auto()
KW_SOME   = auto()
KW_NONE   = auto()
```

```python
KEYWORDS = {
    ...,
    "Option": KW_OPTION,
    "match":  KW_MATCH,
    "case":   KW_CASE,
    "Some":   KW_SOME,
    "None":   KW_NONE,
}
```

#### `rwc/parser.py`

##### `parse_type` で `Option[int]` を受ける

`Future` / `List` と同パターン:

```python
if t.kind == TokenKind.KW_OPTION:
    self.i += 1
    self.eat(TokenKind.LBRACK, "'[' after Option")
    inner = self.cur
    if inner.kind != TokenKind.KW_INT:
        raise ParserError("only Option[int] is supported in this version of rw",
                          inner.line, inner.col, max(1, len(inner.value)))
    self.i += 1
    self.eat(TokenKind.RBRACK, "']' to close Option[int]")
    return A.TypeName("Option[int]", t.line, t.col)
```

##### 式パーサで `Some(e)` と `None` を受ける

primary expression のレベルで:
- `KW_SOME` + `(` + 式 + `)` → `A.SomeExpr(arg)`
- `KW_NONE` → `A.NoneExpr()`

##### 文パーサで `match` を受ける

`parse_stmt` の if/while/return と並列に:

```python
if self.cur.kind == TokenKind.KW_MATCH:
    return self._parse_match()
```

`_parse_match` の仕事:
1. `match` キーワードを消費し、式をパース、`:` を要求
2. INDENT を要求
3. 2 つの case をパース (順不同、両方必須):
   - `case Some(<IDENT>):` + block
   - `case None:` + block
4. DEDENT を要求
5. `A.MatchStmt(target, some_var, some_block, none_block)` を返す

case が 3 つ目以上ある、片側欠落、`case _:` 等は parser エラー。

#### `rwc/ast_nodes.py`

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
    some_var: str        # IDENT bound in some_block
    some_block: List[Stmt]
    none_block: List[Stmt]
```

#### `rwc/sema.py`

##### `_resolve_type`

dict に `"Option[int]": T.OPTION_INT` を追加。

##### 式 Sema

- `SomeExpr`: `arg` の型が `T.INT` でなければエラー、戻り型 `T.OPTION_INT`
- `NoneExpr`: 戻り型 `T.OPTION_INT`

##### `MatchStmt` Sema

```python
if isinstance(stmt, A.MatchStmt):
    tt = self._check_expr(fn, stmt.target, locals_)
    if tt is not T.OPTION_INT:
        raise CompileError("match target must be Option[int]")
    # Some arm: bind some_var as int in a new locals scope
    sub = dict(locals_)
    sub[stmt.some_var] = T.INT
    self._check_block(fn, stmt.some_block, sub)
    # None arm: no binding
    self._check_block(fn, stmt.none_block, locals_)
    return
```

return-coverage チェック (関数の最後の statement が return か) は match 全体
を「両 arm 共に return しているなら全体で return している」と扱う。
これは既存の `if/elif/else` の terminates-in-return ロジックと同じパターンで
実装する。

##### `list_at_opt` 組込み

`_check_call` に `list_at_opt(List[int], int) -> Option[int]` を追加。
SpawnExpr 禁止リストにも追加。

##### == ホワイトリストは触らない

`Option[int]` を含めないことで `opt == None` のような比較を自動的に弾く。

#### `rwc/irgen.py`

##### 型定義

```python
RW_OPTION_INT_TY = ir.LiteralStructType([I64, I64])  # {tag, payload}
```

`llvm_type_of(T.OPTION_INT) -> RW_OPTION_INT_TY`。

##### 値構築

```python
def _emit_some(self, expr, ctx):
    v = self._emit_expr(expr.arg, ctx)            # i64
    s = ir.Constant(RW_OPTION_INT_TY, [ir.Constant(I64, 1), ir.Constant(I64, 0)])
    s = ctx.builder.insert_value(s, v, 1)         # set payload
    return s

def _emit_none(self, expr, ctx):
    return ir.Constant(RW_OPTION_INT_TY,
                       [ir.Constant(I64, 0), ir.Constant(I64, 0)])
```

##### match の lowering

```python
def _emit_match(self, stmt, ctx):
    b = ctx.builder
    v = self._emit_expr(stmt.target, ctx)
    tag = b.extract_value(v, 0)
    payload = b.extract_value(v, 1)

    some_bb = ctx.function.append_basic_block("match.some")
    none_bb = ctx.function.append_basic_block("match.none")
    end_bb = ctx.function.append_basic_block("match.end")

    # i64 switch: 1 -> some, default (0) -> none
    sw = b.switch(tag, none_bb)
    sw.add_case(ir.Constant(I64, 1), some_bb)

    # Some arm: allocate slot for bound var, store payload
    b.position_at_end(some_bb)
    slot = b.alloca(I64, name=stmt.some_var)
    b.store(payload, slot)
    ctx.locals[stmt.some_var] = slot
    self._emit_block(stmt.some_block, ctx)
    if not b.block.is_terminated:
        b.branch(end_bb)
    del ctx.locals[stmt.some_var]

    # None arm
    b.position_at_end(none_bb)
    self._emit_block(stmt.none_block, ctx)
    if not b.block.is_terminated:
        b.branch(end_bb)

    b.position_at_end(end_bb)
```

`ctx.locals` の add/remove は既存の `_emit_block` のスコープ管理に倣う。
両 arm が return している (terminated) なら end_bb は到達不能だが
position するだけで害なし (LLVM が消す)。

##### `list_at_opt` 呼び出し

`rw_list_int_at_opt` の declare を `_declare_runtime` に追加 (pointer-out
パターン: `void (out*, l*, i)`)。`_emit_call` で `list_at_opt` を:

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

##### `_decl_spawn` / `_decl_await`

T.OPTION_INT を含めない (= `Future[Option[int]]` 不可、Sema が先に弾く)。

### テスト

#### `tests/test_sema.py` (positive 6 + negative 7)

Positive:
- `Some(5)` が `Option[int]`
- `None` が `Option[int]`
- `def f() -> Option[int]: return Some(1)` が通る
- `def f() -> Option[int]: return None` が通る
- `match v:` で両 arm 揃えれば通る
- some_arm 内で bound `x` が int として使える (`print(x + 1)`)

Negative:
- `Some("hi")` → 引数型エラー
- `Option[string]` → parser エラー
- `match` の片側欠落 → Sema エラー
- `match` ターゲットが Option[int] でない → Sema エラー
- `print(Some(1))` → printable エラー
- `Some(1) == None` → 比較不可エラー
- `spawn fn() -> Option[int]` で fn を呼ぶ場合 → spawn エラー

#### `examples/option_basic.rw` + `.expected`

(spec 上部の例を採用)

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

期待出力:

```
5
-1
```

`tests/test_e2e.py` の parametrize に `option_basic` を追加。

#### C 単体テスト

`runtime/fiber/test_option.c`: `rw_list_int_at_opt` の Some / None / 空 list
パスをカバー。

## ファイル別変更

### 変更

- `runtime/runtime.h` — `rw_option_int` struct と `rw_list_int_at_opt`
  プロトタイプ
- `runtime/runtime.c` — `rw_list_int_at_opt` 実装
- `rwc/types.py` — `OPTION_INT`
- `rwc/lexer.py` — 5 つの新キーワード
- `rwc/parser.py` — `parse_type` Option 分岐 + `Some`/`None` 式 +
  `parse_match`
- `rwc/ast_nodes.py` — `SomeExpr` / `NoneExpr` / `MatchStmt`
- `rwc/sema.py` — `_resolve_type` / 式 Sema / `MatchStmt` Sema /
  `list_at_opt` 組込み / SpawnExpr 禁止 / return-coverage チェック更新
- `rwc/irgen.py` — `RW_OPTION_INT_TY` / `llvm_type_of` / `_emit_some` /
  `_emit_none` / `_emit_match` / `list_at_opt` 呼び出し / `_declare_runtime`
- `tests/test_sema.py` — positive 6 + negative 7
- `tests/test_e2e.py` — parametrize に `option_basic`
- `.gitignore` — `runtime/fiber/test_option`

### 新規

- `runtime/fiber/test_option.c`
- `examples/option_basic.rw` + `.expected`
- `docs/specs/10-option-type.md` (本ファイル)
- `docs/plans/2026-05-22-option-type.md` (writing-plans で作成)

### 変更なし

- fiber スケジューラ (`runtime/fiber/sched.c` 等)、driver、既存 spec docs

## 検証

```sh
# ランタイム単体
make -C runtime clean && make -C runtime
cd runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_option.c librw.a -o fiber/test_option
./fiber/test_option

# pytest
cd ..
uv run pytest -q
# 期待: 既存 101 + Sema positive 6 + negative 7 + e2e 1 = 115 件全緑

# 単独実行
uv run rwc run examples/option_basic.rw
# 期待出力: 5\n-1\n

# 既存 example 回帰
uv run rwc run examples/list_basic.rw
uv run rwc run examples/string_ops.rw
uv run rwc run examples/spawn_many.rw
```

## コミット構成

4 commits:

1. **rwc (lexer/parser/types)**: 5 つのキーワード、`Option[int]` パース、
   `Some(e)` と `None` の式、`match` 文のパース、AST ノード追加、`T.OPTION_INT`、
   `_resolve_type`。型注釈と AST 構築だけ通る (Sema/irgen は未実装)
2. **rwc (sema)**: `Some`/`None` 式の Sema、`MatchStmt` の Sema (exhaustiveness、
   bound variable、return-coverage)、negative テスト一括
3. **runtime + rwc (irgen)**: `rw_option_int` 構造体、`rw_list_int_at_opt`
   実装と C テスト、irgen で `Some`/`None`/`match`/`list_at_opt` の IR 生成、
   smoke 検証
4. **examples + e2e**: `option_basic.rw` 追加と `tests/test_e2e.py` の
   parametrize 更新

## リスクと対処

| リスク | 対処 |
|---|---|
| `None` を予約語化することで既存ユーザコードに衝突 | rw リポジトリ内の `examples/*.rw` に `None` を変数名として使っている箇所は無い (grep 確認)。新規予約語なので将来も意図せず衝突しない |
| match の return-coverage チェック実装が複雑 | 既存の `if/elif/else` の terminates-in-return ロジックを 1 関数に抽出して match でも再利用。両 arm が return しているかを再帰的に判定する |
| `Some(x) == ...` 比較を期待するユーザ | spec の Non-Goals に明記、エラーメッセージで「use match to inspect Option[int]」と誘導 (ただし今回エラーメッセージはシンプルに「cannot compare `Option[int]`」で済ませる) |
| 将来 `Result[T, E]` を入れたとき match を再利用したい | parser は `case Some(IDENT)` をハードコードしているのでそのままでは使えない。spec の Non-Goals に「Result 型は別 PR、その時 match parser を generalize」を明記 |
| 既存 == の whitelist 化 (#93 で入れたもの) が Option を弾けるか | `Option[int]` を whitelist に追加しないだけで自動的に「cannot compare Option[int]」エラー。テストでカバー |
| `_emit_match` の SSA / basic-block 管理が既存 `if` と整合するか | 既存 `_emit_if` を参考に同じスコープルール (alloca はエントリ BB、locals dict を block 単位で push/pop) を踏襲。実装時に既存パターンを Read で確認 |
