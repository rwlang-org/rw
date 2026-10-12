"""AST node definitions for rw.

Type annotations in source (e.g. `int`, `Future[int]`, `string`, `void`)
are represented by `TypeExpr` nodes here; they are resolved to concrete
`types.Type` objects in Sema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union


# ----- Type expressions (as written in source) -----

@dataclass
class TypeName:
    name: str          # "int", "float", "bool", "string", "void"
    line: int
    col: int
    # Module qualifier for `mod.Class` type names (spec 20). None otherwise.
    module: Optional[str] = None


@dataclass
class TypeFuture:
    inner: "TypeExpr"
    line: int
    col: int


@dataclass
class TypeCallable:
    """`Callable[[P1, ..., Pn], R]` (spec 21)."""
    params: List["TypeExpr"]
    ret: "TypeExpr"
    line: int
    col: int


@dataclass
class TypeList:
    """`List[T]` for a non-int element type (spec 22). `List[int]` stays a
    `TypeName("List[int]")`."""
    elem: "TypeExpr"
    line: int
    col: int


@dataclass
class TypeOption:
    """`Option[T]` for a non-int T (spec 30)."""
    inner: "TypeExpr"
    line: int
    col: int


@dataclass
class TypeResult:
    """`Result[T, E]` other than `Result[int, int]` (spec 30)."""
    ok: "TypeExpr"
    err: "TypeExpr"
    line: int
    col: int


@dataclass
class TypeDict:
    """`Dict[K, V]` (spec 31)."""
    key: "TypeExpr"
    value: "TypeExpr"
    line: int
    col: int


TypeExpr = Union[TypeName, TypeFuture, TypeCallable, TypeList, TypeOption, TypeResult, TypeDict]


# ----- Expressions -----

@dataclass
class IntLit:
    value: int
    line: int
    col: int


@dataclass
class FloatLit:
    value: float
    line: int
    col: int


@dataclass
class BoolLit:
    value: bool
    line: int
    col: int


@dataclass
class StringLit:
    value: str
    line: int
    col: int


@dataclass
class Name:
    name: str
    line: int
    col: int


@dataclass
class UnaryOp:
    op: str            # "-", "not"
    operand: "Expr"
    line: int
    col: int


@dataclass
class BinOp:
    op: str            # "+", "-", "*", "/", "%", "==", "!=", "<", "<=", ">", ">=", "and", "or"
    left: "Expr"
    right: "Expr"
    line: int
    col: int


@dataclass
class IfExpr:
    # Conditional expression: `then if cond else els` (Python ternary syntax).
    then: "Expr"
    cond: "Expr"
    els: "Expr"
    line: int
    col: int


@dataclass
class Call:
    callee: str        # MVP: simple identifier only
    args: List["Expr"]
    line: int
    col: int
    # Module qualifier for `mod.func(...)` calls. None for unqualified calls
    # (the common case: local functions and builtins). See spec 17.
    module: Optional[str] = None


@dataclass
class SpawnExpr:
    call: Call         # spawn target must be a call
    line: int
    col: int


@dataclass
class AwaitExpr:
    target: "Expr"
    line: int
    col: int


@dataclass
class SomeExpr:
    arg: "Expr"
    line: int
    col: int


@dataclass
class NoneExpr:
    line: int
    col: int


@dataclass
class OkExpr:
    arg: "Expr"
    line: int
    col: int


@dataclass
class ErrExpr:
    arg: "Expr"
    line: int
    col: int


@dataclass
class ListLit:
    """`[e1, ..., en]` (spec 29)."""
    elems: List["Expr"]
    line: int
    col: int


@dataclass
class DictLit:
    """`{k1: v1, ..., kn: vn}` (spec 36)."""
    keys: List["Expr"]
    values: List["Expr"]
    line: int
    col: int


@dataclass
class Index:
    """`obj[index]` on a list or a string (spec 29)."""
    obj: "Expr"
    index: "Expr"
    line: int
    col: int


@dataclass
class Slice:
    """`obj[start:stop]` with optional bounds (spec 29)."""
    obj: "Expr"
    start: Optional["Expr"]
    stop: Optional["Expr"]
    line: int
    col: int


@dataclass
class CallValue:
    """A call whose callee is an expression rather than a bare name, e.g.
    `a.b.handler(x)` or `pick()(x)` (spec 21)."""
    callee: "Expr"
    args: List["Expr"]
    line: int
    col: int


@dataclass
class FieldAccess:
    """`obj.field` on a record (spec 20)."""
    obj: "Expr"
    field: str
    line: int
    col: int


Expr = Union[
    IntLit, FloatLit, BoolLit, StringLit, Name,
    UnaryOp, BinOp, IfExpr, Call, SpawnExpr, AwaitExpr,
    SomeExpr, NoneExpr,
    OkExpr, ErrExpr,
    FieldAccess, CallValue, ListLit, Index, Slice, DictLit,
]


# ----- Statements -----

@dataclass
class VarDecl:
    name: str
    type_expr: TypeExpr
    value: Expr
    line: int
    col: int


@dataclass
class Assign:
    name: str
    value: Expr
    line: int
    col: int


@dataclass
class ExprStmt:
    expr: Expr
    line: int
    col: int


@dataclass
class Return:
    value: Optional[Expr]
    line: int
    col: int


@dataclass
class If:
    cond: Expr
    then_body: List["Stmt"]
    # elif chains are normalized into nested If's inside else_body.
    else_body: List["Stmt"]
    line: int
    col: int


@dataclass
class While:
    cond: Expr
    body: List["Stmt"]
    line: int
    col: int
    # Statements run after every iteration, including one ended by
    # `continue` (like the third clause of a C `for`). Used by the desugared
    # range-for to advance its variable.
    update: List["Stmt"] = field(default_factory=list)


@dataclass
class Break:
    line: int
    col: int


@dataclass
class Continue:
    line: int
    col: int


@dataclass
class Assert:
    cond: "Expr"
    msg: Optional["Expr"]
    line: int
    col: int


@dataclass
class For:
    var: str               # loop variable name
    start: Expr            # int expr
    stop: Expr             # int expr
    step: Expr             # int expr (defaults filled by parser)
    body: List["Stmt"]
    line: int
    col: int


@dataclass
class ForEach:
    """`for var in iterable:` over a list or a string (spec 29)."""
    var: str
    iterable: Expr
    body: List["Stmt"]
    line: int
    col: int


@dataclass
class MatchStmt:
    target: Expr
    style: str                              # "option" or "result"
    # Option-style fields (None when style == "result")
    some_var: Optional[str]
    some_block: Optional[List["Stmt"]]
    none_block: Optional[List["Stmt"]]
    # Result-style fields (None when style == "option")
    ok_var: Optional[str]
    ok_block: Optional[List["Stmt"]]
    err_var: Optional[str]
    err_block: Optional[List["Stmt"]]
    line: int
    col: int


@dataclass
class FieldAssign:
    """`obj.field = value` on a record (spec 20)."""
    obj: Expr
    field: str
    value: Expr
    line: int
    col: int


@dataclass
class IndexAssign:
    """`obj[index] = value`; valid only for dicts (spec 31)."""
    obj: Expr
    index: Expr
    value: Expr
    line: int
    col: int


Stmt = Union[
    VarDecl, Assign, ExprStmt, Return, If, While, For, MatchStmt, Break, Continue, Assert,
    FieldAssign, ForEach, IndexAssign,
]


# ----- Top level -----

@dataclass
class Param:
    name: str
    type_expr: TypeExpr
    line: int
    col: int


@dataclass
class FuncDef:
    name: str
    params: List[Param]
    return_type: TypeExpr
    body: List[Stmt]
    line: int
    col: int


@dataclass
class TypeAlias:
    name: str               # the new alias name, e.g. "UserId"
    target: TypeExpr        # the existing type it aliases
    line: int
    col: int


@dataclass
class Import:
    module: str                                                  # e.g. "math_lib"
    # `import x as m` (PR3). None for plain import.
    alias: Optional[str]
    # `from x import y[, z as w]` (PR2). List of (name, alias). None for plain import.
    names: Optional[List["tuple[str, Optional[str]]"]]
    line: int
    col: int


@dataclass
class FieldDecl:
    name: str
    type_expr: TypeExpr
    line: int
    col: int


@dataclass
class ClassDef:
    """A record type: `class Name:` with typed fields (spec 20) and methods
    (spec 33). Each method is also lowered to a module-level FuncDef named
    `Name.method` whose first parameter is `self: Name`."""
    name: str
    fields: List[FieldDecl]
    line: int
    col: int
    methods: List[str] = field(default_factory=list)
    method_defs: List["FuncDef"] = field(default_factory=list)


@dataclass
class ConstDef:
    """Module-level `NAME: T = literal` (spec 35)."""
    name: str
    type_expr: TypeExpr
    value: Expr
    line: int
    col: int


@dataclass
class Module:
    functions: List[FuncDef] = field(default_factory=list)
    type_aliases: List[TypeAlias] = field(default_factory=list)
    imports: List[Import] = field(default_factory=list)
    classes: List[ClassDef] = field(default_factory=list)
    constants: List[ConstDef] = field(default_factory=list)
