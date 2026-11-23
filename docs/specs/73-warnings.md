# 73. Warnings, `-W`, and `--warn-as-error`

Issue: #61

## Warnings

A warning is a diagnostic that does not stop the build. It is printed like
an error, with its rule name after the severity:

```
warning[unused-variable]: unused variable `n`
    --> app.rw:5:5
     |
  5  |     n: int = write(fd, b)
     |     ^
     = help: remove it, or name it `_n` if it is unused on purpose
```

Warnings come from the semantic check, so `rwc build`, `rwc run`, and
`rwc check` print them (before `FILE: ok` for `check`). `rwc test` and
`rwc emit-ir` do not. Code in the bundled stdlib never produces warnings.

## Rules

All rules are on by default.

| Rule | Fires on |
|---|---|
| `unused-variable` | a local declared with `x: T = e` or `x = e` (spec 32) that is never read in its function or in a closure inside it |

`unused-variable` details:

- A read is any use of the value: an expression, a call through it, a
  method call on it, or a capture by a closure (spec 71). `x += 1` reads
  `x`. Assigning a new value is not a read.
- Not checked: parameters, loop variables, tuple-unpacking targets, `match`
  bindings, `with ... as` names, and nested `def`s.
- A name starting with `_` is never reported.

## Options

`rwc build`, `rwc run`, and `rwc check` accept:

- `-W no-RULE`: turn `RULE` off. `-W RULE` turns it back on; the last
  option for a rule wins. An unknown rule name is a usage error (exit
  status 2). The option may be repeated.
- `--warn-as-error`: report every enabled warning as an error
  (`error[RULE]: ...`) and fail with exit status 1. `build` and `run` do
  not link or run anything. Disabled rules are not counted.

## Design notes

- Warnings are collected in `SemaResult.warnings` rather than printed by
  sema, so the CLI decides what to show and the library API stays quiet.
- Uses are found with the same hook that finds closure captures, which
  already has to see every read of a local.
- Per-line suppression comments are left for later; the `_` prefix covers
  the common case of a value that is unused on purpose.
