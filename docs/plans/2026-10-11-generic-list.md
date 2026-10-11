# `List[T]` for any element type — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement task by task with superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** generic `List[T]` with `list_new` / `list_push` / `list_at` / `len`, keeping `List[int]` unchanged.

**Spec:** `docs/specs/22-generic-list.md`

---

## Task 1: parser + types + sema (commit 1)
- [ ] `TypeList(elem)`; `ListType` with a canonical `LIST_INT`
- [ ] Generic builtins; `_check_expr_expect` for `list_new()` at typed sites
- [ ] `tests/test_parser.py`, `tests/test_sema.py`

## Task 2: runtime + irgen (commit 2)
- [ ] `rw_list_new` / `rw_list_push` / `rw_list_at` / `rw_list_len`
- [ ] irgen: element spill + size; `tests/test_irgen.py`

## Task 3: example + e2e (commit 3)
- [ ] `examples/generic_list.rw` + `.expected` (strings, floats, bools, Option, records, function values, nested lists, lists in record fields)

Verify: `uv run --frozen pytest -q`
