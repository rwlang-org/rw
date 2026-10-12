"""signal_wait, close waking parked fibers, and web.rw's graceful shutdown
(spec 57)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ._net import listen_on_port_zero, start_announced

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _build(files: dict[str, str], entry: str) -> Path:
    td = Path(tempfile.mkdtemp())
    for name, src in files.items():
        (td / name).write_text(src)
    exe = td / "prog"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(td / entry), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    return exe


CLOSE_WAKES = """
def acceptor(ln: int) -> int:
    c = accept(ln)
    if c < 0 and error_name(last_error()) == "EBADF":
        return 1
    return 0

def main() -> int:
    ln = listen("tcp://127.0.0.1:0")
    fs: List[Future[int]] = []
    for i in range(0, 4):
        fs = list_push(fs, spawn acceptor(ln))
    sleep(100)
    close(ln)
    total = 0
    for r in await_all(fs):
        total += r
    print(total)
    # Waiting on a closed handle fails at once.
    print(accept(ln))
    print(error_name(last_error()))
    return 0
"""


def test_close_wakes_parked_acceptors():
    exe = _build({"p.rw": CLOSE_WAKES}, "p.rw")
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["4", "-1", "EBADF"]


PENDING = """
def main() -> int:
    # The first wait installs handlers for both; signals sent while nobody waits
    # stay pending and are returned by later waits, one each.
    write(1, bytes_from_str("ready\\n"))
    print(signal_wait(["SIGUSR1", "SIGUSR2"]))
    sleep(300)
    print(signal_wait(["SIGUSR1", "SIGUSR2"]))
    print(signal_wait(["SIGUSR2", "SIGUSR1"]))
    print(signal_wait(["NOT_A_SIGNAL"]) == "")
    return 0
"""


def test_signal_wait_queues_signals():
    exe = _build({"p.rw": PENDING}, "p.rw")
    proc = subprocess.Popen([str(exe)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(0.2)  # let signal_wait install the handler
        proc.send_signal(signal.SIGUSR1)
        time.sleep(0.1)  # during the sleep(300): both become pending
        proc.send_signal(signal.SIGUSR2)
        proc.send_signal(signal.SIGUSR1)
        out, err = proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    lines = out.split()
    assert lines[0] == "SIGUSR1"
    assert sorted(lines[1:3]) == ["SIGUSR1", "SIGUSR2"]
    assert lines[3] == "true"


@pytest.fixture
def web_server():
    web = listen_on_port_zero((EXAMPLES / "web.rw").read_text())
    app = (EXAMPLES / "web_app.rw").read_text()
    app = app.replace("return web.serve(app, 8080)", "app.timeout_ms = 5000\n    return web.serve(app, 8080)", 1)
    exe = _build({"web.rw": web, "web_app.rw": app}, "web_app.rw")
    proc, port = start_announced([str(exe)], env={**os.environ, "RW_WORKERS": "2"})
    yield proc, port
    proc.kill()


def test_sigterm_finishes_inflight_request_then_exits(web_server):
    proc, port = web_server
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(b"GET /hello/inflight HTTP/1.1\r\n")  # request in progress
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        # New connections are refused once the listener is closed.
        deadline = time.monotonic() + 5
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
            except ConnectionRefusedError:
                break
            assert time.monotonic() < deadline, "listener still open after SIGTERM"
            time.sleep(0.05)
        assert proc.poll() is None, "exited before the request in progress finished"
        s.sendall(b"Host: x\r\n\r\n")
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    assert data.startswith(b"HTTP/1.1 200")
    assert data.endswith(b"hello, inflight\n")
    assert proc.wait(timeout=10) == 0


def test_sigint_idle_server_exits_cleanly(web_server):
    proc, _ = web_server
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=10) == 0
