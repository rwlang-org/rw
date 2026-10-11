# rw string builtins: split, join, strip, replace, case, contains, ends_with

## Context

Spec 18 added the minimum for parsing (`str_find`, `str_slice`, ...). Everyday
string work still needs the operations every language ships; the web
framework had to implement `split`, `trim`, and an ASCII `lower` in rw itself
(`examples/web.rw`), the last one with a 26-entry lookup per character.
Issue #39 lists them.

## Goals

| Builtin | Result |
|---|---|
| `str_split(s, sep) -> List[string]` | the pieces between occurrences of `sep`; `"a,,b"` → `["a", "", "b"]`; an empty `sep` gives `[s]` |
| `str_join(parts: List[string], sep) -> string` | the parts with `sep` between them |
| `str_strip(s) -> string` | `s` without leading and trailing ASCII whitespace (space, `\t`, `\n`, `\r`, `\v`, `\f`) |
| `str_replace(s, old, new) -> string` | every non-overlapping `old` replaced, left to right; an empty `old` returns `s` |
| `str_lower(s)` / `str_upper(s) -> string` | ASCII letters converted; other bytes unchanged |
| `str_contains(s, sub) -> bool` | `str_find(s, sub) >= 0` |
| `str_ends_with(s, suffix) -> bool` | the counterpart of `str_starts_with` |

`examples/web.rw` uses them instead of its hand-written versions.

## Non-Goals

- Unicode case mapping or whitespace (bytes only, as in spec 18; UTF-8 is #106)
- `lstrip` / `rstrip`, `rfind`, `split` with a limit, regular expressions
- Method syntax (`s.split(",")`)

## Design

All are builtins in sema's signature table (spec 18), which now compares
types with `==` so that `List[string]` parameters match structurally.
`str_strip` returns a zero-copy slice; `str_split` builds the list in one
pass into a single buffer of `rw_str` (the generic list layout of spec 22),
with each piece a zero-copy slice of `s`; the others allocate their result.

```c
void    rw_str_split    (rw_list *out, rw_str s, rw_str sep);
rw_str  rw_str_join     (const rw_list *parts, rw_str sep);
rw_str  rw_str_strip    (rw_str s);
rw_str  rw_str_replace  (rw_str s, rw_str old, rw_str new_);
rw_str  rw_str_lower    (rw_str s);
rw_str  rw_str_upper    (rw_str s);
int8_t  rw_str_contains (rw_str s, rw_str sub);
int8_t  rw_str_ends_with(rw_str s, rw_str suffix);
```

Layers touched: sema / irgen / runtime (3).
