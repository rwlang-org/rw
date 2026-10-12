"""Boehm GC integration (spec 67): live data survives collections wherever
it is referenced from, and garbage is reclaimed."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ._net import listen_on_port_zero, start_announced

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _build(src: str, files: dict[str, str] | None = None, entry: str = "p.rw") -> Path:
    td = Path(tempfile.mkdtemp())
    (td / "p.rw").write_text(src)
    for name, text in (files or {}).items():
        (td / name).write_text(text)
    exe = td / "prog"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(td / entry), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    return exe


SURVIVAL = """
class Box:
    name: string
    items: List[string]
    counts: Dict[string, int]

def make(id: int) -> Box:
    xs: List[string] = []
    d: Dict[string, int] = {}
    for i in range(0, 300):
        s = f"{id}-{i}-" + str(i * 7)
        xs = list_push(xs, s)
        d[s] = i
    return Box(f"box{id}", xs, d)

def check(b: Box, id: int) -> bool:
    if b.name != f"box{id}" or len(b.items) != 300:
        return false
    for i in range(0, 300):
        s = f"{id}-{i}-" + str(i * 7)
        if b.items[i] != s or b.counts[s] != i:
            return false
    return true

# Builds data, parks (sleep or a channel receive), then checks it: while
# parked, the data is reachable only from this fiber's own stack.
def holder(id: int, go: Channel[int]) -> int:
    b = make(id)
    sleep(20 + id % 30)
    chan_recv(go)
    gc_collect()
    return 1 if check(b, id) else 0

# Allocates garbage to force collections while the holders are parked.
def churn(rounds: int) -> int:
    junk = 0
    for r in range(0, rounds):
        xs: List[string] = []
        for i in range(0, 200):
            xs = list_push(xs, "garbage-" + str(i) + "-" + str(r))
        junk += len(xs)
        if r % 50 == 0:
            gc_collect()
    return junk

def main() -> int:
    go: Channel[int] = chan_new(1000)
    fs: List[Future[int]] = []
    for id in range(0, 200):
        fs = list_push(fs, spawn holder(id, go))
    cs: List[Future[int]] = []
    for k in range(0, 4):
        cs = list_push(cs, spawn churn(400))
    await_all(cs)
    # Values queued in a channel buffer survive too.
    boxes: Channel[Box] = chan_new(50)
    for id in range(0, 50):
        chan_send(boxes, make(1000 + id))
    gc_collect()
    ok_boxes = 0
    for id in range(0, 50):
        match chan_recv(boxes):
            case Some(b):
                if check(b, 1000 + id):
                    ok_boxes += 1
            case None:
                ok_boxes += 0
    for id in range(0, 200):
        chan_send(go, id)
    ok = 0
    for r in await_all(fs):
        ok += r
    print(ok)
    print(ok_boxes)
    return 0
"""


def test_live_data_survives_collections():
    exe = _build(SURVIVAL)
    for _ in range(3):
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120,
                           env={**os.environ, "RW_WORKERS": "4"})
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["200", "50"]


GARBAGE = """
def main() -> int:
    start = gc_heap_size()
    total = 0
    for r in range(0, 2000):
        s = ""
        for i in range(0, 100):
            s = s + "0123456789"
        total += len(s)
    print(total)
    # 2000 * ~50 KiB of strings were built; the heap stays far smaller.
    print(gc_heap_size() < 64 * 1024 * 1024)
    return 0
"""


def test_garbage_is_reclaimed():
    r = subprocess.run([str(_build(GARBAGE))], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [str(2000 * 1000), "true"]


def _rss_kib(pid: int) -> int:
    return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout)


def test_web_server_memory_stays_bounded():
    app = (EXAMPLES / "web_app.rw").read_text().replace(
        "app: App = web.new_app()", "app: App = web.new_app()\n    web.set_log(app, false)", 1)
    web = listen_on_port_zero((EXAMPLES / "web.rw").read_text())
    exe = _build(app, {"web.rw": web, "web_app.rw": app}, entry="web_app.rw")
    proc, port = start_announced([str(exe)], env={**os.environ, "RW_WORKERS": "2"})
    try:
        def burst(n: int) -> None:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                f = s.makefile("rb")
                for i in range(n):
                    s.sendall(f"GET /search?q=item{i}&page={i} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                    while f.readline() not in (b"\r\n", b""):
                        pass
                    f.read(len(f'{{"q":"item{i}","page":"{i}"}}\n'))

        burst(1000)
        time.sleep(0.2)
        base = _rss_kib(proc.pid)
        for _ in range(5):
            burst(1000)
        time.sleep(0.2)
        grown = _rss_kib(proc.pid) - base
        # Without a collector this leaked several KiB per request (~25 MiB
        # for these 5000); with it the heap levels off.
        print(f"RSS growth over 5000 requests: {grown} KiB")
        assert grown < 10 * 1024, f"RSS grew by {grown} KiB over 5000 requests"
    finally:
        proc.kill()
