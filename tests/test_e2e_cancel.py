"""Cooperative cancellation and await_timeout (spec 75)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(src: str, timeout: float = 120) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        rw = Path(td) / "p.rw"
        rw.write_text(src)
        exe = Path(td) / "p"
        build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(rw), "-o", str(exe)],
                               capture_output=True, text=True, cwd=REPO)
        assert build.returncode == 0, build.stderr
        return subprocess.run([str(exe)], capture_output=True, text=True, timeout=timeout)


def test_cancelling_ten_thousand_fibers():
    # cancel() is one atomic store per future: cancelling 10k parked fibers
    # takes well under 50 ms; every fiber then sees cancelled() and returns.
    r = _run("""
def spin() -> int:
    while not cancelled():
        sleep(2)
    return 1

def main() -> int:
    fs: List[Future[int]] = []
    for i in range(10000):
        fs = list_push(fs, spawn spin())
    sleep(20)
    t0 = mono_ns()
    for f in fs:
        cancel(f)
    cancel_ms = (mono_ns() - t0) / 1000000
    total = 0
    for v in await_all(fs):
        total += v
    print(total)
    print(cancel_ms < 50)
    return 0
""")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["10000", "true"]


def test_await_timeout_from_a_fiber_and_cancel_after_timeout():
    r = _run("""
def forever() -> int:
    n = 0
    while not cancelled():
        sleep(1)
        n += 1
    return -1

def supervisor() -> string:
    f = spawn forever()
    match await_timeout(f, 20):
        case Some(v):
            return f"done {v}"
        case None:
            cancel(f)
            return f"timed out, then {await f}"

def main() -> int:
    print(await spawn supervisor())
    g = spawn forever()
    match await_timeout(g, 0):
        case Some(v):
            print(v)
        case None:
            print("zero timeout")
    cancel(g)
    print(await g)
    return 0
""")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["timed out, then -1", "zero timeout", "-1"]
