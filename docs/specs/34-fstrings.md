# rw f-strings

## Context

Building strings in rw means `"v" + str(n) + " of " + str(total)`. Python's
f-strings are the expected way to write this (#18).

```rw
print(f"{user.name} is {user.age}, ratio={ratio}")
```

## Goals

- `f"..."` with `{expr}` placeholders: any expression whose type `str()`
  accepts (int, float, bool, string, spec 26), including calls, method calls,
  indexing, and nested string literals (`f"{d["k"]}"`)
- `{{` and `}}` for literal braces; the usual escapes (`\n`, `\t`, ...) in
  the text parts

## Non-Goals

- Format specs and conversions (`{x:.2f}`, `{x!r}`, `{x=}`)
- Multi-line or raw f-strings
- Placeholders of other types (lists, records): `str()` rejects them

## Design

The lexer reads an f-string into one `FSTRING` token whose `parts` are text
pieces (escapes processed) and expression sources. Inside `{...}` it tracks
bracket depth and skips over string literals, so braces and quotes in the
expression do not end the placeholder. A missing `}` and a lone `}` are
lexer errors.

The parser tokenizes and parses each expression source (shifting positions
so diagnostics point into the f-string) and lowers the whole f-string to a
concatenation: `f"a{x}b"` → `"a" + str(x) + "b"`. Sema and irgen are
unchanged.

Layers touched: lexer / parser (2).
