"""Diagnostic formatting for rw."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Literal, Optional


@dataclass
class Diagnostic:
    file: str
    line: int  # 1-origin
    col: int  # 1-origin
    length: int
    message: str
    severity: Literal["error", "warning"] = "error"
    help: Optional[str] = None

    def render(self, source: str) -> str:
        lines = source.splitlines()
        idx = self.line - 1
        line_text = lines[idx] if 0 <= idx < len(lines) else ""
        gutter = f"{self.line:>3}"
        empty_gutter = " " * len(gutter)
        caret = " " * (self.col - 1) + "^" * max(1, self.length)
        out = [
            f"{self.severity}: {self.message}",
            f"{empty_gutter} --> {self.file}:{self.line}:{self.col}",
            f"{empty_gutter}  |",
            f"{gutter}  | {line_text}",
            f"{empty_gutter}  | {caret}",
        ]
        if self.help:
            out.append(f"{empty_gutter}  = help: {self.help}")
        return "\n".join(out)


def _edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance: insert, delete, substitute and
    swapping two adjacent characters each cost 1."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def suggest(name: str, candidates: Iterable[str], limit: int = 3) -> List[str]:
    """Up to `limit` candidates close to `name`, nearest first (spec 49).
    A candidate qualifies when it differs only in case or is within
    max(1, len(name) // 3) edits."""
    budget = max(1, len(name) // 3)
    scored = []
    for c in set(candidates):
        if c == name or c.startswith("$"):
            continue
        d = 0 if c.lower() == name.lower() else _edit_distance(name, c)
        if d <= budget:
            scored.append((d, c))
    scored.sort()
    return [c for _, c in scored[:limit]]


def did_you_mean(name: str, candidates: Iterable[str]) -> Optional[str]:
    """`did you mean `a`, `b`?` for the near misses of `name`, or None."""
    close = suggest(name, candidates)
    if not close:
        return None
    return "did you mean " + ", ".join(f"`{c}`" for c in close) + "?"


def conversion_hint(expected: object, found: object) -> Optional[str]:
    """A fix-it hint for a type mismatch between two types, keyed by their
    printed names (spec 49)."""
    e, f = str(expected), str(found)
    conv = {"int": "int", "float": "float", "string": "str"}
    if e in conv and f in ("int", "float", "bool") and e != f:
        if e == "int" and f == "float":
            return "convert with `int(...)` (truncates toward zero)"
        if e == "int" and f == "bool":
            return None
        return f"convert with `{conv[e]}(...)`"
    if e == "int" and f == "string":
        return "parse with `str_to_int(...)` (returns `Option[int]`)"
    if e == f"Option[{f}]":
        return "wrap the value with `Some(...)`"
    if e.startswith("Result[") and e[len("Result["):].startswith(f + ","):
        return "wrap the value with `Ok(...)`"
    return None


class CompileError(Exception):
    """Compilation aborted because of a diagnostic."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
