# PR1: plain import + 修飾呼び出し 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development または superpowers:executing-plans でタスクごとに実装する。

**Goal:** `import math_lib` で同一ディレクトリの `math_lib.rw` を取り込み、`math_lib.add(2, 3)` と修飾して関数を呼べるようにする。名前空間付き import の全インフラ (DOT トークン・ローダ・名前空間付き関数テーブル・irgen シンボル規約・循環検出) を本 PR に集約する。`from`/`as` は後続 PR。

**Spec:** `docs/specs/17-namespaced-imports.md`

**Tech Stack:** Python (rwc: lexer/parser/sema/irgen via llvmlite), pytest, uv。runtime (C) は触らない。

---

## File Structure

- `rwc/lexer.py` (modify) — `TokenKind.DOT`、`one_char` に `".": DOT`
- `rwc/ast_nodes.py` (modify) — `Import` ノード、`Call.module=None`、`Module.imports`
- `rwc/parser.py` (modify) — `parse_module` の `KW_IMPORT` 分岐、`parse_import`、`parse_atom_postfix` の修飾呼び出し
- `rwc/loader.py` (new) — `load_program`: 同一ディレクトリ相対解決・推移 import・循環検出・import 先 main 禁止
- `rwc/sema.py` (modify) — `functions` キーの `(module,name)` 化、`analyze_program`、main は entry のみ、`call_resolution`、修飾呼び出し解決
- `rwc/irgen.py` (modify) — `rw_user_<module>_<name>` シンボル、全モジュール宣言・emit、`(module,name)` ルックアップ
- `rwc/driver.py` (modify) — `compile_source`/`emit_ir`/`emit_ast` の 3 経路を loader 経由に
- `examples/import_basic.rw`, `examples/import_basic_lib.rw`, `examples/import_basic.rw.expected` (new)
- `tests/test_lexer.py`, `tests/test_parser.py`, `tests/test_sema.py`, `tests/test_irgen.py`, `tests/test_e2e.py` (modify)

---

## Task 1: lexer — DOT トークン (commit 1)

- [ ] `TokenKind.DOT` を punctuation 群に追加
- [ ] `_read_operator` の `one_char` に `".": TokenKind.DOT`
- [ ] `tests/test_lexer.py`: `math_lib.add` → IDENT DOT IDENT、`1.5` → FLOAT、`3.14e2` → FLOAT を検証

検証: `uv run pytest tests/test_lexer.py -q`

## Task 2: AST + parser — import 文と修飾呼び出し (commit 2)

- [ ] `ast_nodes.py`: `Import{module, alias, names, line, col}` を追加 (alias/names は PR1 では常に None)
- [ ] `ast_nodes.py`: `Call.module: Optional[str] = None` を追加
- [ ] `ast_nodes.py`: `Module.imports: List[Import] = field(default_factory=list)` を追加
- [ ] `parser.py` `parse_module`: `KW_IMPORT` 分岐 → `parse_import()`。import は最初の `def`/`type` より前のみ (後に来たらエラー)
- [ ] `parser.py` `parse_import`: plain `import IDENT NEWLINE` のみ。直後が `KW_AS` や `from` 形なら「`as`/`from` import not yet supported」parser error
- [ ] `parser.py` `parse_atom_postfix` (呼び出しパース箇所): atom が `Name` で次トークンが `DOT` なら `DOT IDENT (args)` を読み `Call(callee=method, module=name)` を生成
- [ ] `tests/test_parser.py`: `import math_lib` が `Module.imports` に入る / 修飾呼び出しが `Call(module=...)` / import が def の後でエラー

検証: `uv run pytest tests/test_parser.py -q`

## Task 3: loader 新規 (commit 3)

- [ ] `rwc/loader.py`: `LoadedProgram{root, modules, root_name}` と `load_program(root_source, root_filename)`
- [ ] entry を tokenize/parse/desugar、`imports` をたどり `<dir>/<mod>.rw` を読む。無ければ「cannot find module」エラー
- [ ] import 先も tokenize/parse/desugar し再帰 (推移 import)。visited 集合で循環検出 → CompileError
- [ ] import 先に `main` 関数があれば「imported module must not define main」エラー
- [ ] ロード済み de-dup
- [ ] `tests/test_loader.py` (new): 正常ロード / ファイル無し / import 先 main / 循環 / 推移 / 重複 de-dup

検証: `uv run pytest tests/test_loader.py -q`

## Task 4: sema — 名前空間化 (commit 4)

- [ ] `SemaResult.functions` のキーを `Tuple[Optional[str], str]` に変更
- [ ] `analyze_program(loaded: LoadedProgram, filename)` を追加 (既存 `analyze` は単一 Module を `LoadedProgram` に包んで委譲)
- [ ] 全モジュールの関数を `(modname, name)` (entry は `(None, name)`) で収集。重複は同一キーのみ衝突
- [ ] main 必須・無引数・int 戻りは entry のみに適用 (`(None, "main")` 直引き)
- [ ] `SemaResult.call_resolution: Dict[int, Tuple[Optional[str], str]]` を導入
- [ ] `_check_call`: 冒頭で `if call.module is None:` のときだけ既存ビルトイン分岐。`module` 有り時は import 済み検証 → `(module, callee)` 解決し `call_resolution[id(call)]` に書く。修飾なしユーザ関数も `(None, callee)` で書く
- [ ] 未 import モジュール参照・未定義関数のエラー
- [ ] `tests/test_sema.py`: 名前空間テーブル / 未 import モジュール参照エラー / 別モジュール同名関数の共存

検証: `uv run pytest tests/test_sema.py -q`

## Task 5: irgen + driver (commit 5)

- [ ] `irgen.generate`: `self.funcs` を `(module,name)` キーに。シンボルを `rw_user_<module>_<name>` (`module=None` は `rw_user_<name>`)。全モジュールの関数を宣言・emit
- [ ] `_emit_call` / `_emit_spawn`: `call_resolution[id(call)]` 経由で `(module,name)` を引く
- [ ] C main シムは `funcs[(None,"main")]`
- [ ] `irgen.generate` (または `irgen_generate`) のシグネチャを `LoadedProgram` + sema に対応させる
- [ ] `driver.py`: `compile_source`/`emit_ir`/`emit_ast` の 3 経路を `load_program`→`analyze_program`→`irgen` に。`emit_ast` は entry Module を返す
- [ ] `tests/test_irgen.py`: `rw_user_import_basic_lib_add` シンボルが出る

検証: `uv run pytest tests/test_irgen.py -q`

## Task 6: examples + e2e (commit 6)

- [ ] `examples/import_basic_lib.rw`: `add`/`mul` を定義 (main 無し)
- [ ] `examples/import_basic.rw`: `import import_basic_lib` して `import_basic_lib.add(2,3)` 等を print
- [ ] `examples/import_basic.rw.expected`: 期待出力
- [ ] `tests/test_e2e.py`: parametrize に `import_basic` を追加 (lib は main 無しなので追加しない)

検証:
```sh
uv run rwc run examples/import_basic.rw
uv run pytest tests/test_e2e.py -k import_basic -q
uv run pytest -q   # 全体回帰
```

## ネガティブ手動確認

- 存在しないモジュール import → 分かりやすいエラー
- import 先に `def main()` → エラー
- 循環 import (a→b→a) → エラー
- import を def の後に書く → エラー
