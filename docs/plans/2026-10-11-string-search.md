# String search and conversion builtins — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement task by task with superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** add `str_find`, `str_slice`, `str_starts_with`, `str_from_int`, and `str_to_int` so that a minimal HTTP/1.1 server can be written in rw.

**Spec:** `docs/specs/18-string-search.md`

**Tech Stack:** Python (rwc: sema/irgen via llvmlite), C runtime, pytest, uv (always `uv run --frozen`).

---

## File Structure

- `runtime/runtime.h`, `runtime/runtime.c` (modify) — five `rw_str_*` helpers
- `rwc/sema.py` (modify) — builtin set + type checks
- `rwc/irgen.py` (modify) — declarations + call emission
- `examples/string_search.rw`, `examples/string_search.rw.expected` (new)
- `examples/http_hello.rw` (new)
- `tests/test_sema.py`, `tests/test_irgen.py`, `tests/test_e2e.py` (modify), `tests/test_e2e_http.py` (new)

---

## Task 1: sema (commit 1)

- [ ] Failing tests in `tests/test_sema.py`: each builtin type-checks with the right result type; wrong arg count and wrong arg types are errors; a user function named `str_find` is rejected
- [ ] Add the five names to `_BUILTIN_FUNCS` and their checks in `_check_call`

Verify: `uv run --frozen pytest tests/test_sema.py -q`

## Task 2: runtime + irgen (commit 2)

- [ ] Failing test in `tests/test_irgen.py`: IR for a program using all five contains `rw_str_find`, `rw_str_slice`, `rw_str_starts_with`, `rw_str_from_int`, `rw_str_to_int`
- [ ] Implement the C helpers (clamped zero-copy slice, overflow-checked parse)
- [ ] Declare them in irgen; emit `str_to_int` through an `RW_OPTION_INT_TY` stack slot

Verify: `uv run --frozen pytest tests/test_irgen.py -q`

## Task 3: examples + e2e (commit 3)

- [ ] `examples/string_search.rw` covering found / not found / empty needle, clamped slices, negative and zero `str_from_int`, `str_to_int` Some/None cases; `.expected`
- [ ] Add `string_search` to the `test_e2e.py` parametrize list
- [ ] `examples/http_hello.rw`: parse the request line, route `/` and `/hello`, 404 otherwise, write a response with `Content-Length`, close
- [ ] `tests/test_e2e_http.py`: build with a free port, send GET requests from Python, check status line, headers, and body for `/`, `/hello`, and an unknown path

Verify:
```sh
uv run --frozen pytest tests/test_e2e.py -k string_search tests/test_e2e_http.py -q
uv run --frozen pytest -q
```
