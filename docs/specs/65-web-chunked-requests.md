# 65. `web.rw`: chunked request bodies

## Motivation

Clients that do not know a body's length up front (`curl -T -`, streaming
uploads, many HTTP libraries) send `Transfer-Encoding: chunked`. The
framework ignored the header, which left the chunk framing in the stream.
Spec 64 therefore had to close such connections.

## Parsing

`read_request` checks `Transfer-Encoding` before `Content-Length` (RFC 9112
§6.3: if both are present, `Transfer-Encoding` wins):

- **`chunked`**: `read_chunked` reads the chunks.
  - Each size line is hex, at most 15 digits; chunk extensions after `;` are
    ignored.
  - Each chunk's data is followed by CRLF, and a `0` chunk ends the body.
  - Trailer lines up to an empty line are read and discarded.
  - The body limit (1 MiB) applies to the decoded size. The trailers count
    against the 64 KiB head limit.
- **Any other transfer coding** (`gzip`, `chunked` stacked on another, …):
  `501 Not Implemented`, and the connection closes.
- **`Content-Length`**: a value that is not a non-negative integer is now a
  400 (it used to be treated as "no body").

A malformed chunk (bad size, missing CRLF, early EOF) is a 400. Because the
whole body is consumed, the connection can stay alive for the next request
(spec 64). `wants_keep_alive` no longer closes on `Transfer-Encoding`.

`reason()` gains `408 Request Timeout` and `501 Not Implemented`.

## Tests

`tests/test_e2e_web.py`:
- a chunked body with extensions and a trailer, followed by a pipelined
  request on the same connection
- a chunk split across TCP sends
- bad size, missing CRLF, an oversized chunk (413), `gzip` (501), and an
  invalid `Content-Length`
