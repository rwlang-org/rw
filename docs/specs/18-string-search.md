# rw string search and conversion builtins

## Context

The next long-term goal is **a web application framework and an HTTP server
written in rw**. TCP (spec 12) already lets rw accept connections and move
bytes, but rw cannot *parse* anything: strings only support `len`, `==`, and
`+` (spec 07). Even the smallest HTTP server has to split the request line
`GET /hello HTTP/1.1\r\n` into a method and a path, and has to print a
`Content-Length` header, which needs an int → string conversion.

Working backwards from that one use case, this sub-project adds the minimum
set of string builtins that makes the following program possible:

```rw
def handle(fd: int) -> int:
    req: string = str_from_bytes(read(fd, 8192))
    line: string = str_slice(req, 0, str_find(req, "\r\n"))
    sp: int = str_find(line, " ")
    rest: string = str_slice(line, sp + 1, len(line))
    path: string = str_slice(rest, 0, str_find(rest, " "))
    body: string = "hello " + path
    resp: string = "HTTP/1.1 200 OK\r\nContent-Length: " + str_from_int(len(body)) + "\r\n\r\n" + body
    write(fd, bytes_from_str(resp))
    return close(fd)
```

Roadmap toward the web framework (one PR each):

1. **this sub-project**: string search / slice / int conversion → an HTTP hello server
2. records (struct with fields) → `Request` / `Response` values
3. function references → a routing table of handlers
4. `List[string]` or `Map[string, string]` → headers and route sets
5. a web framework written in rw on top of 1-4

## Goals

- Five builtins, all on `string`:
  - `str_find(s: string, sub: string) -> int` — byte index of the first
    occurrence of `sub`, or `-1`. An empty `sub` is found at `0`
  - `str_slice(s: string, start: int, end: int) -> string` — bytes
    `[start, end)`. Both bounds are clamped to `[0, len(s)]`; if
    `start >= end` the result is `""`. Never fails
  - `str_starts_with(s: string, prefix: string) -> bool`
  - `str_from_int(n: int) -> string` — decimal, with a leading `-` for
    negatives
  - `str_to_int(s: string) -> Option[int]` — parses an optional leading `-`
    followed by one or more ASCII digits and nothing else. Anything else
    (empty, spaces, `+`, trailing garbage, overflow) is `None`
- `examples/string_search.rw` (pure, deterministic e2e) and
  `examples/http_hello.rw` (an HTTP/1.1 server, verified from Python like the
  TCP echo example)

## Non-Goals

- `split` / `join` / `strip` / `replace` / `lower` / `upper` — `split` needs
  `List[string]`, the rest are not required by the hello server (#39 keeps them)
- Negative (from-the-end) indices in `str_slice`
- A start offset for `str_find` (`str_slice` + `str_find` covers it)
- f-strings / string formatting (#18)
- The same builtins on `Bytes` — HTTP bodies stay `Bytes` at the I/O
  boundary and are converted explicitly with `str_from_bytes`
- Unicode-aware indexing: indices are byte offsets (UTF-8 semantics are #106)
- Method-call syntax (`s.find(sub)`)
- Parsing floats, hex, or a leading `+` in `str_to_int`

## Design

### Representation and ABI

`string` is `rw_str { int64_t len; const char *ptr; }` (16 bytes, returned in
two registers on arm64 and x86_64, so value return is safe). Strings are
immutable, so `str_slice` is **zero-copy**: it returns `{end - start, ptr + start}`
into the same buffer. No runtime allocation is involved.

`str_from_int` allocates a new buffer with `malloc` like `rw_str_concat`.
Leaks are tolerated (no GC yet, same policy as concat).

`str_to_int` returns `Option[int]` through a pointer-out argument, matching
`rw_list_int_at_opt`.

```c
int64_t rw_str_find       (rw_str s, rw_str sub);
rw_str  rw_str_slice      (rw_str s, int64_t start, int64_t end);
int8_t  rw_str_starts_with(rw_str s, rw_str prefix);
rw_str  rw_str_from_int   (int64_t n);
void    rw_str_to_int     (rw_option_int *out, rw_str s);
```

### Names

The builtins use a `str_` prefix (like the existing `str_from_bytes`) instead
of overloading generic names, so that adding `bytes_find` etc. later does not
require overload resolution. They are added to sema's builtin set, so a
`from x import` name with the same name is rejected, and spawning them is an
error, as for every other builtin. (A user function with a builtin's name is
currently shadowed by the builtin rather than rejected; that applies to all
builtins and is out of scope here.)

## Changes per component

- `runtime/runtime.h`, `runtime/runtime.c`: the five helpers
- `rwc/sema.py`: the five builtins with argument count / type checks
- `rwc/irgen.py`: declare the helpers and emit calls (`str_to_int` via a
  stack slot)
- `examples/string_search.rw` + `.expected`; `examples/http_hello.rw`
- tests: `test_sema` (positive and negative), `test_irgen` (symbols emitted),
  `test_e2e` (string_search), `test_e2e_http` (http_hello, like `test_e2e_tcp`)

Layers touched: sema / irgen / runtime (3) + examples and tests. No lexer or
parser change: `\r` / `\n` escapes already exist.

## Verification

```sh
uv run --frozen pytest tests/test_sema.py tests/test_irgen.py -q
uv run --frozen pytest tests/test_e2e.py -k string_search -q
uv run --frozen pytest tests/test_e2e_http.py -q
uv run --frozen rwc run examples/string_search.rw
uv run --frozen pytest -q
```

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| A zero-copy slice outlives its source buffer | Buffers are never freed (no GC yet), so a slice is always valid. Revisit together with the memory management RFC (#67) |
| `str_slice` panicking on bad bounds would kill an HTTP server on a malformed request | Bounds are clamped instead; malformed input yields `""` and `str_find` yields `-1`, which the program checks |
| Overflow in `str_to_int` (e.g. a huge `Content-Length`) | Detected while accumulating digits; returns `None` |
| `str_find` returning `-1` fed straight into `str_slice` | Clamping makes `str_slice(s, 0, -1)` return `""` rather than reading out of bounds |
