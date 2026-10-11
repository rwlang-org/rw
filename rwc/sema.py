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
    # id(Call) -> record type, for calls that construct a record (spec 20).
    constructor_calls: Dict[int, T.ClassType] = field(default_factory=dict)
    # Function values (spec 21). func_refs: id(Name/FieldAccess) -> the
    # referenced function. local_fn_calls: ids of `h(...)` calls through a
    # local. field_fn_calls: id(Call) -> record type, for `r.f(...)` where r
    # is a local record and f a function-valued field.
    func_refs: Dict[int, FuncKey] = field(default_factory=dict)
    local_fn_calls: set[int] = field(default_factory=set)
    field_fn_calls: Dict[int, T.ClassType] = field(default_factory=dict)


# Names of builtin functions. Spawning a builtin is an error, and a qualified
# call (`mod.name`) can never refer to a builtin.
_BUILTIN_FUNCS: frozenset[str] = frozenset({
    "print", "len", "bytes_from_str", "str_from_bytes",
    "list_new", "list_push", "list_at", "list_at_opt",
    "tcp_listen", "tcp_accept", "read", "write", "close", "file_open",
    "sqrt", "floor", "ceil", "exp", "log", "sin", "cos", "fabs", "pow",
    "str_find", "str_slice", "str_starts_with", "str_from_int", "str_to_int",
    "int", "float", "str",
})

# Accepted argument types for the conversion builtins (spec 26).
_CONVERSIONS: Dict[str, Tuple[Tuple[T.Type, ...], T.Type, str]] = {
    "int": ((T.INT, T.FLOAT, T.BOOL), T.INT, "int, float or bool"),
    "float": ((T.INT, T.FLOAT), T.FLOAT, "int or float"),
    "str": ((T.INT, T.FLOAT, T.BOOL, T.STRING), T.STRING, "int, float, bool or string"),
}

# Fixed signatures (parameter types, return type) for builtins that need no
# special-casing beyond argument count and type checks. See spec 18.
_SIMPLE_BUILTIN_SIGS: Dict[str, Tuple[Tuple[T.Type, ...], T.Type]] = {
    "str_find": ((T.STRING, T.STRING), T.INT),
    "str_slice": ((T.STRING, T.INT, T.INT), T.STRING),
    "str_starts_with": ((T.STRING, T.STRING), T.BOOL),
    "str_from_int": ((T.INT,), T.STRING),
    "str_to_int": ((T.STRING,), T.OPTION_INT),
}


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
        # The entry file; `filename` tracks the module currently being checked
        # so diagnostics from an imported module name that module's file.
        self._root_filename = filename
        self.result = SemaResult(filename=filename, functions={})
        # Depth of enclosing loops; break/continue are only valid when > 0.
        self._loop_depth = 0
        # User-declared type aliases (name -> resolved concrete Type), one map
        # per module. Aliases are module-local: type-alias import is a Non-Goal,
        # so `type_alias_map` always points at the current module's map.
        self._aliases_by_module: Dict[Optional[str], Dict[str, T.Type]] = {}
        self.type_alias_map: Dict[str, T.Type] = {}
        # Record types per module (spec 20); `class_map` is the current one.
        self._classes_by_module: Dict[Optional[str], Dict[str, T.ClassType]] = {}
        self.class_map: Dict[str, T.ClassType] = {}
        # id(list_new Call) -> the list type it takes from context (spec 22).
        self._list_new_hints: Dict[int, T.ListType] = {}
        # Import qualifier -> real module, per module; `_import_env` is the
        # current one. Built before any type is resolved so `mod.Class`
        # type names work in signatures and fields.
        self._import_envs: Dict[Optional[str], Dict[str, str]] = {}
        # The module currently being collected/checked. None = entry (root).
        self.current_module: Optional[str] = None
        # Qualifiers visible to the current module via `import`/`import as`:
        # visible name -> real module name. `import x` -> {x: x}; `import x as m`
        # -> {m: x}. A qualified call `q.f()` is valid only for a `q` in here.
        self._import_env: Dict[str, str] = {}
        # Names brought into the current module via `from x import y [as w]`:
        # local name -> resolved (module, name). PR2.
        self._from_env: Dict[str, FuncKey] = {}
        # All (module, A.Module) pairs in load order: entry first, then imports.
        self._all_modules: List[Tuple[Optional[str], A.Module]] = [
            (None, program.root)
        ] + [(name, mod) for name, mod in program.modules.items()]

    def _enter_module(self, mod_name: Optional[str]) -> None:
        """Make `mod_name` the module whose names, aliases, and file are in scope."""
        self.current_module = mod_name
        self.type_alias_map = self._aliases_by_module.setdefault(mod_name, {})
        self.class_map = self._classes_by_module.setdefault(mod_name, {})
        self._import_env = self._import_envs.get(mod_name, {})
        if mod_name is None:
            self.filename = self._root_filename
        else:
            self.filename = self.program.paths.get(mod_name, self._root_filename)

    def _resolve_type(self, ty: A.TypeExpr) -> T.Type:
        if isinstance(ty, A.TypeName) and ty.module is not None:
            return self._qualified_class(ty.module, ty.name, ty.line, ty.col)
        if isinstance(ty, A.TypeName):
            if ty.name in _BUILTIN_TYPES:
                return _BUILTIN_TYPES[ty.name]
            if ty.name in self.type_alias_map:
                return self.type_alias_map[ty.name]
            if ty.name in self.class_map:
                return self.class_map[ty.name]
            raise CompileError(Diagnostic(self.filename, ty.line, ty.col, len(ty.name),
                                          f"unknown type: {ty.name}"))
        if isinstance(ty, A.TypeList):
            elem = self._resolve_type(ty.elem)
            if elem is T.VOID:
                raise CompileError(Diagnostic(self.filename, ty.line, ty.col, 4,
                                              "List element type cannot be void"))
            return T.list_of(elem)
        if isinstance(ty, A.TypeCallable):
            params: List[T.Type] = []
            for p in ty.params:
                pt = self._resolve_type(p)
                if pt is T.VOID:
                    raise CompileError(Diagnostic(self.filename, ty.line, ty.col, 8,
                                                  "Callable parameter type cannot be void"))
                params.append(pt)
            return T.FunctionType(params, self._resolve_type(ty.ret))
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
        # Record names first, so aliases and fields may refer to any class
        # of the module (spec 20).
        for mod_name, mod in self._all_modules:
            self._enter_module(mod_name)
            for cdef in mod.classes:
                if cdef.name in self.class_map:
                    raise CompileError(Diagnostic(
                        self.filename, cdef.line, cdef.col, len(cdef.name),
                        f"duplicate class: {cdef.name}",
                    ))
                if cdef.name in _BUILTIN_FUNCS:
                    raise CompileError(Diagnostic(
                        self.filename, cdef.line, cdef.col, len(cdef.name),
                        f"class name `{cdef.name}` collides with a builtin",
                    ))
                self.class_map[cdef.name] = T.ClassType(cdef.name, mod_name)

        # Import qualifiers, and classes brought in by `from x import C`.
        for mod_name, mod in self._all_modules:
            self._enter_module(mod_name)
            self._import_envs[mod_name] = self._build_import_env(mod)
            self._bind_imported_classes(mod)

        for mod_name, mod in self._all_modules:
            self._enter_module(mod_name)
            for alias in mod.type_aliases:
                if alias.name in self.class_map:
                    raise CompileError(Diagnostic(
                        self.filename, alias.line, alias.col, len(alias.name),
                        f"`{alias.name}` is already a class",
                    ))
                if alias.name in self.type_alias_map:
                    raise CompileError(Diagnostic(
                        self.filename, alias.line, alias.col, len(alias.name),
                        f"duplicate type alias: {alias.name}",
                    ))
                resolved = self._resolve_type(alias.target)
                self.type_alias_map[alias.name] = resolved

        # Record fields, now that every class and alias name is known.
        for mod_name, mod in self._all_modules:
            self._enter_module(mod_name)
            for cdef in mod.classes:
                cls = self.class_map[cdef.name]
                for fdecl in cdef.fields:
                    if cls.field_index(fdecl.name) is not None:
                        raise CompileError(Diagnostic(
                            self.filename, fdecl.line, fdecl.col, len(fdecl.name),
                            f"duplicate field `{fdecl.name}` in class {cdef.name}",
                        ))
                    fty = self._resolve_type(fdecl.type_expr)
                    if fty is T.VOID:
                        raise CompileError(Diagnostic(
                            self.filename, fdecl.line, fdecl.col, len(fdecl.name),
                            f"field `{fdecl.name}` cannot be void",
                        ))
                    cls.fields.append((fdecl.name, fty))

        # First pass: collect signatures across all modules, keyed by
        # (module, name). Same-named functions in different modules coexist.
        for mod_name, mod in self._all_modules:
            self._enter_module(mod_name)
            for fn in mod.functions:
                key: FuncKey = (mod_name, fn.name)
                if fn.name in self.class_map:
                    raise CompileError(Diagnostic(
                        self.filename, fn.line, fn.col, len(fn.name),
                        f"`{fn.name}` is already a class",
                    ))
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
        self._enter_module(None)
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
        # `current_module` / visible imports / from-bindings are scoped correctly.
        for mod_name, mod in self._all_modules:
            self._enter_module(mod_name)
            self._from_env = self._build_from_env(mod_name, mod)
            for fn in mod.functions:
                self._check_func(fn)

        self._enter_module(None)
        return self.result

    def _qualified_class(self, qualifier: str, name: str, line: int, col: int) -> T.ClassType:
        """Resolve `qualifier.Name` to a class of an imported module."""
        if qualifier not in self._import_env:
            raise CompileError(Diagnostic(
                self.filename, line, col, len(qualifier),
                f"module '{qualifier}' is not imported here",
            ))
        real = self._import_env[qualifier]
        cls = self._classes_by_module.get(real, {}).get(name)
        if cls is None:
            raise CompileError(Diagnostic(
                self.filename, line, col, len(qualifier) + 1 + len(name),
                f"module '{qualifier}' has no class `{name}`",
            ))
        return cls

    def _bind_imported_classes(self, mod: A.Module) -> None:
        """`from x import C [as D]` where C is a class of x: make it visible in
        the current module's class table under its local name (spec 20)."""
        local_funcs = {fn.name for fn in mod.functions}
        for imp in mod.imports:
            if imp.names is None:
                continue
            src_classes = self._classes_by_module.get(imp.module, {})
            for src_name, alias in imp.names:
                if src_name not in src_classes:
                    continue  # a function import; handled by _build_from_env
                local = alias or src_name
                if local in self.class_map or local in local_funcs:
                    raise CompileError(Diagnostic(
                        self.filename, imp.line, imp.col, max(1, len(local)),
                        f"imported name `{local}` collides with a local function or class",
                    ))
                self.class_map[local] = src_classes[src_name]

    def _build_import_env(self, mod: A.Module) -> Dict[str, str]:
        """Resolve module qualifiers for one module: visible name -> real
        module. Only plain/aliased imports (names is None) contribute; from
        imports bind individual names instead. Detects duplicate qualifiers."""
        env: Dict[str, str] = {}
        for imp in mod.imports:
            if imp.names is not None:
                continue  # `from x import ...` does not introduce a qualifier
            visible = imp.alias or imp.module
            if visible in env:
                raise CompileError(Diagnostic(
                    self.filename, imp.line, imp.col, max(1, len(visible)),
                    f"duplicate import qualifier `{visible}`",
                ))
            env[visible] = imp.module
        return env

    def _build_from_env(self, mod_name: Optional[str], mod: A.Module) -> Dict[str, FuncKey]:
        """Resolve `from x import y [as w]` bindings for one module: local name
        -> (source_module, source_name). Detects collisions with local
        functions, builtins, and other from-bindings."""
        env: Dict[str, FuncKey] = {}
        local_names = {fn.name for fn in mod.functions} | {c.name for c in mod.classes}
        for imp in mod.imports:
            if imp.names is None:
                continue  # plain `import x` / `import x as m`
            for src_name, alias in imp.names:
                local = alias or src_name
                if src_name in self._classes_by_module.get(imp.module, {}):
                    continue  # a class import; bound by _bind_imported_classes
                src_key: FuncKey = (imp.module, src_name)
                if src_key not in self.result.functions:
                    raise CompileError(Diagnostic(
                        self.filename, imp.line, imp.col, max(1, len(src_name)),
                        f"module '{imp.module}' has no function or class `{src_name}`",
                    ))
                if local in _BUILTIN_FUNCS:
                    raise CompileError(Diagnostic(
                        self.filename, imp.line, imp.col, max(1, len(local)),
                        f"imported name `{local}` collides with a builtin",
                    ))
                if local in local_names:
                    raise CompileError(Diagnostic(
                        self.filename, imp.line, imp.col, max(1, len(local)),
                        f"imported name `{local}` collides with a local function or class",
                    ))
                if local in env:
                    raise CompileError(Diagnostic(
                        self.filename, imp.line, imp.col, max(1, len(local)),
                        f"duplicate imported name `{local}`",
                    ))
                env[local] = src_key
        return env

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
            val_ty = self._check_expr_expect(fn, stmt.value, locals_, declared)
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
            val_ty = self._check_expr_expect(fn, stmt.value, locals_, expected)
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

        if isinstance(stmt, A.FieldAssign):
            obj_ty = self._check_expr(fn, stmt.obj, locals_)
            _, fty = self._field_of(obj_ty, stmt.field, stmt.line, stmt.col)
            val_ty = self._check_expr_expect(fn, stmt.value, locals_, fty)
            if val_ty != fty:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, len(stmt.field),
                    f"cannot assign `{val_ty}` to field `{stmt.field}` of type `{fty}`",
                ))
            return False

        if isinstance(stmt, A.Return):
            if stmt.value is None:
                if ret_ty is not T.VOID:
                    raise CompileError(Diagnostic(
                        self.filename, stmt.line, stmt.col, 6,
                        f"function returns `{ret_ty}` but got bare `return`",
                    ))
                return True
            val_ty = self._check_expr_expect(fn, stmt.value, locals_, ret_ty)
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
    def _check_expr_expect(
        self,
        fn: A.FuncDef,
        expr: A.Expr,
        locals_: Dict[str, T.Type],
        expected: T.Type,
    ) -> T.Type:
        """Like _check_expr, but a bare `list_new()` takes the expected list
        type (spec 22), also through `list_push(list_new(), x)` chains. Used
        wherever the context knows the type it wants."""
        if isinstance(expected, T.ListType):
            self._hint_list_new(expr, expected, locals_)
        return self._check_expr(fn, expr, locals_)

    def _is_builtin_call(self, expr: A.Expr, name: str, locals_: Dict[str, T.Type]) -> bool:
        return (
            isinstance(expr, A.Call)
            and expr.module is None
            and expr.callee == name
            and name not in locals_
            and name not in self.class_map
        )

    def _hint_list_new(self, expr: A.Expr, expected: T.ListType, locals_: Dict[str, T.Type]) -> None:
        """Record the list type a `list_new()` should take, following the list
        argument of nested `list_push` calls."""
        if self._is_builtin_call(expr, "list_new", locals_):
            self._list_new_hints[id(expr)] = expected
        elif self._is_builtin_call(expr, "list_push", locals_):
            assert isinstance(expr, A.Call)
            if expr.args:
                self._hint_list_new(expr.args[0], expected, locals_)

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
            if expr.name in locals_:
                return locals_[expr.name]
            # A function used as a value (spec 21).
            ref: Optional[FuncKey] = None
            if (self.current_module, expr.name) in self.result.functions:
                ref = (self.current_module, expr.name)
            elif expr.name in self._from_env:
                ref = self._from_env[expr.name]
            if ref is not None:
                self.result.func_refs[id(expr)] = ref
                return self._func_value_type(ref)
            if expr.name in _BUILTIN_FUNCS:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, len(expr.name),
                    f"cannot use builtin `{expr.name}` as a value",
                ))
            raise CompileError(Diagnostic(
                self.filename, expr.line, expr.col, len(expr.name),
                f"undefined variable: {expr.name}",
            ))
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
                if lt != rt or not (T.is_numeric(lt) or lt is T.STRING):
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, len(op),
                        f"`{op}` requires int or float on both sides, or two strings, "
                        f"found `{lt}` and `{rt}`",
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
        if (
            isinstance(expr, A.FieldAccess)
            and isinstance(expr.obj, A.Name)
            and expr.obj.name not in locals_
            and expr.obj.name in self._import_env
        ):
            # `mod.f` used as a value (spec 21).
            key: FuncKey = (self._import_env[expr.obj.name], expr.field)
            if key not in self.result.functions:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, len(expr.field),
                    f"module '{expr.obj.name}' has no function `{expr.field}`",
                ))
            self.result.func_refs[id(expr)] = key
            return self._func_value_type(key)
        if isinstance(expr, A.CallValue):
            callee_ty = self._check_expr(fn, expr.callee, locals_)
            return self._check_fn_value_call(fn, callee_ty, expr.args, "function value",
                                             expr.line, expr.col, locals_)
        if isinstance(expr, A.FieldAccess):
            obj_ty = self._check_expr(fn, expr.obj, locals_)
            _, fty = self._field_of(obj_ty, expr.field, expr.line, expr.col)
            return fty
        if isinstance(expr, A.SpawnExpr):
            # spawn requires a user-defined function call.
            call = expr.call
            if (call.module is not None and call.module in locals_) or (
                call.module is None and isinstance(locals_.get(call.callee), T.FunctionType)
            ):
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 5,
                    "cannot spawn a function value",
                ))
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
            if isinstance(ret_ty, T.ClassType):
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 5,
                    "cannot spawn a function returning a record",
                ))
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
            # Qualified call `q.func`: `q` must be a visible import qualifier
            # (a module name or an `as` alias); builtins can never be qualified.
            if call.module not in self._import_env:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.module),
                    f"module '{call.module}' is not imported here",
                ))
            real_module = self._import_env[call.module]
            key: FuncKey = (real_module, call.callee)
            if key not in self.result.functions:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"module '{call.module}' has no function `{call.callee}`",
                ))
        elif call.callee in self._from_env:
            # Unqualified call to a `from x import y` name.
            key = self._from_env[call.callee]
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
        # Calls through function values (spec 21) come first: locals shadow
        # functions, builtins, and import qualifiers.
        if call.module is not None and call.module in locals_:
            # `r.f(...)`: call the function stored in field f of record r.
            cls, fty = self._field_of(locals_[call.module], call.callee, call.line, call.col)
            self.result.field_fn_calls[id(call)] = cls
            return self._check_fn_value_call(fn, fty, call.args, call.callee,
                                             call.line, call.col, locals_)
        if call.module is None and isinstance(locals_.get(call.callee), T.FunctionType):
            self.result.local_fn_calls.add(id(call))
            return self._check_fn_value_call(fn, locals_[call.callee], call.args, call.callee,
                                             call.line, call.col, locals_)
        # Qualified calls (`mod.func`) are always user functions; skip the
        # builtin dispatch entirely. See spec 17.
        if call.module is not None:
            real = self._import_env.get(call.module)
            qcls = self._classes_by_module.get(real, {}).get(call.callee) if real else None
            if qcls is not None:
                return self._check_constructor(fn, call, locals_, qcls)
            return self._check_user_call(fn, call, locals_)
        if call.callee in self.class_map:
            return self._check_constructor(fn, call, locals_, self.class_map[call.callee])
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
            if at is not T.STRING and at is not T.BYTES and not isinstance(at, T.ListType):
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len argument must be string, Bytes or a List, found `{at}`",
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
        if call.callee in _CONVERSIONS:
            accepted, ret, names = _CONVERSIONS[call.callee]
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"{call.callee} takes 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at not in accepted:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"{call.callee} argument must be {names}, found `{at}`",
                ))
            return ret
        if call.callee in _SIMPLE_BUILTIN_SIGS:
            params, ret = _SIMPLE_BUILTIN_SIGS[call.callee]
            if len(call.args) != len(params):
                noun = "argument" if len(params) == 1 else "arguments"
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"{call.callee} takes {len(params)} {noun}, got {len(call.args)}",
                ))
            for i, (arg, want) in enumerate(zip(call.args, params, strict=True), start=1):
                at = self._check_expr(fn, arg, locals_)
                if at is not want:
                    raise CompileError(Diagnostic(
                        self.filename, call.line, call.col, len(call.callee),
                        f"{call.callee} argument {i} must be {want}, found `{at}`",
                    ))
            return ret
        # Builtin: list_new() -> List[T]. With no expected type (see
        # _check_expr_expect) it is List[int], as before spec 22.
        if call.callee == "list_new":
            if len(call.args) != 0:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_new takes no arguments, got {len(call.args)}",
                ))
            return self._list_new_hints.get(id(call), T.LIST_INT)
        # Builtin: list_push(List[T], T) -> List[T].
        if call.callee == "list_push":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_push takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            if not isinstance(t0, T.ListType):
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_push first argument must be a List, found `{t0}`",
                ))
            t1 = self._check_expr_expect(fn, call.args[1], locals_, t0.elem)
            if t1 != t0.elem:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_push second argument must be {t0.elem}, found `{t1}`",
                ))
            return t0
        # Builtin: list_at(List[T], int) -> T.
        if call.callee == "list_at":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at takes 2 arguments, got {len(call.args)}",
                ))
            t0 = self._check_expr(fn, call.args[0], locals_)
            t1 = self._check_expr(fn, call.args[1], locals_)
            if not isinstance(t0, T.ListType):
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at first argument must be a List, found `{t0}`",
                ))
            if t1 is not T.INT:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"list_at second argument must be int, found `{t1}`",
                ))
            return t0.elem
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

    def _check_constructor(
        self, fn: A.FuncDef, call: A.Call, locals_: Dict[str, T.Type], cls: T.ClassType
    ) -> T.Type:
        """`ClassName(args...)`: one positional argument per field (spec 20)."""
        if len(call.args) != len(cls.fields):
            raise CompileError(Diagnostic(
                self.filename, call.line, call.col, len(call.callee),
                f"{cls} takes {len(cls.fields)} arguments, got {len(call.args)}",
            ))
        for i, (arg, (fname, fty)) in enumerate(zip(call.args, cls.fields, strict=True), start=1):
            at = self._check_expr_expect(fn, arg, locals_, fty)
            if at != fty:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"field {i} (`{fname}`) of {cls} must be {fty}, found `{at}`",
                ))
        self.result.constructor_calls[id(call)] = cls
        return cls

    def _field_of(self, obj_ty: T.Type, field: str, line: int, col: int) -> Tuple[T.ClassType, T.Type]:
        """Look up `field` on a record type, or raise a diagnostic."""
        if not isinstance(obj_ty, T.ClassType):
            raise CompileError(Diagnostic(
                self.filename, line, col, len(field),
                f"`{obj_ty}` has no fields",
            ))
        idx = obj_ty.field_index(field)
        if idx is None:
            raise CompileError(Diagnostic(
                self.filename, line, col, len(field),
                f"{obj_ty} has no field `{field}`",
            ))
        return obj_ty, obj_ty.fields[idx][1]

    def _func_value_type(self, key: FuncKey) -> T.FunctionType:
        sig = self.result.functions[key]
        return T.FunctionType([pt for _, pt in sig.params], sig.return_type)

    def _check_fn_value_call(
        self,
        fn: A.FuncDef,
        callee_ty: T.Type,
        args: List[A.Expr],
        what: str,
        line: int,
        col: int,
        locals_: Dict[str, T.Type],
    ) -> T.Type:
        """Type-check a call through a function value of type callee_ty."""
        if not isinstance(callee_ty, T.FunctionType):
            raise CompileError(Diagnostic(
                self.filename, line, col, max(1, len(what)),
                f"`{callee_ty}` is not callable",
            ))
        if len(args) != len(callee_ty.params):
            raise CompileError(Diagnostic(
                self.filename, line, col, max(1, len(what)),
                f"`{what}` expects {len(callee_ty.params)} argument(s), got {len(args)}",
            ))
        for i, (arg, pty) in enumerate(zip(args, callee_ty.params, strict=True), start=1):
            at = self._check_expr_expect(fn, arg, locals_, pty)
            if at != pty:
                raise CompileError(Diagnostic(
                    self.filename, line, col, max(1, len(what)),
                    f"argument {i} of `{what}`: expected `{pty}`, found `{at}`",
                ))
        return callee_ty.ret

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
            at = self._check_expr_expect(fn, arg, locals_, pty)
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
