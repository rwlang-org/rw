# 60. `log`: leveled logging

Issue: #41

## Module

`stdlib/log.rw` (spec 54), written in rw.

```python
import log

lg = log.new(2)                 # 1 = stdout, 2 = stderr, or any handle
lg.prefix = "api"
lg.set_level("debug")
lg.info("listening" + log.kv("addr", "127.0.0.1:8080"))
# 2026-10-13T01:02:03.456Z INFO api: listening addr=127.0.0.1:8080
```

| | |
|---|---|
| `log.new(fd) -> log.Logger` | level INFO, no prefix, timestamps on |
| `log.to_file(path) -> Option[log.Logger]` | appends to `path` (created if missing); `None` with `last_error` if it cannot be opened |
| `lg.debug / info / warn / error(msg)` | one record |
| `lg.log(level, msg)`, `lg.enabled(level)` | with an explicit level: `log.DEBUG` (10), `INFO` (20), `WARN` (30), `ERROR` (40) |
| `lg.set_level(name) -> bool` | `"debug"`, `"info"`, `"warn"` / `"warning"`, `"error"` (any case); `false` and no change for other names |
| fields | `fd`, `level`, `prefix`, `timestamps` can be assigned directly |
| `log.kv(key, value)` | `" key=value"`; the value is quoted (with `\` and `"` escaped) when it is empty or contains a space, a quote, `=`, or a control character |
| `log.iso8601(ms)` | Unix time in ms as `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC; any int64 date) |

## Line format

`[<UTC timestamp> ]<LEVEL>[ <prefix>:] <message>\n`. Levels are printed as
`DEBUG`, `INFO`, `WARN`, and `ERROR`.

## Concurrency and buffering

A record is built as one string and written with a single `write_all` under
the logger's `Mutex` (spec 46). Lines from concurrent fibers therefore never
interleave, even when they exceed a pipe's atomic write size.

Output goes straight to the handle, not through `print`'s stdio buffer.
Unlike `print` to a pipe, records are not lost when the process is killed.
The flip side is that mixing `print` and a logger on the same stream can
reorder lines.

## `examples/web.rw`

`App` gains `logger` (`log.new(1)` with prefix `web`). The access log
(`INFO web: GET /path 200`), the shutdown message, and the listen error use
it. `set_log(app, false)` still turns the access log off.

## Tests

`tests/test_e2e_log.py`:
- `iso8601` agrees with Python's `datetime` for 12 timestamps: negative,
  leap days, years 1900 and 9999.
- Level filtering, prefix, `kv` quoting, and stderr output.
- 8 fibers × 100 records to a file opened with `to_file` give 800 intact
  lines after the existing content; an unopenable path gives `ENOENT`.

`tests/test_e2e_shutdown.py` checks the web access log and shutdown lines.
