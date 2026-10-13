# 54. Standard library search path and `bufio`

Issue: #164

## Motivation

`read(fd, max)` returns whatever has arrived. Protocol code (HTTP heads, line
protocols, length-prefixed bodies) then has to find boundaries itself and
keep the bytes it read too far. `examples/web.rw` did this by hand. These
helpers are plain rw code, so they belong in a standard library written in
rw rather than in the C runtime. Until now there was nowhere to put one.

## Standard library search path

`import foo` resolves, in order:

1. `foo.rw` in the entry file's directory (spec 17). A local module can
   shadow a stdlib one.
2. `foo.rw` in the bundled standard library: `stdlib/` at the repository root,
   next to `runtime/`. The environment variable `RW_STDLIB` overrides the
   directory.

If neither exists, the error names both paths. Stdlib modules are ordinary rw
modules: they compile with the program, may import each other, and must not
define `main`.

## `bufio`

```python
import bufio

r = bufio.reader(fd)
match r.read_line():
    case Some(line):
        ...
    case None:
        ...           # EOF; r.err is last_error() at that point (0 = clean EOF)
```

`bufio.reader(fd) -> bufio.Reader` wraps any handle. Bytes read past what a
call returns stay in the reader, so the calls can be mixed on one handle. Do
not also call `read` on that fd directly.

| Method | Returns |
| --- | --- |
| `read_line()` | `Option[string]`: the next line without `\n` / `\r\n`. A final line with no newline is still returned. `None` at EOF |
| `read_until(delim, limit = 0)` | `string`: up to and including `delim`. At EOF, whatever is left (`""` only when nothing is left). With `limit > 0`, returns the first `limit` bytes (no delimiter) once that many are buffered without a delimiter, so a peer cannot grow the buffer without bound |
| `read_exact(n)` | `Bytes`: exactly `n` bytes, fewer only if the handle ends first |
| `read_some(max)` | `Bytes`: whatever is buffered (up to `max`), or one `read` if nothing is |
| `buffered()` | `int`: bytes buffered and not yet returned |

The record fields are public: `fd`, `buf`, `eof`, and `err`.

Errors and timeouts (specs 51, 53) end reading like EOF. `r.eof` becomes true
and `r.err` holds the cause (`ETIMEDOUT`, `ECONNRESET`, …).

## Users

- `examples/web.rw` reads the request head line by line with
  `read_until("\n", limit)` and the body with `read_exact(Content-Length)`.
  The 64 KiB head limit is now enforced before buffering more.
- `examples/tcp_chat.rw` is now line-based: it echoes each line and `quit`
  ends the session.

## Tests

- `tests/test_e2e_bufio.py`:
  - every method against a peer that sends one byte at a time
  - `tcp_chat.rw` with a line split across two sends
- `tests/test_loader.py`:
  - a stdlib import resolves
  - a local module shadows the stdlib one
  - `RW_STDLIB` overrides the directory
- `tests/test_e2e_web.py`: a request head over 64 KiB gets 413.

## Update

A module loaded from the stdlib resolves its own imports in the stdlib
only, so a user's `json.rw` never replaces the `json` that `template`
imports. If a program needs both its local module and a stdlib module of
the same name, loading fails with a message naming both files.
