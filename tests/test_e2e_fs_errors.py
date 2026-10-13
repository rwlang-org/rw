"""fs keeps last_error after its cleanup close (spec 59)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_read_file_on_a_directory_reports_eisdir():
    td = Path(tempfile.mkdtemp())
    (td / "p.rw").write_text(
        "import fs\n\ndef main() -> int:\n"
        "    match fs.read_file(\"" + str(td) + "\"):\n"
        "        case Some(s):\n            print(\"some\")\n"
        "        case None:\n            print(error_name(last_error()))\n"
        "    set_last_error(0)\n    print(last_error())\n    return 0\n")
    r = subprocess.run([sys.executable, "-m", "rwc.cli", "run", str(td / "p.rw")],
                       capture_output=True, text=True, cwd=REPO, timeout=120)
    assert r.stdout.split() == ["EISDIR", "0"], r.stderr
