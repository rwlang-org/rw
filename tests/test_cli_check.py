"""`rwc check`: parse and type-check without generating code (spec 48)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _check(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "rwc.cli", "check", *args],
                          capture_output=True, text=True, cwd=REPO)


def test_ok_file(tmp_path):
    rw = tmp_path / "ok.rw"
    rw.write_text("def main() -> int:\n    print(\"hi\")\n    return 0\n")
    r = _check(str(rw))
    assert r.returncode == 0, r.stderr
    assert f"{rw}: ok" in r.stdout
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ok.rw"]


def test_quiet(tmp_path):
    rw = tmp_path / "ok.rw"
    rw.write_text("def main() -> int:\n    return 0\n")
    r = _check("-q", str(rw))
    assert r.returncode == 0
    assert r.stdout == ""


def test_type_error_reported():
    r = _check("tests/bad/type_mismatch.rw")
    assert r.returncode == 1
    assert "error" in r.stderr
    assert "type_mismatch.rw" in r.stderr


def test_parse_error_reported(tmp_path):
    rw = tmp_path / "bad.rw"
    rw.write_text("def main() -> int:\n    return (\n")
    r = _check(str(rw))
    assert r.returncode == 1
    assert "bad.rw:" in r.stderr


def test_multiple_files_keep_going(tmp_path):
    ok = tmp_path / "ok.rw"
    ok.write_text("def main() -> int:\n    return 0\n")
    r = _check("tests/bad/undefined_variable.rw", str(ok))
    assert r.returncode == 1
    assert "undefined_variable.rw" in r.stderr
    assert f"{ok}: ok" in r.stdout


def test_examples_check():
    r = _check("-q", "examples/web_app.rw")
    assert r.returncode == 0, r.stderr
