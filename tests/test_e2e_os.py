"""End-to-end test for the OS API (spec 40): arguments, environment, cwd, exit."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _build(td: str) -> Path:
    exe = Path(td) / "os_api"
    build = subprocess.run(
        [sys.executable, "-m", "rwc.cli", "build", str(REPO / "examples" / "os_api.rw"), "-o", str(exe)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert build.returncode == 0, build.stderr
    return exe


def test_args_env_cwd():
    with tempfile.TemporaryDirectory() as td:
        exe = _build(td)
        env = {**os.environ, "NAME": "rw"}
        run = subprocess.run([str(exe), "x y"], capture_output=True, text=True, env=env, timeout=30)
        assert run.returncode == 0, run.stderr
        assert run.stdout.splitlines() == [
            "1 args", "arg: x y", "hello, rw", "set by rw", "/", "chdir failed as expected",
        ]


def test_missing_env_and_exit_code():
    with tempfile.TemporaryDirectory() as td:
        exe = _build(td)
        env = {k: v for k, v in os.environ.items() if k != "NAME"}
        run = subprocess.run([str(exe), "a", "b"], capture_output=True, text=True, env=env, timeout=30)
        assert run.returncode == 3
        assert "NAME is not set" in run.stdout
        assert "arg: b" in run.stdout  # printed before os_exit flushed stdout
