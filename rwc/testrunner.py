"""`rwc test`: find and run rw test functions (spec 68).

A test file is `test_*.rw` or `*_test.rw`; a test is a top-level function
named `test_*` that takes no arguments. For each file, a runner program is
compiled that imports the file and calls one test chosen by argv[1]. Each
test runs in its own process, so a failed assert (which exits) or a crash
ends only that test.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .diagnostics import CompileError, Diagnostic
from .driver import compile_source
from .lexer import LexerError
from .loader import _parse_module
from .parser import ParserError

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass
class Outcome:
    name: str
    passed: bool
    seconds: float
    output: str


def is_test_file(p: Path) -> bool:
    return p.suffix == ".rw" and (p.name.startswith("test_") or p.stem.endswith("_test"))


def discover(paths: Sequence[str]) -> List[Path]:
    """Test files under `paths` (files are taken as given), sorted."""
    found: List[Path] = []
    for raw in paths or ["."]:
        p = Path(raw)
        if p.is_file():
            found.append(p)
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__"))
            for f in sorted(files):
                fp = Path(root) / f
                if is_test_file(fp):
                    found.append(fp)
    return found


def test_names(path: Path, source: str) -> List[str]:
    mod = _parse_module(source, str(path))
    return [fn.name for fn in mod.functions if fn.name.startswith("test_") and not fn.params]


def runner_source(module: str, names: Sequence[str]) -> str:
    lines = [
        f"import {module} as rwtest_mod",
        "",
        "def main() -> int:",
        "    args = os_args()",
        "    if len(args) < 2:",
        '        print("usage: <runner> TEST_NAME")',
        "        return 2",
        "    name = args[1]",
    ]
    for n in names:
        lines += [f'    if name == "{n}":', f"        rwtest_mod.{n}()", "        return 0"]
    lines += ['    print("no such test: " + name)', "    return 2", ""]
    return "\n".join(lines)


def build_runner(path: Path, names: Sequence[str], out: Path) -> None:
    module = path.stem
    if not _IDENT.match(module):
        raise CompileError(Diagnostic(str(path), 1, 1, 1,
                                      f"test file name `{path.name}` is not a valid module name"))
    # The runner is compiled as if it sat next to the test file, so `import`
    # finds the file and its neighbours; nothing is written there.
    compile_source(runner_source(module, names), filename=str(path.parent / "rwtest_runner.rw"), output=out)


def run_one(exe: Path, name: str, timeout: float) -> Outcome:
    t0 = time.monotonic()
    try:
        r = subprocess.run([str(exe), name], capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).rstrip("\n")
        if r.returncode < 0:
            out += f"\n(killed by signal {-r.returncode})"
        return Outcome(name, r.returncode == 0, time.monotonic() - t0, out)
    except subprocess.TimeoutExpired as e:
        partial = ((e.stdout or b"") + (e.stderr or b"")).decode(errors="replace") if isinstance(e.stdout, bytes) else ""
        return Outcome(name, False, time.monotonic() - t0, (partial + f"\n(timed out after {timeout:g} s)").strip())


def _indent(text: str, by: str = "    ") -> str:
    return "\n".join(by + line for line in text.splitlines())


def run(paths: Sequence[str], pattern: Optional[str] = None, timeout: float = 60.0,
        verbose: bool = False, report_error=None) -> int:
    files = discover(paths)
    if not files:
        print("no test files found (test_*.rw or *_test.rw)")
        return 1
    t_start = time.monotonic()
    passed = failed = errors = 0
    jobs = max(1, (os.cpu_count() or 2))
    with tempfile.TemporaryDirectory() as td, ThreadPoolExecutor(max_workers=jobs) as pool:
        for path in files:
            source = path.read_text(encoding="utf-8")
            try:
                names = test_names(path, source)
            except (LexerError, ParserError) as e:
                print(f"{path}: {e.message} (line {e.line})", file=sys.stderr)
                errors += 1
                continue
            if pattern:
                names = [n for n in names if pattern in n]
            if not names:
                continue
            exe = Path(td) / f"t{passed + failed + errors}_{path.stem}"
            try:
                build_runner(path, names, exe)
            except CompileError as e:
                if report_error:
                    report_error(e, source, path)
                else:
                    print(e.diagnostic.render(source), file=sys.stderr)
                print(f"{path}: build failed")
                errors += 1
                continue
            print(str(path))
            for o in pool.map(lambda n, exe=exe: run_one(exe, n, timeout), names):
                ms = o.seconds * 1000
                if o.passed:
                    passed += 1
                    print(f"  PASS {o.name} ({ms:.0f} ms)")
                    if verbose and o.output:
                        print(_indent(o.output))
                else:
                    failed += 1
                    print(f"  FAIL {o.name} ({ms:.0f} ms)")
                    if o.output:
                        print(_indent(o.output))
    summary = f"{passed} passed, {failed} failed"
    if errors:
        summary += f", {errors} file(s) did not build"
    print(f"{summary} in {time.monotonic() - t_start:.2f} s")
    return 0 if failed == 0 and errors == 0 else 1
