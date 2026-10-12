# 64. `web.rw`: persistent connections

## Motivation

Every response closed its connection, so each request paid for a new TCP
handshake (and, later, a TLS handshake). Browsers and HTTP clients expect
HTTP/1.1 connections to persist.

## Behaviour

`serve_conn` keeps one `bufio.Reader` (spec 54) for the whole connection and
loops: read a request, respond, decide whether to continue. Bytes past one
request stay in the reader, so pipelined requests work without extra code.

The connection stays open when all of these hold:
- the request parsed (no 400 / 408 / 413);
- `app.keepalive_ms > 0` (default 5000);
- fewer than 1000 requests have been served on it;
- the server is not shutting down;
- the client allows it: HTTP/1.1 without `Connection: close`, or HTTP/1.0
  with `Connection: keep-alive`;
- the request has no `Transfer-Encoding` header. Chunked bodies are not
  parsed, so the next request's position would be unknown.

The response says `Connection: keep-alive` or `Connection: close` to match.
A request line whose version is neither `HTTP/1.1` nor `HTTP/1.0` is a 400.
`Request` gains a `version` field.

### Idle connections

Between requests the server waits up to `app.keepalive_ms` for the next
request's first byte. It waits in 200 ms read-timeout slices (spec 53):
after a slice times out it clears the reader's EOF mark and checks the
shutdown flag before waiting again. When the request starts, the normal
per-request timeout (`app.timeout_ms`) applies to the rest of it.

### Graceful shutdown

`stop_on_signal` now also sets `Inflight.stopping`. A connection that is
answering a request finishes it with `Connection: close`. An idle one notices
the flag within a slice and closes. Shutdown therefore no longer waits for
idle clients.

## Tests

- `tests/test_e2e_web.py`:
  - three requests on one connection, then `Connection: close`
  - three pipelined requests, one with a body
  - HTTP/1.0 with and without `keep-alive`
  - an idle connection closed after `keepalive_ms`
  - an unknown HTTP version gets 400
- `tests/test_e2e_shutdown.py`: SIGTERM closes an idle keep-alive connection
  promptly and the server exits 0.

The existing tests now send `Connection: close` when they read a response to
EOF.
