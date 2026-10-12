"""stdlib/process.rw: large streams without deadlock, fibers, signals
(spec 61)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _run(src: str) -> subprocess.CompletedProcess:
    td = Path(tempfile.mkdtemp())
    (td / "p.rw").write_text(src)
    exe = td / "p"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(td / "p.rw"), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)


BIG = """
import process

def big() -> string:
    s = "0123456789abcdef"
    for i in range(0, 16):
        s = s + s
    return s

def run_one(tag: int) -> string:
    data = big()
    # cat echoes 1 MiB back while sh also writes 1 MiB to stderr: every pipe
    # fills up, so this only finishes if stdin, stdout and stderr are pumped
    # concurrently.
    match process.run(["sh", "-c", "cat; head -c 1048576 /dev/zero >&2"], data):
        case Some(out):
            return f"{tag} {out.status} {len(out.stdout) == len(data) and out.stdout == data} {len(out.stderr)}"
        case None:
            return f"{tag} failed"

def main() -> int:
    print(run_one(0))
    fs: List[Future[string]] = []
    for i in range(1, 5):
        fs = list_push(fs, spawn run_one(i))
    for r in await_all(fs):
        print(r)
    return 0
"""


def test_large_streams_main_and_fibers():
    r = _run(BIG)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [f"{i} 0 true 1048576" for i in range(5)]


STREAM = """
import process

def main() -> int:
    match process.start(["sh", "-c", "while read line; do echo got:$line; done"]):
        case Some(p):
            write_all(p.stdin, bytes_from_str("a\\nb\\n"))
            close(p.stdin)
            p.stdin = -1
            out = ""
            while true:
                b = read(p.stdout, 100)
                if len(b) == 0:
                    break
                out = out + str_from_bytes(b)
            print(out)
            print(p.wait())
        case None:
            print("failed")
    match process.start(["sh", "-c", "kill -9 $$"]):
        case Some(p):
            print(p.wait())
        case None:
            print("failed")
    match process.start(["sleep", "5"]):
        case Some(p):
            print(p.kill("SIGBOGUS"))
            print(error_name(last_error()))
            print(p.kill("SIGINT"))
            print(p.wait())
        case None:
            print("failed")
    print(len(proc_start([])))
    print(error_name(last_error()))
    return 0
"""


def test_streaming_signals_and_errors():
    r = _run(STREAM)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n")[:8] == ["got:a", "got:b", "", "0", str(128 + 9), "false", "EINVAL", "true"]
    assert r.stdout.split("\n")[8:11] == [str(128 + 2), "0", "EINVAL"]


def test_child_gets_default_sigpipe():
    # rw ignores SIGPIPE itself (spec 51); the child must not inherit that,
    # or `yes | head` style pipelines never end.
    src = """
import process

def main() -> int:
    match process.run(["sh", "-c", "yes | head -n 3"]):
        case Some(out):
            print(out.stdout)
        case None:
            print("failed")
    return 0
"""
    r = _run(src)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["y", "y", "y"]
