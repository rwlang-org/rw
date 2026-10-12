"""Concrete types resolved by Sema (distinct from AST TypeExpr)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


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
        # Method name -> (module, "Class.method") function key (spec 33).
        self.methods: Dict[str, Tuple[Optional[str], str]] = {}

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def field_index(self, name: str) -> Optional[int]:
        for i, (fname, _) in enumerate(self.fields):
            if fname == name:
                return i
        return None


class FunctionType(Type):
    """`Callable[[P1, ..., Pn], R]`: a pointer to a top-level function
    (spec 21). Compared structurally through its display name."""

    def __init__(self, params: List[Type], ret: Type) -> None:
        self.params = params
        self.ret = ret
        self.name = f"Callable[[{', '.join(str(p) for p in params)}], {ret}]"


class ListType(Type):
    """`List[T]` (spec 22). Compared structurally; use `list_of` so that
    `List[int]` is always the canonical `LIST_INT` object."""

    def __init__(self, elem: Type) -> None:
        self.elem = elem
        self.name = f"List[{elem}]"


LIST_INT = ListType(INT)


def list_of(elem: Type) -> ListType:
    return LIST_INT if elem is INT else ListType(elem)


class OptionType(Type):
    """`Option[T]` (spec 30). Use `option_of` so `Option[int]` is OPTION_INT."""

    def __init__(self, inner: Type) -> None:
        self.inner = inner
        self.name = f"Option[{inner}]"


class ResultType(Type):
    """`Result[T, E]` (spec 30). Use `result_of` so `Result[int, int]` is
    RESULT_INT_INT."""

    def __init__(self, ok: Type, err: Type) -> None:
        self.ok = ok
        self.err = err
        self.name = f"Result[{ok}, {err}]"


OPTION_INT = OptionType(INT)
RESULT_INT_INT = ResultType(INT, INT)


def option_of(inner: Type) -> OptionType:
    return OPTION_INT if inner is INT else OptionType(inner)


def result_of(ok: Type, err: Type) -> ResultType:
    return RESULT_INT_INT if ok is INT and err is INT else ResultType(ok, err)


class DictType(Type):
    """`Dict[K, V]` (spec 31): a reference to a heap hash table. K is int or
    string. Compared structurally."""

    def __init__(self, key: Type, value: Type) -> None:
        self.key = key
        self.value = value
        self.name = f"Dict[{key}, {value}]"


def is_numeric(t: Type) -> bool:
    return t is INT or t is FLOAT


def is_printable(t: Type) -> bool:
    return t in (INT, FLOAT, BOOL, STRING)
