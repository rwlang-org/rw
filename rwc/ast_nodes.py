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


@dataclass
class TypeFuture:
    inner: "TypeExpr"
    line: int
    col: int


TypeExpr = Union[TypeName, TypeFuture]


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


Expr = Union[
    IntLit, FloatLit, BoolLit, StringLit, Name,
    UnaryOp, BinOp, IfExpr, Call, SpawnExpr, AwaitExpr,
    SomeExpr, NoneExpr,
    OkExpr, ErrExpr,
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


Stmt = Union[VarDecl, Assign, ExprStmt, Return, If, While, For, MatchStmt, Break, Continue, Assert]


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
class Module:
    functions: List[FuncDef] = field(default_factory=list)
    type_aliases: List[TypeAlias] = field(default_factory=list)
    imports: List[Import] = field(default_factory=list)
