"""Several fibers accepting on one listening handle (#166, spec 53)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ._net import start_announced

REPO = Path(__file__).resolve().parent.parent

# 8 acceptor fibers share one listen handle; each serves connections until
# it has handled its share. Every connection must be served exactly once.
SRC = """
def rw_test_announce(ln: int) -> int:
    write(1, bytes_from_str(local_addr(ln) + "\\n"))
    return ln

def acceptor(ln: int, id: int, quota: int) -> int:
    served = 0
    while served < quota:
        c = accept(ln)
        b = read(c, 100)
        write_all(c, bytes_from_str(f"{id}:" + str_from_bytes(b)))
        close(c)
        served += 1
    return served

def main() -> int:
    ln = rw_test_announce(listen("tcp://127.0.0.1:0"))
    fs: List[Future[int]] = []
    for i in range(0, 8):
        fs = list_push(fs, spawn acceptor(ln, i, 25))
    total = 0
    for n in await_all(fs):
        total += n
    print(total)
    return 0
"""


def test_eight_acceptors_share_one_listener():
    td = Path(tempfile.mkdtemp())
    (td / "p.rw").write_text(SRC)
    exe = td / "p"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(td / "p.rw"), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    proc, port = start_announced([str(exe)], env={**os.environ, "RW_WORKERS": "4"})

    def one(i: int) -> str:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(f"req{i}".encode())
            data = b""
            while chunk := s.recv(100):
                data += chunk
            return data.decode()

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            replies = list(pool.map(one, range(200)))
        assert all(r.split(":", 1)[1] == f"req{i}" for i, r in enumerate(replies))
        # More than one acceptor took part.
        assert len({r.split(":", 1)[0] for r in replies}) > 1
        proc.wait(timeout=30)
        assert proc.returncode == 0
    finally:
        proc.kill()
