"""dial / listen / accept / local_addr / peer_addr against Python peers (spec 50)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _build(src: str) -> Path:
    td = Path(tempfile.mkdtemp())
    rw = td / "p.rw"
    rw.write_text(src)
    exe = td / "p"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(rw), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    return exe


# Dial the address in argv[1], send "hello", print the reply.
CLIENT = """
def main() -> int:
    args = os_args()
    c = dial(args[1])
    if c < 0:
        print("dial failed")
        return 1
    write(c, bytes_from_str("hello"))
    print(str_from_bytes(read(c, 100)))
    close(c)
    return 0
"""

# The same client, but dialing from a spawned fiber (nonblocking connect +
# netpoller park instead of a blocking connect on the main thread).
FIBER_CLIENT = """
def talk(addr: string) -> string:
    c = dial(addr)
    if c < 0:
        return "dial failed"
    write(c, bytes_from_str("hello"))
    reply = str_from_bytes(read(c, 100))
    close(c)
    return reply

def main() -> int:
    args = os_args()
    f = spawn talk(args[1])
    reply = await f
    print(reply)
    if reply == "dial failed":
        return 1
    return 0
"""

# Listen on argv[1], report the bound address, echo one connection. Output
# goes through write(1, ...) because print is block-buffered on a pipe and
# the test needs the address before the program exits.
SERVER = """
def say(s: string) -> void:
    write(1, bytes_from_str(s + "\\n"))

def main() -> int:
    args = os_args()
    ln = listen(args[1])
    if ln < 0:
        say("listen failed")
        return 1
    say(local_addr(ln))
    c = accept(ln)
    say(peer_addr(c))
    b = read(c, 100)
    write(c, b)
    close(c)
    return 0
"""


def _echo_once(srv: socket.socket) -> threading.Thread:
    def run() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.sendall(conn.recv(100).upper())
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


@pytest.fixture(scope="module")
def client() -> Path:
    return _build(CLIENT)


@pytest.fixture(scope="module", params=["main", "fiber"])
def any_client(request, client) -> Path:
    return client if request.param == "main" else _build(FIBER_CLIENT)


@pytest.fixture(scope="module")
def server() -> Path:
    return _build(SERVER)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_dial_tcp(any_client, host):
    client = any_client
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    t = _echo_once(srv)
    r = subprocess.run([str(client), f"tcp://{host}:{port}"], capture_output=True, text=True, timeout=30)
    t.join(5)
    srv.close()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "HELLO"


def test_dial_refused(any_client):
    client = any_client
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = subprocess.run([str(client), f"tcp://127.0.0.1:{port}"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert r.stdout.strip() == "dial failed"


@pytest.mark.parametrize("addr", ["127.0.0.1:80", "tcp://127.0.0.1", "tcp://::1:80", "unix://relative"])
def test_dial_malformed(client, addr):
    r = subprocess.run([str(client), addr], capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == "dial failed"


def test_dial_ipv6(client):
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    try:
        srv = socket.create_server(("::1", 0), family=socket.AF_INET6)
    except OSError:
        pytest.skip("no IPv6 loopback")
    port = srv.getsockname()[1]
    t = _echo_once(srv)
    r = subprocess.run([str(client), f"tcp://[::1]:{port}"], capture_output=True, text=True, timeout=30)
    t.join(5)
    srv.close()
    assert r.stdout.strip() == "HELLO", r.stderr


def test_dial_unix(any_client):
    client = any_client
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        path = os.path.join(td, "s.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        t = _echo_once(srv)
        r = subprocess.run([str(client), f"unix://{path}"], capture_output=True, text=True, timeout=30)
        t.join(5)
        srv.close()
    assert r.stdout.strip() == "HELLO", r.stderr


def _run_server(server: Path, addr: str):
    proc = subprocess.Popen([str(server), addr], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timer = threading.Timer(30, proc.kill)
    timer.daemon = True
    timer.start()
    first = proc.stdout.readline().strip()
    return proc, first


def test_listen_port_zero_and_peer_addr(server):
    proc, bound = _run_server(server, "tcp://127.0.0.1:0")
    try:
        assert bound.startswith("tcp://127.0.0.1:"), bound
        port = int(bound.rsplit(":", 1)[1])
        assert port > 0
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(b"ping")
            assert c.recv(100) == b"ping"
            local_port = c.getsockname()[1]
        out, _ = proc.communicate(timeout=10)
        assert out.strip() == f"tcp://127.0.0.1:{local_port}"
        assert proc.returncode == 0
    finally:
        proc.kill()


def test_listen_unix(server):
    with tempfile.TemporaryDirectory(dir="/tmp") as td:
        path = os.path.join(td, "l.sock")
        proc, bound = _run_server(server, f"unix://{path}")
        try:
            assert bound == f"unix://{path}"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.settimeout(5)
                c.connect(path)
                c.sendall(b"pong")
                assert c.recv(100) == b"pong"
            proc.communicate(timeout=10)
            assert proc.returncode == 0
        finally:
            proc.kill()


def test_listen_bad_address(server):
    r = subprocess.run([str(server), "tcp://127.0.0.1:99999"], capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == "listen failed"
