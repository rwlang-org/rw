"""Command-line entry point for the rw compiler (`rwc`)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .diagnostics import CompileError
from .driver import compile_source, emit_ast, emit_ir, run_executable
from .lexer import LexerError
from .parser import ParserError


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"error: no such file: {path}", file=sys.stderr)
        sys.exit(1)


def _report(err: CompileError, source: str) -> None:
    diag = err.diagnostic
    print(diag.render(source), file=sys.stderr)


def cmd_build(args: argparse.Namespace) -> int:
    src_path = Path(args.input)
    source = _read_source(src_path)
    out_path = Path(args.output) if args.output else src_path.with_suffix("")
    try:
        compile_source(source, filename=str(src_path), output=out_path)
    except CompileError as e:
        _report(e, source)
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    src_path = Path(args.input)
    source = _read_source(src_path)
    # Build into a temp file and run.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / src_path.stem
        try:
            compile_source(source, filename=str(src_path), output=out)
        except CompileError as e:
            _report(e, source)
            return 1
        return run_executable(out)


def cmd_emit_ir(args: argparse.Namespace) -> int:
    src_path = Path(args.input)
    source = _read_source(src_path)
    try:
        text = emit_ir(source, filename=str(src_path))
    except (LexerError, ParserError) as e:
        # Wrap into CompileError for uniform reporting.
        from .diagnostics import Diagnostic
        diag = Diagnostic(str(src_path), e.line, e.col, getattr(e, "length", 1), e.message)
        print(diag.render(source), file=sys.stderr)
        return 1
    except CompileError as e:
        _report(e, source)
        return 1
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_emit_ast(args: argparse.Namespace) -> int:
    src_path = Path(args.input)
    source = _read_source(src_path)
    try:
        ast = emit_ast(source, filename=str(src_path))
    except (LexerError, ParserError) as e:
        from .diagnostics import Diagnostic
        diag = Diagnostic(str(src_path), e.line, e.col, getattr(e, "length", 1), e.message)
        print(diag.render(source), file=sys.stderr)
        return 1
    print(_format_ast(ast))
    return 0


def _format_ast(node, indent: int = 0) -> str:
    """Pretty-printer for AST nodes."""
    from dataclasses import is_dataclass, fields
    pad = "  " * indent
    if is_dataclass(node):
        cls = type(node).__name__
        lines = [f"{pad}{cls}"]
        for f in fields(node):
            v = getattr(node, f.name)
            if isinstance(v, list):
                lines.append(f"{pad}  {f.name}: [")
                for item in v:
                    lines.append(_format_ast(item, indent + 2))
                lines.append(f"{pad}  ]")
            elif is_dataclass(v):
                lines.append(f"{pad}  {f.name}:")
                lines.append(_format_ast(v, indent + 2))
            else:
                lines.append(f"{pad}  {f.name}: {v!r}")
        return "\n".join(lines)
    return f"{pad}{node!r}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="rwc", description="rw language compiler")
    ap.add_argument("--version", action="version", version=f"rwc {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="compile .rw to a native executable")
    p_build.add_argument("input")
    p_build.add_argument("-o", "--output", default=None)
    p_build.set_defaults(func=cmd_build)

    p_run = sub.add_parser("run", help="compile and run a .rw file")
    p_run.add_argument("input")
    p_run.set_defaults(func=cmd_run)

    p_ir = sub.add_parser("emit-ir", help="print generated LLVM IR")
    p_ir.add_argument("input")
    p_ir.set_defaults(func=cmd_emit_ir)

    p_ast = sub.add_parser("emit-ast", help="print parsed AST")
    p_ast.add_argument("input")
    p_ast.set_defaults(func=cmd_emit_ast)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
