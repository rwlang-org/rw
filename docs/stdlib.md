# rw standard library reference

The rw standard library is a set of rw source files in `stdlib/`. It is separate from the builtins compiled into the C runtime (`read`, `str_find`, `fs_kind`, and so on): the modules are written in the same language as user programs and compiled the same way. This document covers each module's public API, error behaviour, and usage examples. It is based on the code in `stdlib/*.rw`.

The design intent of each module is in the specs under `docs/specs/` (54, 58, 59, 60, 61, 62, 66).

## Contents

- [Module index](#module-index)
- [Module search path](#module-search-path)
- [Conventions shared across modules](#conventions-shared-across-modules)
- [bufio: buffered reading](#bufio-buffered-reading)
- [fs: files and directories](#fs-files-and-directories)
- [json: JSON values, parsing, and serialization](#json-json-values-parsing-and-serialization)
- [log: leveled logging](#log-leveled-logging)
- [path: POSIX path strings](#path-posix-path-strings)
- [process: child processes](#process-child-processes)
- [template: Mustache-style templates](#template-mustache-style-templates)
- [url: percent-encoding and query strings](#url-percent-encoding-and-query-strings)
- [An example library: the web framework](#an-example-library-the-web-framework)
- [Adding a standard library module](#adding-a-standard-library-module)

## Module index

| Module | Import | Purpose | Spec |
|---|---|---|---|
| [`bufio`](#bufio-buffered-reading) | `import bufio` | Reading lines, delimited chunks, or exact byte counts from any handle (socket, pipe, file) | 54 |
| [`fs`](#fs-files-and-directories) | `import fs` | Reading and writing files, directory operations, metadata | 59 |
| [`json`](#json-json-values-parsing-and-serialization) | `import json` | A JSON value model, parser, and serializer | 58 |
| [`log`](#log-leveled-logging) | `import log` | One-line log records with level, prefix, and timestamp | 60 |
| [`path`](#path-posix-path-strings) | `import path` | String operations on POSIX paths (never touches the filesystem) | 59 |
| [`process`](#process-child-processes) | `import process` | Starting child processes, their I/O, waiting, and signals | 61 |
| [`template`](#template-mustache-style-templates) | `import template` | Mustache-style templates rendered from a `json.Value` | 66 |
| [`url`](#url-percent-encoding-and-query-strings) | `import url` | Percent-encoding and decoding, query string parsing | 62 |

The only dependency between modules is `template` → `json`.

## Module search path

`import foo` looks for `foo.rw` in this order (`rwc/loader.py`):

1. **The entry file's directory** (for `rwc run app.rw`, the directory containing `app.rw`).
2. **The standard library directory.** By default this is `stdlib/` at the repository root (next to the `rwc` package, alongside `runtime/`). If the environment variable `RW_STDLIB` is set, that directory is used **instead of** `stdlib/` (it replaces it; it is not added to it).

If neither has the file, the error names both paths:

```text
error: cannot find module 'nosuchmod' (looked for ./nosuchmod.rw and /path/to/rw/stdlib/nosuchmod.rw)
```

All three import forms work with the standard library (spec 17), and class names can be imported too:

```rw
import json                    # json.parse(...), json.Value
import json as js              # js.parse(...), js.Value
from json import Value, parse  # parse(...), Value
```

### Caveats

- **A local module wins.** With a `url.rw` next to the entry file, the standard library's `url` is not loaded.
- **A standard library module imports only standard library modules.** `template`'s `import json` reads `stdlib/json.rw` even if a `json.rw` sits next to the entry file. But if a program uses both its own `json` and a standard library module that depends on `json` (`template`), the module names clash and loading fails:

  ```text
  error: module 'json' is both .../stdlib/json.rw and ./json.rw: a local module shadows a standard library module used by another import; rename the local one
  ```

  Avoid giving your own modules the names of standard library modules (`bufio` `fs` `json` `log` `path` `process` `template` `url`).
- There is a single module namespace. A module name is never loaded twice.
- Standard library modules are ordinary rw modules: they are compiled together with the program, may import each other, and must not define `main`.

## Conventions shared across modules

These follow the error-handling guidelines in [spec 72](specs/72-error-handling.md). Inside a function that itself returns an `Option` or a `Result[_, string]`, `?` forwards a failure: `v = json.parse(text)?`.

### "Maybe" is `Option`

Functions that return `None` when a value does not exist or an operation fails return `Option[T]` (`fs.read_file`, `fs.stat`, `json.Value.get`, `url.get`, `process.run`, and so on). Handle both cases with `match`:

```rw
match fs.read_file("config.json"):
    case Some(text):
        print(text)
    case None:
        print(error_message(last_error()))
```

### Parse errors are `Result` with a positioned message

Functions that interpret input text (`json.parse`, `template.compile`, `template.render`) return `Result[T, string]`. The `Err` message says what is wrong and where, in the form `"<problem> at line <line>, column <column>"`. Lines and columns start at 1, and columns count **bytes** (a multi-byte UTF-8 character spans several columns).

```text
expected ',' or '}' at line 1, column 14
unclosed section 'items' at line 2, column 3
```

### OS errors are `last_error()`

When an operation that goes through the OS (files, processes) fails, the return value signals the failure (`None`, `false`, an empty list, `-1`) and `last_error()` gives the cause (spec 51). `last_error()` is the outcome of the current fiber's most recent I/O builtin: 0 on success, otherwise an errno value.

| Builtin | Returns |
|---|---|
| `last_error() -> int` | 0 or an errno value |
| `error_name(e: int) -> string` | the symbolic name, such as `"ENOENT"` or `"EEXIST"`; `""` for 0 |
| `error_message(e: int) -> string` | human-readable text (`strerror`); `""` for 0 |

The next I/O builtin call overwrites `last_error()`. Read it right after detecting the failure, before any other I/O.

### Records are references

Records defined with `class` (`bufio.Reader`, `json.Value`, `log.Logger`, `process.Process`, and so on) are references. Passing one to a function or calling a method on it changes the same object. This is why `json.Value.set` modifies the caller's value, and why a `log.Logger`'s fields can be changed by assignment.

### Visibility

rw has no notion of private, so every function and class in a module can be imported. This document separates the API meant for users from internal helpers. Internal helpers may change without notice.

---

## bufio: buffered reading

```rw
import bufio
```

`read(fd, max)` returns only what has arrived so far, so the caller has to find line and delimiter boundaries and keep the bytes it read too far. `bufio.Reader` does that work. It works on any handle: sockets, pipes, files.

Bytes a call does not return stay in the reader and are returned by the next call, so the methods can be mixed freely on one handle. Do not call `read` directly on an fd that has a reader, though: that would put data out of order with what is buffered.

### `reader`

```rw
def reader(fd: int) -> Reader
```

Returns a reader wrapping handle `fd`. Closing the fd is the caller's job.

### `class Reader`

| Field | Type | Meaning |
|---|---|---|
| `fd` | `int` | the handle being read |
| `buf` | `string` | bytes read from fd but not yet returned |
| `eof` | `bool` | `true` once fd reported EOF or an error |
| `err` | `int` | `last_error()` at that point; 0 is a clean EOF |

Errors and timeouts (specs 51, 53) end reading the same way EOF does: `r.eof` becomes `true` and `r.err` holds the cause (`ETIMEDOUT`, `ECONNRESET`, …). Once a reader is at `eof`, it does not read fd again.

| Method | Returns |
|---|---|
| `read_line(self) -> Option[string]` | the next line, without its trailing `"\n"` or `"\r\n"`. A last line with no newline is still returned. `None` at EOF |
| `read_until(self, delim: string, limit: int = 0) -> string` | up to and including the first `delim`; see below |
| `read_exact(self, n: int) -> Bytes` | exactly `n` bytes, or fewer if fd ends first |
| `read_some(self, max: int) -> Bytes` | whatever is buffered (up to `max` bytes); if nothing is buffered, one read from fd. An empty `Bytes` at EOF |
| `buffered(self) -> int` | the number of bytes buffered and not yet returned |

#### `read_until` in detail

- If `delim` is found, returns everything from the start through the end of `delim`.
- At EOF, returns whatever is left (without `delim`). `""` only when nothing is left.
- With `limit > 0`, once `limit` or more bytes are buffered without a `delim`, returns the first `limit` bytes (without `delim`). The same happens when `delim` is found but does not fit within the first `limit` bytes. This keeps a peer that sends data without a newline from growing the buffer without bound.
- Whether the result ends with `delim` tells "delimiter found" apart from "limit reached or EOF".

`read_line` calls `read_until("\n")` with no limit. When reading lines from an untrusted peer, use `read_until("\n", limit)`.

#### Internal helpers

`fill(self) -> bool` (read up to 4096 more bytes from fd) and `take(self, n: int) -> string` (remove the first `n` buffered bytes) are internal.

### Example: reading a head and a body

Read header lines up to an empty line, then a body of `Content-Length` bytes. This is the shape of reading an HTTP request.

```rw
import bufio
import fs

def main() -> int:
    fs.write_file("request.txt", "POST /echo HTTP/1.1\r\nContent-Length: 5\r\n\r\nhelloEXTRA")
    fd = open("request.txt", "r")
    if fd < 0:
        print(error_message(last_error()))
        return 1
    r = bufio.reader(fd)
    length = 0
    while true:
        line = r.read_until("\n", 8192)
        if not str_ends_with(line, "\n"):
            print("head too long or truncated")
            close(fd)
            return 1
        line = str_strip(line)
        if line == "":
            break
        print("header: " + line)
        if str_starts_with(line, "Content-Length:"):
            match str_to_int(str_strip(str_slice(line, 15, len(line)))):
                case Some(n):
                    length = n
                case None:
                    length = 0
    body = r.read_exact(length)
    print("body: " + str_from_bytes(body))
    print(f"left in buffer: {r.buffered()}")
    close(fd)
    return 0
```

Output:

```text
header: POST /echo HTTP/1.1
header: Content-Length: 5
body: hello
left in buffer: 5
```

### Example: reading line by line

```rw
import bufio

def main() -> int:
    fd = open("request.txt", "r")
    r = bufio.reader(fd)
    n = 0
    while true:
        match r.read_line():
            case Some(line):
                n += 1
                print(f"{n}: [{line}]")
            case None:
                break
    if r.err != 0:
        print("read error: " + error_name(r.err))
    close(fd)
    return 0
```

Output (reading the file created by the previous example):

```text
1: [POST /echo HTTP/1.1]
2: [Content-Length: 5]
3: []
4: [helloEXTRA]
```

An empty line comes back as `Some("")`, distinct from `None`. `examples/tcp_chat.rw` is a line-oriented TCP server built on `bufio`.

---

## fs: files and directories

```rw
import fs
```

A thin layer over the `fs_*` builtins and `open` / `read` / `write_all` / `close`. Failures return `None`, `false`, or an empty list and set `last_error()` to the cause (with one exception; see "Note" below). The runtime runs each system call on the aio pool (spec 50), so other fibers keep running while one waits on a slow filesystem. A path containing a NUL byte is `EINVAL`.

### `class Stat`

| Field | Type | Meaning |
|---|---|---|
| `kind` | `string` | one of `"file"`, `"dir"`, `"other"` |
| `size` | `int` | size in bytes |
| `mtime_ms` | `int` | last modification time (Unix time, milliseconds) |

### Functions

| Function | Returns | On failure |
|---|---|---|
| `stat(p: string) -> Option[Stat]` | metadata for `p`; symlinks are followed | `None` |
| `exists(p: string) -> bool` | whether `p` exists | `false` |
| `is_dir(p: string) -> bool` | whether `p` is a directory | `false` |
| `is_file(p: string) -> bool` | whether `p` is a regular file | `false` |
| `read_dir(p: string) -> List[string]` | entry names in the directory, sorted by byte order, without `.` and `..` | empty list |
| `rename(src: string, dst: string) -> bool` | renames or moves `src` to `dst` | `false` |
| `remove(p: string) -> bool` | removes a file or an **empty** directory | `false` (`ENOTEMPTY` for a non-empty directory) |
| `mkdir(p: string) -> bool` | creates one directory (mode 0755) | `false` (`EEXIST` if it exists) |
| `mkdir_all(p: string) -> bool` | like `mkdir -p`: creates missing parents too. `true` if `p` already is a directory | `false` |
| `read_file(p: string) -> Option[string]` | the whole file | `None` |
| `write_file(p: string, data: string) -> bool` | replaces the file's contents with `data` (creating it if needed) | `false` |

Notes:

- `exists` / `is_dir` / `is_file` do not tell a failure apart from "does not exist". When the reason matters, call `stat` and check `last_error()`.
- `read_dir` returns an empty list on failure; check `last_error()` to tell it apart from an empty directory.
- `mkdir_all` splits the path on `/` and creates it one level at a time from the start. A regular file along the way makes it return `false`. Another fiber or process creating the same directory at the same time does not make it fail.
- `read_file` reads 64 KiB at a time and concatenates. `write_file` truncates with `open(p, "w")` and writes with `write_all`.

`read_file` / `write_file` restore the cause with `set_last_error` after the cleanup `close`, so a failed read or write still leaves it in `last_error()`. For example, when `p` is a directory and reading fails with `EISDIR`, `last_error()` says so.

### Example

```rw
import fs
import path

def main() -> int:
    root = "demo/site"
    if not fs.mkdir_all(path.join(root, "posts")):
        print("mkdir_all: " + error_name(last_error()))
        return 1
    fs.write_file(path.join(root, "posts/b.md"), "# B\n")
    fs.write_file(path.join(root, "posts/a.md"), "# A\n")
    for name in fs.read_dir(path.join(root, "posts")):
        p = path.join(root, "posts/" + name)
        match fs.stat(p):
            case Some(st):
                print(f"{name} {st.kind} {st.size} bytes")
            case None:
                print(name + ": " + error_name(last_error()))
    match fs.read_file(path.join(root, "posts/a.md")):
        case Some(text):
            print("a.md = " + str_strip(text))
        case None:
            print("read failed: " + error_name(last_error()))
    match fs.read_file(path.join(root, "missing.md")):
        case Some(text):
            print(text)
        case None:
            print("missing.md: " + error_name(last_error()))   # ENOENT
    print(fs.remove(path.join(root, "posts")))                # false: not empty
    print(error_name(last_error()))                            # ENOTEMPTY
    print(fs.rename(path.join(root, "posts/b.md"), path.join(root, "b.md")))
    print(fs.exists(path.join(root, "b.md")))
    # Clean up: remove works on files and empty directories only.
    fs.remove(path.join(root, "b.md"))
    fs.remove(path.join(root, "posts/a.md"))
    fs.remove(path.join(root, "posts"))
    fs.remove(root)
    fs.remove("demo")
    print(fs.exists("demo"))
    return 0
```

Output:

```text
a.md file 4 bytes
b.md file 4 bytes
a.md = # A
missing.md: ENOENT
false
ENOTEMPTY
true
true
false
```

---

## json: JSON values, parsing, and serialization

```rw
import json
```

Provides `json.Value` for JSON values, an RFC 8259 parser, and a compact serializer. It is written entirely in rw and uses the builtins `byte_at` (byte-wise scanning), `chr` (turning `\uXXXX` into UTF-8), and `str_to_int` / `str_to_float` (number conversion).

### Constants

| Constant | Value | Meaning |
|---|---|---|
| `MAX_DEPTH: int` | `512` | maximum nesting of arrays and objects. Deeper input is an error instead of a stack overflow |

### `class Value`

| Field | Type | Meaning |
|---|---|---|
| `kind` | `string` | one of `"null"`, `"bool"`, `"int"`, `"float"`, `"string"`, `"array"`, `"object"` |
| `b` | `bool` | the value when `kind == "bool"` |
| `i` | `int` | the value when `kind == "int"` |
| `f` | `float` | the value when `kind == "float"` |
| `s` | `string` | the value when `kind == "string"` |
| `items` | `List[Value]` | array elements, or object member values |
| `keys` | `List[string]` | object keys (in the same order as `items`) |

Only the field matching `kind` is meaningful. Objects keep their keys in insertion order, and duplicate keys in the input are kept as written. `Value` is a reference, so `set` and `push` modify the value itself.

#### Access

| Method | Returns |
|---|---|
| `get(self, key: string) -> Option[Value]` | object member `key`; the first one if the key is duplicated. `None` when missing and for non-objects |
| `at(self, n: int) -> Option[Value]` | array element `n` (0-based). `None` when out of range, negative, or for non-arrays |
| `size(self) -> int` | number of array elements or object members; 0 for scalars |
| `is_null(self) -> bool` | whether `kind == "null"` |

#### Typed reads

| Method | Returns |
|---|---|
| `as_str(self) -> Option[string]` | `Some` for a string, otherwise `None` |
| `as_int(self) -> Option[int]` | `Some` for `"int"`. A `"float"` (even `3.0`) gives `None` |
| `as_float(self) -> Option[float]` | `Some` for any number (an `"int"` is converted to `float`) |
| `as_bool(self) -> Option[bool]` | `Some` for a boolean |
| `as_str_or(self, fallback: string) -> string` | the string, or `fallback` for anything else |
| `as_int_or(self, fallback: int) -> int` | the `"int"` value, or `fallback` for anything else |

#### Building

| Method | Returns |
|---|---|
| `set(self, key: string, v: Value) -> Value` | sets object member `key`. An existing key (the first one) is replaced; otherwise the member is appended. Returns `self`, so calls chain |
| `push(self, v: Value) -> Value` | appends to an array. Returns `self` |
| `dump(self) -> string` | compact JSON text (same as `json.dump(self)`) |

`set` and `push` do not check `kind`. `set` on an array drops the key and appends an element; `push` on an object adds a member that `dump` does not show. Use `set` only on objects and `push` only on arrays.

### Constructors

| Function | Returns |
|---|---|
| `null() -> Value` | `null` |
| `from_bool(b: bool) -> Value` | a boolean |
| `from_int(i: int) -> Value` | an integer |
| `from_float(f: float) -> Value` | a floating-point number |
| `from_str(s: string) -> Value` | a string |
| `array(items: List[Value]) -> Value` | an array. An empty array is `json.array([])` |
| `object() -> Value` | an empty object |

### Parsing

```rw
def parse(text: string) -> Result[Value, string]
```

Parses one JSON document, with surrounding whitespace.

- All escapes are supported, including `\uXXXX`. A surrogate pair combines into one code point; an unpaired surrogate becomes U+FFFD.
- An integer that fits in 64 bits (no fraction, no exponent) is `"int"`. Every other number (`1.0`, `1e3`, an integer beyond int64) is `"float"`.
- Raw control characters in strings, trailing commas, leading zeros (`01`), unquoted keys, and data after the document are all errors.
- Nesting deeper than `MAX_DEPTH` is an error.

The error message reports only the first problem found, in the form `"<problem> at line L, column C"`.

| Message | Example cause |
|---|---|
| `unexpected end of input` | `""`, `"["` |
| `unexpected character` | `[1, 2,]` (at the `]`), `@` |
| `invalid literal` | `tru`, `nul` |
| `invalid number` | `-`, `1.`, `1e` |
| `unterminated string` | `"abc` |
| `control character in string` | a raw tab or newline inside a string |
| `invalid escape` / `invalid \u escape` | `"\q"`, `"\u12G4"` |
| `expected a string key` | `{a:1}` |
| `expected ':'` | `{"a" 1}` |
| `expected ',' or ']'` / `expected ',' or '}'` | `[1 2]`, `{"a":1 "b":2}` |
| `unexpected trailing characters` | `[1,2] x`, `01` |
| `nesting too deep` | 513 or more levels of nesting |

### Serialization

| Function | Returns |
|---|---|
| `dump(v: Value) -> string` | compact JSON text (no whitespace) |
| `quote(s: string) -> string` | `s` as a JSON string literal (including the surrounding `"`) |

- `"`, `\`, `\n`, `\r`, and `\t` are escaped; other control characters are written as `\u00XX`. Bytes ≥ 0x80 pass through, so UTF-8 text stays readable.
- NaN and infinity have no JSON representation and become `null`.
- Floats are written in the `str(float)` format, with `.0` appended when there is neither a fraction nor an exponent (`from_float(2.0)` gives `2.0`, `-0.0` gives `-0.0`). So a dumped float parses back as a `"float"`. When reading numbers, `as_float` accepts both `"int"` and `"float"`.

#### Internal helpers

`make(kind: string) -> Value`, `hex4(n: int) -> string`, and `class Parser` are internal.

### Example: loading a config file and building a response

```rw
import json

class Config:
    name: string
    port: int
    debug: bool
    tags: List[string]

# Member `key`, or json null when it is missing.
def field(doc: json.Value, key: string) -> json.Value:
    match doc.get(key):
        case Some(v):
            return v
        case None:
            return json.null()

def load_config(text: string) -> Result[Config, string]:
    match json.parse(text):
        case Ok(doc):
            if doc.kind != "object":
                return Err("config: expected an object")
            cfg = Config(field(doc, "name").as_str_or("app"), field(doc, "port").as_int_or(8080), false, [])
            d = field(doc, "debug")
            if not d.is_null():
                match d.as_bool():
                    case Some(b):
                        cfg.debug = b
                    case None:
                        return Err("config: debug must be a boolean")
            tags = field(doc, "tags")
            for k in range(0, tags.size()):
                match tags.at(k):
                    case Some(t):
                        cfg.tags = list_push(cfg.tags, t.as_str_or(""))
                    case None:
                        continue
            return Ok(cfg)
        case Err(msg):
            return Err("config: " + msg)

def main() -> int:
    match load_config("{\"name\": \"api\", \"port\": 9000, \"tags\": [\"a\", \"b\"]}"):
        case Ok(c):
            print(f"{c.name}:{c.port} debug={c.debug} tags={str_join(c.tags, ",")}")
        case Err(e):
            print(e)
    match load_config("{\"debug\": \"yes\"}"):
        case Ok(c):
            print(c.name)
        case Err(e):
            print(e)
    match load_config("{\"name\": \"api\",\n \"port\": 9000,\n}"):
        case Ok(c):
            print(c.name)
        case Err(e):
            print(e)

    # Building a document: set / push modify in place and return the value.
    res = json.object()
    res.set("ok", json.from_bool(true)).set("count", json.from_int(2))
    items = json.array([])
    items.push(json.from_str("say \"hi\"")).push(json.null())
    res.set("items", items)
    res.set("ratio", json.from_float(0.25))
    print(res.dump())
    return 0
```

Output:

```text
api:9000 debug=false tags=a,b
config: debug must be a boolean
config: expected a string key at line 3, column 1
{"ok":true,"count":2,"items":["say \"hi\"",null],"ratio":0.25}
```

`examples/json_basic.rw` has more examples, including escapes and surrogate pairs.

---

## log: leveled logging

```rw
import log
```

A logger that writes each record as one line, in this format:

```text
[<UTC timestamp> ]<LEVEL>[ <prefix>:] <message>
2026-10-13T01:02:03.456Z INFO api: listening addr=127.0.0.1:8080
```

A record is built as one string and written with a single `write_all` while holding the logger's `Mutex`, so lines written concurrently from several fibers never interleave. Output goes straight to the handle, not through `print`'s stdio buffer. Records are therefore not lost when the process is killed, but mixing `print` and a logger on the same stream can reorder lines (when stdout is a pipe, `print` output sits in a buffer before it is written).

### Constants

| Constant | Value |
|---|---|
| `DEBUG: int` | 10 |
| `INFO: int` | 20 |
| `WARN: int` | 30 |
| `ERROR: int` | 40 |

### `class Logger`

| Field | Type | Meaning |
|---|---|---|
| `fd` | `int` | the handle lines go to |
| `level` | `int` | records below this level are dropped |
| `prefix` | `string` | printed as `"<prefix>:"` when not empty |
| `timestamps` | `bool` | whether each line starts with a UTC ISO 8601 time |
| `mu` | `Mutex` | the mutex that serializes writes |

`fd`, `level`, `prefix`, and `timestamps` can be changed by direct assignment.

| Method | Returns |
|---|---|
| `debug(self, msg: string) -> void` / `info` / `warn` / `error` | writes one record at that level |
| `log(self, level: int, msg: string) -> void` | writes at any level. The printed name is the nearest defined level at or below `level` (35 prints `WARN`, 99 prints `ERROR`, anything below 10 prints `DEBUG`) |
| `enabled(self, level: int) -> bool` | whether a record at `level` would be written. Use it before building an expensive message |
| `set_level(self, name: string) -> bool` | sets the threshold by name: `"debug"`, `"info"`, `"warn"` / `"warning"`, `"error"` (case-insensitive). For any other name, changes nothing and returns `false` |

### Functions

| Function | Returns |
|---|---|
| `new(fd: int) -> Logger` | a logger writing to handle `fd` (1 = stdout, 2 = stderr, or any other). Level INFO, no prefix, timestamps on |
| `to_file(p: string) -> Option[Logger]` | a logger appending to file `p` (created if missing). `None` if it cannot be opened, with the cause in `last_error()` |
| `kv(key: string, value: string) -> string` | `" key=value"` (with a leading space). When the value is empty or contains a space, `"`, `=`, or a control character, it is wrapped in `"` with `\` and `"` escaped, so log lines stay machine-splittable |
| `iso8601(ms: int) -> string` | Unix time in milliseconds as `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC). Negative values (before 1970) work too |

A logger has no method to close its file. When a logger made by `to_file` is no longer needed, call `close(lg.fd)`.

#### Internal helpers

`level_of(name: string) -> int` (name to number, -1 if unknown), `level_name(level: int) -> string`, and `pad(n: int, width: int) -> string` are internal.

### Example

```rw
import log

def main() -> int:
    lg = log.new(1)              # 1 = stdout, 2 = stderr, or any handle
    lg.prefix = "api"
    lg.timestamps = false        # keep this example's output stable
    lg.debug("not shown: the default level is INFO")
    lg.info("listening" + log.kv("addr", "127.0.0.1:8080"))
    lg.set_level("debug")
    lg.debug("cache" + log.kv("hits", str(42)) + log.kv("path", "/var/cache/my app"))
    lg.warn("slow request" + log.kv("ms", str(1530)) + log.kv("user", ""))
    lg.error("upstream failed" + log.kv("err", "connection refused"))
    return 0
```

Output:

```text
INFO api: listening addr=127.0.0.1:8080
DEBUG api: cache hits=42 path="/var/cache/my app"
WARN api: slow request ms=1530 user=""
ERROR api: upstream failed err="connection refused"
```

`iso8601` examples:

| Call | Result |
|---|---|
| `log.iso8601(0)` | `1970-01-01T00:00:00.000Z` |
| `log.iso8601(1700000000123)` | `2023-11-14T22:13:20.123Z` |
| `log.iso8601(-1)` | `1969-12-31T23:59:59.999Z` |
| `log.iso8601(951782400000)` | `2000-02-29T00:00:00.000Z` |

---

## path: POSIX path strings

```rw
import path
```

Functions that operate on POSIX paths as strings. Nothing here touches the filesystem: `..` is resolved lexically and symlinks are not followed.

| Function | Returns |
|---|---|
| `is_abs(p: string) -> bool` | whether `p` starts with `/` |
| `join(a: string, b: string) -> string` | `a` and `b` joined with one `/`. If `b` is absolute, `b` (as in Python's `os.path.join`). `a` if `b` is empty, `b` if `a` is empty |
| `split(p: string) -> tuple[string, string]` | `(directory, last component)`, split at the last `/`. The directory keeps no trailing `/` unless it is the root. Without a `/`, `("", p)` |
| `basename(p: string) -> string` | the second half of `split(p)` |
| `dirname(p: string) -> string` | the first half of `split(p)` |
| `ext(p: string) -> string` | the extension of the last component, with its dot (`"x.tar.gz"` → `".gz"`); `""` if there is none. A leading dot (`.bashrc`) is not an extension |
| `normalize(p: string) -> string` | the shortest equivalent path; see below |
| `join_under(root: string, rel: string) -> Option[string]` | `rel` resolved under `root`, or `None` if it would leave `root`; see below |

### `normalize`

- Drops `.` and empty components (`//`).
- A `..` cancels the component before it.
- A relative path keeps leading `..` it cannot resolve (`"../../a/.."` → `"../.."`).
- An absolute path never goes above `/` (`"/../x/.."` → `"/"`).
- A relative path that becomes empty (`""`, `"./"`) becomes `"."`.
- No trailing `/` is kept (`"a/b/"` → `"a/b"`).

### `join_under`

For mapping an untrusted path (such as a URL path) onto a directory.

- If `rel` is absolute: `None`.
- If `normalize(rel)` is `..` or starts with `../` (it leaves `root`): `None`.
- If `normalize(rel)` is `.` (including an empty `rel`): `Some(normalize(root))`.
- Otherwise: `Some(join(normalize(root), normalize(rel)))`.

A `..` inside `rel` is allowed as long as the result stays inside `root` (`"css/../js/app.js"` is fine). The check is lexical, so it cannot stop a symlink inside `root` that points outside it.

### Example

```rw
import path

def show(o: Option[string]) -> string:
    match o:
        case Some(s):
            return s
        case None:
            return "None"

def main() -> int:
    print(path.join("/srv/www", "css/site.css"))     # /srv/www/css/site.css
    print(path.join("a/", "b"))                      # a/b
    print(path.join("a", "/abs"))                    # /abs
    d, b = path.split("/usr/lib/libx.so")
    print(d + " | " + b)                             # /usr/lib | libx.so
    print(path.dirname("/x") + " " + path.basename("noslash"))   # / noslash
    print(path.ext("x.tar.gz") + " [" + path.ext(".bashrc") + "]")  # .gz []
    print(path.normalize("a/./b/../c//d"))           # a/c/d
    print(path.normalize("/../x/.."))                # /
    print(path.normalize("../../a/.."))              # ../..
    print(show(path.join_under("/srv/www", "css/../js/app.js")))  # /srv/www/js/app.js
    print(show(path.join_under("/srv/www", "../etc/passwd")))     # None
    print(show(path.join_under("/srv/www", "/etc/passwd")))       # None
    return 0
```

Each line prints what its comment shows.

---

## process: child processes

```rw
import process
```

Starts child processes with their standard streams connected through pipes. `argv[0]` is looked up in `PATH`. No shell is involved: for pipes or redirects, use `["sh", "-c", "..."]` explicitly. The child inherits the parent's environment and working directory.

### `class Process`

| Field | Type | Meaning |
|---|---|---|
| `pid` | `int` | process ID |
| `stdin` | `int` | write end connected to the child's stdin |
| `stdout` | `int` | read end connected to the child's stdout |
| `stderr` | `int` | read end connected to the child's stderr |

The three handles work directly with `read` / `write` / `write_all` / `close`, or with `bufio.reader`.

| Method | Returns |
|---|---|
| `wait(self) -> int` | closes `stdin` if it is still open (the child sees EOF), then waits for the child to exit. Returns the exit status, `128 + N` if it was killed by signal N, or -1 on error. A second `wait` on the same process returns -1 |
| `kill(self, sig: string = "SIGTERM") -> bool` | sends a signal by name (`SIGTERM`, `SIGKILL`, `SIGINT`, `SIGHUP`, `SIGQUIT`, `SIGUSR1`, `SIGUSR2`, `SIGSTOP`, `SIGCONT`). `true` on success. An unknown name gives `false` with `last_error()` set to `EINVAL` |

`wait` closes only `stdin`. When using `start`, close `stdout` and `stderr` yourself (otherwise the handles leak).

### `class Output`

| Field | Type | Meaning |
|---|---|---|
| `status` | `int` | exit status, with the same meaning as `Process.wait` |
| `stdout` | `string` | everything written to stdout |
| `stderr` | `string` | everything written to stderr |

### Functions

| Function | Returns |
|---|---|
| `run(argv: List[string], input: string = "") -> Option[Output]` | starts the child, writes `input` to its stdin, collects all of stdout and stderr, and waits for it to exit. `None` if it cannot be started |
| `start(argv: List[string]) -> Option[Process]` | starts the child and returns at once. `None` if it cannot be started |

`last_error()` when starting fails:

| Situation | Error |
|---|---|
| the command is not found | `ENOENT` |
| `argv` is empty, or an argument contains a NUL byte | `EINVAL` |

`run` writes stdin and reads stderr in separate fibers while it reads stdout. A child that writes a lot before it has read all its input therefore cannot deadlock against the parent. `run` returns `Some` whenever the child started, even with a non-zero exit status; check `out.status` for success.

#### Internal helpers

`read_all(fd: int) -> string` (read to EOF, then close fd) and `feed(fd: int, data: string) -> int` (write `data`, then close fd) are internal to `run`.

### Example

```rw
import process
import bufio

def main() -> int:
    # Run to completion, feeding stdin.
    match process.run(["sort"], "pear\napple\nfig\n"):
        case Some(out):
            print(f"status={out.status}")
            print(out.stdout)
        case None:
            print("sort: " + error_name(last_error()))

    # No shell is involved: use sh -c explicitly for pipes and redirects.
    match process.run(["sh", "-c", "echo to-stderr >&2; exit 3"]):
        case Some(out):
            print(f"status={out.status} stderr={str_strip(out.stderr)}")
        case None:
            print("sh: " + error_name(last_error()))

    # A missing command is None with ENOENT.
    match process.run(["no-such-command"]):
        case Some(out):
            print(out.status)
        case None:
            print("failed: " + error_name(last_error()))

    # Streaming: read the child's stdout line by line as it is produced.
    match process.start(["sh", "-c", "for i in 1 2 3; do echo tick $i; done"]):
        case Some(p):
            r = bufio.reader(p.stdout)
            while true:
                match r.read_line():
                    case Some(line):
                        print("got: " + line)
                    case None:
                        break
            close(p.stdout)
            close(p.stderr)
            print(f"exit={p.wait()}")
        case None:
            print("start failed")

    # Stop a long-running child: 128 + 15 (SIGTERM).
    match process.start(["sleep", "30"]):
        case Some(p):
            p.kill()
            close(p.stdout)
            close(p.stderr)
            print(p.wait())
        case None:
            print("start failed")
    return 0
```

Output:

```text
status=0
apple
fig
pear

status=3 stderr=to-stderr
failed: ENOENT
got: tick 1
got: tick 2
got: tick 3
exit=0
143
```

---

## template: Mustache-style templates

```rw
import template
import json
```

Renders a logic-less template with `json.Value` data. It implements a subset of Mustache. Read the data with `json.parse` or build it with the `json` constructors.

### Tags

| Tag | Meaning |
|---|---|
| `{{name}}` | the value as text, HTML-escaped (`&` `<` `>` `"` `'`) |
| `{{{name}}}`, `{{& name}}` | the value as text, not escaped |
| `{{a.b.c}}` | dotted lookup |
| `{{.}}` | the current item (used inside a section) |
| `{{#name}}…{{/name}}` | section: rendered once per element if the value is an array, or once (with the value as the new context) if it is truthy |
| `{{^name}}…{{/name}}` | inverted section: rendered once if the value is missing or falsy |
| `{{! comment }}` | renders nothing |

Whitespace around the name inside a tag is ignored (`{{ name }}` and `{{# items }}` work).

### Lookup, text form, and truthiness

- **Lookup**: the first segment of a name is searched in the context stack, innermost first, so a section can still use outer values. The remaining segments walk down from the value found. Once the first segment is found in an inner context, a missing later segment does not fall back to outer contexts (`{{a.b}}` where `a` has no `b` renders `""` even if an outer context has `b`). A name that is not found renders as `""` and is falsy.
- **Text form**: strings as they are, `null` as `""`, numbers and booleans as text (`1.5`, `true`), arrays and objects as compact JSON (which `{{name}}` also HTML-escapes).
- **Falsy values**: missing, `null`, `false`, `""`, and `[]`. The number `0` and an empty object `{}` are **truthy**.

Not supported: partials (`{{> x}}`), lambdas, delimiter changes, and the Mustache spec's whitespace trimming for standalone tag lines.

### Types and functions

| Declaration | Description |
|---|---|
| `compile(src: string) -> Result[Template, string]` | parses the template once into a tree |
| `render(self, data: json.Value) -> string` on `class Template` | renders a compiled template. Cannot fail |
| `render(src: string, data: json.Value) -> Result[string, string]` | compiles and renders in one go |
| `escape_html(s: string) -> string` | replaces `&` `<` `>` `"` `'` with `&amp;` `&lt;` `&gt;` `&quot;` `&#39;` |

For a template used by many requests, call `compile` once and reuse the `Template`.

`compile` errors point at the start (the `{{`) of the offending tag.

| Message | Cause |
|---|---|
| `unclosed section 'NAME'` | `{{#NAME}}` / `{{^NAME}}` has no matching `{{/NAME}}` (the position is the opening tag) |
| `unclosed tag` | `{{` has no matching `}}` (`}}}` for a triple mustache) |
| `unexpected closing tag 'NAME'` | `{{/NAME}}` for a section that is not open |
| `'NAME' closes section 'OTHER'` | the closing tag's name does not match the open section |
| `empty tag` | `{{}}` |

`{{{}}}` and `{{&}}` are not errors even though the name is empty; they render as `""`.

#### Internal helpers

`class Node`, `class Parser`, `to_text`, `truthy`, `lookup`, and `render_nodes` are internal.

### Example

```rw
import json
import template

def main() -> int:
    src = "<h1>{{title}}</h1>\n{{#items}}<li>{{name}}: {{price}} {{currency}}</li>\n{{/items}}{{^items}}<p>no items</p>\n{{/items}}{{! comments render as nothing }}{{{footer}}}"
    match template.compile(src):
        case Ok(t):
            match json.parse("{\"title\": \"Tom & Jerry\", \"currency\": \"JPY\", \"items\": [{\"name\": \"<tea>\", \"price\": 300}, {\"name\": \"cake\", \"price\": 450, \"currency\": \"USD\"}], \"footer\": \"<hr>\"}"):
                case Ok(data):
                    print(t.render(data))
                case Err(msg):
                    print("data: " + msg)
            # The same compiled template, rendered again with other data.
            empty = json.object()
            empty.set("title", json.from_str("Empty")).set("items", json.array([]))
            print(t.render(empty))
        case Err(msg):
            print("template: " + msg)

    # Compile errors carry the position of the offending tag.
    match template.compile("<ul>\n  {{#items}}<li>{{.}}</li>\n</ul>"):
        case Ok(t):
            print("compiled")
        case Err(msg):
            print(msg)
    return 0
```

Output:

```text
<h1>Tom &amp; Jerry</h1>
<li>&lt;tea&gt;: 300 JPY</li>
<li>cake: 450 USD</li>
<hr>
<h1>Empty</h1>
<p>no items</p>

unclosed section 'items' at line 2, column 3
```

In the first `<li>`, the item has no `currency`, so the outer context's `"JPY"` is used. The second item has its own `currency`, `"USD"`. In the second render there is no `footer`, so `{{{footer}}}` is `""` and only `print`'s newline remains, as an empty line.

---

## url: percent-encoding and query strings

```rw
import url
```

Converts percent-encoding and parses the `application/x-www-form-urlencoded` format (query strings and form bodies). Decoding uses the `str_from_byte` builtin to produce single bytes ≥ 0x80 (`chr` encodes a code point as UTF-8, so it cannot produce a lone byte).

### `class Param`

| Field | Type | Meaning |
|---|---|---|
| `key` | `string` | the decoded key |
| `value` | `string` | the decoded value |

### Functions

| Function | Returns |
|---|---|
| `unescape(s: string, plus: bool = true) -> string` | `s` with `%XX` decoded. With `plus` `true` (the default, the query/form convention), `+` also becomes a space; pass `false` for path segments. A `%` not followed by two hex digits is kept as is |
| `escape(s: string) -> string` | everything except the RFC 3986 unreserved set (`A-Z a-z 0-9 - . _ ~`) as `%XX` (uppercase hex). A space becomes `%20` |
| `parse_query(q: string) -> List[Param]` | the `key=value` pairs split on `&`, decoded, in order. A piece without `=` has the value `""`; empty pieces (between `a=1&&b=2`) are skipped. A second or later `=` is part of the value. Do not include a leading `?` in `q` |
| `get(params: List[Param], key: string) -> Option[string]` | the first value for `key`, or `None` |

`%XX` is decoded one byte at a time, so multi-byte UTF-8 (`%C3%A9` → `é`) comes back intact. The result is not checked for valid UTF-8. When a key appears more than once (`tag=a&tag=b`), iterate over the result of `parse_query` to get every value.

#### Internal helpers

`hex_value(c: int) -> int` (the value of one hex digit, or -1 if it is not one) is internal.

### Example

```rw
import url

def main() -> int:
    q = url.parse_query("q=caf%C3%A9+au+lait&tag=a&tag=b&&debug")
    for p in q:
        print("[" + p.key + "]=[" + p.value + "]")
    match url.get(q, "tag"):
        case Some(v):
            print("first tag: " + v)              # a
        case None:
            print("no tag")
    match url.get(q, "page"):
        case Some(v):
            print(v)
        case None:
            print("no page")                      # missing key
    print(url.unescape("a%20b+c"))                # a b c
    print(url.unescape("a%20b+c", false))         # a b+c  (path segments)
    print(url.unescape("100%"))                   # 100%   (kept as is)
    print(url.escape("a b&c=d/é~"))               # a%20b%26c%3Dd%2F%C3%A9~
    print("/search?q=" + url.escape("rw & go"))   # /search?q=rw%20%26%20go
    return 0
```

Output:

```text
[q]=[café au lait]
[tag]=[a]
[tag]=[b]
[debug]=[]
first tag: a
no page
a b c
a b+c
100%
a%20b%26c%3Dd%2F%C3%A9~
/search?q=rw%20%26%20go
```

---

## An example library: the web framework

`examples/web.rw` is a small HTTP/1.1 web framework written in rw on top of the standard library. It is not part of the standard library itself: it lives in `examples/`, and you copy it next to your application to use it (`import web` is resolved from the entry file's directory).

It also serves as a worked example of using the standard library:

| Module | Use in `web.rw` |
|---|---|
| `bufio` | reads the request head one line at a time with `read_until("\n", limit)` (with a 64 KiB limit) and the body with `read_exact` |
| `url` | decodes query strings, path parameters, and form bodies |
| `path` / `fs` | static file serving (`join_under` keeps requests inside the directory) |
| `log` | the access log |

`examples/web_app.rw` is a complete application that additionally uses `json` and `template`. For the API, see [`docs/web-framework.md`](web-framework.md).

---

## Adding a standard library module

1. **Put it in `stdlib/<name>.rw`.** Make sure the name does not clash with an existing module or builtin, and that it is not a name users are likely to give their own modules (a local module wins).
2. **Do not define `main`.** An imported module that has `main` is a compile error.
3. **Start with a comment giving an overview and a short usage example.** As in the existing modules, cite the spec number and show everything from the `import` to a typical call. Give each public function and class a one- or two-line comment on its return value and failure behaviour.
4. **Follow the conventions in this document.** "Maybe" is `Option`, input interpretation errors are `Result[T, string]` with a positioned message, and OS errors are left to `last_error()` with `None` / `false` returned.
5. **Add an example and tests.**
   - Write `examples/<name>_basic.rw` and its expected output `examples/<name>_basic.rw.expected`, and add the name to the `test_synchronous_example` list in `tests/test_e2e.py`. Keep the output identical across environments (do not print times, random numbers, or PIDs).
   - Verify behaviour that an expected-output file cannot express well (networking, concurrency, error paths) in `tests/test_e2e_<name>.py`. Tests that can be written in rw alone can also use `rwc test` with `test_*.rw` files (spec 68).
6. **Write a spec.** In `docs/specs/NN-<name>.md`, describe the motivation, an API table, error behaviour, limits, and the list of tests. If new builtins are needed, specify them in the same document.
7. **Update this document.** Add the module to the index table and add its section, in both `docs/ja/stdlib.md` and `docs/stdlib.md`.
