"""Semantic analysis: type checking + symbol resolution.

Produces a `SemaResult` containing:
- a function table (name -> signature)
- an `expr_types` map from AST expression nodes to concrete Types
- a `local_types` map from (function, var_name) to Type

A single diagnostic is raised via CompileError on the first error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import ast_nodes as A
from . import types as T
from .diagnostics import CompileError, Diagnostic
from .loader import LoadedProgram


# A function is identified by (module, name). The entry (root) module uses
# module=None; imported modules use their import name. See spec 17.
FuncKey = Tuple[Optional[str], str]


@dataclass
class FuncSig:
    name: str
    params: List[Tuple[str, T.Type]]
    return_type: T.Type
    node: A.FuncDef
    module: Optional[str] = None


@dataclass
class SemaResult:
    filename: str
    functions: Dict[FuncKey, FuncSig]
    # id(expr_node) -> Type. We use object identity to avoid dataclass hash issues.
    expr_types: Dict[int, T.Type] = field(default_factory=dict)
    # (module, func_name, var_name) -> declared Type
    local_types: Dict[Tuple[Optional[str], str, str], T.Type] = field(default_factory=dict)
    # id(Call) -> resolved (module, name) of the target user function. irgen
    # uses this to pick the right LLVM symbol regardless of how the call was
    # written (qualified, unqualified, or — in later PRs — via from/as).
    call_resolution: Dict[int, FuncKey] = field(default_factory=dict)


# Names of builtin functions. Spawning a builtin is an error, and a qualified
# call (`mod.name`) can never refer to a builtin.
_BUILTIN_FUNCS: frozenset[str] = frozenset({
    "print", "len", "bytes_from_str", "str_from_bytes",
    "list_new", "list_push", "list_at", "list_at_opt",
    "tcp_listen", "tcp_accept", "read", "write", "close", "file_open",
    "sqrt", "floor", "ceil", "exp", "log", "sin", "cos", "fabs", "pow",
})


_BUILTIN_TYPES: Dict[str, T.Type] = {
    "int": T.INT,
    "float": T.FLOAT,
    "bool": T.BOOL,
    "string": T.STRING,
    "Bytes": T.BYTES,
    "List[int]": T.LIST_INT,
    "Option[int]": T.OPTION_INT,
    "Result[int, int]": T.RESULT_INT_INT,
    "void": T.VOID,
}


class Sema:
    def __init__(self, program: LoadedProgram, filename: str) -> None:
        self.program = program
        self.filename = filename
        self.result = SemaResult(filename=filename, functions={})
        # Depth of enclosing loops; break/continue are only valid when > 0.
        self._loop_depth = 0
        # User-declared type aliases (name -> resolved concrete Type).
        # Type aliases are module-local in spirit but PR1 keeps a single map
        # (the entry module's aliases dominate; imported modules' aliases are
        # processed when that module is checked). Type-alias import is a Non-Goal.
        self.type_alias_map: Dict[str, T.Type] = {}
        # The module currently being collected/checked. None = entry (root).
        self.current_module: Optional[str] = None
        # Module names visible to the current module via `import`.
        # PR1: a module can call `m.f()` only for an `m` it imported.
        self._visible_imports: set[str] = set()
        # All (module, A.Module) pairs in load order: entry first, then imports.
        self._all_modules: List[Tuple[Optional[str], A.Module]] = [
            (None, program.root)
        ] + [(name, mod) for name, mod in program.modules.items()]

    def _resolve_type(self, ty: A.TypeExpr) -> T.Type:
        if isinstance(ty, A.TypeName):
            if ty.name in _BUILTIN_TYPES:
                return _BUILTIN_TYPES[ty.name]
            if ty.name in self.type_alias_map:
                return self.type_alias_map[ty.name]
            raise CompileError(Diagnostic(self.filename, ty.line, ty.col, len(ty.name),
                                          f"unknown type: {ty.name}"))
        if isinstance(ty, A.TypeFuture):
            inner = self._resolve_type(ty.inner)
            if inner is T.VOID:
                return T.FutureType(T.VOID)
            return T.FutureType(inner)
        raise CompileError(Diagnostic(self.filename, 0, 0, 1,
                                      "internal: unknown type expr"))

    # ---------- entry ----------
    def analyze(self) -> SemaResult:
        # Phase 0: resolve top-level type aliases.
        # Aliases are resolved in declaration order, so an alias may refer to
        # an earlier alias (e.g. `type A = int; type B = A`). Forward
        # references are not supported in this minimal implementation.
        # Built-in type names (int, Bytes, List, ...) are lexer keywords, so
        # they can never appear as an alias name (the parser requires IDENT).
        # Hence no built-in-shadowing check is needed here.
        for mod_name, mod in self._all_modules:
            for alias in mod.type_aliases:
                if alias.name in self.type_alias_map:
                    raise CompileError(Diagnostic(
                        self.filename, alias.line, alias.col, len(alias.name),
                        f"duplicate type alias: {alias.name}",
                    ))
                resolved = self._resolve_type(alias.target)
                self.type_alias_map[alias.name] = resolved

        # First pass: collect signatures across all modules, keyed by
        # (module, name). Same-named functions in different modules coexist.
        for mod_name, mod in self._all_modules:
            for fn in mod.functions:
                key: FuncKey = (mod_name, fn.name)
                if key in self.result.functions:
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
                    pt = self._resolve_type(p.type_expr)
                    if pt is T.VOID:
                        raise CompileError(Diagnostic(
                            self.filename, p.line, p.col, len(p.name),
                            "parameter type cannot be void",
                        ))
                    params.append((p.name, pt))
                rt = self._resolve_type(fn.return_type)
                self.result.functions[key] = FuncSig(fn.name, params, rt, fn, mod_name)

        # main is required, in the entry module only.
        if (None, "main") not in self.result.functions:
            line = self.program.root.functions[0].line if self.program.root.functions else 1
            raise CompileError(Diagnostic(
                self.filename, line, 1, 1,
                "every rw program must define `def main() -> int`",
            ))
        main_sig = self.result.functions[(None, "main")]
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

        # Second pass: check each function body, module by module, so that
        # `current_module` / visible imports are scoped correctly.
        for mod_name, mod in self._all_modules:
            self.current_module = mod_name
            self._visible_imports = {imp.module for imp in mod.imports}
            for fn in mod.functions:
                self._check_func(fn)

        return self.result

    # ---------- function body ----------
    def _check_func(self, fn: A.FuncDef) -> None:
        sig = self.result.functions[(self.current_module, fn.name)]
        # Local scope: param name -> Type. New scopes are pushed for if/while bodies,
        # but in rw the entire function shares one flat scope. Re-declaring an
        # existing local is an error.
        locals_: Dict[str, T.Type] = {}
        for pname, pty in sig.params:
            locals_[pname] = pty
            self.result.local_types[(self.current_module, fn.name, pname)] = pty
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
            declared = self._resolve_type(stmt.type_expr)
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
            self.result.local_types[(self.current_module, fn.name, stmt.name)] = declared
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
            self._loop_depth += 1
            self._check_block(fn, stmt.body, dict(locals_), ret_ty)
            self._loop_depth -= 1
            return False  # while bodies don't guarantee return

        if isinstance(stmt, A.Break):
            if self._loop_depth == 0:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 5,
                    "`break` used outside of a loop",
                ))
            return False

        if isinstance(stmt, A.Continue):
            if self._loop_depth == 0:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 8,
                    "`continue` used outside of a loop",
                ))
            return False

        if isinstance(stmt, A.Assert):
            cond_ty = self._check_expr(fn, stmt.cond, locals_)
            if cond_ty is not T.BOOL:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 6,
                    f"assert condition must be bool, found `{cond_ty}`",
                ))
            if stmt.msg is not None:
                msg_ty = self._check_expr(fn, stmt.msg, locals_)
                if msg_ty is not T.STRING:
                    raise CompileError(Diagnostic(
                        self.filename, stmt.line, stmt.col, 6,
                        f"assert message must be string, found `{msg_ty}`",
                    ))
            return False

        if isinstance(stmt, A.MatchStmt):
            tt = self._check_expr(fn, stmt.target, locals_)
            if stmt.style == "option":
                if tt is not T.OPTION_INT:
                    raise CompileError(Diagnostic(
                        self.filename, stmt.line, stmt.col, 5,
                        f"match target must be Option[int], found `{tt}`",
                    ))
                # Some arm: bind some_var as int in a new locals scope.
                some_locals = dict(locals_)
                some_locals[stmt.some_var] = T.INT
                some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
                # None arm: no binding.
                none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
                # match terminates in return iff both arms do.
                return some_ret and none_ret
            # style == "result"
            if tt is not T.RESULT_INT_INT:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 5,
                    f"match target must be Result[int, int], found `{tt}`",
                ))
            ok_locals = dict(locals_)
            ok_locals[stmt.ok_var] = T.INT
            ok_ret = self._check_block(fn, stmt.ok_block, ok_locals, ret_ty)
            err_locals = dict(locals_)
            err_locals[stmt.err_var] = T.INT
            err_ret = self._check_block(fn, stmt.err_block, err_locals, ret_ty)
            return ok_ret and err_ret

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
            if expr.op == "~":
                if inner is not T.INT:
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 1,
                        f"unary `~` requires int, found `{inner}`",
                    ))
                return T.INT
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
            if op in ("&", "|", "^", "<<", ">>"):
                if lt is not T.INT or rt is not T.INT:
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        f"operator `{op}` requires int on both sides, "
                        f"found `{lt}` and `{rt}`",
                    ))
                return T.INT
            if op in ("==", "!="):
                if lt != rt:
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        f"`{op}` requires same type, found `{lt}` and `{rt}`",
                    ))
                if lt not in (T.INT, T.FLOAT, T.BOOL, T.STRING, T.BYTES):
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        f"cannot compare `{lt}` with `{op}`",
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
            if call.module is None and call.callee in _BUILTIN_FUNCS:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 5,
                    f"cannot spawn the builtin `{call.callee}`",
                ))
            # Resolve to a user function (raises on undefined / unimported).
            key = self._resolve_user_func(call)
            # Type-check the call as a normal call (we want arg types to flow through).
            self._check_call(fn, call, locals_)
            ret_ty = self.result.functions[key].return_type
            return T.FutureType(ret_ty)
        if isinstance(expr, A.AwaitExpr):
            tgt = self._check_expr(fn, expr.target, locals_)
            if not isinstance(tgt, T.FutureType):
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 5,
                    f"`await` requires a Future, found `{tgt}`",
                ))
            return tgt.inner
        if isinstance(expr, A.SomeExpr):
            at = self._check_expr(fn, expr.arg, locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 4,
                    f"Some argument must be int, found `{at}`",
                ))
            return T.OPTION_INT
        if isinstance(expr, A.NoneExpr):
            return T.OPTION_INT
        if isinstance(expr, A.OkExpr):
            at = self._check_expr(fn, expr.arg, locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 2,
                    f"Ok argument must be int, found `{at}`",
                ))
            return T.RESULT_INT_INT
        if isinstance(expr, A.ErrExpr):
            at = self._check_expr(fn, expr.arg, locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 3,
                    f"Err argument must be int, found `{at}`",
                ))
            return T.RESULT_INT_INT
        if isinstance(expr, A.IfExpr):
            ct = self._check_expr(fn, expr.cond, locals_)
            if ct is not T.BOOL:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 2,
                    f"conditional expression requires bool condition, found `{ct}`",
                ))
            tt = self._check_expr(fn, expr.then, locals_)
            et = self._check_expr(fn, expr.els, locals_)
            if tt != et:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 2,
                    f"conditional expression branches must have the same type, "
                    f"found `{tt}` and `{et}`",
                ))
            return tt
        raise CompileError(Diagnostic(
            self.filename, 0, 0, 1, f"internal: unknown expr {type(expr).__name__}",
        ))

    def _resolve_user_func(self, call: A.Call) -> FuncKey:
        """Resolve a (qualified or unqualified) call to a user function key, or
        raise. Does not type-check arguments. Records the result in
        call_resolution for irgen."""
        if call.module is not None:
            # Qualified call `mod.func`: `mod` must be imported by the current
            # module; builtins can never be qualified.
            if call.module not in self._visible_imports:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.module),
                    f"module '{call.module}' is not imported here",
                ))
            key: FuncKey = (call.module, call.callee)
            if key not in self.result.functions:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"module '{call.module}' has no function `{call.callee}`",
                ))
        else:
            # Unqualified call: a function in the current module.
            key = (self.current_module, call.callee)
            if key not in self.result.functions:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"undefined function: {call.callee}",
                ))
        self.result.call_resolution[id(call)] = key
        return key

    def _check_call(self, fn: A.FuncDef, call: A.Call, locals_: Dict[str, T.Type]) -> T.Type:
        # Qualified calls (`mod.func`) are always user functions; skip the
        # builtin dispatch entirely. See spec 17.
        if call.module is not None:
            return self._check_user_call(fn, call, locals_)
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
        # Builtin: len(string|Bytes|List[int]) -> int.
        if call.callee == "len":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.STRING and at is not T.BYTES and at is not T.LIST_INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len argument must be string, Bytes or List[int], found `{at}`",
                ))
            return T.INT
        # Builtin: bytes_from_str(string) -> Bytes.
        if call.callee == "bytes_from_str":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"bytes_from_str takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.STRING:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"bytes_from_str argument must be string, found `{at}`",
                ))
            return T.BYTES
        # Builtin: str_from_bytes(Bytes) -> string.
        if call.callee == "str_from_bytes":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"str_from_bytes takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.BYTES:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"str_from_bytes argument must be Bytes, found `{at}`",
                ))
            return T.STRING
        # Builtin: list_new() -> List[int].
        if call.callee == "list_new":
            if len(call.args) != 0:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_new takes no arguments, got {len(call.args)}",
                ))
            return T.LIST_INT
        # Builtin: list_push(List[int], int) -> List[int].
        if call.callee == "list_push":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_push takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.LIST_INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_push first argument must be List[int], found `{t0}`",
                ))
            if t1 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_push second argument must be int, found `{t1}`",
                ))
            return T.LIST_INT
        # Builtin: list_at(List[int], int) -> int.
        if call.callee == "list_at":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.LIST_INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at first argument must be List[int], found `{t0}`",
                ))
            if t1 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at second argument must be int, found `{t1}`",
                ))
            return T.INT
        # Builtin: list_at_opt(List[int], int) -> Option[int].
        if call.callee == "list_at_opt":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at_opt takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.LIST_INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at_opt first argument must be List[int], found `{t0}`",
                ))
            if t1 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at_opt second argument must be int, found `{t1}`",
                ))
            return T.OPTION_INT
        # Builtin: tcp_listen(int) -> int.
        if call.callee == "tcp_listen":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_listen takes 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_listen argument must be int, found `{at}`",
                ))
            return T.INT
        # Builtin: tcp_accept(int) -> int.
        if call.callee == "tcp_accept":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_accept takes 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"tcp_accept argument must be int, found `{at}`",
                ))
            return T.INT
        # Builtin: read(int, int) -> Bytes.
        if call.callee == "read":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"read takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"read first argument must be int, found `{t0}`",
                ))
            if t1 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"read second argument must be int, found `{t1}`",
                ))
            return T.BYTES
        # Builtin: write(int, Bytes) -> int.
        if call.callee == "write":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"write takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"write first argument must be int, found `{t0}`",
                ))
            if t1 is not T.BYTES:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"write second argument must be Bytes, found `{t1}`",
                ))
            return T.INT
        # Builtin: close(int) -> int.
        if call.callee == "close":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"close takes 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"close argument must be int, found `{at}`",
                ))
            return T.INT
        # Builtin: file_open(string, string) -> int.
        if call.callee == "file_open":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"file_open takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.STRING:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"file_open first argument must be string, found `{t0}`",
                ))
            if t1 is not T.STRING:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"file_open second argument must be string, found `{t1}`",
                ))
            return T.INT
        # Builtin math: unary float -> float (LLVM intrinsics).
        if call.callee in ("sqrt", "floor", "ceil", "exp", "log", "sin", "cos", "fabs"):
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"{call.callee} takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.FLOAT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"{call.callee} argument must be float, found `{at}`",
                ))
            return T.FLOAT
        # Builtin math: pow(float, float) -> float.
        if call.callee == "pow":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"pow takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if t0 is not T.FLOAT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"pow first argument must be float, found `{t0}`",
                ))
            if t1 is not T.FLOAT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"pow second argument must be float, found `{t1}`",
                ))
            return T.FLOAT
        return self._check_user_call(fn, call, locals_)

    def _check_user_call(self, fn: A.FuncDef, call: A.Call, locals_: Dict[str, T.Type]) -> T.Type:
        """Resolve and type-check a call to a user-defined function (qualified
        or not). Builtins must already have been dispatched."""
        key = self._resolve_user_func(call)
        sig = self.result.functions[key]
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
    """Analyze a single module (no imports). Convenience wrapper used by tests."""
    program = LoadedProgram(root=module, root_name="<root>")
    return Sema(program, filename).analyze()


def analyze_program(program: LoadedProgram, filename: str = "<input>") -> SemaResult:
    return Sema(program, filename).analyze()
