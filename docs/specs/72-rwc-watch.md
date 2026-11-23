# 72. `rwc watch`

Issue: #49

```
rwc watch [--interval SECONDS] [-O LEVEL] [--unchecked] FILE [-- ARGS...]
```

Builds `FILE`, runs it, and does both again whenever a source changes.
It is the edit, rebuild, rerun loop for servers and small programs.

## Behavior

- `rwc watch` builds `FILE` into a temporary directory and starts it with
  `ARGS` (a leading `--` is dropped). The program's stdin, stdout, and
  stderr are the terminal's.
- Every `--interval` seconds (default 0.5) it compares the modification
  times of every `*.rw` file in `FILE`'s directory with the previous
  check. A change is an edited, added, or removed file. Imports resolve
  to that directory or to the stdlib, so this covers every module of the
  program, including ones that do not exist yet.
- On a change it stops the program and builds and starts it again. Stopping
  sends `SIGTERM` to the program's process group (the program runs in its
  own session, so whatever it spawned is stopped too), and `SIGKILL` if it
  is still running after 2 seconds.
- If the build fails, the diagnostic is printed as `rwc build` would print
  it, and `rwc watch` waits for the next change. A program that exits on
  its own is not restarted until the next change either.
- Status lines go to stderr with the prefix `rwc watch: `:
  `running FILE`, `exited with status N; waiting for changes`,
  `build failed; waiting for changes`, `change detected; restarting`.
- Ctrl-C (`SIGINT`) stops the program and exits with status 0. A `FILE`
  that does not exist at startup is an error (exit status 1).

`-O` and `--unchecked` are as for `rwc build` (specs 70 and 42).

## Design notes

- Changes are found by polling, not by inotify / FSEvents / kqueue. A
  directory of source files is small, polling has no dependency and works
  the same on every platform, and half a second is short next to a build.
- Only `FILE`'s directory is watched, not subdirectories. rw has no
  packages yet (#101); when it does, the watched set will follow the
  import search path.
- The program's stdout is the terminal, so it is line buffered as usual.
  When `rwc watch` itself is piped, `print` output is buffered by stdio
  until the program exits.
