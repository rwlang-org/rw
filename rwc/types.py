"""Concrete types resolved by Sema (distinct from AST TypeExpr)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


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
RESULT_INT_INT = _Primitive("Result[int, int]")


@dataclass(eq=False)
class FutureType(Type):
    inner: Type

    def __post_init__(self) -> None:
        self.name = f"Future[{self.inner}]"


class ClassType(Type):
    """A user-declared record type (spec 20).

    Compared by identity: two classes with the same name in different
    modules are different types. `fields` is filled in after every class
    name of the module is known, so classes may refer to later classes.
    """

    def __init__(self, name: str, module: Optional[str]) -> None:
        # Display name: bare for the entry module, qualified for imports.
        self.name = name if module is None else f"{module}.{name}"
        self.short_name = name
        self.module = module
        self.fields: List[Tuple[str, Type]] = []

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def field_index(self, name: str) -> Optional[int]:
        for i, (fname, _) in enumerate(self.fields):
            if fname == name:
                return i
        return None


def is_numeric(t: Type) -> bool:
    return t is INT or t is FLOAT


def is_printable(t: Type) -> bool:
    return t in (INT, FLOAT, BOOL, STRING)
