"""End-to-end tests for the web framework (examples/web.rw + examples/web_app.rw)."""

from __future__ import annotations

import os
import re
import shutil
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
    """Build the app (and the framework next to it) on a free port."""
    port = _free_port()
    td = Path(tempfile.mkdtemp())
    shutil.copy(EXAMPLES / "web.rw", td / "web.rw")
    src = (EXAMPLES / "web_app.rw").read_text()
    src = re.sub(r"serve\(app, \d+\)", f"serve(app, {port})", src, count=1)
    # A short per-connection timeout so the slow-client tests run quickly.
    src = src.replace(f"return web.serve(app, {port})",
                      f"app.timeout_ms = 300\n    return web.serve(app, {port})", 1)
    assert "app.timeout_ms = 300" in src
    (td / "web_app.rw").write_text(src)
    out = td / "web_app"
    build = subprocess.run(
        [sys.executable, "-m", "rwc.cli", "build", str(td / "web_app.rw"), "-o", str(out)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert build.returncode == 0, f"rwc build failed:\n{build.stderr}"
    proc = subprocess.Popen(
        [str(out)],
        env={**os.environ, "RW_WORKERS": "2"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    wait_for_port(port, proc)
    try:
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=2.0)
        shutil.rmtree(td, ignore_errors=True)


def _request(port: int, *chunks: bytes) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=3.0)
    try:
        for i, chunk in enumerate(chunks):
            if i:
                time.sleep(0.05)
            s.sendall(chunk)
        data = b""
        while part := s.recv(65536):
            data += part
        return data
    finally:
        s.close()


def _parse(resp: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = resp.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:])}
    return status, headers, body


def _call(port: int, method: str, target: str, body: bytes = b"", extra: str = "") -> tuple[int, dict[str, str], bytes]:
    head = f"{method} {target} HTTP/1.1\r\nHost: x\r\n{extra}"
    if body:
        head += f"Content-Length: {len(body)}\r\n"
    return _parse(_request(port, head.encode() + b"\r\n" + body))


@pytest.mark.parametrize(
    "target, status, ctype, body",
    [
        ("/", 200, "text/html; charset=utf-8", b"<h1>Hello from rw</h1>\n"),
        ("/hello/rw", 200, "text/plain; charset=utf-8", b"hello, rw\n"),
        ("/users/7/posts/42", 200, "text/plain; charset=utf-8", b"user 7 post 42\n"),
        ("/search?q=rw&page=2", 200, "application/json", b'{"q": "rw", "page": "2"}\n'),
        ("/search", 200, "application/json", b'{"q": "", "page": ""}\n'),
        ("/nope", 404, "text/html; charset=utf-8", b"<p>no page at /nope</p>\n"),
        ("/hello/", 404, "text/html; charset=utf-8", b"<p>no page at /hello/</p>\n"),
    ],
)
def test_get_routes(server, target, status, ctype, body):
    got_status, headers, got_body = _call(server, "GET", target)
    assert got_status == status
    assert headers["content-type"] == ctype
    assert headers["content-length"] == str(len(body))
    assert got_body == body


def test_header_lookup_is_case_insensitive(server):
    _, _, body = _call(server, "GET", "/agent", extra="uSeR-AgEnT:   rw-test/1.0  \r\n")
    assert body == b"rw-test/1.0\n"


def test_post_body_and_custom_header(server):
    status, headers, body = _call(server, "POST", "/echo", body=b"ping body")
    assert status == 201
    assert body == b"ping body"
    assert headers["x-echo-length"] == "9"


def test_post_body_split_across_reads(server):
    resp = _request(
        server,
        b"POST /echo HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello",
        b" world",
    )
    status, _, body = _parse(resp)
    assert status == 201
    assert body == b"hello world"


def test_redirect(server):
    status, headers, body = _call(server, "GET", "/old")
    assert status == 302
    assert headers["location"] == "/"
    assert body == b""


def test_wrong_method_is_405(server):
    status, _, _ = _call(server, "POST", "/")
    assert status == 405


def test_malformed_request_is_400(server):
    status, _, _ = _parse(_request(server, b"NONSENSE\r\n\r\n"))
    assert status == 400


def test_slow_partial_head_is_408(server):
    with socket.create_connection(("127.0.0.1", server), timeout=10) as s:
        s.sendall(b"GET /hello/slow HTTP/1.1\r\nHost: x\r\n")  # head never finished
        t0 = time.monotonic()
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        took = time.monotonic() - t0
    status, _, _ = _parse(data)
    assert status == 408
    assert 0.25 <= took < 5


def test_idle_connection_is_closed(server):
    with socket.create_connection(("127.0.0.1", server), timeout=10) as s:
        t0 = time.monotonic()
        assert s.recv(4096) == b""
        assert time.monotonic() - t0 < 5


def test_oversized_head_is_413(server):
    # Just over the 64 KiB limit and never terminated. The server reads all
    # of it before answering, so closing does not reset the connection.
    head = b"GET / HTTP/1.1\r\nX: " + b"a" * (66000 - 20)
    status, _, _ = _parse(_request(server, head))
    assert status == 413


def test_413_with_unread_data_is_not_reset(server):
    # The server stops reading at 64 KiB but the client keeps sending; the
    # lingering close must still deliver the response instead of an RST.
    head = b"GET / HTTP/1.1\r\nX: " + b"a" * 200000 + b"\r\n\r\n"
    status, _, _ = _parse(_request(server, head))
    assert status == 413


def test_oversized_body_is_413(server):
    status, _, _ = _parse(_request(server, b"POST /echo HTTP/1.1\r\nContent-Length: 2000000\r\n\r\n"))
    assert status == 413


def test_large_response_is_written_completely(server):
    big = b"x" * 300_000
    status, headers, body = _call(server, "POST", "/echo", body=big)
    assert status == 201
    assert headers["content-length"] == str(len(big))
    assert body == big


def test_concurrent_requests(server):
    socks = [socket.create_connection(("127.0.0.1", server), timeout=3.0) for _ in range(20)]
    try:
        for i, s in enumerate(socks):
            s.sendall(f"GET /hello/c{i} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        for i, s in enumerate(socks):
            data = b""
            while part := s.recv(4096):
                data += part
            assert _parse(data)[2] == f"hello, c{i}\n".encode()
    finally:
        for s in socks:
            s.close()
