# rw ファイル I/O と fd 汎用 `read` / `write` / `close`

## Context

rw はコンパイラ・ランタイム・サンプル・テストが一体で育つ小さな言語で、TCP
ソケット ([[12-netpoller-tcp]]) を `tcp_listen` / `tcp_accept` / `tcp_read` /
`tcp_write` / `tcp_close` という組み込みで提供してきた。一方、issue #33
(stdlib: ファイル I/O) のとおり、ファイルを開いて読み書きする手段がまだない。

ここで TCP の組み込みを見直すと、`tcp_read` / `tcp_write` / `tcp_close` は実は
**ソケット専用ではない操作**である。Unix では `read(2)` / `write(2)` /
`close(2)` はファイル・ソケット・パイプ・標準入出力など任意の fd に使える汎用
システムコールであり、ソケット専用なのは `tcp_listen` / `tcp_accept`
(と接続の確立) だけだ。

そこで本サブプロジェクトは、**`tcp_read` / `tcp_write` / `tcp_close` を廃止し、
fd 汎用の `read` / `write` / `close` に一本化**したうえで、ファイルを開く
`file_open` を追加する。これにより書く側は「開く操作はソース別、読み書き閉じる
操作は共通」という Unix の意味論で、TCP もファイルも同じ `read` / `write` /
`close` で書けるようになる。

```
開く（ソース別）              読み書き閉じる（共通）
─────────────────            ──────────────────────
tcp_listen(port) -> fd
tcp_accept(lfd)  -> fd  ──┐
file_open(path, mode) -> fd ─┤──→  read(fd, n) -> Bytes
                          │        write(fd, b) -> int
                          └──→     close(fd) -> int
```

## Goals

- fd 汎用の組み込みを導入する:
  - `read(fd: int, max: int) -> Bytes` — fd から最大 max バイト読む
  - `write(fd: int, b: Bytes) -> int` — fd へ書く。書けたバイト数を返す
  - `close(fd: int) -> int` — fd を閉じる。0 成功 / 負失敗
- ファイルを開く組み込みを追加する:
  - `file_open(path: string, mode: string) -> int` — `"r"` / `"w"` / `"a"`
    を `open(2)` フラグに変換。失敗時は負の fd
- runtime 内部で `read(2)` / `write(2)` を使い、`EAGAIN` のとき fiber 上なら
  netpoller に park する。**ソケット fd (ノンブロッキング) は park され、
  ファイル fd (EAGAIN を出さない) はそのまま同期 read になる** — 種別判定の
  分岐コードを書かずに両対応する (本設計の核心)
- `tcp_listen` / `tcp_accept` は据え置き (ソケット固有のため)
- 既存の TCP サンプル・テスト・spec を `read` / `write` / `close` に書き換える
  (破壊的・完全統一)

## Non-Goals

- ディレクトリ操作・path 操作 (`mkdir` / `readdir` / path join 等) — #43 で別途
- `seek` / `tell` / `truncate` / `stat` などのファイル位置・メタ操作
- バッファリング (毎回 syscall を発行する)
- テキスト / バイナリの区別 — `read` は常に `Bytes` を返し、文字列化は既存の
  `str_from_bytes` に委ねる
- ファイルパーミッションの指定 — `open` で作成するファイルは固定 `0644`
- `tcp_listen` / `tcp_accept` のリネーム (ソケット固有のまま残す)

## 組み込み関数 (言語側)

| 関数 | シグネチャ | 説明 |
|---|---|---|
| `file_open` | `(path: string, mode: string) -> int` | `"r"`→`O_RDONLY`, `"w"`→`O_WRONLY\|O_CREAT\|O_TRUNC`, `"a"`→`O_WRONLY\|O_CREAT\|O_APPEND`。不正 mode / open 失敗は負の fd |
| `read` | `(fd: int, max: int) -> Bytes` | 最大 max バイト読む。EOF・エラーは len=0 の Bytes |
| `write` | `(fd: int, b: Bytes) -> int` | 書けたバイト数。エラーは負 |
| `close` | `(fd: int) -> int` | 0 成功 / 負失敗 |

`read` / `write` の Bytes ABI は旧 `tcp_read` / `tcp_write` と同一
(`{i64 len, i8* ptr}` の sret / 値渡し) なので、irgen の emit ロジックを流用
できる。失敗時に負値を返す慣習は既存の `tcp_*` に揃える。

## runtime (C) の設計

`runtime/runtime.c` (または新規 `runtime/io.c`) に fd 汎用ヘルパを置く:

- `rw_read(rw_str *out, int64_t fd, int64_t max)` — `recv` ではなく
  **`read(2)`**。戻り値で分岐:
  - `n > 0`: `out` に len=n / ptr=buf
  - `n == 0`: EOF。len=0 / ptr=NULL
  - `n < 0` かつ `errno == EAGAIN/EWOULDBLOCK`: fiber 上なら
    `rw_net_park_read(fd)` して継続、そうでなければ len=0 で返す
  - その他のエラー: len=0 で返す
- `rw_write(int64_t fd, rw_str b)` — `send` ではなく **`write(2)`**。
  `EAGAIN` のとき fiber 上なら `rw_net_park_write(fd)` して継続。書けた
  バイト数を返す
- `rw_close(int64_t fd)` — `close(2)`
- `rw_file_open(rw_str path, rw_str mode)` — path を NUL 終端にコピーし、
  mode 文字列を `O_*` フラグに変換して `open(path, flags, 0644)`。負の fd で
  失敗を表す

### なぜ分岐なしで両対応できるか

ソケット fd は `tcp_accept` 等で **ノンブロッキング**に設定される (既存の
netpoller 連携のため)。ノンブロッキングソケットは読めるデータがないと
`EAGAIN` を返すので、上記ロジックは netpoller に park して fiber を退避する。
一方、**正規ファイルの fd は `read(2)` で `EAGAIN` を返さず**、データが
揃うまでカーネルがブロックして完了する (ディスク I/O はノンブロッキングに
ならない)。したがって同じ `rw_read` のコードが、ソケットでは park、ファイル
では同期 read として正しく振る舞う。fd 種別を `fstat` で判定する分岐は不要。

既存 `tcp.c` の `rw_tcp_read` / `rw_tcp_write` / `rw_tcp_close` は削除し、
中身を `rw_read` / `rw_write` / `rw_close` に統合・委譲する。

## 触るレイヤー

| レイヤー | ファイル | 変更 |
|---|---|---|
| Lexer | `rwc/lexer.py` | **無改修** (すべて通常の関数呼び出し) |
| Parser | `rwc/parser.py` | **無改修** |
| AST | `rwc/ast_nodes.py` | **無改修** (`Call` で表現) |
| Sema | `rwc/sema.py` | `file_open` / `read` / `write` / `close` を組み込みに追加。`tcp_read` / `tcp_write` / `tcp_close` の分岐を削除 (spawn 拒否リストと `_check_call` の 2 箇所)。`tcp_listen` / `tcp_accept` は据え置き |
| irgen | `rwc/irgen.py` | `rw_read` / `rw_write` / `rw_close` / `rw_file_open` を declare。`_emit_call` の `tcp_read` 等を `read` 等に置換 (Bytes ABI は流用) |
| Runtime | `runtime/runtime.c` ほか, `runtime/net/tcp.c` | `rw_read` / `rw_write` / `rw_close` / `rw_file_open` を実装。`rw_tcp_read` / `rw_tcp_write` / `rw_tcp_close` を削除・委譲 |
| Examples | `examples/file_io.rw` (+ `.expected`) 新規。`tcp_echo.rw` / `tcp_chat.rw` を書き換え | round-trip サンプル + 既存 TCP を `read`/`write`/`close` に |
| Tests | `tests/test_e2e.py` / `test_e2e_tcp.py` / `test_sema.py` / `test_irgen.py` | `file_io` を parametrize に追加、TCP テストを書き換え、sema/irgen の unit test 追加 |

`incremental-language-extensions` の「1 PR 4 層まで」に対し、本 PR は sema /
irgen / runtime + 例題が中心。lexer / parser / AST は無改修なので層数は収まる。
ただし TCP の破壊的書き換えを含むため commit を分けて影響範囲を明示する。

## 検証

```sh
make -C runtime
uv run pytest -v                       # 全緑 (書き換え後の TCP テスト含む)
uv run rwc run examples/file_io.rw     # round-trip 出力が .expected と一致
uv run rwc run examples/tcp_echo.rw    # 書き換え後も TCP echo が動く
```

- e2e (round-trip): `file_open(path, "w")` → `write` → `close` →
  `file_open(path, "r")` → `read` → `print` の自己完結サンプルで書き / 読み
  両パスを踏む
- unit (sema): `file_open` の引数型・個数、`read`/`write`/`close` の型
  (`read` が Bytes を返す、`write` の第 2 引数が Bytes 等)、不正 mode は
  実行時 (負 fd) なので sema ではなく e2e/手動で確認
- unit (irgen): 生成 IR に `rw_read` / `rw_write` / `rw_file_open` の呼び出し
  が出る
- TCP リグレッション: `test_e2e_tcp.py` を `read`/`write`/`close` で通す

## リスクと対処

- **TCP の破壊的変更**: `tcp_read`/`tcp_write`/`tcp_close` を使う既存サンプル・
  テスト・spec をすべて洗い出して書き換える (grep で網羅)。commit を「runtime
  統合」「sema/irgen 切替」「サンプル/テスト書き換え」「file_open 追加」に分け、
  TCP リグレッションを e2e で担保する
- **ファイル fd を netpoller に park してしまう懸念**: 正規ファイルは `EAGAIN`
  を返さないため park 経路に入らない。仮に特殊 fd が EAGAIN を返しても fiber
  外なら同期フォールバックする
- **path の NUL 終端**: rw の string は `{len, ptr}` で NUL 終端保証がないため、
  `rw_file_open` で path をコピーして NUL 終端を付ける
- **mode 文字列の不正値**: `"r"`/`"w"`/`"a"` 以外は負の fd を返す (trap しない)。
  呼び出し側が fd < 0 を判定する
- **「ついでに」誘惑**: seek / dir 操作 / バッファリングには手を出さない
  (Non-Goals)。`tcp_listen`/`tcp_accept` のリネームもしない
