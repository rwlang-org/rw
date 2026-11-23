"""`rwc watch`: rebuild and rerun a program whenever its sources change
(spec 72)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .diagnostics import CompileError
from .driver import compile_source

Snapshot = dict[str, float]


def _say(msg: str) -> None:
    print(f"rwc watch: {msg}", file=sys.stderr, flush=True)


def snapshot(entry: Path) -> Snapshot:
    """Modification times of every `.rw` file next to `entry`. Imports
    resolve to that directory (or the stdlib, which does not change while
    you edit), so this covers the program's own modules, including ones
    that do not exist yet."""
    snap: Snapshot = {}
    for p in sorted(entry.parent.glob("*.rw")):
        try:
            snap[str(p)] = p.stat().st_mtime_ns
        except OSError:
            pass  # removed between glob and stat
    return snap


def _stop(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """SIGTERM the child's process group, then SIGKILL it after `grace`
    seconds."""
    if proc.poll() is not None:
        return
    # killpg fails with ESRCH once the group is gone, and on macOS with
    # EPERM while its leader is an unreaped zombie; wait() settles both.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()


def watch(
    entry: Path,
    *,
    interval: float = 0.5,
    checked: bool = True,
    opt_level: int | None = None,
    report_error: Callable[[CompileError, str, Path], None],
    args: list[str] | None = None,
) -> int:
    """Build and run `entry`; on every change to a `.rw` file in its
    directory, stop the running program and do it again. Runs until
    interrupted (Ctrl-C), then stops the program and returns 0."""
    args = args or []
    proc: subprocess.Popen | None = None
    with tempfile.TemporaryDirectory() as td:
        exe = Path(td) / entry.stem
        try:
            while True:
                snap = snapshot(entry)
                proc = _start(entry, exe, checked, opt_level, report_error, args)
                reported = proc is None
                while True:
                    time.sleep(interval)
                    if proc is not None and not reported and proc.poll() is not None:
                        _say(f"exited with status {proc.returncode}; waiting for changes")
                        reported = True
                    if snapshot(entry) != snap:
                        break
                if proc is not None:
                    _stop(proc)
                _say("change detected; restarting")
        except KeyboardInterrupt:
            if proc is not None:
                _stop(proc)
            return 0


def _start(
    entry: Path,
    exe: Path,
    checked: bool,
    opt_level: int | None,
    report_error: Callable[[CompileError, str, Path], None],
    args: list[str],
) -> subprocess.Popen | None:
    """Compile `entry` to `exe` and start it in its own process group.
    Returns None, after printing the error, when the source does not read
    or compile."""
    try:
        source = entry.read_text(encoding="utf-8")
    except OSError as e:
        _say(f"cannot read {entry}: {e.strerror}; waiting for changes")
        return None
    try:
        compile_source(source, filename=str(entry), output=exe, checked=checked, opt_level=opt_level)
    except CompileError as e:
        report_error(e, source, entry)
        _say("build failed; waiting for changes")
        return None
    _say(f"running {entry}")
    # A new session so _stop can signal everything the program spawned.
    return subprocess.Popen([str(exe), *args], start_new_session=True)
