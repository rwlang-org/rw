"""write_all delivers a large payload through partial writes (spec 52)."""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

UNIT = b"0123456789abcdef"
DOUBLINGS = 19  # 16 B * 2**19 = 8 MiB

BODY = f"""
def payload() -> Bytes:
    s = "{UNIT.decode()}"
    for i in range(0, {DOUBLINGS}):
        s = s + s
    return bytes_from_str(s)

def send(addr: string) -> int:
    c = dial(addr)
    n = write_all(c, payload())
    if n < 0:
        print(error_name(last_error()))
    close(c)
    return n
"""

MAIN_THREAD = BODY + """
def main() -> int:
    print(send(os_args()[1]))
    return 0
"""

FIBER = BODY + """
def main() -> int:
    f = spawn send(os_args()[1])
    print(await f)
    return 0
"""


def _build(src: str) -> Path:
    td = Path(tempfile.mkdtemp())
    rw = td / "p.rw"
    rw.write_text(src)
    exe = td / "p"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(rw), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    return exe


@pytest.mark.parametrize("src", [MAIN_THREAD, FIBER], ids=["main", "fiber"])
def test_write_all_8mib_slow_reader(src):
    exe = _build(src)
    srv = socket.socket()
    # A small receive buffer plus a slow reader forces many partial writes.
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = bytearray()

    def reader() -> None:
        conn, _ = srv.accept()
        with conn:
            first = True
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                got.extend(chunk)
                if first:
                    time.sleep(0.3)  # let the sender fill the socket buffer
                    first = False

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    r = subprocess.run([str(exe), f"tcp://127.0.0.1:{port}"], capture_output=True, text=True, timeout=120)
    t.join(30)
    srv.close()
    want = UNIT * (2 ** DOUBLINGS)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(len(want))
    assert len(got) == len(want)
    assert bytes(got) == want


def test_write_all_closed_peer_reports_error():
    exe = _build(MAIN_THREAD)
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def accept_and_close() -> None:
        conn, _ = srv.accept()
        conn.close()

    t = threading.Thread(target=accept_and_close, daemon=True)
    t.start()
    r = subprocess.run([str(exe), f"tcp://127.0.0.1:{port}"], capture_output=True, text=True, timeout=120)
    t.join(5)
    srv.close()
    assert r.returncode == 0, r.stderr
    lines = r.stdout.split()
    assert lines[0] in ("EPIPE", "ECONNRESET")
    assert lines[1] == "-1"
