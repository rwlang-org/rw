"""stdlib/log.rw (spec 60)."""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(src: str, *args: str) -> subprocess.CompletedProcess:
    td = Path(tempfile.mkdtemp())
    (td / "p.rw").write_text(src)
    exe = td / "p"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(td / "p.rw"), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    return subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=60)


STAMPS = [0, 1, -1, 999, 86399999, 86400000, 951782400000, 951868799999, 1760317323456,
          4102444800000, -2208988800000, 253402300799999]


def test_iso8601_matches_python():
    body = "\n".join(f"    print(log.iso8601({ms}))" for ms in STAMPS)
    r = _run(f"import log\n\ndef main() -> int:\n{body}\n    return 0\n")
    assert r.returncode == 0, r.stderr
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    want = [(epoch + datetime.timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            for ms in STAMPS]
    assert r.stdout.split() == want


LEVELS = """
import log

def main() -> int:
    lg = log.new(2)
    lg.prefix = "api"
    lg.debug("hidden")
    lg.info("shown" + log.kv("k", "v") + log.kv("msg", "two words") + log.kv("q", "a\\"b") + log.kv("e", ""))
    if not lg.set_level("loud"):
        lg.warn("unknown level")
    lg.set_level("DEBUG")
    lg.debug("now shown")
    lg.set_level("error")
    lg.warn("hidden")
    lg.error("boom")
    return 0
"""

LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z (\w+) api: (.*)$")


def test_levels_prefix_kv_to_stderr():
    r = _run(LEVELS)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    got = [LINE.match(line).groups() for line in r.stderr.splitlines()]
    assert got == [
        ("INFO", 'shown k=v msg="two words" q="a\\"b" e=""'),
        ("WARN", "unknown level"),
        ("DEBUG", "now shown"),
        ("ERROR", "boom"),
    ]


FILE = """
import log

def worker(lg: log.Logger, id: int) -> int:
    for k in range(0, 100):
        lg.info(f"w{id} {k}" + log.kv("pad", "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"))
    return 0

def main() -> int:
    match log.to_file(os_args()[1]):
        case Some(lg):
            lg.timestamps = false
            fs: List[Future[int]] = []
            for i in range(0, 8):
                fs = list_push(fs, spawn worker(lg, i))
            await_all(fs)
        case None:
            print("cannot open")
    match log.to_file("/nonexistent/dir/x.log"):
        case Some(lg):
            print("unexpected")
        case None:
            print(error_name(last_error()))
    return 0
"""


def test_file_output_appends_whole_lines():
    td = tempfile.mkdtemp()
    path = os.path.join(td, "app.log")
    with open(path, "w") as f:
        f.write("existing\n")
    r = _run(FILE, path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ENOENT"
    lines = open(path).read().splitlines()
    assert lines[0] == "existing"
    assert len(lines) == 801
    pat = re.compile(r"^INFO w(\d) (\d+) pad=x{50}$")
    seen = {pat.match(line).groups() for line in lines[1:]}
    assert len(seen) == 800
