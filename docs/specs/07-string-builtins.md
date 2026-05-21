# rw 文字列拡張: len / == / +

## Context

rw は `print` と `spawn`/`await` 以外に組込み機能がほとんど無く、`string` は
**リテラルを `print` するだけ** の存在になっている。

```rw
def main() -> int:
    name: string = "alice"
    # ここから先が書けない:
    #   - len(name) で長さを取りたい
    #   - if name == "alice": の分岐がしたい
    #   - print("hello, " + name) でログを組み立てたい
    print(name)
    return 0
```

この PR は **netpoller / TCP API** を将来入れる前提で、その手前にある
「言語の最小拡張」の 1 ステップ目を入れる。echo server を rw コードで
書くのに必要なのは「読んだ文字列を書き戻す」「長さで条件分岐する」
「ログを組み立てる」の 3 つで、これらは `len` / `==` / `+` だけで足りる。

長期計画はこの spec の外で進める:

1. **このサブプロジェクト**: string の `len` / `==` / `+`
2. Bytes 型と buffer API
3. List[T]
4. Result[T, E] / Option[T]
5. netpoller + TCP API

## Goals

- 組込み関数 `len(s: string) -> int` を追加
- 演算子 `==` / `!=` を string 同士で許可 (バイト一致)
- 演算子 `+` を string 同士で許可 (連結、新しい string を確保)
- 既存テスト (66 件 + C テスト) を緑のまま維持
- 公開 ABI (`rw_spawn_*` / `rw_await_*` / `rw_str`) は変更しない

## Non-Goals

- `s.len` のドット記法 (rw に struct 経由のメンバアクセスが無いため筋が悪い)
- 辞書順比較 `<` / `<=` / `>` / `>=` (echo server に不要)
- `*` (string 反復) や `s[i]` (index)
- 文字列の mutate (`rw_str` は const ポインタ前提)
- メモリ回収 (連結結果は malloc しっぱなし、リーク許容。学習・実験用なので OK)
- 国際化、Unicode 正規化 (バイト列としての扱いのみ)

## 設計

### 言語レベルから見た挙動

```rw
def main() -> int:
    a: string = "hello"
    b: string = ", world"
    c: string = a + b               # "hello, world" (新規 allocate)
    print(len(c))                   # 12
    if c == "hello, world":         # true
        print("matched")
    if a != b:                      # true
        print("differ")
    return 0
```

### ランタイム追加関数 (`runtime.c` / `runtime.h`)

3 つの helper 関数を C ABI で追加する。

```c
/* New helpers — internal but exposed for irgen. */
int64_t rw_str_len   (rw_str s);
int8_t  rw_str_eq    (rw_str a, rw_str b);
rw_str  rw_str_concat(rw_str a, rw_str b);
```

実装方針:

- `rw_str_len`: `s.len` をそのまま返すワンライナー。irgen が `extractvalue`
  を直接出してもよかったが、ABI を 1 本に揃えるため C 関数で出す。後で
  「\0 終端でない場合の境界チェック」を入れる余地もできる。
- `rw_str_eq`: `a.len != b.len` → 0 即返し。同じなら `memcmp(a.ptr, b.ptr, a.len) == 0`
  を `int8_t` (0/1) で返す。`rw_print_bool` と同じ i8 表現に合わせる。
- `rw_str_concat`: `malloc(a.len + b.len)` で確保、`memcpy(p, a.ptr, a.len)`、
  `memcpy(p + a.len, b.ptr, b.len)`、`{len = a.len+b.len, ptr = p}` を返す。
  両方 len=0 の場合は ptr=NULL, len=0 を返す (malloc(0) を呼ばない)。
  解放はしない (リーク許容)。

これらは pthread-safe (各呼び出しは局所的)、副作用なし (concat の malloc
を除く)、シングルスレッド・マルチスレッドどちらでも安全。

### コンパイラ変更点

#### lexer / parser

変更なし。`len(s)` は既存の `Call` ノードでパースされ、`s1 + s2` と
`s1 == s2` は既存の二項演算子ノードでパースされる。

#### sema (`rwc/sema.py`)

3 ヶ所:

1. **組込み関数テーブル** に `len(string) -> int` を追加。現状 `print` の
   特例処理が `analyze_call` 周辺にあるので、同じ場所に `len` を追加。
   `print` は引数型が「printable な型なら何でも」だが、`len` は
   「引数 1 つ、型は string、戻り値は int」と厳密。
2. **`==` / `!=` の型チェック** で、両辺が string なら結果型 bool を許可。
   既存コードに `# We could allow string equality later; disallow for MVP simplicity.`
   というコメントとともに弾く分岐がある。これを「string も OK」に変更。
3. **`+` の型チェック** で、両辺が string なら結果型 string を許可。
   `int + int -> int` / `float + float -> float` の隣に同じパターンで
   `string + string -> string` を足す。

ネガティブテストを `tests/test_sema.py` に足す:
- `"a" + 1` → 型エラー
- `"a" == 1` → 型エラー
- `len(1)` → 型エラー (引数型違い)
- `len("a", "b")` → 型エラー (引数数違い)

#### irgen (`rwc/irgen.py`)

3 ヶ所:

1. **外部関数宣言** に `rw_str_len`, `rw_str_eq`, `rw_str_concat` を追加
   (既存の `rw_print_*` と同じ形)。
2. **`len(s)` 呼び出し** の生成。Sema で「組込み `len` の呼び出し」と
   印が付いた `Call` を `_emit_call` で受けたら、`rw_str_len` を呼び出す
   IR を出す。
3. **二項演算子の string ケース** の生成。`==` / `!=` は `rw_str_eq` を
   呼んで結果 i8 を `icmp` で i1 に変換 (`!=` の場合は `xor` で反転)。
   `+` は `rw_str_concat` を呼んで返り値 `rw_str` をそのまま使う。

### コンポーネント間の境界

- ランタイム側は **C ABI 関数 3 本** のみ追加。テストは C レベルでも可能。
- Sema/irgen の変更は **既存パターンの拡張のみ**。新しい AST ノードや
  IR 命令を導入しない。
- 公開 ABI (`rw_spawn_*` 等) は無変更。`librw.a` のシンボル増加のみ。

## ファイル別変更

### 変更

- `runtime/runtime.c` — 3 関数の実装を追加
- `runtime/runtime.h` — 3 プロトタイプを追加
- `rwc/sema.py` — `len` 組込み、`+`/`==`/`!=` の string 許可
- `rwc/irgen.py` — `rw_str_*` の外部宣言と呼び出し生成
- `tests/test_sema.py` — string オペレーションの型検査ケース追加
- `tests/test_e2e.py` — `string_ops` を parametrize に追加

### 新規

- `examples/string_ops.rw` — len / `==` / `+` を 1 つの main で使うサンプル
- `examples/string_ops.rw.expected` — 期待出力

### 変更なし

- lexer, parser, driver
- fiber 関連 (`runtime/fiber/*`)
- 既存の examples / spec docs

## 検証

```sh
# ランタイム単体
make -C runtime clean && make -C runtime

# 全テスト
uv run pytest -q

# 新しい example が単独で動く
uv run rwc run examples/string_ops.rw

# 既存 example が壊れていない
uv run rwc run examples/hello.rw
uv run rwc run examples/spawn_many.rw
```

成功基準: 全テスト緑、`string_ops.rw` の出力が `.expected` と一致、
新規 ネガティブ Sema テストが期待通り型エラーを返す。

## コミット構成

3 commits:

1. **runtime**: `rw_str_len` / `rw_str_eq` / `rw_str_concat` を追加。
   C レベルから直接呼んで動作確認。
2. **rwc**: Sema で string の `len` / `==` / `+` を許可し、irgen で
   ランタイム helper を呼ぶ IR を生成。Sema ネガティブテスト追加。
3. **examples + e2e**: `string_ops.rw` を追加、`tests/test_e2e.py` の
   parametrize に組み込む。

## リスクと対処

| リスク | 対処 |
|---|---|
| `rw_str` の `ptr` が NULL のケース (空文字列リテラル) | concat と eq は len=0 を最初に分岐して NULL deref を避ける |
| Sema で `+` の型推論が壊れる (int 計算が string 経路にハマる等) | 既存の `+` ハンドラを「両辺 int / 両辺 float / 両辺 string」の 3 分岐に明示。テストで `1 + 1` / `1.0 + 2.0` / `"a" + "b"` を網羅 |
| `len` を変数名としてユーザが使っているコード | rw リポジトリ内の `examples/*.rw` に `len` を変数として使っている箇所は無い (grep 確認済み)。Sema で「組込み名と衝突する変数定義」は別途エラーにしてもよいが今回はやらない (shadowing 許可、組込みは「呼び出される時に組込み」というだけ) |
| 連結リークが long-running echo server で問題化 | このサブプロジェクトの非ゴール。netpoller 統合より前に GC を入れる判断をするなら別 PR で対処 |
