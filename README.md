# rw

**Python の書き味で書ける、非同期ファーストの静的型コンパイル言語。**

LLVM をバックエンドに使い、macOS arm64 / Linux x86_64 のネイティブ実行
ファイルを生成します。

```python
def add(a: int, b: int) -> int:
    return a + b

def main() -> int:
    fu: Future[int] = spawn add(3, 4)
    r: int = await fu
    print(r)
    return 0
```

```
$ rwc run hello.rw
7
```

## 設計の柱

1. **非同期が中心** — `Future[T]` が型システムの一級市民。`spawn` / `await` は予約語。
2. **Python 似の見た目** — インデント、`def`、`elif`、`and`/`or`/`not`、`true`/`false`。
3. **静的型・型注釈必須** — 引数・戻り値・ローカル変数すべてに型注釈。
4. **薄い C ランタイム + LLVM** — `librw.a` がスレッド・Future・print を提供。
5. **学習・実験フレンドリー** — コンパイラ本体は Python。`rwc emit-ir` / `emit-ast` で内部を覗ける。

詳しい仕様は [`docs/specs/`](docs/specs/) を参照。

## Install

### 1. 前提となるツール

| ツール | 用途 | macOS | Linux (Ubuntu/Debian) |
|---|---|---|---|
| **uv** | Python 依存とランナーを管理 | `brew install uv` または公式 installer | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **clang** | LLVM IR → ネイティブのリンカ呼び出し | Xcode Command Line Tools(`xcode-select --install`)で標準搭載 | `sudo apt install clang` |
| **make** + **cc** | C ランタイム `librw.a` のビルド | Xcode Command Line Tools で標準搭載 | `sudo apt install build-essential` |
| Python 3.12 | コンパイラ本体の実行環境 | uv が自動で取得するので手動インストール不要 | 同左 |

Python は `.python-version` ファイルで 3.12 に固定しており、`uv sync` のタイミングで uv が自動的にダウンロード・展開します。ホストにシステム Python を別途用意する必要はありません。

### 2. リポジトリを取得

```sh
git clone https://github.com/ryuichi1208/rw.git
cd rw
```

### 3. セットアップ(2 ステップ)

```sh
# (a) Python 仮想環境を作って依存をインストール
uv sync --extra dev

# (b) C ランタイム librw.a をビルド
make -C runtime
```

ここまでで `.venv/`、`runtime/librw.a`、`uv.lock` が生成され、`rwc` コマンドが使えるようになります。

### 4. 動作確認

```sh
# Hello world
uv run rwc run examples/hello.rw
# => hello

# 非同期サンプル(spawn + await)
uv run rwc run examples/spawn_basic.rw
# => 7

# 全テストを回す(66 ケース、~3 秒)
uv run pytest
```

### 5. (任意)グローバルに `rwc` を使えるようにする

`uv run rwc ...` を毎回打つのが面倒なら、いずれかを選んでください。

```sh
# A. このシェルだけ
source .venv/bin/activate
rwc run examples/hello.rw   # 直接呼べる

# B. PATH に通す(永続)
ln -s "$PWD/.venv/bin/rwc" /usr/local/bin/rwc
```

### 6. (任意)Vim のシンタックスハイライト

`vim/` 配下にハイライト定義を同梱しています。詳しくは下の「エディタサポート」セクション参照。

## CLI

```
rwc build  foo.rw [-o foo]   # ネイティブ実行ファイルを生成
rwc run    foo.rw            # コンパイルして即実行
rwc emit-ir  foo.rw          # 生成された LLVM IR を表示
rwc emit-ast foo.rw          # パース後の AST を表示
```

## サンプル

[`examples/`](examples/) に MVP の受け入れテストを兼ねた 7 本のサンプルがあります。

| ファイル | 内容 |
|---|---|
| `hello.rw` | 文字列リテラルと `print` |
| `arith.rw` | 整数・浮動小数・bool・比較・if/else |
| `fib.rw` | 再帰関数 `fib(20)` |
| `while_count.rw` | `while` ループで 1..5 |
| `spawn_basic.rw` | `spawn add(3,4)` → `await` で 7 |
| `spawn_many.rw` | 4 スレッド並列で計算し合計 |
| `spawn_string.rw` | `Future[string]` を返す関数 |

すべて `uv run rwc run examples/<name>.rw` で実行できます。

## LLVM IR を覗いてみる

```sh
$ uv run rwc emit-ir examples/spawn_basic.rw | head -30
```

クロージャ構造体・トランポリン関数・`rw_spawn_i64` 呼び出しが見えます。

## 開発

```sh
uv sync --extra dev          # 依存をインストール
make -C runtime              # librw.a をビルド
uv run pytest -v             # 全テスト
uv run pytest tests/test_e2e.py  # E2E のみ
```

新しい構文を試したいときは、`rwc/lexer.py` → `rwc/parser.py` → `rwc/sema.py` → `rwc/irgen.py` の順に手を入れます。各ファイルは 500 行以下に保たれています。

## MVP の範囲とこれから

**MVP に入っているもの**:
- 型: `int`, `float`, `bool`, `string`, `Future[T]`
- 関数定義、`if`/`elif`/`else`、`while`、ローカル変数
- 算術・比較・論理演算子(短絡評価)
- `print` 組み込み
- `spawn` / `await` による pthread ベースの並行処理
- macOS arm64 + Linux x86_64

**今後の拡張(MVPの外、優先度順)**:
1. `list[T]` と `for x in xs`
2. `extern "c"` (Cライブラリ・プロセス起動)
3. プロセス起動・パイプ用ランタイム関数(STT/TTS の Python ワーカー連携の布石)
4. `class` / `import` / モジュールシステム
5. 型推論
6. GC または所有権モデル
7. `Result[T, E]` とエラー処理

## エディタサポート

Vim 用のシンタックスハイライトを `vim/` 配下に同梱しています。

```sh
# vim-plug / packer などを使っていなければ、シンボリックリンクが一番手軽。
mkdir -p ~/.vim/syntax ~/.vim/ftdetect
ln -s "$PWD/vim/syntax/rw.vim"   ~/.vim/syntax/rw.vim
ln -s "$PWD/vim/ftdetect/rw.vim" ~/.vim/ftdetect/rw.vim
```

または `~/.vimrc` に直接書き足しても OK:

```vim
set runtimepath+=/path/to/rw/vim
```

ハイライト対象:`def` / `return` / `if` / `while` などのキーワード、
`spawn` / `await`(`Statement` グループで他のキーワードと色分け)、
`int` / `Future` などの型、`true` / `false`、整数 / 浮動小数 / 文字列
(エスケープ `\n` などは `SpecialChar`)、コメント中の `TODO` マーカー、
将来予約語(`extern` など、MVP では `Error` 色で目立つ)。
