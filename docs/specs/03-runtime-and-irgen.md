# rw Runtime ABI and IR Generation Strategy

## Runtime ABI

Header `runtime/runtime.h`:

```c
#ifndef RW_RUNTIME_H
#define RW_RUNTIME_H
#include <stdint.h>

typedef struct { int64_t len; const char *ptr; } rw_str;
typedef struct rw_future rw_future_t;

/* print */
void rw_print_i64(int64_t v);
void rw_print_f64(double v);
void rw_print_bool(int8_t v);
void rw_print_str(rw_str s);

/* string helpers */
rw_str rw_str_from_cstr(const char *cstr, int64_t len);

/* spawn / await (separated per return type) */
rw_future_t *rw_spawn_i64 (int64_t (*fn)(void *), void *args);
rw_future_t *rw_spawn_f64 (double  (*fn)(void *), void *args);
rw_future_t *rw_spawn_bool(int8_t  (*fn)(void *), void *args);
rw_future_t *rw_spawn_str (rw_str  (*fn)(void *), void *args);
rw_future_t *rw_spawn_void(void    (*fn)(void *), void *args);

int64_t rw_await_i64 (rw_future_t *f);
double  rw_await_f64 (rw_future_t *f);
int8_t  rw_await_bool(rw_future_t *f);
rw_str  rw_await_str (rw_future_t *f);
void    rw_await_void(rw_future_t *f);

/* process init / shutdown (rwc inserts these at the start/end of main) */
void rw_init(void);
void rw_shutdown(void);

#endif
```

Implementation strategy (runtime.c):
- `rw_spawn_*` internally calls `pthread_create` and stores the thread ID and a result slot in the `rw_future_t`
- `rw_await_*` calls `pthread_join`, then returns the result and `free`s the `rw_future_t`
- `rw_print_*` simply calls the `printf` family
- `rw_init` / `rw_shutdown` may be empty in the MVP (reserved for a future thread pool)

## IR expansion of `spawn f(a, b)`

rw code:
```python
fut: Future[int] = spawn add(3, 4)
```

What rwc generates:

1. **A closure struct** defined anonymously:
   ```llvm
   %closure_add_0 = type { i64, i64 }
   ```

2. **A trampoline function** (unique per call site):
   ```llvm
   define i64 @rw_trampoline_add_0(i8* %args) {
       %p  = bitcast i8* %args to %closure_add_0*
       %ap = getelementptr %closure_add_0, %closure_add_0* %p, i32 0, i32 0
       %bp = getelementptr %closure_add_0, %closure_add_0* %p, i32 0, i32 1
       %a  = load i64, i64* %ap
       %b  = load i64, i64* %bp
       %r  = call i64 @rw_user_add(i64 %a, i64 %b)
       call void @free(i8* %args)
       ret i64 %r
   }
   ```

3. **The call site** (expansion of the spawn expression):
   ```llvm
   %args = call i8* @malloc(i64 16)
   ; store %a, %b into the struct
   %fut  = call %rw_future_t* @rw_spawn_i64(
       i64 (i8*)* @rw_trampoline_add_0, i8* %args)
   ```

## IR expansion of `await fut`

The return type is known from Sema. The corresponding `rw_await_*` is called directly:
```llvm
%v = call i64 @rw_await_i64(%rw_future_t* %fut)
```

## The main function

rwc requires `def main() -> int:`, and the generated IR is:

```llvm
define i32 @main() {
    call void @rw_init()
    %r   = call i64 @rw_user_main()
    call void @rw_shutdown()
    %r32 = trunc i64 %r to i32
    ret i32 %r32
}
```

`@rw_user_main` is the renamed form of the user-defined `main`.

## Memory management (MVP)

| Subject | Strategy |
|---|---|
| String literals | Placed in `.rodata`. `rw_str` is length + pointer. Never freed |
| Closure structs | `malloc` / `free`d at the end of the trampoline |
| Future | `free`d inside `rw_await_*` |
| User-defined types | Do not exist in the MVP |

No GC is introduced. As long as strings are not dynamically generated, no leaks occur.

When features that entail dynamic heap allocation — such as `list[T]` or string
concatenation — are added later, a memory-management strategy (ARC / mark-and-sweep GC /
ownership) will need to be chosen. **That decision is deferred for now.** The inputs
needed to decide will only be clear once the following are settled:

- What users want to write (a web server? numerical computing? Python-library interop?)
- Concurrency performance (how well fibers and GC interact, the cost of atomic retain/release in ARC)
- The shape of the standard library (how strings, arrays, and dicts fit into the language spec)

We plan to eventually create `docs/specs/06-memory-tbd.md` to organize these discussion points.

## Error handling (MVP)

No exceptions, no Result type. Conditions that cause the process to die at runtime:
- Integer division by zero → left to LLVM `sdiv`'s undefined behavior
- Thread creation failure → `perror` + `exit(1)` inside `rw_spawn_*`
- malloc failure → same as above

## Reserved for the future (not-yet-implemented error in the MVP)

```python
extern "c" def name(arg: int) -> int
```

This syntax is accepted by the Lexer/Parser but Sema emits a "not implemented" error.
This is for backward compatibility when it is implemented in Phase 2+.
