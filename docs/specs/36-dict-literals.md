# rw dict literals

## Context

`Dict[K, V]` (spec 31) could only be built with `dict_new()` and one
assignment per entry, because the lexer had no braces.

## Goals

- `{k1: v1, ..., kn: vn}`: the key type (int or string) and value type come
  from the first entry, every other entry must match; with an expected type
  (declaration, argument, field, ...) the values are checked against it
- `{}` takes its type from the expected type; without one it is an error,
  including `d = {}` (spec 32 requires an annotation)
- A repeated key keeps the last value, as in Python

## Non-Goals

- Dict comprehensions, set literals, multi-line literals

## Design

The lexer adds `LBRACE` / `RBRACE`; the parser adds `DictLit(keys, values)`.
irgen lowers a literal to `rw_dict_new` followed by one `rw_dict_set` per
entry, in source order.

Layers touched: lexer / parser / sema / irgen (4).
