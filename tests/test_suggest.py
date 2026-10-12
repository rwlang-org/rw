"""did-you-mean suggestions and conversion hints (spec 49)."""

from __future__ import annotations

import pytest

from rwc.diagnostics import CompileError, conversion_hint, suggest
from rwc.driver import check_source


def _help(src: str) -> str | None:
    with pytest.raises(CompileError) as ei:
        check_source(src, "t.rw")
    return ei.value.diagnostic.help


def test_suggest_ranks_and_limits():
    assert suggest("cuont", ["count", "amount", "x"]) == ["count"]
    assert suggest("prnt", ["print", "len"]) == ["print"]
    assert suggest("abc", ["abd", "abe", "abf", "abg"]) == ["abd", "abe", "abf"]
    assert suggest("zzzzzz", ["count", "print"]) == []
    assert suggest("Name", ["name"]) == ["name"]


@pytest.mark.parametrize("src, want", [
    ("def main() -> int:\n    count = 1\n    print(cuont)\n    return 0\n",
     "did you mean `count`?"),
    ("def helper() -> int:\n    return 1\n\ndef main() -> int:\n    return helpr()\n",
     "did you mean `helper`?"),
    ("def main() -> int:\n    prnt(\"x\")\n    return 0\n", "did you mean `print`?"),
    ("def main() -> int:\n    x: flaot = 1.0\n    return 0\n", "did you mean `float`?"),
    ("class P:\n    name: string\n\ndef main() -> int:\n    p = P(\"a\")\n    print(p.nmae)\n    return 0\n",
     "did you mean `name`?"),
    ("class P:\n    name: string\n    def greet(self) -> string:\n        return self.name\n\n"
     "def main() -> int:\n    p = P(\"a\")\n    print(p.gret())\n    return 0\n",
     "did you mean `greet`?"),
    ("def main() -> int:\n    x: float = 1\n    return 0\n", "convert with `float(...)`"),
    ("def f(s: string) -> int:\n    return 0\n\ndef main() -> int:\n    return f(3)\n",
     "convert with `str(...)`"),
    ("def main() -> int:\n    x: Option[int] = 1\n    return 0\n", "wrap the value with `Some(...)`"),
])
def test_help_text(src, want):
    assert _help(src) == want


def test_no_help_when_nothing_close():
    assert _help("def main() -> int:\n    print(zzzzzz)\n    return 0\n") is None


def test_conversion_hint_table():
    assert conversion_hint("int", "float") == "convert with `int(...)` (truncates toward zero)"
    assert conversion_hint("int", "string").startswith("parse with `str_to_int(...)`")
    assert conversion_hint("Result[int, string]", "int") == "wrap the value with `Ok(...)`"
    assert conversion_hint("int", "bool") is None
    assert conversion_hint("List[int]", "int") is None


def test_render_help_line():
    from rwc.diagnostics import Diagnostic
    out = Diagnostic("t.rw", 1, 1, 3, "undefined variable: foo", help="did you mean `fob`?").render("foo\n")
    assert out.splitlines()[-1] == "     = help: did you mean `fob`?"
