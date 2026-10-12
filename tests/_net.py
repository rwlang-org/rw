"""Helpers for end-to-end tests that run a server binary."""

from __future__ import annotations

import subprocess
import time

# Port 0 instead of a "free" port picked in advance (spec 50, #163): the
# kernel assigns the port at listen() and the server announces it, so two
# tests can never race for the same port and no readiness probe is needed.
_ANNOUNCE = '''

def rw_test_announce(ln: int) -> int:
    write(1, bytes_from_str(local_addr(ln) + "\\n"))
    return ln
'''


def listen_on_port_zero(src: str) -> str:
    """Rewrite the program's `tcp_listen(...)` call to listen on
    127.0.0.1:0 and print the bound address as its first output line."""
    import re

    new, n = re.subn(r"tcp_listen\([^)]*\)", 'rw_test_announce(listen("tcp://127.0.0.1:0"))', src, count=1)
    assert n == 1, "no tcp_listen(...) call to rewrite"
    return new + _ANNOUNCE


def start_announced(argv: list[str], env: dict | None = None, timeout: float = 10.0) -> tuple[subprocess.Popen, int]:
    """Start a server built from `listen_on_port_zero` source and return
    (proc, port) once it has announced its address. The rest of its stdout
    is drained in the background so a chatty server never blocks on a full
    pipe; it is kept in `proc.rest_of_stdout` (a list of byte chunks)."""
    import selectors
    import threading

    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    line = b""
    deadline = time.monotonic() + timeout
    while not line.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0 or not sel.select(left):
            proc.kill()
            raise AssertionError(f"server did not announce its address within {timeout}s")
        b = proc.stdout.read1(1) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
        if not b:
            _, err = proc.communicate(timeout=1.0)
            raise AssertionError(f"server exited early (rc={proc.returncode}): {err!r}")
        line += b
    sel.close()
    addr = line.decode().strip()
    assert addr.startswith("tcp://127.0.0.1:"), addr
    proc.rest_of_stdout = []

    def drain() -> None:
        while chunk := proc.stdout.read1(65536):
            proc.rest_of_stdout.append(chunk)

    threading.Thread(target=drain, daemon=True).start()
    return proc, int(addr.rsplit(":", 1)[1])
