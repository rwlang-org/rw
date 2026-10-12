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

from ._net import listen_on_port_zero, start_announced

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


@pytest.fixture(scope="module")
def server():
    """Build the app (and the framework next to it) on port 0."""
    td = Path(tempfile.mkdtemp())
    # web.serve(app, port) calls tcp_listen(port); listen on port 0 instead.
    (td / "web.rw").write_text(listen_on_port_zero((EXAMPLES / "web.rw").read_text()))
    src = (EXAMPLES / "web_app.rw").read_text()
    # A short per-connection timeout so the slow-client tests run quickly.
    src = src.replace("return web.serve(app, 8080)",
                      "app.timeout_ms = 300\n    app.keepalive_ms = 600\n    return web.serve(app, 8080)", 1)
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
    # Files for the /static mount, with a secret next to (not inside) the root.
    public = td / "public"
    (public / "css").mkdir(parents=True)
    (public / "sub").mkdir()
    (public / "index.html").write_text("<h1>home</h1>\n")
    (public / "css" / "a.css").write_text("h1{}\n")
    (public / "sub" / "index.html").write_text("sub index\n")
    (td / "secret.txt").write_text("do not serve\n")
    proc, port = start_announced([str(out)], env={**os.environ, "RW_WORKERS": "2", "WEB_PUBLIC": str(public)})
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
    head = f"{method} {target} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{extra}"
    if body:
        head += f"Content-Length: {len(body)}\r\n"
    return _parse(_request(port, head.encode() + b"\r\n" + body))


@pytest.mark.parametrize(
    "target, status, ctype, body",
    [
        ("/", 200, "text/html; charset=utf-8", b"<h1>Hello from rw</h1>\n"),
        ("/hello/rw", 200, "text/plain; charset=utf-8", b"hello, rw\n"),
        ("/users/7/posts/42", 200, "text/plain; charset=utf-8", b"user 7 post 42\n"),
        ("/search?q=rw&page=2", 200, "application/json", b'{"q":"rw","page":"2"}\n'),
        ("/search", 200, "application/json", b'{"q":"","page":""}\n'),
        # A raw quote in the query is escaped in the JSON body.
        ('/search?q=a"b', 200, "application/json", b'{"q":"a\\"b","page":""}\n'),
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


def test_percent_decoding(server):
    status, _, body = _call(server, "GET", "/hello/caf%C3%A9%20au%2Blait")
    assert (status, body) == (200, "hello, café au+lait\n".encode())
    status, _, body = _call(server, "GET", "/search?q=a+b%26c&page=%32")
    assert (status, body) == (200, b'{"q":"a b&c","page":"2"}\n')


def test_form_post_sets_cookie_and_cookie_is_read_back(server):
    status, headers, body = _call(server, "POST", "/login", b"user=J%C3%BCrgen+M&x=1",
                                  "Content-Type: application/x-www-form-urlencoded\r\n")
    assert (status, body) == (200, "welcome, Jürgen M\n".encode())
    assert headers["set-cookie"] == "user=J%C3%BCrgen%20M; Path=/; HttpOnly; SameSite=Lax"
    cookie = headers["set-cookie"].split(";")[0]
    status, _, body = _call(server, "GET", "/whoami", extra=f"Cookie: theme=dark; {cookie}\r\n")
    assert (status, body) == (200, "you are Jürgen M\n".encode())
    status, _, _ = _call(server, "GET", "/whoami")
    assert status == 401


def test_form_ignores_other_content_types(server):
    status, _, body = _call(server, "POST", "/login", b"user=x", "Content-Type: text/plain\r\n")
    assert (status, body) == (400, b"user required\n")


def test_static_files(server):
    status, headers, body = _call(server, "GET", "/static/index.html")
    assert (status, body) == (200, b"<h1>home</h1>\n")
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert headers["x-powered-by"] == "rw"  # after hooks see static responses too
    assert re.fullmatch(r"\w{3}, \d\d \w{3} \d{4} \d\d:\d\d:\d\d GMT", headers["last-modified"])
    status, headers, body = _call(server, "GET", "/static/css/a.css")
    assert (status, headers["content-type"], body) == (200, "text/css; charset=utf-8", b"h1{}\n")
    assert _call(server, "GET", "/static/")[2] == b"<h1>home</h1>\n"
    assert _call(server, "GET", "/static/sub")[2] == b"sub index\n"


def test_static_not_modified(server):
    _, headers, _ = _call(server, "GET", "/static/css/a.css")
    status, headers2, body = _call(server, "GET", "/static/css/a.css",
                                   extra=f"If-Modified-Since: {headers['last-modified']}\r\n")
    assert (status, body) == (304, b"")
    assert headers2["last-modified"] == headers["last-modified"]


@pytest.mark.parametrize("target", [
    "/static/../secret.txt", "/static/%2e%2e/secret.txt", "/static/..%2Fsecret.txt",
    "/static/sub/../../secret.txt", "/static//etc/passwd", "/static/missing.txt",
])
def test_static_never_leaves_root(server, target):
    status, _, body = _call(server, "GET", target)
    assert status == 404
    assert b"do not serve" not in body


def test_head_has_headers_but_no_body(server):
    resp = _request(server, b"HEAD /static/index.html HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    status, headers, body = _parse(resp)
    assert (status, body) == (200, b"")
    assert headers["content-length"] == str(len(b"<h1>home</h1>\n"))
    status, headers, body = _parse(_request(server, b"HEAD /hello/x HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"))
    assert (status, body, headers["content-length"]) == (200, b"", str(len(b"hello, x\n")))


def test_before_hook_can_answer(server):
    assert _call(server, "GET", "/admin")[:3:2] == (403, b"forbidden\n")
    status, headers, body = _call(server, "GET", "/admin", extra="X-Token: secret\r\n")
    assert (status, body, headers["x-powered-by"]) == (200, b"admin area\n", "rw")


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
            s.sendall(f"GET /hello/c{i} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        for i, s in enumerate(socks):
            data = b""
            while part := s.recv(4096):
                data += part
            assert _parse(data)[2] == f"hello, c{i}\n".encode()
    finally:
        for s in socks:
            s.close()


# ---------- keep-alive (spec 64) ----------

def _read_response(f) -> tuple[int, dict[str, str], bytes]:
    """One response from a socket file, using Content-Length."""
    status_line = f.readline()
    if not status_line:
        raise EOFError
    headers = {}
    while (line := f.readline()) not in (b"\r\n", b""):
        k, v = line.decode().rstrip("\r\n").split(": ", 1)
        headers[k.lower()] = v
    body = f.read(int(headers.get("content-length", "0")))
    return int(status_line.split()[1]), headers, body


def test_keep_alive_serves_several_requests(server):
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        f = s.makefile("rb")
        for name in ["a", "b", "c"]:
            s.sendall(f"GET /hello/{name} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            status, headers, body = _read_response(f)
            assert (status, body, headers["connection"]) == (200, f"hello, {name}\n".encode(), "keep-alive")
        s.sendall(b"GET /hello/last HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        status, headers, body = _read_response(f)
        assert (body, headers["connection"]) == (b"hello, last\n", "close")
        assert f.read() == b""  # the server closed


def test_pipelined_requests(server):
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall(b"GET /hello/p1 HTTP/1.1\r\nHost: x\r\n\r\n"
                  b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc"
                  b"GET /hello/p3 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        f = s.makefile("rb")
        got = [_read_response(f)[::2] for _ in range(3)]
    assert got == [(200, b"hello, p1\n"), (201, b"abc"), (200, b"hello, p3\n")]


def test_http10_closes_unless_asked(server):
    status, headers, body = _parse(_request(server, b"GET /hello/old HTTP/1.0\r\n\r\n"))
    assert (status, headers["connection"]) == (200, "close")
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        f = s.makefile("rb")
        s.sendall(b"GET /hello/k HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        assert _read_response(f)[1]["connection"] == "keep-alive"


def test_idle_keep_alive_connection_is_closed(server):
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        f = s.makefile("rb")
        s.sendall(b"GET /hello/idle HTTP/1.1\r\nHost: x\r\n\r\n")
        _read_response(f)
        t0 = time.monotonic()
        assert f.read() == b""  # closed after keepalive_ms (600 ms in this fixture)
        assert 0.4 < time.monotonic() - t0 < 4


def test_bad_version_is_400(server):
    assert _parse(_request(server, b"GET / HTTP/2.0\r\n\r\n"))[0] == 400



# ---------- chunked request bodies (spec 65) ----------

def test_chunked_body_then_keep_alive(server):
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall(b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                  b"5;ext=1\r\nhello\r\n"
                  b"7\r\n, world\r\n"
                  b"0\r\nX-Trailer: t\r\n\r\n"
                  b"GET /hello/next HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        f = s.makefile("rb")
        status, headers, body = _read_response(f)
        assert (status, body, headers["x-echo-length"]) == (201, b"hello, world", "12")
        assert headers["connection"] == "keep-alive"
        assert _read_response(f)[2] == b"hello, next\n"


def test_chunked_split_across_reads(server):
    status, _, body = _parse(_request(server, b"POST /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
                                              b"Transfer-Encoding: chunked\r\n\r\n3\r\nab",
                                      b"c\r\n", b"0\r\n\r\n"))
    assert (status, body) == (201, b"abc")


@pytest.mark.parametrize("raw, status", [
    (b"Transfer-Encoding: chunked\r\n\r\nzz\r\nabc\r\n0\r\n\r\n", 400),       # bad size
    (b"Transfer-Encoding: chunked\r\n\r\n3\r\nabcX\r\n0\r\n\r\n", 400),       # no CRLF after data
    (b"Transfer-Encoding: chunked\r\n\r\n200000\r\n", 413),                    # over 1 MiB
    (b"Transfer-Encoding: gzip\r\n\r\n", 501),
    (b"Content-Length: abc\r\n\r\n", 400),
])
def test_bad_bodies(server, raw, status):
    resp = _request(server, b"POST /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n" + raw)
    assert _parse(resp)[0] == status


def test_template_page(server):
    status, headers, body = _call(server, "GET", "/page/%3Cann%3E?tags=a,b%26c")
    assert (status, headers["content-type"]) == (200, "text/html; charset=utf-8")
    assert body == b"<h1>Hello, &lt;ann&gt;</h1><span>a</span><span>b&amp;c</span>\n"
    assert _call(server, "GET", "/page/bob")[2] == b"<h1>Hello, bob</h1><p>no tags</p>\n"

