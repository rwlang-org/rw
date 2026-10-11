# rw compound assignment (`+=` and friends)

## Context

Every counter in rw is written `i = i + 1`, and every accumulating string
`out = out + piece`. Python, Go, and C all have compound assignment; its
absence is one of the first things noticed when writing rw.

## Goals

- `x OP= e` for `OP` in `+ - * / % & | ^`, on locals and on record fields
  (`p.count += 1`, `a.b.total -= n`)
- Same typing and semantics as `x = x OP e` (so `s += "!"` works on strings)

## Non-Goals

- `<<=` / `>>=` (three-character tokens; rare)
- Compound assignment to a field reached through a call (`f().x += 1`):
  rejected, because the desugaring would evaluate `f()` twice
- Assignment as an expression, chained assignment, `++` / `--`

## Design

The lexer emits one token per operator (`PLUS_ASSIGN`, ...). The parser
desugars on the spot, so sema and irgen are unchanged:

| Source | Desugared |
|---|---|
| `x += e` | `Assign(x, BinOp("+", Name(x), e))` |
| `a.b.f *= e` | `FieldAssign(a.b, f, BinOp("*", FieldAccess(a.b, f), e))` |

The record path of a field target may only consist of names and field
accesses; anything else (a call) is a parse error. Repeating such a path
reads the same record again, with no side effects.

Layers touched: lexer / parser (2).
