"""End-to-end tests: compile + run examples and compare to .expected files."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _run_rwc(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke the rwc CLI in-process to avoid path issues with `uv run`."""
    return subprocess.run(
        [sys.executable, "-m", "rwc.cli", *args],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def _build_and_run(rw_path: Path) -> tuple[int, str]:
    """Build the example and run it, returning (exit_code, stdout).

    Pin RW_WORKERS=1 so output stays byte-identical regardless of the
    machine's core count. The M:N scheduler is exercised separately
    via the C-level tests under runtime/fiber/test_*.c.
    """
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / rw_path.stem
        build = _run_rwc(["build", str(rw_path), "-o", str(out)])
        assert build.returncode == 0, f"rwc build failed:\n{build.stderr}"
        env = {**os.environ, "RW_WORKERS": "1"}
        run = subprocess.run([str(out)], capture_output=True, text=True, env=env)
        return run.returncode, run.stdout


@pytest.mark.parametrize(
    "name",
    ["hello", "arith", "fib", "while_count", "spawn_basic", "spawn_many", "spawn_string", "string_ops", "bytes_basic", "list_basic", "option_basic", "result_basic", "for_count", "ternary", "file_io", "file_par", "num_literals", "bitops", "break_continue", "math_basic"],
)
def test_synchronous_example(name: str):
    rw_path = EXAMPLES / f"{name}.rw"
    expected_path = EXAMPLES / f"{name}.rw.expected"
    expected = expected_path.read_text()
    rc, out = _build_and_run(rw_path)
    assert rc == 0
    assert out == expected, f"output mismatch for {name}:\nexpected:\n{expected!r}\ngot:\n{out!r}"


def test_rwc_emit_ir_runs():
    rw_path = EXAMPLES / "hello.rw"
    res = _run_rwc(["emit-ir", str(rw_path)])
    assert res.returncode == 0
    assert "rw_user_main" in res.stdout
    assert "rw_print_str" in res.stdout
