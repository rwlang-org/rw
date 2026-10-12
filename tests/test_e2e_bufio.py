"""stdlib/bufio.rw: Reader over a socket that delivers one byte at a time,
and the line-based chat example (spec 54)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _build(src: str) -> Path:
    td = Path(tempfile.mkdtemp())
    rw = td / "p.rw"
    rw.write_text(src)
    exe = td / "p"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(rw), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    return exe


READER = """
import bufio

def show(o: Option[string]) -> void:
    match o:
        case Some(s):
            print("[" + s + "]")
        case None:
            print("None")

def main() -> int:
    c = dial(os_args()[1])
    r = bufio.reader(c)
    show(r.read_line())
    show(r.read_line())
    print("[" + r.read_until("END") + "]")
    print("[" + str_from_bytes(r.read_exact(5)) + "]")
    print("[" + r.read_until("@@", 4) + "]")
    show(r.read_line())
    show(r.read_line())
    print(len(r.read_exact(3)))
    print(r.err)
    return 0
"""

PAYLOAD = b"GET / HTTP/1.1\r\nsecond line\nabcENDhelloXYZWrest of it"
EXPECTED = [
    "[GET / HTTP/1.1]",
    "[second line]",
    "[abcEND]",
    "[hello]",
    "[XYZW]",  # limit 4 reached before any "@@"
    "[rest of it]",  # last line without a newline
    "None",
    "0",
    "0",
]


def test_reader_one_byte_at_a_time():
    exe = _build(READER)
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def dribble() -> None:
        conn, _ = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with conn:
            for b in PAYLOAD:
                conn.sendall(bytes([b]))
                time.sleep(0.002)

    t = threading.Thread(target=dribble, daemon=True)
    t.start()
    r = subprocess.run([str(exe), f"tcp://127.0.0.1:{port}"], capture_output=True, text=True, timeout=60)
    t.join(10)
    srv.close()
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == EXPECTED


def test_tcp_chat_example():
    port_sock = socket.socket()
    port_sock.bind(("127.0.0.1", 0))
    port = port_sock.getsockname()[1]
    port_sock.close()
    td = Path(tempfile.mkdtemp())
    src = (EXAMPLES / "tcp_chat.rw").read_text().replace("tcp_listen(8080)", f"tcp_listen({port})")
    (td / "chat.rw").write_text(src)
    exe = td / "chat"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(td / "chat.rw"), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    proc = subprocess.Popen([str(exe)], env={**os.environ, "RW_WORKERS": "2"},
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        from ._net import wait_for_port
        wait_for_port(port, proc)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as c:
            f = c.makefile("rwb", buffering=0)
            c.sendall(b"hel")
            time.sleep(0.05)
            c.sendall(b"lo\r\nsecond\nquit\n")
            data = b""
            while chunk := c.recv(4096):
                data += chunk
        text = data.decode()
        assert "you said: hello\n" in text
        assert "you said: second\n" in text
        assert text.endswith("bye\n")
        del f
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(td, ignore_errors=True)
