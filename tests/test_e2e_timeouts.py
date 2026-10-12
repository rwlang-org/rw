"""Read / write / accept / connect timeouts (spec 53)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _run(src: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        rw = Path(td) / "p.rw"
        rw.write_text(src)
        exe = Path(td) / "p"
        build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(rw), "-o", str(exe)],
                               capture_output=True, text=True, cwd=REPO)
        assert build.returncode == 0, build.stderr
        return subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=timeout)


BODY = """
def elapsed_ms(t0: int) -> int:
    return (mono_ns() - t0) / 1000000

def big() -> Bytes:
    s = "0123456789abcdef"
    for i in range(0, 22):
        s = s + s
    return bytes_from_str(s)

def run() -> int:
    ln = listen("tcp://127.0.0.1:0")

    # accept with nobody connecting
    set_read_timeout(ln, 100)
    t0 = mono_ns()
    print(accept(ln))
    print(error_name(last_error()))
    print(elapsed_ms(t0) >= 100)

    # read from a client that never sends
    c = dial(local_addr(ln))
    s = accept(ln)
    set_read_timeout(s, 100)
    t0 = mono_ns()
    print(len(read(s, 10)))
    print(error_name(last_error()))
    print(elapsed_ms(t0) >= 100)

    # write to a peer that never reads: 64 MiB overflows the socket buffers
    set_write_timeout(c, 100)
    t0 = mono_ns()
    print(write_all(c, big()))
    print(error_name(last_error()))
    print(elapsed_ms(t0) >= 100)

    # without a timeout, data still flows normally
    set_read_timeout(s, 0)
    set_write_timeout(c, 0)
    close(c)
    close(s)
    c = dial(local_addr(ln))
    s = accept(ln)
    set_read_timeout(s, 5000)
    write(c, bytes_from_str("hi"))
    print(str_from_bytes(read(s, 10)))
    print(last_error())
    return 0
"""

EXPECTED = ["-1", "ETIMEDOUT", "true", "0", "ETIMEDOUT", "true", "-1", "ETIMEDOUT", "true", "hi", "0"]


@pytest.mark.parametrize("where", ["main", "fiber"])
def test_timeouts(where):
    main = "def main() -> int:\n    return run()\n" if where == "main" else \
        "def main() -> int:\n    f = spawn run()\n    return await f\n"
    r = _run(BODY + main)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == EXPECTED


DIAL = """
def run() -> int:
    t0 = mono_ns()
    c = dial_timeout(os_args()[1], 300)
    took = (mono_ns() - t0) / 1000000
    print(c)
    print(error_name(last_error()))
    print(took)
    return 0
"""


@pytest.mark.parametrize("where", ["main", "fiber"])
def test_dial_timeout_does_not_hang(where):
    # 10.255.255.1 is normally unroutable: the SYN goes nowhere, so the
    # connect would hang for a long time without the deadline. Some networks
    # reject it right away (ENETUNREACH / EHOSTUNREACH); that is fine too.
    main = "def main() -> int:\n    return run()\n" if where == "main" else \
        "def main() -> int:\n    f = spawn run()\n    return await f\n"
    t0 = time.monotonic()
    r = _run(DIAL + main, "tcp://10.255.255.1:81")
    assert r.returncode == 0, r.stderr
    c, name, took = r.stdout.split()
    assert c == "-1"
    assert name in ("ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH", "ECONNREFUSED", "EACCES", "EPERM")
    assert int(took) < 3000
    if name == "ETIMEDOUT":
        assert int(took) >= 300
    assert time.monotonic() - t0 < 30


def test_dial_timeout_success():
    src = """
def main() -> int:
    ln = listen("tcp://127.0.0.1:0")
    c = dial_timeout(local_addr(ln), 1000)
    print(c > 0)
    print(last_error())
    return 0
"""
    r = _run(src)
    assert r.stdout.split() == ["true", "0"], r.stderr
