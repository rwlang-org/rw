# rw namespaced imports (Python style)

## Context

Until now rw could only compile a **single `.rw` file**. `lexer.py` had
`KW_IMPORT` as a reserved word, but nothing past the lexer handled it. As code
grows we want to "split shared functions into another file and reuse them" —
the first step toward package management.

The goal is imports that **feel like Python**:

```rw
import math_lib              # call qualified:   math_lib.add(...)
from math_lib import add     # call unqualified: add(...)
import math_lib as m         # call via alias:   m.add(...)
```

The search path is **relative to the entry file's directory only**
(`import foo` → `<entry_dir>/foo.rw`).

This exceeds the `incremental-language-extensions` rule of "touch at most 4
core layers per PR": shipping all three forms with namespacing involves the
lexer (`.` token), parser (qualified calls), sema (namespaced table), irgen
(symbol convention), and a new loader at once. So the spec lives in this one
file, and the work is split into **3 independently mergeable PRs**.

- **PR1 (heaviest, all infrastructure)**: `import math_lib` + qualified call `math_lib.add()`
- **PR2**: `from math_lib import add` (selective import, unqualified call)
- **PR3**: `import math_lib as m` (alias)

PR2 / PR3 are thin diffs on top of PR1's loader and namespaced table.

## Goals

- `import foo` loads module `foo.rw` (same directory) and functions can be
  called qualified as `foo.bar()` (PR1)
- `from foo import bar [as baz]` imports selected symbols, callable unqualified
  as `bar()` / `baz()` (PR2)
- `import foo as f` gives an alias, callable as `f.bar()` (PR3)
- Import statements are only allowed at the top of a module (before the first
  `def` / `type`)
- Transitive imports are allowed (an imported module may import). Cycles are
  **detected and reported as errors**
- Defining `main` in an imported module is an error (libraries don't need main)
- Same-named functions in different modules coexist (no flat-namespace clashes),
  both in sema and in the emitted LLVM symbols
- Diagnostics raised in an imported module name that module's file and line
- All existing single-file programs and tests stay green without changes

## Non-Goals

- Package hierarchies (dotted module paths like `import pkg.sub`)
- Re-exports / wildcards (`from foo import *`)
- Visibility / private (every top-level function is importable)
- A standard library / search paths such as `RW_PATH` (same directory only)
- Importing type aliases (PR1-3 import **functions only**; aliases are
  module-local and never visible from another module)
- Import statements inside blocks (function bodies)
- Resolving import cycles (they are only detected and reported)

## Design

### Overall approach: load, don't merge, then a unified sema

The loader walks the import graph from the entry file and
tokenizes/parses/desugars each module separately. ASTs are **not merged** (to
avoid function-name clashes and keep module boundaries). Instead, sema builds a
"module-qualified function table" and resolves qualified calls by
`(module, name)`.

### Representing qualified calls: `Call.module: Optional[str]`

Add `module: Optional[str] = None` to `ast_nodes.Call`.

- `add(2, 3)` → `Call(callee="add", module=None)` (unchanged)
- `math_lib.add(2, 3)` → `Call(callee="add", module="math_lib")`

Because the default is `None`, existing `A.Call(...)` construction sites need no
changes. The large built-in checks in sema / irgen (`call.callee == "print"`,
etc.) run **only when `call.module is None`**. Turning this into a
member-access node was rejected (making the callee an expression would break
dozens of branches). rw has no first-class functions, and `mod.func` only ever
appears in call position, so this is sufficient.

### Import statement AST: `Import`

```python
@dataclass
class Import:
    module: str                                       # "math_lib"
    alias: Optional[str]                              # the m in `import x as m` (PR3)
    names: Optional[List[Tuple[str, Optional[str]]]]  # from x import y[, z as w] (PR2)
    line: int
    col: int
```

- `import x` → `Import("x", None, None)`
- `import x as m` → `Import("x", "m", None)`
- `from x import y` → `Import("x", None, [("y", None)])`
- `from x import y as w` → `Import("x", None, [("y", "w")])`

Add `imports: List[Import] = field(default_factory=list)` to `Module` (empty by
default, so existing construction is unchanged).

### Namespacing the function table: key is `(Optional[str], str)`

Change the key of `sema.SemaResult.functions` from `str` to
`Tuple[Optional[str], str]`. Local functions are `(None, name)`; imported ones
are `(modname, name)`.

- Duplicate detection only clashes on the same key → same-named functions in
  different modules coexist naturally
- `main` is looked up as `(None, "main")`. The "main is required, takes no
  arguments, returns int" checks apply **to the entry module only**
- irgen's LLVM symbol is `rw_user_<module>.<name>` (`module=None` keeps the
  historical `rw_user_<name>`). `.` cannot appear in an rw identifier, so the
  qualified form never collides with an entry function (`lib_add` vs
  `lib.add`) or another module (`a_b.c` vs `a.b_c`). Spawn trampolines follow
  the same rule (`rw_trampoline_<module>.<name>`). `main` is always
  `(None, "main")` and stays `rw_user_main`, so the C `@main` shim is unchanged

### Module-local scopes in sema

Sema checks one module at a time and switches three pieces of state together
when it enters a module: the current module name (for resolving calls), that
module's type-alias map, and that module's file name (used for every
diagnostic). Consequently:

- Type aliases are module-local: two modules may each declare `type Num`, and
  a module cannot use an alias it did not declare
- An error in an imported module's function body is reported against that
  module's file, and the CLI renders the snippet from that file

### Resolution map: `SemaResult.call_resolution: Dict[int, Tuple[Optional[str], str]]`

Maps `id(Call)` → the resolved target `(module, name)`. Even when `from` / `as`
change the name used at the call site, the Call is not mutated; the real
target is passed to irgen through this map. In PR1 qualified and local calls
map to themselves. PR2/PR3 feed alias resolution into it. desugar never
recreates Call nodes (there is no Call construction in `desugar.py`), so the
`id` matches between sema's input and irgen's input. The existing
`expr_types` / `local_types` use the same id-based approach.

### Loader: `rwc/loader.py` (new)

```python
@dataclass
class LoadedProgram:
    root: A.Module                 # the entry module
    root_name: str
    modules: Dict[str, A.Module]   # imported modules (name -> Module)
    sources: Dict[str, str]        # source text per module (root included)
    paths: Dict[str, str]          # file path per module (root included)

def load_program(root_source: str, root_filename: str) -> LoadedProgram: ...
```

- Tokenize/parse/desugar the entry file
- Follow `imports` and read `Path(root_filename).parent / f"{name}.rw"`. If it
  is missing, error with "cannot find module 'foo' (looked for foo.rw)"
- Tokenize/parse/desugar each imported module too, and follow its `imports`
  recursively (transitive imports). A visited set **detects import cycles →
  CompileError**
- If an imported module has a function named `main`, error with "imported
  module must not define main", pointing at that `main` in the imported file
- Already-loaded modules are de-duplicated

### Driver wiring

Switch the **3 paths** in `driver.py` — `compile_source` / `emit_ir` /
`emit_ast` — from `parse(tokenize(...))` to `load_program(source, filename)`,
and call sema as `analyze_program(loaded, filename)`. `emit_ast` returns the
entry `Module` (including its imports).

## Changes per component (per PR)

### PR1: plain import + qualified calls

Core layers touched: lexer / parser / sema / irgen (4) + new loader + driver
wiring. The runtime is not touched.

- `rwc/lexer.py`: `TokenKind.DOT`, `".": DOT` in `one_char` (does not clash with floats)
- `rwc/ast_nodes.py`: `Import`, `Call.module=None`, `Module.imports`
- `rwc/parser.py`: a `KW_IMPORT` branch in `parse_module` + `parse_import` (PR1
  accepts only plain `import IDENT`; `as` / `from` are parser errors). A guard
  for "imports come before def/type". In `parse_atom_postfix`, a `Name`
  followed by `DOT` is a qualified call
- `rwc/sema.py`: `(module, name)` keys for `functions`, `analyze_program`, main
  in the entry only, `call_resolution`, qualified call resolution (errors for
  not-imported / undefined), module-local aliases and per-file diagnostics
- `rwc/irgen.py`: `rw_user_<module>.<name>` symbols, declare/emit all modules,
  `(module, name)` lookups
- `rwc/driver.py`: route the 3 paths through the loader
- `rwc/cli.py`: render a diagnostic's snippet from the file it names
- `examples/import_basic.rw` + `import_basic_lib.rw` + `.expected`
- tests: `test_lexer` / `test_parser` / `test_sema` / `test_irgen` /
  `test_loader` / `test_diagnostics` + `test_e2e` parametrize

### PR2: `from x import y [as w]`

Layers touched: lexer (`KW_FROM`) / parser / sema (irgen unchanged).

- `rwc/lexer.py`: `"from"` / `KW_FROM` in `KEYWORDS`
- `rwc/parser.py`: extend `parse_import` to `from IDENT import IDENT [as IDENT] {, ...}`
- `rwc/sema.py`: a per-module `from_env: Dict[localname, (real_mod, real_name)]`.
  Clash detection (local function names, built-ins, other `from` names).
  Unqualified calls resolve local → built-in → `from_env` and are recorded in
  `call_resolution`
- `examples/import_from.rw` + lib + `.expected`, tests

### PR3: `import x as m`

Layers touched: parser / sema (lexer `KW_AS` already exists, irgen unchanged).

- `rwc/parser.py`: allow `[as IDENT]` on plain imports, stored in `Import.alias`
- `rwc/sema.py`: `import_env: Dict[visible_name, real_mod]` (plain `x→x`, as
  `m→x`). Resolving `Call(module=m)` maps to the real module. Detect duplicate
  alias definitions
- `examples/import_as.rw` + lib + `.expected`, tests

## Verification

```sh
# Unit
uv run pytest tests/test_lexer.py tests/test_parser.py tests/test_sema.py tests/test_irgen.py tests/test_loader.py
# DOT vs float / Call.module and Import parsing / namespaced table /
# errors for not-imported refs, cycles, main in an import, clashes /
# rw_user_<mod>.<fn> symbols / module-local aliases / per-file diagnostics

# emit-ast / emit-ir
uv run python -m rwc.cli emit-ast examples/import_basic.rw   # imports and Call(module=...)
uv run python -m rwc.cli emit-ir  examples/import_basic.rw   # rw_user_import_basic_lib.add

# e2e
uv run pytest tests/test_e2e.py -k import_
uv run rwc run examples/import_basic.rw

# Existing regressions
uv run pytest -q
```

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Adding a `.` token clashes with float literals | The lexer's `_read_number` requires digits around `.`, so `1.5` stays one FLOAT and `math_lib.add` splits into IDENT DOT IDENT. test_lexer covers both |
| Breaking the existing built-in branches (`callee == "print"`) | Default `Call.module` to `None` and add an `if call.module is None:` guard at the top of the built-in branches. Qualified calls never enter built-in checks |
| LLVM symbol clashes between same-named functions in different modules, or between a module-qualified name and an entry function | Symbols are `rw_user_<module>.<name>`; `.` is not valid in an rw identifier, so no rw name can produce the same symbol. Only `main` keeps the historical name via `(None, "main")`, leaving the C shim unchanged |
| Errors in an imported module reported against the entry file | Sema tracks the file of the module being checked; the CLI renders the snippet from the diagnostic's file |
| Lifetime of `id(Call)` in the resolution map | desugar never recreates Call nodes (same approach as the existing expr_types), so ids match between sema's and irgen's input |
| `spawn m.add()` (spawning a qualified call) | `_emit_spawn` and the trampoline are keyed by `(module, name)`, so it works; covered by `test_irgen` |
