# 48. `rwc check`

Issue: #46

## Motivation

`rwc build` always runs IR generation, LLVM, and a clang link. For editor
integration and quick feedback we want a command that stops after the type
checker.

## Interface

```
rwc check [-q] FILE...
```

- Runs lex → parse → import loading → desugar → sema for each file. No IR is
  generated, nothing is written to disk, and clang is not invoked.
- Each file that passes prints `FILE: ok` on stdout (suppressed by `-q`).
- Errors are rendered on stderr with the same diagnostic format as
  `rwc build`.
- All files are checked even if an earlier one fails; the exit status is 0
  when every file passes and 1 otherwise.

## Implementation

`driver.check_source(source, filename)` calls `load_program` and
`analyze_program`, converting `LexerError` / `ParserError` into
`CompileError` so the CLI reports every failure the same way.

## Limits

- Only the first error per file is reported (the type checker stops at the
  first error, as in `rwc build`).
- Machine-readable output (e.g. JSON for an LSP) is future work.
