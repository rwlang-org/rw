# rw OS API: arguments, environment, cwd, exit

## Context

A command-line tool written in rw could not read its arguments or the
environment, change directory, or exit with a status (#37).

## Goals

| Builtin | Result |
|---|---|
| `os_args() -> List[string]` | `argv`, program name first |
| `os_getenv(name) -> Option[string]` | `None` when unset |
| `os_setenv(name, value) -> bool` | overwrite; false on failure |
| `os_exit(code)` | flush stdout / stderr and exit with `code` |
| `os_getcwd() -> string` | current directory |
| `os_chdir(path) -> bool` | false on failure |

The issue's `os.args()` spelling would need namespaced builtins; the `os_`
prefix follows the existing `str_` / `dict_` convention.

## Non-Goals

- `unsetenv`, listing the environment, process spawning (#42), signals (#168)
- Running deferred cleanup or joining fibers on `os_exit`

## Design

The C `main` emitted by the compiler now takes `(argc, argv)` and passes
them to `rw_set_args` before `rw_init`. `Option[string]` is returned through
a pointer to `{int64 tag, rw_str value}`, the same layout as the LLVM
`Option[string]` struct.

Layers touched: sema / irgen / runtime (3).
