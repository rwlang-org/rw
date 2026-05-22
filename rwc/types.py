"""Concrete types resolved by Sema (distinct from AST TypeExpr)."""

from __future__ import annotations

from dataclasses import dataclass


class Type:
    name: str

    def __repr__(self) -> str:
        return self.name

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Type) and repr(self) == repr(other)

    def __hash__(self) -> int:
        return hash(repr(self))


class _Primitive(Type):
    def __init__(self, name: str) -> None:
        self.name = name


INT = _Primitive("int")
FLOAT = _Primitive("float")
BOOL = _Primitive("bool")
STRING = _Primitive("string")
VOID = _Primitive("void")
BYTES = _Primitive("Bytes")
LIST_INT = _Primitive("List[int]")
OPTION_INT = _Primitive("Option[int]")


@dataclass(eq=False)
class FutureType(Type):
    inner: Type

    def __post_init__(self) -> None:
        self.name = f"Future[{self.inner}]"


def is_numeric(t: Type) -> bool:
    return t is INT or t is FLOAT


def is_printable(t: Type) -> bool:
    return t in (INT, FLOAT, BOOL, STRING)
