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
from .desugar import desugar_module
from .diagnostics import CompileError, Diagnostic
from .irgen import generate as irgen_generate
from .lexer import LexerError, tokenize
from .parser import ParserError, parse
from .sema import analyze


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
) -> CompileResult:
    """Compile a single rw source string to a native executable."""
    try:
        tokens = tokenize(source, filename=filename)
        ast = parse(tokens)
        ast = desugar_module(ast)
        sema = analyze(ast, filename=filename)
        llmod = irgen_generate(ast, sema)
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
        cmd = [clang, ll_path, str(librw), "-o", str(output), "-lpthread"]
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


def emit_ir(source: str, filename: str) -> str:
    """Generate LLVM IR text for a source string without invoking the linker."""
    tokens = tokenize(source, filename=filename)
    ast = parse(tokens)
    ast = desugar_module(ast)
    sema = analyze(ast, filename=filename)
    llmod = irgen_generate(ast, sema)
    llmod.triple = llvm_binding.get_default_triple()
    return str(llmod)


def emit_ast(source: str, filename: str) -> ASTModule:
    tokens = tokenize(source, filename=filename)
    return desugar_module(parse(tokens))


def run_executable(path: Path) -> int:
    """Execute a built binary, propagating stdout/stderr."""
    proc = subprocess.run([str(path)])
    return proc.returncode
