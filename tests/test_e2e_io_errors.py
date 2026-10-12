"""last_error / error_name: EPIPE without SIGPIPE, per-fiber errors (spec 51)."""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(src: str, *args: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as td:
        rw = Path(td) / "p.rw"
        rw.write_text(src)
        exe = Path(td) / "p"
        build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(rw), "-o", str(exe)],
                               capture_output=True, text=True, cwd=REPO)
        assert build.returncode == 0, build.stderr
        return subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=60)


WRITER = """
def main() -> int:
    args = os_args()
    c = dial(args[1])
    chunk = rand_bytes(65536)
    for i in range(0, 1000):
        if write(c, chunk) < 0:
            print(error_name(last_error()))
            return 0
    print("never failed")
    return 1
"""


def test_write_to_closed_peer_is_epipe_not_sigpipe():
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def accept_and_close() -> None:
        conn, _ = srv.accept()
        conn.close()

    t = threading.Thread(target=accept_and_close, daemon=True)
    t.start()
    r = _run(WRITER, f"tcp://127.0.0.1:{port}")
    t.join(5)
    srv.close()
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert r.stdout.strip() in ("EPIPE", "ECONNRESET")


# Each fiber fails differently, then parks (sleep) so it can resume on another
# M, and checks it still sees its own error.
PER_FIBER = """
def refused(addr: string) -> int:
    dial(addr)
    sleep(5)
    if error_name(last_error()) == "ECONNREFUSED":
        return 1
    return 0

def missing(i: int) -> int:
    open(f"/nonexistent/{i}", "r")
    sleep(5)
    if error_name(last_error()) == "ENOENT":
        return 1
    return 0

def fine(i: int) -> int:
    ln = listen("tcp://127.0.0.1:0")
    sleep(5)
    ok = last_error() == 0
    close(ln)
    if ok:
        return 1
    return 0

def main() -> int:
    args = os_args()
    fs: List[Future[int]] = []
    for i in range(0, 40):
        fs = list_push(fs, spawn refused(args[1]))
        fs = list_push(fs, spawn missing(i))
        fs = list_push(fs, spawn fine(i))
    total = 0
    for r in await_all(fs):
        total += r
    print(total)
    return 0
"""


def test_errors_follow_the_fiber():
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = _run(PER_FIBER, f"tcp://127.0.0.1:{port}")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "120"
