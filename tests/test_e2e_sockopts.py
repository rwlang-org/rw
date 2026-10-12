"""shutdown / set_nodelay / set_keepalive / set_backlog (spec 56)."""

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


HALF_CLOSE = """
def main() -> int:
    c = dial(os_args()[1])
    print(set_nodelay(c, true))
    print(set_keepalive(c, true))
    write_all(c, bytes_from_str("request"))
    print(shutdown(c, "write"))
    # The peer saw EOF on its side but can still answer.
    print(str_from_bytes(read(c, 100)))
    print(len(read(c, 100)))
    print(last_error())
    close(c)
    return 0
"""


def test_half_close_then_read_reply():
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    got = []

    def peer() -> None:
        conn, _ = srv.accept()
        with conn:
            data = b""
            while chunk := conn.recv(100):  # until the client's FIN
                data += chunk
            got.append(data)
            conn.sendall(b"reply after EOF")

    t = threading.Thread(target=peer, daemon=True)
    t.start()
    r = _run(HALF_CLOSE, f"tcp://127.0.0.1:{port}")
    t.join(10)
    srv.close()
    assert r.returncode == 0, r.stderr
    assert got == [b"request"]
    assert r.stdout.split("\n")[:6] == ["0", "0", "0", "reply after EOF", "0", "0"]


ERRORS = """
def main() -> int:
    ln = listen("tcp://127.0.0.1:0")
    print(set_backlog(ln, 1024))
    print(set_backlog(ln, 0))
    print(error_name(last_error()))
    print(shutdown(ln, "sideways"))
    print(error_name(last_error()))
    close(ln)
    print(set_nodelay(ln, true))
    print(error_name(last_error()))
    print(shutdown(ln, "both"))
    print(error_name(last_error()))
    return 0
"""


def test_errors():
    r = _run(ERRORS)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["0", "-1", "EINVAL", "-1", "EINVAL", "-1", "EBADF", "-1", "EBADF"]
