"""End-to-end tests for the TCP echo example."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_echo_server(port: int) -> Path:
    """Materialise examples/tcp_echo.rw with the desired port and
    build it. Returns the path to the built binary."""
    src = (EXAMPLES / "tcp_echo.rw").read_text()
    src = re.sub(r"tcp_listen\(\d+\)", f"tcp_listen({port})", src, count=1)
    td = tempfile.mkdtemp()
    rw_path = Path(td) / "tcp_echo_test.rw"
    rw_path.write_text(src)
    out = Path(td) / "tcp_echo_test"
    build = subprocess.run(
        [sys.executable, "-m", "rwc.cli", "build", str(rw_path), "-o", str(out)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert build.returncode == 0, f"rwc build failed:\n{build.stderr}"
    return out


def _start_server(binary: Path) -> subprocess.Popen:
    env = {**os.environ, "RW_WORKERS": "2"}
    proc = subprocess.Popen(
        [str(binary)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.5)  # give the server time to bind+listen
    # If the server already died, surface its stderr.
    if proc.poll() is not None:
        _, err = proc.communicate(timeout=1.0)
        raise AssertionError(
            f"server exited early (rc={proc.returncode}): {err!r}")
    return proc


def test_echo_single_connection():
    port = _free_port()
    binary = _build_echo_server(port)
    proc = _start_server(binary)
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        s.sendall(b"hello\n")
        data = s.recv(64)
        assert data == b"hello\n"
        s.close()
    finally:
        proc.terminate()
        proc.wait(timeout=2.0)


def test_echo_ten_concurrent_connections():
    port = _free_port()
    binary = _build_echo_server(port)
    proc = _start_server(binary)
    try:
        socks = [socket.create_connection(("127.0.0.1", port), timeout=2.0)
                 for _ in range(10)]
        for i, s in enumerate(socks):
            s.sendall(f"client-{i}\n".encode())
        for i, s in enumerate(socks):
            data = s.recv(64)
            assert data == f"client-{i}\n".encode(), f"client {i}: {data!r}"
        for s in socks:
            s.close()
    finally:
        proc.terminate()
        proc.wait(timeout=2.0)
