"""Blocking fds (stdin, pipes) are offloaded to the aio pool (spec 25)."""

from __future__ import annotations

import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def test_stdin_read_does_not_block_the_worker():
    with tempfile.TemporaryDirectory() as td:
        exe = Path(td) / "stdin_offload"
        build = subprocess.run(
            [sys.executable, "-m", "rwc.cli", "build", str(EXAMPLES / "stdin_offload.rw"), "-o", str(exe)],
            capture_output=True, text=True, cwd=REPO,
        )
        assert build.returncode == 0, build.stderr
        proc = subprocess.Popen(
            [str(exe)], env={**os.environ, "RW_WORKERS": "1"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            # stdin stays open and empty: `reader` is blocked in read(0).
            # `ticker` must still get the only worker and print.
            ready, _, _ = select.select([proc.stdout], [], [], 5.0)
            assert ready, "ticker did not run while reader waited on stdin"
            assert proc.stdout.readline() == b"tick\n"
            proc.stdin.write(b"hello\n")
            proc.stdin.close()
            assert proc.stdout.readline() == b"read: hello\n"
            assert proc.wait(timeout=10) == 6
        finally:
            if proc.poll() is None:
                proc.kill()
