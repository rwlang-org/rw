# Fiber lifecycle (detach + safe reclamation) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement task by task with superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** free a fire-and-forget fiber when it finishes, and make every handle free happen exactly once after the fiber left its stack.

**Spec:** `docs/specs/19-fiber-lifecycle.md`

---

## Task 1: C test + pytest runner (commit 1)

- [ ] `runtime/fiber/test_detach.c`: for RW_WORKERS in {1, 2, 4}: spawn N detached fibers and wait for `rw_sched_live_fibers() == 0`; spawn N fibers and join them, then check the live count is 0 once the workers have marked them finished
- [ ] `tests/test_runtime_c.py`: compile `test_detach.c` (and `test_shutdown.c`) against `runtime/librw.a` and run them

## Task 2: runtime (commit 2)

- [ ] `reap` field, `release()`, worker-side `REAP_FINISHED` mark, `rw_sched_detach`, `rw_sched_live_fibers`
- [ ] `rw_detach` in runtime.c/h

Verify: `make -C runtime && uv run --frozen pytest tests/test_runtime_c.py -q`

## Task 3: irgen (commit 3)

- [ ] Failing test: statement `spawn f()` emits `rw_detach`; `x: Future[int] = spawn f()` does not
- [ ] Emit `rw_detach` for statement-level spawns

Verify: `uv run --frozen pytest -q`
