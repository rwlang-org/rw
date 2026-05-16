"""Diagnostic tests: compile bad rw sources, check the error message and line."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BAD_DIR = REPO / "tests" / "bad"


def _expectations(source: str) -> tuple[str, int]:
    msg = None
    line = None
    for raw in source.splitlines():
        m1 = re.match(r"#\s*ERROR:\s*(.*)", raw)
        if m1:
            msg = m1.group(1).strip()
        m2 = re.match(r"#\s*ERROR_LINE:\s*(\d+)", raw)
        if m2:
            line = int(m2.group(1).strip())
    assert msg is not None and line is not None, "test file missing ERROR/ERROR_LINE"
    return msg, line


@pytest.mark.parametrize("rw_path", sorted(BAD_DIR.glob("*.rw")))
def test_bad_source_produces_expected_diagnostic(rw_path: Path):
    source = rw_path.read_text()
    expected_msg, expected_line = _expectations(source)
    res = subprocess.run(
        [sys.executable, "-m", "rwc.cli", "build", str(rw_path), "-o", "/tmp/__rwc_bad"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert res.returncode != 0, f"expected failure for {rw_path.name}, got success"
    err = res.stderr
    assert expected_msg in err, f"missing expected message {expected_msg!r} in:\n{err}"
    # the rendered diagnostic includes `<file>:<line>:<col>`
    assert f":{expected_line}:" in err, f"diagnostic did not reference line {expected_line}: {err}"
