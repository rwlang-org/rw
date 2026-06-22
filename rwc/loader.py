"""Module loader: resolves `import` statements into a program graph.

The entry `.rw` file may `import foo`, which is resolved to `foo.rw` in the
*same directory* as the entry file (the only search path; see spec 17).
Imported modules may themselves import (transitive imports). Import cycles
are detected and reported as errors.

Each module is tokenized, parsed, and desugared independently. ASTs are NOT
merged: Sema builds a namespaced function table keyed by `(module, name)` and
resolves qualified calls against it. The loader's job is purely to discover
and parse the reachable modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from . import ast_nodes as A
from .desugar import desugar_module
from .diagnostics import CompileError, Diagnostic
from .lexer import LexerError, tokenize
from .parser import ParserError, parse


@dataclass
class LoadedProgram:
    """The entry module plus every module reachable via imports."""

    root: A.Module
    root_name: str
    # Imported modules by their module name (the `foo` in `import foo`).
    # The root module is NOT included here.
    modules: Dict[str, A.Module] = field(default_factory=dict)
    # Source text per module, keyed by module name (root included under
    # root_name), for diagnostics that need to render a snippet.
    sources: Dict[str, str] = field(default_factory=dict)


def _parse_module(source: str, filename: str) -> A.Module:
    """Tokenize, parse, and desugar a single source string into a Module."""
    try:
        return desugar_module(parse(tokenize(source, filename=filename)))
    except LexerError as e:
        raise CompileError(Diagnostic(filename, e.line, e.col, e.length, e.message)) from e
    except ParserError as e:
        raise CompileError(Diagnostic(filename, e.line, e.col, e.length, e.message)) from e


def _has_main(mod: A.Module) -> bool:
    return any(fn.name == "main" for fn in mod.functions)


def load_program(root_source: str, root_filename: str) -> LoadedProgram:
    """Load the entry module and all transitively imported modules.

    `import foo` is resolved to `<dir-of-root>/foo.rw`. Raises CompileError on a
    missing module, an import cycle, or a `main` defined in an imported module.
    """
    root_path = Path(root_filename)
    base_dir = root_path.parent
    root_name = root_path.stem

    root = _parse_module(root_source, root_filename)

    program = LoadedProgram(root=root, root_name=root_name)
    program.sources[root_name] = root_source

    # DFS over the import graph. `stack` holds the current import chain so we
    # can point at the cycle; `loaded` de-dups modules already parsed.
    loaded: Dict[str, A.Module] = {}

    def visit(mod: A.Module, mod_name: str, mod_filename: str, stack: List[str]) -> None:
        for imp in mod.imports:
            target = imp.module
            if target == root_name or target == mod_name:
                # Importing the entry file or oneself is a cycle.
                raise CompileError(Diagnostic(
                    mod_filename, imp.line, imp.col, max(1, len(target)),
                    f"import cycle detected: '{target}' is already being loaded",
                ))
            if target in stack:
                chain = " -> ".join(stack + [target])
                raise CompileError(Diagnostic(
                    mod_filename, imp.line, imp.col, max(1, len(target)),
                    f"import cycle detected: {chain}",
                ))
            if target in loaded:
                continue  # already parsed via another path

            target_path = base_dir / f"{target}.rw"
            if not target_path.is_file():
                raise CompileError(Diagnostic(
                    mod_filename, imp.line, imp.col, max(1, len(target)),
                    f"cannot find module '{target}' (looked for {target_path})",
                ))
            target_source = target_path.read_text(encoding="utf-8")
            target_mod = _parse_module(target_source, str(target_path))

            if _has_main(target_mod):
                raise CompileError(Diagnostic(
                    str(target_path), imp.line, imp.col, max(1, len(target)),
                    f"imported module '{target}' must not define 'main'",
                ))

            loaded[target] = target_mod
            program.modules[target] = target_mod
            program.sources[target] = target_source
            visit(target_mod, target, str(target_path), stack + [target])

    visit(root, root_name, root_filename, [root_name])
    return program
