# Changelog

## [v0.0.6](https://github.com/rwlang-org/rw/compare/v0.0.5...v0.0.6) - 2026-06-21

- ci: add lint/format checks with ruff/black/mypy (#76) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/124
- chore: add pre-commit-hooks configuration (#88) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/125
- docs: add CONTRIBUTING.md (#70) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/126
- docs: add Issue / PR templates (#72) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/127
- ci: SHA-pin GitHub Actions and add dependabot (#81) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/128
- docs: add CoC / SECURITY / LICENSE (#71) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/129

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
