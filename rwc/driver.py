"""Driver: glue between Python compiler stages and clang/linker.

End-to-end:
    .rw -> tokens -> AST -> Sema -> ir.Module -> .ll on disk -> clang -> executable
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from llvmlite import binding as llvm_binding

from .ast_nodes import Module as ASTModule
from .diagnostics import CompileError, Diagnostic
from .irgen import generate as irgen_generate
from .lexer import LexerError
from .loader import load_program
from .parser import ParserError
from .sema import analyze_program


def _find_runtime_dir() -> Path:
    """Locate the C runtime directory (`runtime/` next to the `rwc` package)."""
    here = Path(__file__).resolve()
    # repo layout: <repo>/rwc/driver.py and <repo>/runtime/librw.a
    repo_root = here.parent.parent
    rt_dir = repo_root / "runtime"
    return rt_dir


def _ensure_runtime_built(rt_dir: Path) -> Path:
    """Build `librw.a` on first use if it's missing."""
    lib = rt_dir / "librw.a"
    if lib.exists():
        return lib
    if not rt_dir.exists():
        raise CompileError(Diagnostic(
            "<driver>", 1, 1, 1,
            f"runtime directory not found at {rt_dir}",
        ))
    res = subprocess.run(["make", "-C", str(rt_dir)], capture_output=True, text=True)
    if res.returncode != 0:
        raise CompileError(Diagnostic(
            "<driver>", 1, 1, 1,
            f"failed to build C runtime: {res.stderr.strip()}",
        ))
    return lib


# LLVM optimization level when neither -O nor $RW_OPT_LEVEL is given (spec 70).
DEFAULT_OPT_LEVEL = 2


def resolve_opt_level(level: Optional[int]) -> int:
    if level is None:
        env = os.environ.get("RW_OPT_LEVEL", "")
        level = int(env) if env.isdigit() else DEFAULT_OPT_LEVEL
    if not 0 <= level <= 3:
        raise CompileError(Diagnostic("<rwc>", 1, 1, 1, f"optimization level must be 0-3, got {level}"))
    return level


_GC_FLAGS: Optional[list] = None


def gc_link_flags() -> list:
    """Linker flags for the Boehm GC (spec 67), found by
    scripts/gc-prefix.sh (RW_GC_PREFIX overrides it)."""
    global _GC_FLAGS
    if _GC_FLAGS is None:
        script = _find_runtime_dir().parent / "scripts" / "gc-prefix.sh"
        res = subprocess.run(["sh", str(script)], capture_output=True, text=True)
        if res.returncode != 0:
            raise CompileError(Diagnostic(
                "<rwc>", 1, 1, 1,
                res.stderr.strip() or "Boehm GC (bdw-gc) not found",
            ))
        prefix = res.stdout.strip()
        flags = []
        if prefix not in ("/usr", "/usr/local"):
            flags += [f"-L{prefix}/lib", f"-Wl,-rpath,{prefix}/lib"]
        _GC_FLAGS = flags + ["-lgc"]
    return list(_GC_FLAGS)


def _find_clang() -> str:
    clang = shutil.which("clang") or shutil.which("cc")
    if not clang:
        raise CompileError(Diagnostic(
            "<driver>", 1, 1, 1,
            "could not find `clang` or `cc` on PATH",
        ))
    return clang


@dataclass
class CompileResult:
    output: Path
    ir_text: str


def compile_source(
    source: str,
    filename: str,
    output: Optional[Path] = None,
    *,
    target_triple: Optional[str] = None,
    checked: bool = True,
    opt_level: Optional[int] = None,
) -> CompileResult:
    """Compile a single rw source string to a native executable. `checked`
    keeps the run-time checks for integer division (spec 42). `opt_level`
    (0-3) is the LLVM optimization level (spec 70); None means
    $RW_OPT_LEVEL, else the default."""
    try:
        program = load_program(source, filename)
        sema = analyze_program(program, filename=filename)
        llmod = irgen_generate(program, sema, checked)
    except LexerError as e:
        raise CompileError(Diagnostic(filename, e.line, e.col, e.length, e.message)) from e
    except ParserError as e:
        raise CompileError(Diagnostic(filename, e.line, e.col, e.length, e.message)) from e
    except CompileError:
        raise

    # Fill in target triple for clang's benefit.
    triple = target_triple or llvm_binding.get_default_triple()
    llmod.triple = triple

    ir_text = str(llmod)

    if output is None:
        output = Path(filename).with_suffix("")

    rt_dir = _find_runtime_dir()
    librw = _ensure_runtime_built(rt_dir)
    clang = _find_clang()

    with tempfile.NamedTemporaryFile("w", suffix=".ll", delete=False) as f:
        f.write(ir_text)
        ll_path = f.name

    try:
        # -lm: math intrinsics (llvm.pow/exp/log/sin/cos, ...) may be lowered
        # to libm calls (e.g. `pow`) by the backend. macOS bundles these in
        # libSystem, but GNU/Linux requires explicit linking against libm.
        level = resolve_opt_level(opt_level)
        cmd = [clang, f"-O{level}", ll_path, str(librw), "-o", str(output), *gc_link_flags(), "-lpthread", "-lm"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise CompileError(Diagnostic(
                filename, 1, 1, 1,
                f"linker error: {proc.stderr.strip()}",
            ))
    finally:
        try:
            os.unlink(ll_path)
        except OSError:
            pass

    return CompileResult(output=Path(output), ir_text=ir_text)


def emit_ir(source: str, filename: str, checked: bool = True) -> str:
    """Generate LLVM IR text for a source string without invoking the linker."""
    program = load_program(source, filename)
    sema = analyze_program(program, filename=filename)
    llmod = irgen_generate(program, sema, checked)
    llmod.triple = llvm_binding.get_default_triple()
    return str(llmod)


def check_source(source: str, filename: str) -> None:
    """Parse and type-check without generating code (`rwc check`, spec 48).
    Raises CompileError on the first error."""
    try:
        program = load_program(source, filename)
        analyze_program(program, filename=filename)
    except LexerError as e:
        raise CompileError(Diagnostic(filename, e.line, e.col, e.length, e.message)) from e
    except ParserError as e:
        raise CompileError(Diagnostic(filename, e.line, e.col, e.length, e.message)) from e


def emit_ast(source: str, filename: str) -> ASTModule:
    """Return the entry module AST (imports resolved & parsed, not merged)."""
    return load_program(source, filename).root


def run_executable(path: Path) -> int:
    """Execute a built binary, propagating stdout/stderr."""
    proc = subprocess.run([str(path)])
    return proc.returncode
