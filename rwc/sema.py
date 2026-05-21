"""Semantic analysis: type checking + symbol resolution.

Produces a `SemaResult` containing:
- a function table (name -> signature)
- an `expr_types` map from AST expression nodes to concrete Types
- a `local_types` map from (function, var_name) to Type

A single diagnostic is raised via CompileError on the first error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from . import ast_nodes as A
from . import types as T
from .diagnostics import CompileError, Diagnostic


@dataclass
class FuncSig:
    name: str
    params: List[Tuple[str, T.Type]]
    return_type: T.Type
    node: A.FuncDef


@dataclass
class SemaResult:
    filename: str
    functions: Dict[str, FuncSig]
    # id(expr_node) -> Type. We use object identity to avoid dataclass hash issues.
    expr_types: Dict[int, T.Type] = field(default_factory=dict)
    # (func_name, var_name) -> declared Type
    local_types: Dict[Tuple[str, str], T.Type] = field(default_factory=dict)


def _resolve_type(filename: str, ty: A.TypeExpr) -> T.Type:
    if isinstance(ty, A.TypeName):
        m = {
            "int": T.INT,
            "float": T.FLOAT,
            "bool": T.BOOL,
            "string": T.STRING,
            "void": T.VOID,
        }
        if ty.name not in m:
            raise CompileError(Diagnostic(filename, ty.line, ty.col, len(ty.name),
                                          f"unknown type: {ty.name}"))
        return m[ty.name]
    if isinstance(ty, A.TypeFuture):
        inner = _resolve_type(filename, ty.inner)
        if inner is T.VOID:
            return T.FutureType(T.VOID)
        return T.FutureType(inner)
    raise CompileError(Diagnostic(filename, 0, 0, 1, "internal: unknown type expr"))


class Sema:
    def __init__(self, module: A.Module, filename: str) -> None:
        self.module = module
        self.filename = filename
        self.result = SemaResult(filename=filename, functions={})

    # ---------- entry ----------
    def analyze(self) -> SemaResult:
        # First pass: collect signatures.
        for fn in self.module.functions:
            if fn.name in self.result.functions:
                raise CompileError(Diagnostic(
                    self.filename, fn.line, fn.col, len(fn.name),
                    f"duplicate function: {fn.name}",
                ))
            params: List[Tuple[str, T.Type]] = []
            seen: set[str] = set()
            for p in fn.params:
                if p.name in seen:
                    raise CompileError(Diagnostic(
                        self.filename, p.line, p.col, len(p.name),
                        f"duplicate parameter: {p.name}",
                    ))
                seen.add(p.name)
                pt = _resolve_type(self.filename, p.type_expr)
                if pt is T.VOID:
                    raise CompileError(Diagnostic(
                        self.filename, p.line, p.col, len(p.name),
                        "parameter type cannot be void",
                    ))
                params.append((p.name, pt))
            rt = _resolve_type(self.filename, fn.return_type)
            self.result.functions[fn.name] = FuncSig(fn.name, params, rt, fn)

        # main is required.
        if "main" not in self.result.functions:
            # Synthesize a diagnostic at line 1 if module is empty.
            line = self.module.functions[0].line if self.module.functions else 1
            raise CompileError(Diagnostic(
                self.filename, line, 1, 1,
                "every rw program must define `def main() -> int`",
            ))
        main_sig = self.result.functions["main"]
        if main_sig.params:
            raise CompileError(Diagnostic(
                self.filename, main_sig.node.line, main_sig.node.col, 4,
                "main must take no parameters",
            ))
        if main_sig.return_type is not T.INT:
            raise CompileError(Diagnostic(
                self.filename, main_sig.node.line, main_sig.node.col, 4,
                "main must return int",
            ))

        # Second pass: check each function body.
        for fn in self.module.functions:
            self._check_func(fn)

        return self.result

    # ---------- function body ----------
    def _check_func(self, fn: A.FuncDef) -> None:
        sig = self.result.functions[fn.name]
        # Local scope: param name -> Type. New scopes are pushed for if/while bodies,
        # but in rw the entire function shares one flat scope. Re-declaring an
        # existing local is an error.
        locals_: Dict[str, T.Type] = {}
        for pname, pty in sig.params:
            locals_[pname] = pty
            self.result.local_types[(fn.name, pname)] = pty
        ended = self._check_block(fn, fn.body, locals_, sig.return_type)
        if sig.return_type is not T.VOID and not ended:
            raise CompileError(Diagnostic(
                self.filename, fn.line, fn.col, len(fn.name),
                f"function `{fn.name}` does not return on all paths",
            ))

    def _check_block(
        self,
        fn: A.FuncDef,
        stmts: List[A.Stmt],
        locals_: Dict[str, T.Type],
        ret_ty: T.Type,
    ) -> bool:
        """Returns True if this block definitely returns on every path."""
        block_returns = False
        for stmt in stmts:
            stmt_returns = self._check_stmt(fn, stmt, locals_, ret_ty)
            block_returns = block_returns or stmt_returns
        return block_returns

    def _check_stmt(
        self,
        fn: A.FuncDef,
        stmt: A.Stmt,
        locals_: Dict[str, T.Type],
        ret_ty: T.Type,
    ) -> bool:
        if isinstance(stmt, A.VarDecl):
            if stmt.name in locals_:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, len(stmt.name),
                    f"variable `{stmt.name}` already declared",
                ))
            declared = _resolve_type(self.filename, stmt.type_expr)
            if declared is T.VOID:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, len(stmt.name),
                    "variable type cannot be void",
                ))
            val_ty = self._check_expr(fn, stmt.value, locals_)
            if val_ty != declared:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, len(stmt.name),
                    f"type mismatch: declared `{declared}`, value has type `{val_ty}`",
                ))
            locals_[stmt.name] = declared
            self.result.local_types[(fn.name, stmt.name)] = declared
            return False

        if isinstance(stmt, A.Assign):
            if stmt.name not in locals_:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, len(stmt.name),
                    f"undefined variable: {stmt.name}",
                ))
            expected = locals_[stmt.name]
            val_ty = self._check_expr(fn, stmt.value, locals_)
            if val_ty != expected:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, len(stmt.name),
                    f"type mismatch in assignment to `{stmt.name}`: "
                    f"expected `{expected}`, found `{val_ty}`",
                ))
            return False

        if isinstance(stmt, A.ExprStmt):
            self._check_expr(fn, stmt.expr, locals_)
            return False

        if isinstance(stmt, A.Return):
            if stmt.value is None:
                if ret_ty is not T.VOID:
                    raise CompileError(Diagnostic(
                        self.filename, stmt.line, stmt.col, 6,
                        f"function returns `{ret_ty}` but got bare `return`",
                    ))
                return True
            val_ty = self._check_expr(fn, stmt.value, locals_)
            if ret_ty is T.VOID:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 6,
                    "void function cannot return a value",
                ))
            if val_ty != ret_ty:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 6,
                    f"return type mismatch: expected `{ret_ty}`, found `{val_ty}`",
                ))
            return True

        if isinstance(stmt, A.If):
            cond_ty = self._check_expr(fn, stmt.cond, locals_)
            if cond_ty is not T.BOOL:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 2,
                    f"if condition must be bool, found `{cond_ty}`",
                ))
            then_ret = self._check_block(fn, stmt.then_body, dict(locals_), ret_ty)
            else_ret = False
            if stmt.else_body:
                else_ret = self._check_block(fn, stmt.else_body, dict(locals_), ret_ty)
            return then_ret and else_ret

        if isinstance(stmt, A.While):
            cond_ty = self._check_expr(fn, stmt.cond, locals_)
            if cond_ty is not T.BOOL:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 5,
                    f"while condition must be bool, found `{cond_ty}`",
                ))
            self._check_block(fn, stmt.body, dict(locals_), ret_ty)
            return False  # while bodies don't guarantee return

        raise CompileError(Diagnostic(
            self.filename, 0, 0, 1, f"internal: unknown stmt: {type(stmt).__name__}",
        ))

    # ---------- expressions ----------
    def _check_expr(
        self,
        fn: A.FuncDef,
        expr: A.Expr,
        locals_: Dict[str, T.Type],
    ) -> T.Type:
        ty = self._infer_expr(fn, expr, locals_)
        self.result.expr_types[id(expr)] = ty
        return ty

    def _infer_expr(
        self,
        fn: A.FuncDef,
        expr: A.Expr,
        locals_: Dict[str, T.Type],
    ) -> T.Type:
        if isinstance(expr, A.IntLit):
            return T.INT
        if isinstance(expr, A.FloatLit):
            return T.FLOAT
        if isinstance(expr, A.BoolLit):
            return T.BOOL
        if isinstance(expr, A.StringLit):
            return T.STRING
        if isinstance(expr, A.Name):
            if expr.name not in locals_:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, len(expr.name),
                    f"undefined variable: {expr.name}",
                ))
            return locals_[expr.name]
        if isinstance(expr, A.UnaryOp):
            inner = self._check_expr(fn, expr.operand, locals_)
            if expr.op == "-":
                if not T.is_numeric(inner):
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 1,
                        f"unary `-` requires int or float, found `{inner}`",
                    ))
                return inner
            if expr.op == "not":
                if inner is not T.BOOL:
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 3,
                        f"`not` requires bool, found `{inner}`",
                    ))
                return T.BOOL
            raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                          f"internal: unknown unary op {expr.op}"))
        if isinstance(expr, A.BinOp):
            lt = self._check_expr(fn, expr.left, locals_)
            rt = self._check_expr(fn, expr.right, locals_)
            op = expr.op
            if op == "+" and lt is T.STRING and rt is T.STRING:
                return T.STRING
            if op in ("+", "-", "*", "/", "%"):
                if lt != rt:
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        f"operator `{op}` requires same type on both sides, "
                        f"found `{lt}` and `{rt}`",
                    ))
                if not T.is_numeric(lt):
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        f"operator `{op}` requires int or float, found `{lt}`",
                    ))
                return lt
            if op in ("==", "!="):
                if lt != rt:
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        f"`{op}` requires same type, found `{lt}` and `{rt}`",
                    ))
                return T.BOOL
            if op in ("<", "<=", ">", ">="):
                if lt != rt or not T.is_numeric(lt):
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        f"`{op}` requires int or float on both sides, found `{lt}` and `{rt}`",
                    ))
                return T.BOOL
            if op in ("and", "or"):
                if lt is not T.BOOL or rt is not T.BOOL:
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        f"`{op}` requires bool on both sides, found `{lt}` and `{rt}`",
                    ))
                return T.BOOL
            raise CompileError(Diagnostic(
                self.filename, expr.line, expr.col, len(op),
                f"internal: unknown binary op `{op}`",
            ))
        if isinstance(expr, A.Call):
            return self._check_call(fn, expr, locals_)
        if isinstance(expr, A.SpawnExpr):
            # spawn requires a user-defined function call.
            call = expr.call
            if call.callee not in self.result.functions:
                if call.callee == "print":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `print`",
                    ))
                if call.callee == "len":
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 5,
                        "cannot spawn the builtin `len`",
                    ))
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"undefined function: {call.callee}",
                ))
            # Type-check the call as a normal call (we want arg types to flow through).
            self._check_call(fn, call, locals_)
            ret_ty = self.result.functions[call.callee].return_type
            return T.FutureType(ret_ty)
        if isinstance(expr, A.AwaitExpr):
            tgt = self._check_expr(fn, expr.target, locals_)
            if not isinstance(tgt, T.FutureType):
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 5,
                    f"`await` requires a Future, found `{tgt}`",
                ))
            return tgt.inner
        raise CompileError(Diagnostic(
            self.filename, 0, 0, 1, f"internal: unknown expr {type(expr).__name__}",
        ))

    def _check_call(self, fn: A.FuncDef, call: A.Call, locals_: Dict[str, T.Type]) -> T.Type:
        # Builtin: print accepts any printable.
        if call.callee == "print":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 5,
                    f"print takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if not T.is_printable(at):
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 5,
                    f"print does not support `{at}`",
                ))
            return T.VOID
        # Builtin: len(string) -> int.
        if call.callee == "len":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.STRING:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len argument must be string, found `{at}`",
                ))
            return T.INT
        if call.callee not in self.result.functions:
            raise CompileError(Diagnostic(
                self.filename, call.line, call.col, len(call.callee),
                f"undefined function: {call.callee}",
            ))
        sig = self.result.functions[call.callee]
        if len(call.args) != len(sig.params):
            raise CompileError(Diagnostic(
                self.filename, call.line, call.col, len(call.callee),
                f"`{call.callee}` expects {len(sig.params)} argument(s), got {len(call.args)}",
            ))
        for i, (arg, (pname, pty)) in enumerate(zip(call.args, sig.params)):
            at = self._check_expr(fn, arg, locals_)
            if at != pty:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"argument {i + 1} of `{call.callee}`: expected `{pty}`, found `{at}`",
                ))
        return sig.return_type


def analyze(module: A.Module, filename: str = "<input>") -> SemaResult:
    return Sema(module, filename).analyze()
