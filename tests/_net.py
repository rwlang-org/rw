"""Helpers for end-to-end tests that run a server binary."""

from __future__ import annotations

import socket
import subprocess
import time


def wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """Block until `proc` accepts TCP connections on `port`.

    A fixed sleep was flaky on a loaded machine: the server had not reached
    listen() yet and the first request was refused. The probe connection is
    closed immediately; the servers treat it as a client that hung up.
    """
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            _, err = proc.communicate(timeout=1.0)
            raise AssertionError(f"server exited early (rc={proc.returncode}): {err!r}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise AssertionError(f"server did not listen on port {port} within {timeout}s") from None
            time.sleep(0.05)
