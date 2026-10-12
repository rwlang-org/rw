# 58. JSON (`stdlib/json.rw`) and byte / code point builtins

Issue: #40

## Motivation

A web framework needs to read and write JSON. Building responses by string
concatenation (`examples/web_app.rw`'s `search`) breaks as soon as a value
contains a quote. The module is written in rw on the standard library path
(spec 54); three small builtins fill the gaps that made that impossible.

## Builtins

| Builtin | Result |
|---|---|
| `byte_at(s, i) -> int` | the byte at index `i` (0–255), or -1 when out of range |
| `chr(cp) -> string` | the UTF-8 encoding of code point `cp`; invalid code points (negative, surrogates, above U+10FFFF) give U+FFFD |
| `str_to_float(s) -> Option[float]` | `s` parsed as a float (`strtod` syntax); `None` if any of `s` is left over or it starts with whitespace |

## `json` module

```python
import json

match json.parse(text):
    case Ok(v):
        match v.get("name"):
            case Some(n):
                print(n.as_str_or("?"))
            case None:
                print("missing")
    case Err(msg):
        print(msg)                  # "expected ':' at line 3, column 9"

obj = json.object()
obj.set("ok", json.from_bool(true)).set("items", json.array([json.from_int(1)]))
print(obj.dump())                   # {"ok":true,"items":[1]}
```

### Value model

`json.Value` is a record. `kind` is one of `"null"`, `"bool"`, `"int"`,
`"float"`, `"string"`, `"array"`, or `"object"`, and only the matching field
is meaningful. Integers that fit in 64 bits are `"int"`; other numbers are
`"float"`. Objects keep insertion order, and duplicate keys in the input are
kept as written. Values are references: `set` and `push` change the value in
place and return it, so calls chain.

| | |
|---|---|
| Constructors | `null()`, `from_bool(b)`, `from_int(i)`, `from_float(f)`, `from_str(s)`, `array(items)`, `object()` |
| Access | `get(key) -> Option[Value]`, `at(i) -> Option[Value]`, `size()`, `is_null()` |
| Typed reads | `as_str()`, `as_int()`, `as_float()` (any number), `as_bool()` return `Option`; `as_str_or(d)`, `as_int_or(d)` take a default |
| Building | `set(key, v)` (replaces an existing key), `push(v)` |
| Output | `dump()` / `json.dump(v)`: compact text, like Python's `json.dumps(..., separators=(",", ":"), ensure_ascii=False)`. `json.quote(s)` gives one string literal |

### Parsing

`json.parse(text) -> Result[Value, string]` accepts one RFC 8259 document
with surrounding whitespace.
- Escapes include `\uXXXX`, and surrogate pairs combine into one code point.
- Raw control characters in strings, trailing commas, leading zeros,
  unquoted keys, and trailing data are all errors.
- Errors name what was expected and where, as a 1-based line and byte
  column: `"expected ',' or ']' at line 1, column 4"`.
- Nesting deeper than `json.MAX_DEPTH` (512) is an error, not a stack
  overflow.

### Serialization

`dump` escapes `"`, `\`, `\n`, `\r`, and `\t`, and writes other control
characters as `\u00XX`. Bytes ≥ 0x80 pass through, so UTF-8 text stays
readable. A NaN or infinite float becomes `null`, because JSON has no
representation for them.

## Limits

- Parsing and dumping build strings by concatenation; fine for request-sized
  documents (a 150 KB document parses in milliseconds).
- No streaming parser, no pretty-printing.

## Tests

- `tests/test_e2e_json.py`:
  - 26 valid documents round-trip and compare equal under Python's `json`
  - `dump` output matches Python's compact form
  - 16 invalid documents report the expected message and position
  - a 2000-element document
- `examples/json_basic.rw` (expected output).
- `examples/web_app.rw`'s `/search` builds its body with the module; the web
  e2e test sends a raw quote in the query and checks it is escaped.
- `examples/string_builtins.rw` covers the new builtins.
