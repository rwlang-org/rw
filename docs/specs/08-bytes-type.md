# rw Bytes 型 (immutable, echo 最小セット)

## Context

`docs/specs/07-string-builtins.md` で `string` の `len` / `==` / `+` を入れた。
次に必要なのは **バイナリセーフな可変長バイト列** の表現で、これは将来の
netpoller + TCP API で `read(fd, n)` (旧 `tcp_read`、#33 で fd 汎用に統合) の
戻り値型として使われる。

`string` だとダメな理由:
- `string` は実質 immutable な `{i64 len, i8* ptr}` で、**型システム上は「文字列」**。
  バイナリ (\0 を含む、UTF-8 でないデータ) を `string` で持つと意味論が壊れる
- 「`print(s)` できる」「`s + s` で連結できる」前提でテキストデータが想定されている

`Bytes` を **別の型** として導入し、`string` と区別する。echo server に必要な
最小操作 (`len`, `==`, string との相互変換) だけを今回入れる。

長期計画 (再掲):

1. 文字列 `len` / `==` / `+` (済)
2. **このサブプロジェクト**: Bytes 型 + 最小 API
3. List[T]
4. Result[T, E] / Option[T]
5. netpoller + TCP API

## Goals

- 新しいプリミティブ型 `Bytes` を導入 (キーワード `Bytes`)
- 組込み:
  - `len(b: Bytes) -> int` (既存 `len(string)` をオーバーロード)
  - `b1 == b2`, `b1 != b2` (Bytes 同士のみ、`string` と Bytes の比較は禁止)
  - `bytes_from_str(s: string) -> Bytes`
  - `str_from_bytes(b: Bytes) -> string`
- `Bytes` を `spawn fn() -> Bytes` の戻り値型としても使える
  (= `Future[Bytes]` が動く)
- 公開 ABI 不変、既存テスト緑

## Non-Goals

- `b"..."` のような Bytes リテラル構文 (lexer 拡張が必要、後で)
- `bytes_at(b, i)` / `bytes_slice(b, i, j)` / Bytes 連結 (`+`) — プロトコル
  解析用、echo server には不要、別 PR
- `print(b: Bytes)` を許可する — UTF-8 でないかもしれないデータを直接出力する
  運用は型システムで禁止。必要なら `str_from_bytes(b)` で明示変換
- `Bytes` と `string` の暗黙変換 / `==` の混合

## 設計

### 内部表現

LLVM IR レベルでは `Bytes` も `string` と **同じ `{i64 len, i8* ptr}` (= `RW_STR_TY`)**
として表現する。Sema レベルで `T.STRING` と `T.BYTES` を別物として扱い、
型混同を Sema が静的に弾く。

利点:
- ランタイムには新しい関数を 1 つも追加しなくて済む。`rw_str_len` / `rw_str_eq`
  をそのまま流用
- `bytes_from_str` / `str_from_bytes` は型情報だけが変わる **noop** —
  irgen は引数の SSA 値をそのまま返す
- `Future[Bytes]` も `rw_spawn_str` / `rw_await_str` をそのまま使える

注意: 同じ表現を使う以上、**`Bytes` と `string` を取り違えると Sema が
失敗するだけで、不正アクセスはしない**。安全性ではなく言語のクリーンさのための
区別。

### 言語レベルから見た挙動

```rw
def main() -> int:
    b: Bytes = bytes_from_str("hello")
    print(len(b))                    # => 5
    if b == bytes_from_str("hello"):
        print("eq ok")
    s: string = str_from_bytes(b)
    print(s)                         # => hello
    return 0
```

エラーになるケース:

```rw
def main() -> int:
    b: Bytes = bytes_from_str("hi")
    print(b)                  # error: print does not support `Bytes`
    x: Bytes = b + b          # error: `+` requires int/float/string
    y: bool = b == "hi"       # error: `==` requires same type (Bytes vs string)
    z: Bytes = bytes_from_str(1)  # error: argument must be string
    return 0
```

### コンポーネント別の変更

#### `rwc/types.py`

```python
BYTES = _Primitive("Bytes")
```

を追加。`is_printable` / `is_numeric` には**含めない**。

#### `rwc/lexer.py`

```python
KW_BYTES = auto()
```

を `TokenKind` に追加し、`KEYWORDS` に `"Bytes": TokenKind.KW_BYTES` を入れる。
キーワード命名は大文字始まり (`Future` と同様、`int`/`string` 等の小文字
プリミティブとは別グループ)。

#### `rwc/parser.py`

`parse_type` の `kind_to_name` dict に `TokenKind.KW_BYTES: "Bytes"` を追加。
1 行。

#### `rwc/sema.py`

3 ヶ所:

1. `_resolve_type` の dict に `"Bytes": T.BYTES` を追加。
2. `_check_call` の `len` ハンドラを `string` または `Bytes` に拡張:
   ```python
   if at is not T.STRING and at is not T.BYTES:
       raise ... f"len argument must be string or Bytes, found `{at}`"
   ```
3. `_check_call` に 2 つの新組込みを追加:
   ```python
   if call.callee == "bytes_from_str":
       # arity 1, arg is string, returns Bytes
   if call.callee == "str_from_bytes":
       # arity 1, arg is Bytes, returns string
   ```
   どちらも `spawn bytes_from_str(...)` / `spawn str_from_bytes(...)` を
   `print` / `len` と同様に禁止する分岐を `SpawnExpr` 経路に追加。

二項演算子の `==` / `!=` は既に「両辺同じ型」を要求しており、`T.BYTES ==
T.BYTES` も自動的に通る。irgen 側で string と同じく `rw_str_eq` ルートに乗せる
分岐を追加するだけでよい。

#### `rwc/irgen.py`

- `llvm_type_of` に Bytes を追加:
  ```python
  if t is T.BYTES:
      return RW_STR_TY
  ```
- `_emit_binop` の `==`/`!=` 分岐で `is_str` の代わりに `is_strlike = lty in (T.STRING, T.BYTES)` を判定基準にし、`rw_str_eq` に渡す。
- `_emit_call`:
  - `len` の呼び出し: 引数が `T.STRING` でも `T.BYTES` でも `rw_str_len` を呼ぶ。Sema が既に型を検証しているので irgen 側は型に関係なく helper を呼ぶだけ。
  - `bytes_from_str` / `str_from_bytes`: 引数の SSA 値をそのまま返す noop:
    ```python
    if call.callee in ("bytes_from_str", "str_from_bytes"):
        return self._emit_expr(call.args[0], ctx)
    ```
- `_decl_spawn` / `_decl_await` の戻り値型分岐に `T.BYTES` を追加し、`rw_spawn_str` / `rw_await_str` を返す:
  ```python
  elif ret_ty is T.STRING or ret_ty is T.BYTES:
      name, ret_llvm = "rw_spawn_str", RW_STR_TY
  ```

#### ランタイム

変更なし。

### テスト

#### `tests/test_sema.py`

Positive (5 件):
- `Bytes` 型注釈と `bytes_from_str` の戻り値推論
- `len(Bytes)` の戻り値が int
- `Bytes == Bytes` が bool
- `str_from_bytes(b)` の戻り値が string
- `spawn fn() -> Bytes` が `Future[Bytes]`

Negative (5 件):
- `print(b)` で `Bytes` が printable でないエラー
- `b + b` で `+` が Bytes を許可しないエラー
- `b == "hi"` で `==` の型不一致エラー
- `bytes_from_str(1)` で引数型エラー
- `spawn bytes_from_str("a")` で組込み禁止エラー

#### e2e

- `examples/bytes_basic.rw` (上の挙動例の通り) と `.expected` を追加
- `tests/test_e2e.py` の parametrize に `"bytes_basic"` を追加

ランタイム単体テスト追加は不要 (新しい C 関数なし)。

## ファイル別変更

### 変更

- `rwc/types.py` — `BYTES = _Primitive("Bytes")`
- `rwc/lexer.py` — `KW_BYTES` 追加、`KEYWORDS` 拡張
- `rwc/parser.py` — `parse_type` の dict 拡張
- `rwc/sema.py` — `_resolve_type` / `_check_call` (3 組込み拡張) / SpawnExpr 禁止リスト
- `rwc/irgen.py` — `llvm_type_of` / `_emit_binop` / `_emit_call` / `_decl_spawn` / `_decl_await`
- `tests/test_sema.py` — positive 5 + negative 5
- `tests/test_e2e.py` — parametrize に 1 行追加

### 新規

- `examples/bytes_basic.rw`
- `examples/bytes_basic.rw.expected`

### 変更なし

- `runtime/` (一切手を入れない)
- `docs/specs/05-fibers.md` / `06-scheduler-mn.md` / `07-string-builtins.md`

## 検証

```sh
# pytest
uv run pytest -q
# expected: 既存 75 件 + Sema positive 5 + negative 5 + e2e 1 = 86 件 全緑

# 単独実行
uv run rwc run examples/bytes_basic.rw

# 既存 example 回帰
uv run rwc run examples/hello.rw
uv run rwc run examples/string_ops.rw
uv run rwc run examples/spawn_many.rw

# ランタイムには手を入れていないので C テストは前回のまま緑
cd runtime && make clean && make
```

## コミット構成

3 commits:

1. **rwc: introduce Bytes type (lexer/parser/types)** — `T.BYTES` /
   `KW_BYTES` / parse_type / `_resolve_type` だけ。Sema/irgen はまだなので
   `Bytes` を使うコードは別所でエラーになるが、`b: Bytes = ...` の型
   注釈だけは parse + resolve できる
2. **rwc: Bytes operations in sema + irgen** — 4 つの組込み (`len`
   オーバーロード, `bytes_from_str`, `str_from_bytes`, `==`) と spawn/await
   の Bytes 対応。positive/negative テスト一括追加
3. **examples + e2e** — `bytes_basic.rw` 追加、parametrize に組み込み

## リスクと対処

| リスク | 対処 |
|---|---|
| `Bytes` が変数名としてユーザコードに既出 | `grep -rE '\bBytes\b' examples/` で確認済み (該当なし)。新キーワードなので一般変数名と競合しても致命的でない (キーワードは予約語、ユーザは別名にする) |
| `==` で string vs Bytes が「同表現なのに型エラー」 | 仕様通り。Bytes と string は別型、明示変換を要求する。Sema のエラーメッセージは `"\`==\` requires same type, found \`Bytes\` and \`string\`"` のような既存形 |
| `Future[Bytes]` が `rw_spawn_str` を共有することで仮想的な型混乱が将来 | 現状の Sema は型レベルで Bytes/string を区別するので、ユーザコードから見て混乱はない。将来 Bytes の表現を変える (たとえば独自 struct 化) ことになったら `_decl_spawn` の分岐を分けるだけで対処できる |
| `print(b)` が言語サイドで弾かれない (= irgen が落ちる) リスク | `is_printable(T.BYTES) == False` を確実に Sema レベルでテスト (negative テストでカバー) |
