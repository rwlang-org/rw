"""End-to-end tests for the TCP echo example."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from ._net import listen_on_port_zero, start_announced

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _build_echo_server() -> Path:
    """Materialise examples/tcp_echo.rw listening on port 0 and build it.
    Returns the path to the built binary."""
    src = listen_on_port_zero((EXAMPLES / "tcp_echo.rw").read_text())
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


def _start_server(binary: Path) -> tuple[subprocess.Popen, int]:
    return start_announced([str(binary)], env={**os.environ, "RW_WORKERS": "2"})


def test_echo_single_connection():
    binary = _build_echo_server()
    proc, port = _start_server(binary)
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
    binary = _build_echo_server()
    proc, port = _start_server(binary)
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
