# Function values — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement task by task with superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** top-level functions as values (`Callable[[...], R]`), stored in locals and record fields and called indirectly.

**Spec:** `docs/specs/21-function-values.md`

---

## Task 1: parser (commit 1)
- [ ] `TypeCallable`, `CallValue`; `Callable[[...], R]` in `parse_type`; `(` after a non-name postfix expression
- [ ] `tests/test_parser.py`

## Task 2: types + sema (commit 2)
- [ ] `FunctionType`; references (local fn, from-import, `mod.f`); indirect / field / value calls; builtin-as-value error
- [ ] `tests/test_sema.py`

## Task 3: irgen + example (commit 3)
- [ ] Function pointer types, references, indirect calls
- [ ] `tests/test_irgen.py`; `examples/func_values.rw` + `.expected`; e2e list

Verify: `uv run --frozen pytest -q`
