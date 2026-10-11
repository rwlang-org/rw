# PR1: plain import + qualified calls — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement task by task with superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** `import math_lib` loads `math_lib.rw` from the same directory, and functions can be called qualified as `math_lib.add(2, 3)`. This PR carries all the infrastructure for namespaced imports (DOT token, loader, namespaced function table, irgen symbol convention, cycle detection). `from` / `as` come in follow-up PRs.

**Spec:** `docs/specs/17-namespaced-imports.md`

**Tech Stack:** Python (rwc: lexer/parser/sema/irgen via llvmlite), pytest, uv. The runtime (C) is not touched.

---

## File Structure

- `rwc/lexer.py` (modify) — `TokenKind.DOT`, `".": DOT` in `one_char`
- `rwc/ast_nodes.py` (modify) — `Import` node, `Call.module=None`, `Module.imports`
- `rwc/parser.py` (modify) — `KW_IMPORT` branch in `parse_module`, `parse_import`, qualified calls in `parse_atom_postfix`
- `rwc/loader.py` (new) — `load_program`: same-directory resolution, transitive imports, cycle detection, no `main` in imported modules
- `rwc/sema.py` (modify) — `(module, name)` keys for `functions`, `analyze_program`, main in the entry only, `call_resolution`, qualified call resolution
- `rwc/irgen.py` (modify) — `rw_user_<module>.<name>` symbols, declare/emit all modules, `(module, name)` lookups
- `rwc/driver.py` (modify) — route `compile_source` / `emit_ir` / `emit_ast` through the loader
- `examples/import_basic.rw`, `examples/import_basic_lib.rw`, `examples/import_basic.rw.expected` (new)
- `tests/test_lexer.py`, `tests/test_parser.py`, `tests/test_sema.py`, `tests/test_irgen.py`, `tests/test_e2e.py` (modify)

---

## Task 1: lexer — DOT token (commit 1)

- [ ] Add `TokenKind.DOT` to the punctuation group
- [ ] Add `".": TokenKind.DOT` to `one_char` in `_read_operator`
- [ ] `tests/test_lexer.py`: `math_lib.add` → IDENT DOT IDENT, `1.5` → FLOAT, `3.14e2` → FLOAT

Verify: `uv run pytest tests/test_lexer.py -q`

## Task 2: AST + parser — import statements and qualified calls (commit 2)

- [ ] `ast_nodes.py`: add `Import{module, alias, names, line, col}` (alias/names are always None in PR1)
- [ ] `ast_nodes.py`: add `Call.module: Optional[str] = None`
- [ ] `ast_nodes.py`: add `Module.imports: List[Import] = field(default_factory=list)`
- [ ] `parser.py` `parse_module`: `KW_IMPORT` branch → `parse_import()`. Imports only before the first `def` / `type` (error if after)
- [ ] `parser.py` `parse_import`: only plain `import IDENT NEWLINE`. If followed by `KW_AS` or in `from` form, raise the parser error "`as`/`from` import not yet supported"
- [ ] `parser.py` `parse_atom_postfix` (where calls are parsed): if the atom is a `Name` and the next token is `DOT`, read `DOT IDENT (args)` and build `Call(callee=method, module=name)`
- [ ] `tests/test_parser.py`: `import math_lib` lands in `Module.imports` / a qualified call is `Call(module=...)` / an import after a def is an error

Verify: `uv run pytest tests/test_parser.py -q`

## Task 3: new loader (commit 3)

- [ ] `rwc/loader.py`: `LoadedProgram{root, modules, root_name}` and `load_program(root_source, root_filename)`
- [ ] Tokenize/parse/desugar the entry, follow `imports`, read `<dir>/<mod>.rw`. Error "cannot find module" if missing
- [ ] Tokenize/parse/desugar imported modules too and recurse (transitive imports). Detect cycles with a visited set → CompileError
- [ ] Error "imported module must not define main" if an imported module has a `main` function
- [ ] De-dup already-loaded modules
- [ ] `tests/test_loader.py` (new): normal load / missing file / main in an import / cycle / transitive / de-dup

Verify: `uv run pytest tests/test_loader.py -q`

## Task 4: sema — namespacing (commit 4)

- [ ] Change the key of `SemaResult.functions` to `Tuple[Optional[str], str]`
- [ ] Add `analyze_program(loaded: LoadedProgram, filename)` (the existing `analyze` wraps a single Module in a `LoadedProgram` and delegates)
- [ ] Collect every module's functions as `(modname, name)` (the entry as `(None, name)`). Duplicates clash only on the same key
- [ ] Apply "main is required, takes no arguments, returns int" to the entry only (look up `(None, "main")`)
- [ ] Introduce `SemaResult.call_resolution: Dict[int, Tuple[Optional[str], str]]`
- [ ] `_check_call`: run the existing built-in branches only when `call.module is None`. With a module, verify it was imported → resolve `(module, callee)` and write `call_resolution[id(call)]`. Unqualified user calls also write `(None, callee)`
- [ ] Errors for references to not-imported modules and undefined functions
- [ ] `tests/test_sema.py`: namespaced table / not-imported module reference error / same-named functions in different modules coexist

Verify: `uv run pytest tests/test_sema.py -q`

## Task 5: irgen + driver (commit 5)

- [ ] `irgen.generate`: key `self.funcs` by `(module, name)`. Symbols are `rw_user_<module>.<name>` (`module=None` → `rw_user_<name>`). Declare and emit functions from all modules
- [ ] `_emit_call` / `_emit_spawn`: look up `(module, name)` via `call_resolution[id(call)]`
- [ ] The C main shim uses `funcs[(None, "main")]`
- [ ] Adapt the signature of `irgen.generate` (or `irgen_generate`) to `LoadedProgram` + sema
- [ ] `driver.py`: route `compile_source` / `emit_ir` / `emit_ast` through `load_program` → `analyze_program` → `irgen`. `emit_ast` returns the entry Module
- [ ] `tests/test_irgen.py`: the `rw_user_import_basic_lib.add` symbol is emitted

Verify: `uv run pytest tests/test_irgen.py -q`

## Task 6: examples + e2e (commit 6)

- [ ] `examples/import_basic_lib.rw`: define `add` / `mul` (no main)
- [ ] `examples/import_basic.rw`: `import import_basic_lib` and print `import_basic_lib.add(2, 3)` etc.
- [ ] `examples/import_basic.rw.expected`: expected output
- [ ] `tests/test_e2e.py`: add `import_basic` to the parametrize list (not the lib, since it has no main)

Verify:
```sh
uv run rwc run examples/import_basic.rw
uv run pytest tests/test_e2e.py -k import_basic -q
uv run pytest -q   # full regression
```

## Manual negative checks

- Importing a module that does not exist → a clear error
- `def main()` in an imported module → error
- Circular import (a→b→a) → error
- An import after a def → error
