# rw List[int] 型 (immutable, モノモーフ, echo 最小セット)

## Context

これまでに rw 言語に入れたプリミティブ:

- `string` + `len` / `==` / `+` (#91)
- `Bytes` + `len` / `==` / 相互変換 (#92)

`List[T]` は **本物のジェネリック型** だが、ロードマップの今段階で目的とする
echo server に必要なのは「クライアント fd の配列を保持する」だけ。
そのために **`List[int]` 1 種類だけ** を入れる。

将来の `List[string]` / `List[Bytes]` / 汎用 `List[T]` への拡張パスは残すが、
今回は **言語にジェネリック構文を導入しない**。`List[int]` を 1 つの
プリミティブ型として扱い、`int` 以外の型パラメータは parser でエラーにする。

ロードマップ:

1. 文字列 `len` / `==` / `+` (済)
2. Bytes 型 (済)
3. **このサブプロジェクト**: List[int] 最小 API
4. Result[T, E] / Option[T]
5. netpoller + TCP API

## Goals

- 新しいプリミティブ型 `List[int]` を導入 (`Future[T]` と同様、parser が
  `List` の後の `[int]` を要求する固定形)
- 4 つの組込み:
  - `list_new() -> List[int]`
  - `list_push(l: List[int], v: int) -> List[int]` (新しい List を返す)
  - `list_at(l: List[int], i: int) -> int`
  - `len(l: List[int]) -> int` (既存 `len` をオーバーロード)
- 公開 ABI 既存部分は不変
- echo server で fd 配列を扱える状態にする

## Non-Goals

- 汎用 `List[T]` (T が int 以外): parser で「only `List[int]` is supported」
  エラー
- `Future[List[int]]` (= `spawn fn() -> List[int]`): Sema で禁止
- mutable list (push が in-place で副作用): 値型 immutable のみ
- `list_pop` / `list_remove` / `list_slice` / `list_concat` / `list_eq` /
  `list_set` (要素更新)
- `for x in l` のイテレーション構文
- 範囲外アクセスを言語レベルで表現 (= `Result[int, IndexError]` 等は
  まだ無いので、当面 `rw_list_int_at` は範囲外で `abort()`)
- `print(l)` (debug 用に便利かもしれないが、`print` の挙動定義を
  オーバーロードで広げる作業を今回は避ける)

## 設計

### 内部表現

`List[int]` は 3 ワード fat struct:

```c
typedef struct {
    int64_t  len;   /* current element count */
    int64_t  cap;   /* allocated capacity in elements */
    int64_t *data;  /* pointer to int64_t[cap]; NULL when cap == 0 */
} rw_list_int;
```

LLVM IR:

```
%rw_list_int = { i64 len, i64 cap, i64* data }
```

3 ワードを SSA 値としてそのまま渡し回す (Bytes/string が `{len, ptr}` 2 ワード
fat struct なのと同じ流儀)。`Future[List[int]]` で `i8*` 経由の引き渡しを
する必要がない (今回 non-goal)。

### 不変性の取り扱い

`list_push` のたびに **新しい `data` 配列を malloc し、要素を全部 memcpy** し、
末尾に新しい値を足し、新しい `{len+1, new_cap, new_data}` を返す。
古い `data` は free しない (= 他の SSA がまだ参照しているかもしれない、
**リーク許容**)。Push 計算量は O(n) だが、echo server スケール (fd 数 1024
程度) では問題なし。

シェアリングしないので、コードの読み手は「`l2 = list_push(l1, x)` の後でも
`l1` は変わらない」と即断できる。これは Bytes/string と同じ「fat pointer 値型」
モデルの自然な拡張。

### 容量拡張ポリシー

毎回新しい配列を確保するので「ポリシー」は実質「初回は 4 個分、以降 2 倍」:

```c
int64_t new_cap = (l.cap == 0) ? 4 : l.cap * 2;
while (new_cap < l.len + 1) new_cap *= 2;
```

`l.len + 1 <= new_cap` を保証して `malloc(new_cap * 8)` する。

### 言語レベルから見た挙動

```rw
def main() -> int:
    l: List[int] = list_new()
    l = list_push(l, 10)
    l = list_push(l, 20)
    l = list_push(l, 30)
    print(len(l))           # => 3
    print(list_at(l, 0))    # => 10
    print(list_at(l, 2))    # => 30
    return 0
```

エラーになるケース:

```rw
def main() -> int:
    a: List[string] = list_new()      # parser error: only List[int] supported
    b: List[int] = list_new()
    print(b)                          # sema error: print does not support `List[int]`
    c: List[int] = b + b              # sema error: + requires int or float
    if b == b:                        # sema error: == requires int/float/bool/string (no List)
        return 0
    n: int = list_at(b, "x")          # sema error: list_at index must be int
    f: Future[List[int]] = spawn list_new()  # sema error: cannot spawn builtin
    return 0
```

### コンポーネント別の変更

#### ランタイム (`runtime/runtime.h`, `runtime/runtime.c`)

公開構造体 `rw_list_int` と 4 関数を追加 (string ops と同じスタイル):

```c
typedef struct {
    int64_t  len;
    int64_t  cap;
    int64_t *data;
} rw_list_int;

rw_list_int  rw_list_int_new (void);
rw_list_int  rw_list_int_push(rw_list_int l, int64_t v);
int64_t      rw_list_int_at  (rw_list_int l, int64_t i);
int64_t      rw_list_int_len (rw_list_int l);
```

実装:

- `_new`: `{0, 0, NULL}` を返す。
- `_push`: 上記の容量拡張ポリシーで malloc、`memcpy(new_data, l.data, l.len * 8)`、
  `new_data[l.len] = v`、`{l.len+1, new_cap, new_data}` を返す。
- `_at`: `i < 0 || i >= l.len` なら `fputs("rw: list_at: index out of bounds\n", stderr); abort();`。
  範囲内なら `l.data[i]` を返す。
- `_len`: `l.len` を返す (irgen の単純化用、`len(l)` の呼び先)。

#### `rwc/types.py`

```python
LIST_INT = _Primitive("List[int]")
```

`is_printable` / `is_numeric` には含めない。

#### `rwc/lexer.py`

変更なし。`List` は既存 `Future` と同じく **キーワードにせず、parser が
IDENT として処理**する手もあったが、`Future` は実際は `KW_FUTURE` キーワード
扱いになっている。同じくしたい。

→ **`KW_LIST = auto()` を追加し、`KEYWORDS["List"] = KW_LIST`**。
spec 提案では parser で IDENT 扱いと書いたが、`Future` との一貫性のため
キーワード化する (1 行で済む)。

#### `rwc/parser.py`

`parse_type` で `Future` を処理する分岐の **直下** に `List` 用の分岐を追加:

```python
if t.kind == TokenKind.KW_LIST:
    self.i += 1
    self.eat(TokenKind.LBRACK, "'[' after List")
    inner_tok = self.cur
    if inner_tok.kind != TokenKind.KW_INT:
        raise ParserError(
            "only List[int] is supported in this version of rw",
            inner_tok.line, inner_tok.col, max(1, len(inner_tok.value)),
        )
    self.i += 1
    self.eat(TokenKind.RBRACK, "']' to close List[int]")
    return A.TypeName("List[int]", t.line, t.col)
```

AST には既存 `A.TypeName` をそのまま使う (名前 `"List[int]"`)。新しい AST
ノードは不要。

#### `rwc/sema.py`

3 ヶ所:

1. `_resolve_type` の dict に `"List[int]": T.LIST_INT` を追加。
2. `_check_call` で `len` ハンドラを `T.LIST_INT` も許可するよう拡張。さらに
   3 つの組込みを追加:
   - `list_new()` arity 0, returns `T.LIST_INT`
   - `list_push(List[int], int)` arity 2, returns `T.LIST_INT`
   - `list_at(List[int], int)` arity 2, returns `T.INT`
3. SpawnExpr の禁止リストに `list_new` / `list_push` / `list_at` を追加。

#### `rwc/irgen.py`

- `RW_LIST_INT_TY` の定数を `RW_STR_TY` と同じレベルで定義:
  ```python
  RW_LIST_INT_TY = ir.LiteralStructType([I64, I64, I64.as_pointer()])
  ```
- `llvm_type_of(T.LIST_INT) -> RW_LIST_INT_TY`。
- `_declare_runtime` で 4 外部関数を宣言:
  ```python
  self._rw_list_int_new  = ir.Function(m, ir.FunctionType(RW_LIST_INT_TY, []), "rw_list_int_new")
  self._rw_list_int_push = ir.Function(m, ir.FunctionType(RW_LIST_INT_TY, [RW_LIST_INT_TY, I64]), "rw_list_int_push")
  self._rw_list_int_at   = ir.Function(m, ir.FunctionType(I64, [RW_LIST_INT_TY, I64]), "rw_list_int_at")
  self._rw_list_int_len  = ir.Function(m, ir.FunctionType(I64, [RW_LIST_INT_TY]), "rw_list_int_len")
  ```
- `_emit_call` で 3 つの組込み + `len(List[int])` を扱う。`len` は Sema で
  渡された引数の型 (`self.sema.expr_types[id(call.args[0])]`) を見て
  `T.LIST_INT` なら `rw_list_int_len` を、`T.STRING` / `T.BYTES` なら従来の
  `rw_str_len` を呼ぶ。
- `_decl_spawn` / `_decl_await` には `T.LIST_INT` を**追加しない** (= 渡された
  場合は `RuntimeError`、ただし Sema が既に弾いているのでここに到達しない)。

### ランタイムでの List[int] 値の引き渡し

ABI 上、`{i64, i64, i64*}` の fat struct を C コードに値渡しする。アーキ別の
calling convention:
- aarch64 (Apple/Linux ARM64): struct が 16 バイト以下なら xN レジスタで渡す。
  3 ワード (24 バイト) は **メモリ経由** で渡される (SysV AArch64 AAPCS では
  HFA 規則の外なので通常はスタックまたは hidden pointer)。
- x86_64 SysV: struct が 16 バイト超 → メモリ経由。

clang は自動的に正しい convention を選ぶので、rwc が IR で `{i64, i64, i64*}`
を引き渡すだけで OK。llvmlite は LLVM が ABI を解決する形のままで動く。

### スレッド安全性

`rw_list_int_push` 単体は副作用 (`malloc` + `memcpy`) のみで、`l` を mutate
しない。並行に同じ `l` を push しても両方が独立した新しい List を作る (= 元
の `l` を共有して読むだけ)。問題なし。

`l.data` の free は **しない** ので、別 fiber が古い data を read している
最中に GC で free される事故は起こりえない (リーク許容の代償)。

## ファイル別変更

### 変更

- `runtime/runtime.h` — `rw_list_int` struct と 4 プロトタイプ
- `runtime/runtime.c` — 4 関数実装
- `rwc/types.py` — `LIST_INT = _Primitive("List[int]")`
- `rwc/lexer.py` — `KW_LIST` + `KEYWORDS["List"]`
- `rwc/parser.py` — `parse_type` の Future 分岐の隣に List 分岐
- `rwc/sema.py` — `_resolve_type` / `_check_call` (3 組込み + len) / SpawnExpr
  禁止リスト 3 件
- `rwc/irgen.py` — `RW_LIST_INT_TY` / `llvm_type_of` / `_declare_runtime` /
  `_emit_call`
- `tests/test_sema.py` — positive 4 + negative 7
- `tests/test_e2e.py` — parametrize に `list_basic` を追加
- `.gitignore` — `runtime/fiber/test_list_int`

### 新規

- `runtime/fiber/test_list_int.c` — C 単体テスト
- `examples/list_basic.rw` + `.expected`

### 変更なし

- fiber 関連 (`runtime/fiber/sched.c` 等)、driver、`docs/specs/05`〜`08`

## 検証

```sh
# ランタイム単体
make -C runtime clean && make -C runtime
cd runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_list_int.c librw.a -o fiber/test_list_int
./fiber/test_list_int

# pytest
cd ..
uv run pytest -q
# 期待: 既存 87 + sema positive 4 + sema negative 7 + e2e 1 = 99 件全緑

# 単独実行
uv run rwc run examples/list_basic.rw

# 既存 example 回帰
uv run rwc run examples/string_ops.rw
uv run rwc run examples/bytes_basic.rw
uv run rwc run examples/spawn_many.rw
```

## コミット構成

4 commits:

1. **runtime**: `rw_list_int` 構造と 4 helper、`test_list_int.c` で単体テスト
2. **rwc (lexer/parser/types)**: `KW_LIST`、`parse_type` の List 分岐、
   `T.LIST_INT`、`_resolve_type` の dict 更新。型注釈だけ parse + resolve
   できる状態 (Sema/irgen はまだ未対応で `list_*` 呼び出しはエラーになる)
3. **rwc (sema + irgen)**: 4 組込みの Sema 検証、`len` のオーバーロード拡張、
   irgen で IR 生成、negative テスト一括
4. **examples + e2e**: `list_basic.rw` 追加と `tests/test_e2e.py` の
   parametrize 更新

## リスクと対処

| リスク | 対処 |
|---|---|
| `List` が既存の変数名 / 関数名と競合 | `grep -rE '\bList\b' examples/ tests/` で確認済み (該当なし)。`List` を新キーワードにすると、ユーザが `List` という識別子を使えなくなるが、`Future` と同じ扱いなので問題なし |
| `list_at` の範囲外で abort するのは粗い | 本来 `Result[int, IndexError]` で返したいが Result 型未実装。spec の non-goals に明記、netpoller 後に Result を入れたら戻す |
| 同じ `l` を 2 つの fiber が push しても安全か | `_push` は `l` を読むだけで mutate しないので並行 push 安全。両者が異なる新 List を返す (= シェアできない、broadcast like の用途には別 API が要る) |
| `Future[List[int]]` を将来入れる時の互換性 | Sema が今は禁止しているので、後で `_decl_spawn` / `_decl_await` に LIST_INT を追加するだけで済む (struct を新 spawn helper で受け渡しする ABI 設計が要るが、別 PR) |
| `cap` 拡張で 2^63 オーバーフロー | echo server スケールではあり得ない。実装上 `new_cap` は `int64_t`、`l.len + 1` チェックも `int64_t` で行うが、overflow 検査は省略 (rw は学習用) |
| `int` 以外の List を書こうとしたユーザへの導線 | parser のエラーメッセージを「only `List[int]` is supported in this version of rw」と明示。誤って `List[string]` 等を書いた場合に意図が伝わる |
