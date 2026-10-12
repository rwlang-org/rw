"""rwc build / run -O levels (spec 70)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = "def fib(n: int) -> int:\n    if n < 2:\n        return n\n    return fib(n - 1) + fib(n - 2)\n\n" \
      "def main() -> int:\n    print(fib(20))\n    return 0\n"


@pytest.mark.parametrize("level", ["0", "1", "2", "3"])
def test_every_level_builds_and_runs(tmp_path, level):
    src = tmp_path / "p.rw"
    src.write_text(SRC)
    r = subprocess.run([sys.executable, "-m", "rwc.cli", "run", f"-O{level}", str(src)],
                       capture_output=True, text=True, cwd=REPO, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "6765"


def test_bad_level_is_rejected(tmp_path):
    src = tmp_path / "p.rw"
    src.write_text(SRC)
    r = subprocess.run([sys.executable, "-m", "rwc.cli", "build", "-O7", str(src)],
                       capture_output=True, text=True, cwd=REPO, timeout=120)
    assert r.returncode == 2
    assert "invalid choice" in r.stderr


def test_env_level_is_used(monkeypatch):
    from rwc.driver import DEFAULT_OPT_LEVEL, resolve_opt_level

    monkeypatch.delenv("RW_OPT_LEVEL", raising=False)
    assert resolve_opt_level(None) == DEFAULT_OPT_LEVEL == 2
    monkeypatch.setenv("RW_OPT_LEVEL", "0")
    assert resolve_opt_level(None) == 0
    assert resolve_opt_level(3) == 3   # an explicit -O wins
