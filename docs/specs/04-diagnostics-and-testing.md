# rw 診断とテスト戦略

## 診断フォーマット

すべての診断は **`file:line:col` + キャレット + メッセージ** の3点セット。

```
error: type mismatch
  --> examples/hello.rw:3:13
   |
 3 |     x: int = "hello"
   |              ^^^^^^^ expected `int`, found `string`
```

`diagnostics.py` は単一クラス `Diagnostic` を提供:

```python
@dataclass
class Diagnostic:
    file: str
    line: int      # 1-origin
    col: int       # 1-origin
    length: int
    message: str
    severity: Literal["error", "warning"]

    def render(self, source: str) -> str: ...
```

MVP は **最初のエラーで停止**。エラー回復(複数エラー収集)は将来。

## 診断を出す箇所

| ステージ | 例 |
|---|---|
| Lexer | unterminated string, inconsistent indentation |
| Parser | unexpected token, expected `:` after function signature |
| Sema | undefined variable, type mismatch, wrong argument count, await on non-Future |
| IRGen | 出さない(Sema で全部弾く) |
| Driver | clang not found, link error |

## テスト構成

`pytest` で全部回す。4階層:

### 1. 単体テスト

- `tests/test_lexer.py`: 入力文字列 → トークン列を検証。INDENT/DEDENT、空行、
  コメント行、インデント混在検出
- `tests/test_parser.py`: トークン列 → AST を `repr` 比較
- `tests/test_sema.py`: AST → 型付き AST、または期待される `Diagnostic`

### 2. IRGen スナップショットテスト

`tests/test_irgen.py` + `tests/snapshots/*.ll`:
- rw ソース → 生成 LLVM IR を文字列化
- 既存スナップショットと差分があれば失敗
- 意図変更は `pytest --update-snapshots` で更新

### 3. E2E テスト

`tests/test_e2e.py`:
- `examples/*.rw` を `rwc build` でコンパイル → 実行 → 標準出力を期待値と比較
- 期待値は `examples/*.rw.expected` に同名で配置
- CI は macOS arm64 + Linux x86_64 の両方で実行

### 4. 診断テスト

`tests/test_diagnostics.py` + `tests/bad/*.rw`:
- エラーになるべき rw コードを集める
- ファイル先頭コメントに期待エラーを書く:
  ```python
  # ERROR: type mismatch
  # ERROR_LINE: 3
  x: int = "hello"
  ```
- ランナーがコメントを抜き出し `rwc build` の出力と照合

## MVP の examples(受け入れテスト相当)

| ファイル | 検証内容 |
|---|---|
| `examples/hello.rw` | 文字列リテラルと `print` |
| `examples/arith.rw` | 整数・浮動小数・bool・比較・if/else |
| `examples/fib.rw` | 再帰関数 `fib(20)` |
| `examples/while_count.rw` | `while` ループで 1..10 |
| `examples/spawn_basic.rw` | `spawn add(3,4)` → `await` で 7 |
| `examples/spawn_many.rw` | 4 スレッド並列で合計計算 |
| `examples/spawn_string.rw` | `Future[string]` を返す関数 |

**これら 7 本が緑になれば MVP 完成。**

## CI

`.github/workflows/ci.yml`:

```yaml
strategy:
  matrix:
    os: [macos-latest, ubuntu-latest]
    python: ["3.11"]
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with: { python-version: ${{ matrix.python }} }
  - run: pip install -e ".[dev]"
  - run: make -C runtime
  - run: pytest -v
```

clang はどちらのランナーにも標準で入っているので追加インストール不要。
