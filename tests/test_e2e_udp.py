"""UDP against Python peers (spec 69)."""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from ._net import start_announced

REPO = Path(__file__).resolve().parent.parent


def _build(src: str) -> Path:
    td = Path(tempfile.mkdtemp())
    (td / "p.rw").write_text(src)
    exe = td / "p"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(td / "p.rw"), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    return exe


CLIENT = """
def main() -> int:
    c = dial(os_args()[1])
    for word in ["ping", "pong", "x"]:
        write(c, bytes_from_str(word))
        print(str_from_bytes(read(c, 100)))
    return 0
"""


@pytest.mark.parametrize("family, host", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_rw_client_python_server(family, host):
    try:
        srv = socket.socket(family, socket.SOCK_DGRAM)
        srv.bind((host, 0))
    except OSError:
        pytest.skip(f"no {host}")
    port = srv.getsockname()[1]

    def serve() -> None:
        for _ in range(3):
            data, peer = srv.recvfrom(100)
            srv.sendto(data.upper(), peer)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    target = f"udp://{host}:{port}" if family == socket.AF_INET else f"udp://[{host}]:{port}"
    r = subprocess.run([str(_build(CLIENT)), target], capture_output=True, text=True, timeout=30)
    t.join(5)
    srv.close()
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["PING", "PONG", "X"]


SERVER = """
def rw_test_announce(ln: int) -> int:
    write(1, bytes_from_str(local_addr(ln) + "\\n"))
    return ln

def main() -> int:
    srv = rw_test_announce(listen("udp://127.0.0.1:0"))
    for i in range(0, 50):
        data, sender = recv_from(srv, 65536)
        send_to(srv, bytes_from_str(sender + " " + str(len(data))), sender)
    return 0
"""


def test_rw_server_many_python_clients():
    proc, port = start_announced([str(_build(SERVER))])
    try:
        socks = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(10)]
        for s in socks:
            s.settimeout(5)
        for round_ in range(5):
            for n, s in enumerate(socks):
                s.sendto(b"x" * (100 * n + round_), ("127.0.0.1", port))
            for n, s in enumerate(socks):
                reply = s.recv(200).decode()
                local = s.getsockname()
                assert reply == f"udp://127.0.0.1:{local[1]} {100 * n + round_}"
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
