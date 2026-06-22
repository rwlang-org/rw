# rw 名前空間付き import (Python 風)

## Context

ここまでの rw は **単一 `.rw` ファイル** しかコンパイルできない。`lexer.py` に
`KW_IMPORT` が予約語として存在するだけで、parser 以降は完全に未処理だった。
コードが育つにつれ「共通関数を別ファイルに切り出して使い回したい」= パッケージ
管理の最初の一歩が必要になった。

目指すのは **Python と同じ書き味** の import:

```rw
import math_lib              # math_lib.add(...)  と修飾して呼ぶ
from math_lib import add     # add(...)           と修飾なしで呼ぶ
import math_lib as m         # m.add(...)         と別名で呼ぶ
```

検索パスは **同一ディレクトリ相対のみ** (`import foo` → `<entry_dir>/foo.rw`)。

これは `incremental-language-extensions` の鉄則「1 PR で触るコアレイヤーは 4 つ
まで」を超える。3 形式すべてを名前空間付きで入れると lexer (`.` トークン) /
parser (修飾呼び出し) / sema (名前空間テーブル) / irgen (シンボル規約) / 新規
loader が同時に絡むためである。よって spec は本ファイル 1 本にまとめ、
**独立 mergeable な 3 PR** に分割する。

- **PR1 (最重量・全インフラ)**: `import math_lib` + 修飾呼び出し `math_lib.add()`
- **PR2**: `from math_lib import add` (選択取り込み・修飾なし呼び出し)
- **PR3**: `import math_lib as m` (別名)

PR2 / PR3 は PR1 のローダと名前空間テーブルに乗るだけの薄い差分。

## Goals

- `import foo` でモジュール `foo.rw` (同一ディレクトリ) を取り込み、`foo.bar()`
  と修飾して関数を呼べる (PR1)
- `from foo import bar [as baz]` で選択シンボルを取り込み、修飾なしで `bar()` /
  `baz()` と呼べる (PR2)
- `import foo as f` で別名を付け `f.bar()` と呼べる (PR3)
- import 文はモジュール冒頭 (最初の `def`/`type` より前) でのみ許可
- 推移的 import を許す (import 先がさらに import)。循環は **検出してエラー**
- import 先のモジュールに `main` を定義したらエラー (ライブラリに main は不要)
- 別モジュールの同名関数が共存できる (フラット名前空間衝突を起こさない)
- 既存の単一ファイルプログラム・既存テストはすべて無改変で緑

## Non-Goals

- パッケージ階層 (`import pkg.sub` のドット区切りモジュールパス)
- 再エクスポート / ワイルドカード (`from foo import *`)
- 可視性 / private (全 top-level 関数が import 可能)
- 標準ライブラリ / `RW_PATH` 等の検索パス (同一ディレクトリ相対のみ)
- 型エイリアスの import (PR1-3 は **関数のみ**。`type` は import 対象外)
- import 文をブロック内 (関数本体) に書く
- 循環 import の解決 (検出してエラーにするのみ)
- import 先で発生したエラーの **正確な per-file 行番号** (統合後 sema は entry の
  filename で診断する。既知の制限として記載)

## 設計

### 全体方針: ロード → マージせず統合 sema

ローダが entry から import グラフをたどり、各モジュールを個別に
tokenize/parse/desugar する。AST は **マージしない** (関数名の衝突を避け、
モジュール境界を保つため)。代わりに sema が「モジュール名付き関数テーブル」を
構築し、修飾呼び出しを `(module, name)` で解決する。

### 修飾呼び出しの表現: `Call.module: Optional[str]`

`ast_nodes.Call` に `module: Optional[str] = None` を追加する。

- `add(2, 3)` → `Call(callee="add", module=None)` (従来どおり)
- `math_lib.add(2, 3)` → `Call(callee="add", module="math_lib")`

デフォルト `None` なので既存の `A.Call(...)` 生成箇所は無改変。sema / irgen の
巨大なビルトイン判定 (`call.callee == "print"` 等) は **`call.module is None` の
ときだけ** 走らせる。member-access ノード化案は却下 (callee を式にすると数十の
分岐が全壊するため)。rw に first-class 関数はなく `mod.func` は呼び出し位置に
しか現れないので、これで足りる。

### import 文の AST: `Import`

```python
@dataclass
class Import:
    module: str                                       # "math_lib"
    alias: Optional[str]                              # import x as m の m (PR3)
    names: Optional[List[Tuple[str, Optional[str]]]]  # from x import y[,z as w] (PR2)
    line: int
    col: int
```

- `import x` → `Import("x", None, None)`
- `import x as m` → `Import("x", "m", None)`
- `from x import y` → `Import("x", None, [("y", None)])`
- `from x import y as w` → `Import("x", None, [("y", "w")])`

`Module` に `imports: List[Import] = field(default_factory=list)` を追加
(デフォルト空なので既存生成は無改変)。

### 関数テーブルの名前空間化: キーを `(Optional[str], str)`

`sema.SemaResult.functions` のキーを `str` から `Tuple[Optional[str], str]` に
変える。ローカル関数は `(None, name)`、import 先は `(modname, name)`。

- 重複検出は同一キーでのみ衝突 → 別モジュールの同名関数は自然に共存
- `main` 直引きは `(None, "main")`。main 必須・無引数・int 戻りチェックは
  **entry モジュールのみ** に適用
- irgen の LLVM シンボルは `rw_user_<module>_<name>` (`module=None` は従来
  `rw_user_<name>`) → シンボル衝突回避。`main` は常に `(None,"main")` で
  `rw_user_main` のまま、C `@main` シム無改変

### 解決マップ: `SemaResult.call_resolution: Dict[int, Tuple[Optional[str], str]]`

`id(Call)` → 解決後の実体 `(module, name)`。`from`/`as` で呼び名が変わっても
Call を変異させず irgen に実体を伝える。PR1 では修飾呼び出し/ローカル呼び出しを
恒等的に埋める。PR2/PR3 がここに別名解決を流し込む。desugar は Call を再生成
しない (`desugar.py` に Call 生成なし) ので `id` は sema 入力と irgen 入力で一致。
既存の `expr_types`/`local_types` も同じ id 方式で実績あり。

### ローダ: `rwc/loader.py` (新規)

```python
@dataclass
class LoadedProgram:
    root: A.Module                 # entry モジュール
    modules: Dict[str, A.Module]   # import されたモジュール群 (name -> Module)
    root_name: str

def load_program(root_source: str, root_filename: str) -> LoadedProgram: ...
```

- entry を tokenize/parse/desugar
- `imports` をたどり `Path(root_filename).parent / f"{name}.rw"` を読む。無ければ
  「cannot find module 'foo' (looked for foo.rw)」エラー
- import 先も tokenize/parse/desugar し、その `imports` を再帰的にたどる
  (推移的 import)。visited 集合で **循環 import を検出 → CompileError**
- import 先に `name == "main"` の関数があれば
  「imported module must not define main」エラー
- ロード済みモジュールは de-dup

### driver 配線

`driver.py` の `compile_source` / `emit_ir` / `emit_ast` の **3 経路** を
`parse(tokenize(...))` から `load_program(source, filename)` 経由に差し替え、
sema を `analyze_program(loaded, filename)` 形に拡張して呼ぶ。`emit_ast` は
entry の `Module` (imports を含む) を返す。

## コンポーネント別の変更 (PR ごと)

### PR1: plain import + 修飾呼び出し

触るコアレイヤー: lexer / parser / sema / irgen (4 つ) + 新規 loader + driver 配線。
runtime は触らない。

- `rwc/lexer.py`: `TokenKind.DOT`、`one_char` に `".": DOT` (float と非衝突)
- `rwc/ast_nodes.py`: `Import`、`Call.module=None`、`Module.imports`
- `rwc/parser.py`: `parse_module` に `KW_IMPORT` 分岐 + `parse_import` (PR1 は plain
  `import IDENT` のみ、`as`/`from` は parser error)。「import は def/type より前」
  ガード。`parse_atom_postfix` で `Name` の次が `DOT` なら修飾呼び出し
- `rwc/sema.py`: `functions` キーの `(module,name)` 化、`analyze_program`、
  main は entry のみ、`call_resolution`、修飾呼び出し解決 (未 import / 未定義エラー)
- `rwc/irgen.py`: `rw_user_<module>_<name>` シンボル、全モジュール宣言・emit、
  `(module,name)` ルックアップ
- `rwc/driver.py`: 3 経路を loader 経由に
- `examples/import_basic.rw` + `import_basic_lib.rw` + `.expected`
- tests: `test_lexer`/`test_parser`/`test_sema`/`test_irgen` + `test_e2e` parametrize

### PR2: `from x import y [as w]`

触るレイヤー: lexer (`KW_FROM`) / parser / sema (irgen 無改変)。

- `rwc/lexer.py`: `KEYWORDS` に `"from"` / `KW_FROM`
- `rwc/parser.py`: `parse_import` を `from IDENT import IDENT [as IDENT] {, ...}` に
- `rwc/sema.py`: モジュールごとの `from_env: Dict[localname, (real_mod, real_name)]`。
  衝突検出 (ローカル関数名・ビルトイン・他 from)。修飾なし呼び出しを
  ローカル→ビルトイン→`from_env` 順に解決し `call_resolution` へ
- `examples/import_from.rw` + lib + `.expected`、tests

### PR3: `import x as m`

触るレイヤー: parser / sema (lexer `KW_AS` 既存、irgen 無改変)。

- `rwc/parser.py`: plain import に `[as IDENT]` を許し `Import.alias`
- `rwc/sema.py`: `import_env: Dict[visible_name, real_mod]` (plain `x→x`、as `m→x`)。
  `Call(module=m)` 解決時に実モジュールへ変換。エイリアス二重定義・元名衝突を検出
- `examples/import_as.rw` + lib + `.expected`、tests

## 検証

```sh
# 単体
uv run pytest tests/test_lexer.py tests/test_parser.py tests/test_sema.py tests/test_irgen.py
# DOT が float と非衝突 / Call.module・Import パース / 名前空間テーブル /
# 未 import 参照・循環・import 先 main・衝突の各エラー / rw_user_<mod>_<fn> シンボル

# emit-ast / emit-ir
uv run python -m rwc.cli emit-ast examples/import_basic.rw   # imports と Call(module=...)
uv run python -m rwc.cli emit-ir  examples/import_basic.rw   # rw_user_import_basic_lib_add

# e2e
uv run pytest tests/test_e2e.py -k import_
uv run rwc run examples/import_basic.rw

# 既存回帰
uv run pytest -q
```

## リスクと対処

| リスク | 対処 |
|---|---|
| `.` トークン追加が float リテラルと衝突 | lexer の `_read_number` は `.` の前後に数字を要求するため `1.5` は丸ごと FLOAT。`math_lib.add` は IDENT DOT IDENT に割れる。test_lexer で両方を検証 |
| 既存ビルトイン分岐 (`callee == "print"`) を壊す | `Call.module` のデフォルトを `None` にし、ビルトイン分岐の冒頭に `if call.module is None:` ガードを足すだけ。修飾呼び出しはビルトイン判定に入らない |
| import 先のエラー行番号が entry 基準で出る | per-file 正確診断は名前空間 PR では行わず、Non-Goals / 既知の制限に明記 |
| 別モジュールの同名関数で LLVM シンボル衝突 | irgen シンボルを `rw_user_<module>_<name>` に。`main` だけ `(None,"main")` で従来名を維持し C シム無改変 |
| 解決マップの `id(Call)` 寿命 | desugar は Call を再生成しない (既存 expr_types と同方式)。sema 入力と irgen 入力で id 一致 |
| `spawn m.add()` (修飾呼び出しの spawn) | `_emit_spawn` / トランポリンも `(module,name)` 化すれば動く。PR1 ではテスト範囲を絞るため spawn+修飾は parser/sema で弾き Non-Goal に倒してもよい |
