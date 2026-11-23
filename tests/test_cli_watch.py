"""rwc watch (spec 72)."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PROG = 'def main() -> int:\n    print("{msg}")\n    return 0\n'
# Writes straight to fd 1: print goes through stdio, which a pipe buffers
# until exit, and this program never exits on its own.
SERVER = (
    'import greet\n\ndef main() -> int:\n    write_all(1, bytes_from_str(greet.msg() + "\\n"))\n'
    "    while true:\n        sleep(1000)\n    return 0\n"
)


class Watcher:
    """`rwc watch` in a subprocess, with stdout and stderr merged into a
    queue of lines."""

    def __init__(self, src: Path, *extra: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "rwc.cli", "watch", "--interval", "0.1", str(src), *extra],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.seen: list = []

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def expect(self, needle: str, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            self.seen.append(line)
            if needle in line:
                return
        raise AssertionError(f"{needle!r} not seen in:\n" + "\n".join(self.seen))

    def close(self) -> int:
        self.proc.send_signal(signal.SIGINT)
        code = self.proc.wait(timeout=30)
        if code != 0:
            time.sleep(0.2)
            while not self.lines.empty():
                print(self.lines.get())
        return code


def _touch(path: Path, text: str) -> None:
    # Bump the mtime past the previous write even on coarse-grained clocks.
    st = path.stat()
    path.write_text(text)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_rebuilds_on_change_and_survives_errors(tmp_path):
    src = tmp_path / "p.rw"
    src.write_text(PROG.format(msg="one"))
    w = Watcher(src)
    try:
        w.expect("one")
        w.expect("exited with status 0")
        _touch(src, "def main() -> int:\n    return nope\n")
        w.expect("change detected")
        w.expect("nope")
        w.expect("build failed")
        _touch(src, PROG.format(msg="two"))
        w.expect("two")
    finally:
        assert w.close() == 0


def test_change_to_imported_module_restarts_running_program(tmp_path):
    lib = tmp_path / "greet.rw"
    lib.write_text('def msg() -> string:\n    return "hello"\n')
    src = tmp_path / "server.rw"
    src.write_text(SERVER)
    w = Watcher(src)
    try:
        w.expect("hello")
        _touch(lib, 'def msg() -> string:\n    return "bye"\n')
        w.expect("change detected")
        w.expect("bye")
    finally:
        assert w.close() == 0


def test_passes_arguments(tmp_path):
    src = tmp_path / "a.rw"
    src.write_text("def main() -> int:\n    args = os_args()\n    print(args[1])\n    return 0\n")
    w = Watcher(src, "--", "hi")
    try:
        w.expect("hi")
    finally:
        assert w.close() == 0


def test_missing_file(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "rwc.cli", "watch", str(tmp_path / "none.rw")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 1
    assert "no such file" in r.stderr
