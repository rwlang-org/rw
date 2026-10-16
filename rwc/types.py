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


class IntType(_Primitive):
    """An integer type of a fixed width (spec 73). `int` is the 64-bit
    signed one; `i64` / `isize` name the same object, `usize` names `u64`."""

    def __init__(self, name: str, bits: int, signed: bool) -> None:
        super().__init__(name)
        self.bits = bits
        self.signed = signed

    @property
    def min(self) -> int:
        return -(1 << (self.bits - 1)) if self.signed else 0

    @property
    def max(self) -> int:
        return (1 << (self.bits - 1)) - 1 if self.signed else (1 << self.bits) - 1


INT = IntType("int", 64, True)
I8 = IntType("i8", 8, True)
I16 = IntType("i16", 16, True)
I32 = IntType("i32", 32, True)
U8 = IntType("u8", 8, False)
U16 = IntType("u16", 16, False)
U32 = IntType("u32", 32, False)
U64 = IntType("u64", 64, False)

# Integer type names, including the aliases (spec 73).
INT_TYPE_NAMES: Dict[str, IntType] = {
    "int": INT, "i64": INT, "isize": INT,
    "i8": I8, "i16": I16, "i32": I32,
    "u8": U8, "u16": U16, "u32": U32, "u64": U64, "usize": U64,
}
FLOAT = _Primitive("float")
BOOL = _Primitive("bool")
STRING = _Primitive("string")
VOID = _Primitive("void")
BYTES = _Primitive("Bytes")
MUTEX = _Primitive("Mutex")  # a reference to a runtime lock (spec 46)


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


class TupleType(Type):
    """`tuple[T1, ..., Tn]` (spec 37): an immutable value, compared
    structurally."""

    def __init__(self, elems: List[Type]) -> None:
        self.elems = elems
        self.name = f"tuple[{', '.join(str(e) for e in elems)}]"


class ChannelType(Type):
    """`Channel[T]` (spec 44): a reference to a runtime channel."""

    def __init__(self, elem: Type) -> None:
        self.elem = elem
        self.name = f"Channel[{elem}]"


class PtrType(Type):
    """`ptr[T]` (spec 74): an opaque C pointer. Compared structurally."""

    def __init__(self, elem: Type) -> None:
        self.elem = elem
        self.name = f"ptr[{elem}]"


def is_integer(t: object) -> bool:
    return isinstance(t, IntType)


def is_numeric(t: Type) -> bool:
    return is_integer(t) or t is FLOAT


def is_printable(t: Type) -> bool:
    return is_integer(t) or t in (FLOAT, BOOL, STRING)
