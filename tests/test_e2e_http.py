"""End-to-end tests for the HTTP hello example (spec 18)."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ._net import wait_for_port

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def server():
    """Build examples/http_hello.rw on a free port and run it for the module."""
    port = _free_port()
    src = (EXAMPLES / "http_hello.rw").read_text()
    src = re.sub(r"tcp_listen\(\d+\)", f"tcp_listen({port})", src, count=1)
    td = tempfile.mkdtemp()
    rw_path = Path(td) / "http_hello_test.rw"
    rw_path.write_text(src)
    out = Path(td) / "http_hello_test"
    build = subprocess.run(
        [sys.executable, "-m", "rwc.cli", "build", str(rw_path), "-o", str(out)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert build.returncode == 0, f"rwc build failed:\n{build.stderr}"
    proc = subprocess.Popen(
        [str(out)],
        env={**os.environ, "RW_WORKERS": "2"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for_port(port, proc)
    try:
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=2.0)


def _request(port: int, *chunks: bytes) -> bytes:
    """Send the chunks (with a short pause between them) and read until close."""
    s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        for i, chunk in enumerate(chunks):
            if i:
                time.sleep(0.05)
            s.sendall(chunk)
        data = b""
        while True:
            part = s.recv(4096)
            if not part:
                return data
            data += part
    finally:
        s.close()


def _parse(resp: bytes) -> tuple[str, dict[str, str], bytes]:
    head, _, body = resp.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return lines[0], headers, body


def _get(port: int, path: str) -> tuple[str, dict[str, str], bytes]:
    return _parse(_request(port, f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode()))


@pytest.mark.parametrize(
    "path, status, body",
    [
        ("/", "HTTP/1.1 200 OK", b"Welcome to rw\n"),
        ("/hello", "HTTP/1.1 200 OK", b"hello, world\n"),
        ("/echo/abc", "HTTP/1.1 200 OK", b"abc\n"),
        ("/nope", "HTTP/1.1 404 Not Found", b"not found: /nope\n"),
    ],
)
def test_routes(server, path, status, body):
    line, headers, got = _get(server, path)
    assert line == status
    assert got == body
    assert headers["Content-Length"] == str(len(body))
    assert headers["Content-Type"] == "text/plain"


def test_non_get_is_405(server):
    line, _, body = _parse(_request(server, b"POST / HTTP/1.1\r\nHost: x\r\n\r\n"))
    assert line == "HTTP/1.1 405 Method Not Allowed"
    assert body == b"method not allowed\n"


def test_malformed_request_line_is_400(server):
    line, _, _ = _parse(_request(server, b"GARBAGE\r\n\r\n"))
    assert line == "HTTP/1.1 400 Bad Request"


def test_request_split_across_reads(server):
    resp = _request(server, b"GET /hel", b"lo HTTP/1.1\r\nHost:", b" x\r\n\r\n")
    line, _, body = _parse(resp)
    assert line == "HTTP/1.1 200 OK"
    assert body == b"hello, world\n"


def test_ten_concurrent_requests(server):
    socks = [socket.create_connection(("127.0.0.1", server), timeout=2.0) for _ in range(10)]
    try:
        for i, s in enumerate(socks):
            s.sendall(f"GET /echo/c{i} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        for i, s in enumerate(socks):
            data = b""
            while part := s.recv(4096):
                data += part
            _, _, body = _parse(data)
            assert body == f"c{i}\n".encode()
    finally:
        for s in socks:
            s.close()
