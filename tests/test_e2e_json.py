"""stdlib/json.rw against Python's json module (spec 58)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Parse each file named on the command line and print one line per file:
# the compact dump, or "ERR <message>".
ROUNDTRIP = """
import json

def slurp(path: string) -> string:
    fd = open(path, "r")
    out = ""
    while true:
        b = read(fd, 65536)
        if len(b) == 0:
            break
        out = out + str_from_bytes(b)
    close(fd)
    return out

def main() -> int:
    args = os_args()
    for k in range(1, len(args)):
        match json.parse(slurp(args[k])):
            case Ok(v):
                print(v.dump())
            case Err(msg):
                print("ERR " + msg)
    return 0
"""

VALID = [
    "null", "true", "false", "0", "-0", "42", "-17", "9223372036854775807",
    "9223372036854775808", "3.25", "-1.5e-3", "1E10", "0.1",
    '""', '"plain"', '"esc \\" \\\\ \\/ \\b \\f \\n \\r \\t"', '"\\u00e9\\u3042"', '"\\ud83d\\ude00"',
    '"utf8 é あ 😀"', "[]", "{}", "[1, [2, [3, []]]]",
    '{"a": 1, "b": [true, false, null], "c": {"d": "e"}}',
    '  {\n  "spaced" : [ 1 , 2 ]\n}  ',
    '{"dup": 1, "dup": 2}',
    '"\\u0000 control"',
]

INVALID = [
    ("", "unexpected end of input at line 1, column 1"),
    ("[1, 2,]", "unexpected character at line 1, column 7"),
    ('{"a" 1}', "expected ':' at line 1, column 6"),
    ("{1: 2}", "expected a string key at line 1, column 2"),
    ("[1 2]", "expected ',' or ']' at line 1, column 4"),
    ('"abc', "unterminated string at line 1, column 5"),
    ('"a\\qb"', "invalid escape at line 1, column 4"),
    ('"\\u12g4"', "invalid \\u escape"),
    ("tru", "invalid literal at line 1, column 1"),
    ("01", "unexpected trailing characters at line 1, column 2"),
    ("1.", "invalid number at line 1, column 3"),
    ("-", "invalid number at line 1, column 2"),
    ("[1]\n[2]", "unexpected trailing characters at line 2, column 1"),
    ('{\n  "a": [\n    1,\n    x\n  ]\n}', "unexpected character at line 4, column 5"),
    ('"tab\there"', "control character in string at line 1, column 5"),
    ("[" * 600 + "]" * 600, "nesting too deep"),
]


@pytest.fixture(scope="module")
def roundtrip() -> Path:
    td = Path(tempfile.mkdtemp())
    (td / "rt.rw").write_text(ROUNDTRIP)
    exe = td / "rt"
    build = subprocess.run([sys.executable, "-m", "rwc.cli", "build", str(td / "rt.rw"), "-o", str(exe)],
                           capture_output=True, text=True, cwd=REPO)
    assert build.returncode == 0, build.stderr
    return exe


def _run(exe: Path, docs: list[str]) -> list[str]:
    td = Path(tempfile.mkdtemp())
    paths = []
    for n, d in enumerate(docs):
        p = td / f"{n}.json"
        p.write_bytes(d.encode())
        paths.append(str(p))
    r = subprocess.run([str(exe), *paths], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.decode().split("\n")[: len(docs)]


def test_valid_documents_roundtrip(roundtrip):
    out = _run(roundtrip, VALID)
    for doc, line in zip(VALID, out, strict=True):
        assert not line.startswith("ERR"), (doc, line)
        want = json.loads(doc, object_pairs_hook=lambda kv: dict(kv))
        got = json.loads(line)
        if doc.strip() == '{"dup": 1, "dup": 2}':
            assert line == '{"dup":1,"dup":2}'  # duplicate keys are kept as written
            continue
        assert got == want, (doc, line)


def test_dump_matches_python_compact_form(roundtrip):
    docs = ['{"a": [1, "x\\ny", null, true]}', '["\\u0001"]', '"quote \\" back \\\\"']
    out = _run(roundtrip, docs)
    for doc, line in zip(docs, out, strict=True):
        assert line == json.dumps(json.loads(doc), separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize("doc, msg", INVALID, ids=[str(i) for i in range(len(INVALID))])
def test_invalid_documents_report_position(roundtrip, doc, msg):
    (line,) = _run(roundtrip, [doc])
    assert line.startswith("ERR "), line
    assert msg in line, line


def test_large_document(roundtrip):
    doc = json.dumps([{"id": i, "name": f"user{i}", "tags": ["a", "b"], "score": i / 7} for i in range(2000)])
    (line,) = _run(roundtrip, [doc])
    assert json.loads(line) == json.loads(doc)


def test_floats_stay_floats():
    src = """
import json

def main() -> int:
    for f in [2.0, -0.0, 1.5, 100.0]:
        d = json.from_float(f).dump()
        match json.parse(d):
            case Ok(v):
                print(d + " " + v.kind)
            case Err(m):
                print(m)
    return 0
"""
    td = Path(tempfile.mkdtemp())
    (td / "p.rw").write_text(src)
    r = subprocess.run([sys.executable, "-m", "rwc.cli", "run", str(td / "p.rw")],
                       capture_output=True, text=True, cwd=REPO, timeout=120)
    assert r.stdout.split("\n")[:4] == ["2.0 float", "-0.0 float", "1.5 float", "100.0 float"], r.stderr

