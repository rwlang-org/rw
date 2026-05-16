"""Diagnostic formatting for rw."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class Diagnostic:
    file: str
    line: int  # 1-origin
    col: int  # 1-origin
    length: int
    message: str
    severity: Literal["error", "warning"] = "error"

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
        return "\n".join(out)


class CompileError(Exception):
    """Compilation aborted because of a diagnostic."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
