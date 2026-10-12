# 55. `str_lstrip` / `str_rstrip` / `str_rfind`

Issue: #39

## Context

Spec 28 covered most of #39 but left out one-sided stripping and searching
from the right. Both come up in protocol code: trimming only the trailing
`\r\n`, or splitting a path or `host:port` at its last separator.

## Builtins

| Builtin | Result |
|---|---|
| `str_lstrip(s) -> string` | `s` without leading ASCII whitespace |
| `str_rstrip(s) -> string` | `s` without trailing ASCII whitespace |
| `str_rfind(s, sub) -> int` | byte index of the last occurrence of `sub`, or -1; an empty `sub` gives `len(s)` |

Whitespace is the same set as `str_strip`: space, `\t`, `\n`, `\r`, `\v`,
`\f`. The strips return zero-copy slices.

## Bytes, not code points

Like every string builtin so far (specs 18, 28), indices and lengths count
bytes, and only ASCII is treated specially. A UTF-8 string passes through
unchanged, but an index can point inside a multi-byte character. Code-point
aware operations are #106.

## Tests

`examples/string_builtins.rw` (expected output).
