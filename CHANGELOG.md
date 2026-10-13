# Changelog

## [v0.0.14](https://github.com/rwlang-org/rw/compare/v0.0.13...v0.0.14) - 2026-10-13

- feat: closures (nested def and lambda, capture by value) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/231
- fix: stdlib json floats, fs/process last_error, stdlib import isolation, leading-zero literals by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/233
- docs: language reference, runtime design, and stdlib reference (en + ja) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/234

## [v0.0.13](https://github.com/rwlang-org/rw/compare/v0.0.12...v0.0.13) - 2026-10-12

- feat: garbage collection with the Boehm GC by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/224
- feat: rwc test and assert_eq by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/226
- ci: test Python 3.11, 3.12 and 3.13 by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/227
- feat: UDP (udp:// for dial / listen, recv_from, send_to) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/228
- feat: -O0..-O3 for rwc build / run, -O2 by default by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/229

## [v0.0.12](https://github.com/rwlang-org/rw/compare/v0.0.11...v0.0.12) - 2026-10-12

- ci: bump astral-sh/setup-uv from 10.1.0 to 10.2.0 by @dependabot[bot] in https://github.com/rwlang-org/rw/pull/179
- ci: bump Songmu/tagpr from 1.6.1 to 1.21.2 by @dependabot[bot] in https://github.com/rwlang-org/rw/pull/180
- feat: json stdlib module, byte_at / chr / str_to_float by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/215
- feat: path and fs stdlib modules by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/216
- feat: log stdlib module; web.rw logs through it by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/217
- feat: process stdlib module (spawn, pipes, wait, kill) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/218
- feat: url module; decoded paths/queries, forms, and cookies in web.rw by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/219
- feat: static files, before/after middleware, and HEAD in web.rw by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/220
- feat: keep-alive and pipelining in web.rw by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/221
- feat: chunked request bodies in web.rw by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/222
- feat: template stdlib module (Mustache subset over json.Value) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/223

## [v0.0.11](https://github.com/rwlang-org/rw/compare/v0.0.10...v0.0.11) - 2026-10-12
- feat: local type inference (x = expr declares x) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/182
- feat: methods on records by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/184
- test: wait for e2e servers to listen instead of a fixed sleep by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/185
- feat: f-strings by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/186
- feat: module constants by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/187
- feat: dict literals by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/188
- feat: tuples and multiple return values by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/189
- feat: match on int / string / bool values by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/190
- feat: sleep, time_ms, and mono_ns by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/191
- feat: OS API (args, environment, cwd, exit) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/192
- feat: default and keyword arguments by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/193
- feat: run-time checks for integer division by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/194
- feat: random numbers by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/195
- fix: loops may reuse an existing variable name by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/196
- feat: channels (Channel[T]) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/197
- feat: await_all and await_any by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/198
- feat: fiber-aware Mutex by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/199
- fix(runtime): await_any returns the first future to finish by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/200
- feat: with and defer by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/201
- feat: rwc check (type-check without codegen) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/202
- feat: did-you-mean suggestions and conversion hints by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/203
- feat: dial / listen / accept / open with address strings (RFC #175) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/204
- feat: last_error / error_name / error_message for I/O builtins by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/205
- feat: write_all builtin by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/206
- feat: read / write / accept / connect timeouts by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/207
- feat: stdlib search path and bufio.Reader by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/208
- feat: str_lstrip / str_rstrip / str_rfind by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/209
- feat: shutdown / set_nodelay / set_keepalive / set_backlog by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/210
- test: run e2e servers on port 0 and read the announced address by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/211
- test: eight fibers accepting on one listener by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/212
- feat: signal_wait, close wakes parked fibers, graceful web shutdown by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/213

## [v0.0.10](https://github.com/rwlang-org/rw/compare/v0.0.9...v0.0.10) - 2026-10-12
- feat: int()/float()/str() conversions and string ordering by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/170
- feat: compound assignment (+= and friends) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/171
- feat: string builtins (split, join, strip, replace, lower, upper, contains, ends_with) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/173
- fix: continue in a range-for no longer loops forever by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/174
- fix(irgen): allocate locals in the entry block (loops overflowed fiber stacks) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/176
- feat: list literals, indexing, slicing, and for-each by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/177
- feat: generic Option[T] and Result[T, E] by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/178
- feat: Dict[K, V] by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/181

## [v0.0.9](https://github.com/rwlang-org/rw/compare/v0.0.8...v0.0.9) - 2026-10-11
- feat: preempt long-running fibers at safe points by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/155
- fix(runtime): make parked fibers wakeable only after their context is saved by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/157
- feat(runtime): offload blocking fds (stdin, pipes) to the aio pool by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/158

## [v0.0.8](https://github.com/rwlang-org/rw/compare/v0.0.7...v0.0.8) - 2026-10-11
- feat: string search and conversion builtins + HTTP hello server by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/147
- fix(runtime): detach fire-and-forget fibers and free handles safely by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/149
- feat: records (class with fields) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/150
- feat: import records from other modules by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/151
- feat: function values (Callable[[...], R]) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/152
- feat: List[T] for any element type by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/153
- feat: a web application framework written in rw by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/154

## [v0.0.7](https://github.com/rwlang-org/rw/compare/v0.0.6...v0.0.7) - 2026-10-11
- ci: bump actions/checkout from 4.2.2 to 7.0.1 by @dependabot[bot] in https://github.com/rwlang-org/rw/pull/140
- ci: bump astral-sh/setup-uv from 6.1.0 to 10.1.0 by @dependabot[bot] in https://github.com/rwlang-org/rw/pull/143
- docs: translate all Japanese docs to English by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/136
- feat: namespaced import (PR1: plain import + qualified calls) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/133
- feat: from import (PR2: from x import y [as w]) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/134
- feat: import as (PR3: import x as m) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/135

## [v0.0.6](https://github.com/rwlang-org/rw/compare/v0.0.5...v0.0.6) - 2026-10-11

- ci: add lint/format checks with ruff/black/mypy (#76) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/124
- chore: add pre-commit-hooks configuration (#88) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/125
- docs: add CONTRIBUTING.md (#70) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/126
- docs: add Issue / PR templates (#72) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/127
- ci: SHA-pin GitHub Actions and add dependabot (#81) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/128
- docs: add CoC / SECURITY / LICENSE (#71) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/129
- ci: fix tagpr action SHA pin by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/144

## [v0.0.5](https://github.com/rwlang-org/rw/compare/v0.0.4...v0.0.5) - 2026-06-06

- examples: add a TCP chat server sample by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/113
- lang: implement the ternary / conditional expression (x if cond else y) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/114
- docs: add the language spec for the conditional (ternary) expression by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/115
- stdlib: file I/O and generic fd read/write/close (#33) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/116
- runtime: make file I/O asynchronous (offload to a thread pool) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/117
- lang: extend numeric literals (hex, octal, binary, exponent, underscore separators) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/118
- lang: implement bitwise operators (& | ^ ~ << >>) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/119
- lang: implement break / continue statements (targeting while loops) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/120
- lang: implement the assert statement by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/121
- lang: implement type aliases (type Foo = ...) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/122
- stdlib: implement math built-in functions (LLVM intrinsics) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/123

## [v0.0.4](https://github.com/rwlang-org/rw/compare/v0.0.3...v0.0.4) - 2026-05-23
- rwc + runtime: netpoller + minimal TCP API (echo server) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/97

## [v0.0.3](https://github.com/rwlang-org/rw/compare/v0.0.2...v0.0.3) - 2026-05-23
- rwc: minimal string extensions (len, ==, +) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/91
- rwc: Bytes type (immutable, minimal echo set) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/92
- rwc: List[int] type (immutable, monomorphic, minimal echo set) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/93
- rwc: Option[int] type and minimal match syntax (Python 3.10 style) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/94
- rwc: Result[int, int] type and Ok/Err arm extension for match by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/95

## [v0.0.2](https://github.com/rwlang-org/rw/compare/v0.0.1...v0.0.2) - 2026-05-20
- evolve the runtime into a Go-style M:N scheduler by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/90

## [v0.0.1](https://github.com/ryuichi1208/rw/commits/v0.0.1) - 2026-05-17
- rw v0.0.1: MVP compiler with fiber-backed runtime by @ryuichi1208 in https://github.com/ryuichi1208/rw/pull/1
- ci: automate releases with tagpr by @ryuichi1208 in https://github.com/ryuichi1208/rw/pull/2
- fix(ci): pass GITHUB_TOKEN to tagpr by @ryuichi1208 in https://github.com/ryuichi1208/rw/pull/3
