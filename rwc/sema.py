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
from .diagnostics import CompileError, Diagnostic, conversion_hint, did_you_mean
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
class ClosureInfo:
    """A nested `def` or a `lambda` (spec 71), compiled as its own function
    `key` taking an environment of `captures` (copied by value when the
    closure is created) before its parameters."""
    key: FuncKey
    func: A.FuncDef
    captures: List[Tuple[str, T.Type]]
    self_name: Optional[str]
    type: T.FunctionType


@dataclass
class _ClosureCtx:
    candidates: set
    used: set


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
    # id(ForEach) -> type of its loop variable (spec 29). Kept per loop, not
    # per name, because sibling loops may reuse a name with another type.
    foreach_types: Dict[int, T.Type] = field(default_factory=dict)
    # id(VarDecl or declaring Assign) -> the declared type (spec 32). Kept
    # per statement because sibling blocks may declare one name with
    # different types.
    decl_types: Dict[int, T.Type] = field(default_factory=dict)
    # Method calls (spec 33): id(Call | CallValue) -> the method's function key.
    method_calls: Dict[int, FuncKey] = field(default_factory=dict)
    # id(With) -> the resource type (spec 47).
    with_types: Dict[int, T.Type] = field(default_factory=dict)
    # id(Name | FieldAccess) -> the constant it refers to (spec 35); irgen
    # emits the constant's literal in its place.
    const_refs: Dict[int, A.ConstDef] = field(default_factory=dict)
    # Tuples (spec 37): id(Index on a tuple) -> normalised element index, and
    # (id(TupleAssign), position) -> type for targets that are declared.
    tuple_index: Dict[int, int] = field(default_factory=dict)
    unpack_decls: Dict[Tuple[int, int], T.Type] = field(default_factory=dict)
    # Closures (spec 71): id(LocalFunc | Lambda) -> what irgen needs.
    closures: Dict[int, ClosureInfo] = field(default_factory=dict)
    # id(Call) in local_fn_calls -> the type of the function value called.
    local_fn_call_types: Dict[int, T.Type] = field(default_factory=dict)


# Names of builtin functions. Spawning a builtin is an error, and a qualified
# call (`mod.name`) can never refer to a builtin.
_BUILTIN_FUNCS: frozenset[str] = frozenset({
    "print", "len", "bytes_from_str", "str_from_bytes",
    "list_new", "list_push", "list_at", "list_at_opt",
    "tcp_listen", "tcp_accept", "read", "write", "close", "file_open",
    "sqrt", "floor", "ceil", "exp", "log", "sin", "cos", "fabs", "pow",
    "str_find", "str_slice", "str_starts_with", "str_from_int", "str_to_int",
    "int", "float", "str",
    "str_split", "str_join", "str_strip", "str_replace", "str_lower", "str_upper",
    "str_contains", "str_ends_with",
    "str_lstrip", "str_rstrip", "str_rfind",
    "dict_new", "dict_get", "dict_has", "dict_remove", "dict_keys", "dict_values",
    "sleep", "time_ms", "mono_ns",
    "rand_int", "rand_float", "rand_seed", "rand_bytes",
    "chan_new", "chan_send", "chan_recv", "chan_close",
    "await_all", "await_any",
    "mutex_new", "mutex_lock", "mutex_unlock", "mutex_try_lock",
    "os_args", "os_getenv", "os_setenv", "os_exit", "os_getcwd", "os_chdir",
    "dial", "listen", "accept", "open", "local_addr", "peer_addr",
    "last_error", "error_name", "error_message",
    "write_all",
    "dial_timeout", "set_read_timeout", "set_write_timeout",
    "shutdown", "set_nodelay", "set_keepalive", "set_backlog",
    "signal_wait",
    "byte_at", "chr", "str_to_float", "str_from_byte",
    "fs_kind", "fs_size", "fs_mtime_ms", "fs_read_dir", "fs_rename", "fs_remove", "fs_mkdir",
    "proc_start", "proc_wait", "proc_kill",
    "gc_collect", "gc_heap_size",
    "assert_eq",
    "recv_from", "send_to",
})

_DICT_BUILTINS = frozenset({
    "dict_new", "dict_get", "dict_has", "dict_remove", "dict_keys", "dict_values",
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
    # spec 28
    "str_split": ((T.STRING, T.STRING), T.list_of(T.STRING)),
    "str_join": ((T.list_of(T.STRING), T.STRING), T.STRING),
    "str_strip": ((T.STRING,), T.STRING),
    "str_replace": ((T.STRING, T.STRING, T.STRING), T.STRING),
    "str_lower": ((T.STRING,), T.STRING),
    "str_upper": ((T.STRING,), T.STRING),
    "str_contains": ((T.STRING, T.STRING), T.BOOL),
    "str_ends_with": ((T.STRING, T.STRING), T.BOOL),
    # spec 55
    "str_lstrip": ((T.STRING,), T.STRING),
    "str_rstrip": ((T.STRING,), T.STRING),
    "str_rfind": ((T.STRING, T.STRING), T.INT),
    # spec 39
    "sleep": ((T.INT,), T.VOID),
    "time_ms": ((), T.INT),
    "mono_ns": ((), T.INT),
    # spec 43
    "rand_int": ((T.INT, T.INT), T.INT),
    "rand_float": ((), T.FLOAT),
    "rand_seed": ((T.INT,), T.VOID),
    "rand_bytes": ((T.INT,), T.BYTES),
    # spec 46
    "mutex_new": ((), T.MUTEX),
    "mutex_lock": ((T.MUTEX,), T.VOID),
    "mutex_unlock": ((T.MUTEX,), T.VOID),
    "mutex_try_lock": ((T.MUTEX,), T.BOOL),
    # spec 40
    "os_args": ((), T.list_of(T.STRING)),
    "os_getenv": ((T.STRING,), T.option_of(T.STRING)),
    "os_setenv": ((T.STRING, T.STRING), T.BOOL),
    "os_exit": ((T.INT,), T.VOID),
    "os_getcwd": ((), T.STRING),
    "os_chdir": ((T.STRING,), T.BOOL),
    # spec 50
    "dial": ((T.STRING,), T.INT),
    "listen": ((T.STRING,), T.INT),
    "accept": ((T.INT,), T.INT),
    "open": ((T.STRING, T.STRING), T.INT),
    "local_addr": ((T.INT,), T.STRING),
    "peer_addr": ((T.INT,), T.STRING),
    # spec 51
    "last_error": ((), T.INT),
    "error_name": ((T.INT,), T.STRING),
    "error_message": ((T.INT,), T.STRING),
    # spec 52
    "write_all": ((T.INT, T.BYTES), T.INT),
    # spec 53
    "dial_timeout": ((T.STRING, T.INT), T.INT),
    "set_read_timeout": ((T.INT, T.INT), T.VOID),
    "set_write_timeout": ((T.INT, T.INT), T.VOID),
    # spec 56
    "shutdown": ((T.INT, T.STRING), T.INT),
    "set_nodelay": ((T.INT, T.BOOL), T.INT),
    "set_keepalive": ((T.INT, T.BOOL), T.INT),
    "set_backlog": ((T.INT, T.INT), T.INT),
    # spec 57
    "signal_wait": ((T.list_of(T.STRING),), T.STRING),
    # spec 58
    "byte_at": ((T.STRING, T.INT), T.INT),
    "chr": ((T.INT,), T.STRING),
    "str_to_float": ((T.STRING,), T.option_of(T.FLOAT)),
    "str_from_byte": ((T.INT,), T.STRING),  # spec 62
    # spec 59
    "fs_kind": ((T.STRING,), T.STRING),
    "fs_size": ((T.STRING,), T.INT),
    "fs_mtime_ms": ((T.STRING,), T.INT),
    "fs_read_dir": ((T.STRING,), T.list_of(T.STRING)),
    "fs_rename": ((T.STRING, T.STRING), T.INT),
    "fs_remove": ((T.STRING,), T.INT),
    "fs_mkdir": ((T.STRING,), T.INT),
    # spec 61
    "proc_start": ((T.list_of(T.STRING),), T.list_of(T.INT)),
    "proc_wait": ((T.INT,), T.INT),
    "proc_kill": ((T.INT, T.STRING), T.INT),
    # spec 69
    "recv_from": ((T.INT, T.INT), T.TupleType([T.BYTES, T.STRING])),
    "send_to": ((T.INT, T.BYTES, T.STRING), T.INT),
    # spec 67
    "gc_collect": ((), T.VOID),
    "gc_heap_size": ((), T.INT),
}


_BUILTIN_TYPES: Dict[str, T.Type] = {
    "int": T.INT,
    "float": T.FLOAT,
    "bool": T.BOOL,
    "string": T.STRING,
    "Bytes": T.BYTES,
    "Mutex": T.MUTEX,
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
        # Closures being checked, innermost last (spec 71).
        self._closure_stack: List[_ClosureCtx] = []
        self._closure_counter = 0
        # User-declared type aliases (name -> resolved concrete Type), one map
        # per module. Aliases are module-local: type-alias import is a Non-Goal,
        # so `type_alias_map` always points at the current module's map.
        self._aliases_by_module: Dict[Optional[str], Dict[str, T.Type]] = {}
        self.type_alias_map: Dict[str, T.Type] = {}
        # Record types per module (spec 20); `class_map` is the current one.
        self._classes_by_module: Dict[Optional[str], Dict[str, T.ClassType]] = {}
        self.class_map: Dict[str, T.ClassType] = {}
        # Constants per module (local name -> (definition, type)), spec 35.
        self._consts_by_module: Dict[Optional[str], Dict[str, Tuple[A.ConstDef, T.Type]]] = {}
        self.const_map: Dict[str, Tuple[A.ConstDef, T.Type]] = {}
        # id(expr) -> the type it takes from context: list_new() / [] (spec 22,
        # 29) and None / Some / Ok / Err (spec 30).
        self._type_hints: Dict[int, T.Type] = {}
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
        self.const_map = self._consts_by_module.setdefault(mod_name, {})
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
            raise CompileError(Diagnostic(
                self.filename, ty.line, ty.col, len(ty.name), f"unknown type: {ty.name}",
                help=did_you_mean(ty.name, [*_BUILTIN_TYPES, *self.type_alias_map, *self.class_map]),
            ))
        if isinstance(ty, A.TypeChannel):
            elem = self._resolve_type(ty.elem)
            if elem is T.VOID:
                raise CompileError(Diagnostic(self.filename, ty.line, ty.col, 7,
                                              "Channel element type cannot be void"))
            return T.ChannelType(elem)
        if isinstance(ty, A.TypeTuple):
            elems = [self._resolve_type(e) for e in ty.elems]
            if any(e is T.VOID for e in elems):
                raise CompileError(Diagnostic(self.filename, ty.line, ty.col, 5,
                                              "tuple elements cannot be void"))
            return T.TupleType(elems)
        if isinstance(ty, A.TypeDict):
            key, value = self._resolve_type(ty.key), self._resolve_type(ty.value)
            if key is not T.INT and key is not T.STRING:
                raise CompileError(Diagnostic(self.filename, ty.line, ty.col, 4,
                                              f"Dict keys must be int or string, found `{key}`"))
            if value is T.VOID:
                raise CompileError(Diagnostic(self.filename, ty.line, ty.col, 4,
                                              "Dict values cannot be void"))
            return T.DictType(key, value)
        if isinstance(ty, A.TypeOption):
            inner = self._resolve_type(ty.inner)
            if inner is T.VOID:
                raise CompileError(Diagnostic(self.filename, ty.line, ty.col, 6,
                                              "Option payload cannot be void"))
            return T.option_of(inner)
        if isinstance(ty, A.TypeResult):
            ok, err = self._resolve_type(ty.ok), self._resolve_type(ty.err)
            if ok is T.VOID or err is T.VOID:
                raise CompileError(Diagnostic(self.filename, ty.line, ty.col, 6,
                                              "Result payloads cannot be void"))
            return T.result_of(ok, err)
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
                for m in cdef.methods:
                    if cls.field_index(m) is not None:
                        raise CompileError(Diagnostic(
                            self.filename, cdef.line, cdef.col, len(cdef.name),
                            f"`{m}` is both a field and a method of {cdef.name}",
                        ))
                    cls.methods[m] = (mod_name, f"{cdef.name}.{m}")

        # Module constants (spec 35): types are known now; imported constants
        # are bound after every module's own constants are registered.
        for mod_name, mod in self._all_modules:
            self._enter_module(mod_name)
            for cdef in mod.constants:
                self._register_const(cdef, mod)
        for mod_name, mod in self._all_modules:
            self._enter_module(mod_name)
            self._bind_imported_consts(mod)

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
                    if p.default is not None:
                        lit = p.default.operand if isinstance(p.default, A.UnaryOp) else p.default
                        dt = {A.IntLit: T.INT, A.FloatLit: T.FLOAT, A.StringLit: T.STRING,
                              A.BoolLit: T.BOOL}[type(lit)]
                        if dt != pt:
                            raise CompileError(Diagnostic(
                                self.filename, p.line, p.col, len(p.name),
                                f"default value of `{p.name}` must be {pt}, found `{dt}`",
                            ))
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

    def _register_const(self, cdef: A.ConstDef, mod: A.Module) -> None:
        taken = (
            cdef.name in self.const_map or cdef.name in self.class_map
            or cdef.name in self.type_alias_map or cdef.name in _BUILTIN_FUNCS
            or any(fn.name == cdef.name for fn in mod.functions)
        )
        if taken:
            raise CompileError(Diagnostic(
                self.filename, cdef.line, cdef.col, len(cdef.name),
                f"`{cdef.name}` is already defined",
            ))
        declared = self._resolve_type(cdef.type_expr)
        v = cdef.value.operand if isinstance(cdef.value, A.UnaryOp) else cdef.value
        lit_ty = {A.IntLit: T.INT, A.FloatLit: T.FLOAT, A.StringLit: T.STRING,
                  A.BoolLit: T.BOOL}[type(v)]
        if declared != lit_ty:
            raise CompileError(Diagnostic(
                self.filename, cdef.line, cdef.col, len(cdef.name),
                f"type mismatch: declared `{declared}`, value has type `{lit_ty}`",
                help=conversion_hint(declared, lit_ty),
            ))
        self.result.expr_types[id(v)] = lit_ty
        self.result.expr_types[id(cdef.value)] = lit_ty
        self.const_map[cdef.name] = (cdef, declared)

    def _bind_imported_consts(self, mod: A.Module) -> None:
        """`from x import NAME` where NAME is a constant of x (spec 35)."""
        for imp in mod.imports:
            if imp.names is None:
                continue
            src = self._consts_by_module.get(imp.module, {})
            for src_name, alias in imp.names:
                if src_name in src:
                    local = alias or src_name
                    if local in self.const_map or local in self.class_map:
                        raise CompileError(Diagnostic(
                            self.filename, imp.line, imp.col, max(1, len(local)),
                            f"imported name `{local}` collides with a local definition",
                        ))
                    self.const_map[local] = src[src_name]

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
                if src_name in self._consts_by_module.get(imp.module, {}):
                    continue  # a constant import; bound by _bind_imported_consts
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
    def _reject_nested_defer(self, stmts: List[A.Stmt], top: bool) -> None:
        """`defer` is only allowed directly in a function body (spec 47)."""
        for s in stmts:
            if isinstance(s, A.Defer) and not top:
                raise CompileError(Diagnostic(self.filename, s.line, s.col, 5,
                                              "defer is only allowed at the top level of a function"))
            for attr in ("then_body", "else_body", "body", "some_block", "none_block",
                         "ok_block", "err_block", "update"):
                inner = getattr(s, attr, None)
                if isinstance(inner, list):
                    self._reject_nested_defer(inner, False)
            if isinstance(s, A.ValueMatch):
                for arm in s.arms:
                    self._reject_nested_defer(arm.body, False)

    # ---------- closures (spec 71) ----------
    def _note_use(self, name: str) -> None:
        """A read of local `name`: inside a closure, a name from the
        enclosing function becomes a capture."""
        if self._closure_stack and name in self._closure_stack[-1].candidates:
            self._closure_stack[-1].used.add(name)

    def _reject_captured_assign(self, stmt: A.Stmt) -> None:
        if not self._closure_stack:
            return
        names: List[str] = []
        if isinstance(stmt, (A.VarDecl, A.Assign)):
            names = [stmt.name]
        elif isinstance(stmt, A.TupleAssign):
            names = list(stmt.names)
        elif isinstance(stmt, A.ForEach):
            names = [stmt.var]
        for n in names:
            if n in self._closure_stack[-1].candidates:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, max(1, len(n)),
                    f"`{n}` belongs to the enclosing function: a closure captures it by value "
                    f"and cannot assign to it (spec 71)",
                    help="keep shared mutable state in a record field, or use a different name",
                ))

    def _check_closure(self, fn: A.FuncDef, node: object, label: str,
                       params: List[Tuple[str, T.Type]], ret: T.Type, body: List[A.Stmt],
                       outer: Dict[str, T.Type], self_name: Optional[str],
                       line: int, col: int) -> ClosureInfo:
        """Check the body of a nested def / lambda as a function of its own.
        Every local of the enclosing function visible here may be read; the
        ones that are become captures, copied when the closure is created."""
        self._closure_counter += 1
        qual = f"{fn.name}${label}${self._closure_counter}"
        key: FuncKey = (self.current_module, qual)
        synth = A.FuncDef(qual, [A.Param(n, A.TypeName("int", line, col), line, col) for n, _ in params],
                          A.TypeName("void", line, col), body, line, col)
        self.result.functions[key] = FuncSig(qual, params, ret, synth, self.current_module)
        ftype = T.FunctionType([t for _, t in params], ret)
        candidates = dict(outer)
        inner: Dict[str, T.Type] = dict(outer)
        if self_name is not None:
            candidates.pop(self_name, None)
            inner[self_name] = ftype   # the closure can call itself
        for pn, pt in params:
            candidates.pop(pn, None)
            inner[pn] = pt
            self.result.local_types[(self.current_module, qual, pn)] = pt
        ctx = _ClosureCtx(set(candidates), set())
        self._closure_stack.append(ctx)
        saved_loops = self._loop_depth
        self._loop_depth = 0   # break / continue do not cross into a closure
        try:
            self._reject_nested_defer(body, True)
            ended = self._check_block(synth, body, inner, ret)
        finally:
            self._closure_stack.pop()
            self._loop_depth = saved_loops
        if ret is not T.VOID and not ended:
            raise CompileError(Diagnostic(self.filename, line, col, max(1, len(label)),
                                          f"function `{label}` does not return on all paths"))
        captures = sorted((n, candidates[n]) for n in ctx.used)
        # A capture of the enclosing closure's own capture is a use there too.
        if self._closure_stack:
            for n, _ in captures:
                self._note_use(n)
        info = ClosureInfo(key, synth, captures, self_name, ftype)
        self.result.closures[id(node)] = info
        return info

    def _check_func(self, fn: A.FuncDef) -> None:
        self._reject_nested_defer(fn.body, True)
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
        self._reject_captured_assign(stmt)
        if isinstance(stmt, A.LocalFunc):
            inner = stmt.func
            if inner.name in locals_ or inner.name in self.const_map:
                raise CompileError(Diagnostic(
                    self.filename, inner.line, inner.col, 3,
                    f"`{inner.name}` is already defined in this function",
                ))
            params: List[Tuple[str, T.Type]] = []
            for p in inner.params:
                if any(p.name == n for n, _ in params):
                    raise CompileError(Diagnostic(self.filename, p.line, p.col, len(p.name),
                                                  f"duplicate parameter `{p.name}`"))
                params.append((p.name, self._resolve_type(p.type_expr)))
            ret = self._resolve_type(inner.return_type)
            info = self._check_closure(fn, stmt, inner.name, params, ret, inner.body, locals_,
                                       inner.name, inner.line, inner.col)
            locals_[inner.name] = info.type
            self.result.local_types[(self.current_module, fn.name, inner.name)] = info.type
            return False
        if isinstance(stmt, A.VarDecl):
            if stmt.name in locals_ and not (stmt.implicit and locals_[stmt.name] is T.INT):
                if stmt.implicit:
                    raise CompileError(Diagnostic(
                        self.filename, stmt.line, stmt.col, len(stmt.name),
                        f"loop variable `{stmt.name}` is already declared as "
                        f"`{locals_[stmt.name]}`, not int",
                    ))
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
                    help=conversion_hint(declared, val_ty),
                ))
            locals_[stmt.name] = declared
            self.result.local_types[(self.current_module, fn.name, stmt.name)] = declared
            self.result.decl_types[id(stmt)] = declared
            return False

        if isinstance(stmt, A.Assign):
            if stmt.name not in locals_ and stmt.name in self.const_map:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, len(stmt.name),
                    f"cannot assign to constant `{stmt.name}`",
                ))
            if stmt.name not in locals_:
                return self._declare_by_assignment(fn, stmt, locals_)
            expected = locals_[stmt.name]
            val_ty = self._check_expr_expect(fn, stmt.value, locals_, expected)
            if val_ty != expected:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, len(stmt.name),
                    f"type mismatch in assignment to `{stmt.name}`: "
                    f"expected `{expected}`, found `{val_ty}`",
                    help=conversion_hint(expected, val_ty),
                ))
            return False

        if isinstance(stmt, A.ExprStmt):
            self._check_expr(fn, stmt.expr, locals_)
            return False

        if isinstance(stmt, A.ValueMatch):
            return self._check_value_match(fn, stmt, locals_, ret_ty)

        if isinstance(stmt, A.Defer):
            self._check_stmt(fn, stmt.stmt, locals_, ret_ty)
            return False

        if isinstance(stmt, A.With):
            rt = self._check_expr(fn, stmt.expr, locals_)
            closable = rt is T.MUTEX or (
                isinstance(rt, T.ClassType) and "close" in rt.methods
                and len(self.result.functions[rt.methods["close"]].params) == 1
            )
            if not closable:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 4,
                    f"with needs a Mutex or a record with a `close(self)` method, found `{rt}`",
                ))
            self.result.with_types[id(stmt)] = rt
            body_locals = dict(locals_)
            if stmt.var is not None:
                if stmt.var in locals_:
                    raise CompileError(Diagnostic(self.filename, stmt.line, stmt.col, len(stmt.var),
                                                  f"variable `{stmt.var}` already declared"))
                body_locals[stmt.var] = rt
            return self._check_block(fn, stmt.body, body_locals, ret_ty)

        if isinstance(stmt, A.TupleAssign):
            vt = self._check_expr(fn, stmt.value, locals_)
            if not isinstance(vt, T.TupleType):
                raise CompileError(Diagnostic(self.filename, stmt.line, stmt.col, 1,
                                              f"cannot unpack `{vt}`"))
            if len(vt.elems) != len(stmt.names):
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 1,
                    f"cannot unpack a tuple of {len(vt.elems)} into {len(stmt.names)} names",
                ))
            for i, (name, et) in enumerate(zip(stmt.names, vt.elems, strict=True)):
                if name == "_":
                    continue
                if name in locals_:
                    if locals_[name] != et:
                        raise CompileError(Diagnostic(
                            self.filename, stmt.line, stmt.col, len(name),
                            f"type mismatch in assignment to `{name}`: "
                            f"expected `{locals_[name]}`, found `{et}`",
                        ))
                    continue
                if name in self.const_map:
                    raise CompileError(Diagnostic(self.filename, stmt.line, stmt.col, len(name),
                                                  f"cannot assign to constant `{name}`"))
                locals_[name] = et
                self.result.local_types[(self.current_module, fn.name, name)] = et
                self.result.unpack_decls[(id(stmt), i)] = et
            return False

        if isinstance(stmt, A.IndexAssign):
            obj_ty = self._check_expr(fn, stmt.obj, locals_)
            if isinstance(obj_ty, T.ListType) or obj_ty is T.STRING:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 1,
                    "lists and strings are immutable: cannot assign through an index",
                ))
            if not isinstance(obj_ty, T.DictType):
                raise CompileError(Diagnostic(self.filename, stmt.line, stmt.col, 1,
                                              f"cannot index `{obj_ty}`"))
            self._check_dict_key(fn, obj_ty, stmt.index, locals_, stmt.line, stmt.col)
            val_ty = self._check_expr_expect(fn, stmt.value, locals_, obj_ty.value)
            if val_ty != obj_ty.value:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 1,
                    f"cannot assign `{val_ty}` to an element of {obj_ty}",
                ))
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
                    help=conversion_hint(ret_ty, val_ty),
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
            self._check_block(fn, stmt.update, dict(locals_), ret_ty)
            return False  # while bodies don't guarantee return

        if isinstance(stmt, A.ForEach):
            it_ty = self._check_expr(fn, stmt.iterable, locals_)
            if isinstance(it_ty, T.ListType):
                elem: T.Type = it_ty.elem
            elif isinstance(it_ty, T.DictType):
                elem = it_ty.key  # iterates over the keys, in insertion order
            elif isinstance(it_ty, T.ChannelType):
                elem = it_ty.elem  # receives until the channel is closed
            elif it_ty is T.STRING:
                elem = T.STRING
            else:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 3,
                    f"cannot iterate over `{it_ty}`",
                ))
            if stmt.var in locals_ and locals_[stmt.var] != elem:
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 3,
                    f"loop variable `{stmt.var}` is already declared as "
                    f"`{locals_[stmt.var]}`, not `{elem}`",
                ))
            self.result.foreach_types[id(stmt)] = elem
            body_locals = dict(locals_)
            body_locals[stmt.var] = elem
            self._loop_depth += 1
            self._check_block(fn, stmt.body, body_locals, ret_ty)
            self._loop_depth -= 1
            return False

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
                if not isinstance(tt, T.OptionType):
                    raise CompileError(Diagnostic(
                        self.filename, stmt.line, stmt.col, 5,
                        f"match target must be an Option, found `{tt}`",
                    ))
                # Some arm: bind some_var to the payload in a new locals scope.
                some_locals = dict(locals_)
                some_locals[stmt.some_var] = tt.inner
                some_ret = self._check_block(fn, stmt.some_block, some_locals, ret_ty)
                # None arm: no binding.
                none_ret = self._check_block(fn, stmt.none_block, dict(locals_), ret_ty)
                # match terminates in return iff both arms do.
                return some_ret and none_ret
            # style == "result"
            if not isinstance(tt, T.ResultType):
                raise CompileError(Diagnostic(
                    self.filename, stmt.line, stmt.col, 5,
                    f"match target must be a Result, found `{tt}`",
                ))
            ok_locals = dict(locals_)
            ok_locals[stmt.ok_var] = tt.ok
            ok_ret = self._check_block(fn, stmt.ok_block, ok_locals, ret_ty)
            err_locals = dict(locals_)
            err_locals[stmt.err_var] = tt.err
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
        self._hint_expected(expr, expected, locals_)
        return self._check_expr(fn, expr, locals_)

    def _is_builtin_call(self, expr: A.Expr, name: str, locals_: Dict[str, T.Type]) -> bool:
        return (
            isinstance(expr, A.Call)
            and expr.module is None
            and expr.callee == name
            and name not in locals_
            and name not in self.class_map
        )

    def _hint_expected(self, expr: A.Expr, expected: T.Type, locals_: Dict[str, T.Type]) -> None:
        """Record the type an expression whose type is not fully determined
        by itself should take: `list_new()` / `[]` (spec 22, 29) and `None` /
        `Some` / `Ok` / `Err` (spec 30). Follows the list argument of nested
        `list_push` calls and both arms of a ternary."""
        if isinstance(expr, A.Lambda) and isinstance(expected, T.FunctionType):
            self._type_hints[id(expr)] = expected
        if isinstance(expected, T.ListType) and (
            self._is_builtin_call(expr, "list_new", locals_) or isinstance(expr, A.ListLit)
        ):
            self._type_hints[id(expr)] = expected
        elif isinstance(expected, T.ListType) and self._is_builtin_call(expr, "list_push", locals_):
            assert isinstance(expr, A.Call)
            if expr.args:
                self._hint_expected(expr.args[0], expected, locals_)
        elif isinstance(expected, T.DictType) and (
            self._is_builtin_call(expr, "dict_new", locals_) or isinstance(expr, A.DictLit)
        ):
            self._type_hints[id(expr)] = expected
        elif isinstance(expected, T.ChannelType) and self._is_builtin_call(expr, "chan_new", locals_):
            self._type_hints[id(expr)] = expected
        elif isinstance(expected, T.TupleType) and isinstance(expr, A.TupleLit):
            self._type_hints[id(expr)] = expected
        elif isinstance(expected, T.OptionType) and isinstance(expr, (A.NoneExpr, A.SomeExpr)):
            self._type_hints[id(expr)] = expected
        elif isinstance(expected, T.ResultType) and isinstance(expr, (A.OkExpr, A.ErrExpr)):
            self._type_hints[id(expr)] = expected
        elif isinstance(expr, A.IfExpr):
            self._hint_expected(expr.then, expected, locals_)
            self._hint_expected(expr.els, expected, locals_)

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
                self._note_use(expr.name)
                return locals_[expr.name]
            if expr.name in self.const_map:
                cdef, cty = self.const_map[expr.name]
                self.result.const_refs[id(expr)] = cdef
                return cty
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
                help=did_you_mean(expr.name, self._value_names(locals_)),
            ))
        if isinstance(expr, A.Lambda):
            ft = self._type_hints.get(id(expr))
            if not isinstance(ft, T.FunctionType):
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 6,
                    "cannot infer the parameter types of this lambda",
                    help="use it where a Callable type is expected: an argument, a variable "
                         "declared with a Callable type, or a return value",
                ))
            if len(expr.params) != len(ft.params):
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 6,
                    f"lambda takes {len(expr.params)} parameter(s) but `{ft}` has {len(ft.params)}",
                ))
            if len(set(expr.params)) != len(expr.params):
                raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 6,
                                              "duplicate lambda parameter"))
            body: List[A.Stmt] = (
                [A.ExprStmt(expr.body, expr.line, expr.col)] if ft.ret is T.VOID
                else [A.Return(expr.body, expr.line, expr.col)]
            )
            info = self._check_closure(fn, expr, "lambda", list(zip(expr.params, ft.params)), ft.ret,
                                       body, locals_, None, expr.line, expr.col)
            return info.type
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
            real_mod = self._import_env[expr.obj.name]
            qconst = self._consts_by_module.get(real_mod, {}).get(expr.field)
            if qconst is not None:  # `mod.NAME` (spec 35)
                self.result.const_refs[id(expr)] = qconst[0]
                return qconst[1]
            # `mod.f` used as a value (spec 21).
            key: FuncKey = (real_mod, expr.field)
            if key not in self.result.functions:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, len(expr.field),
                    f"module '{expr.obj.name}' has no function `{expr.field}`",
                    help=did_you_mean(expr.field, [*self._module_funcs(real_mod),
                                                   *self._consts_by_module.get(real_mod, {})]),
                ))
            self.result.func_refs[id(expr)] = key
            return self._func_value_type(key)
        if isinstance(expr, A.ListLit):
            lhint = self._type_hints.get(id(expr))
            hint = lhint if isinstance(lhint, T.ListType) else None
            if not expr.elems:
                return hint or T.LIST_INT
            elem_hint: Optional[T.Type] = hint.elem if hint else None
            first = (
                self._check_expr_expect(fn, expr.elems[0], locals_, elem_hint)
                if elem_hint else self._check_expr(fn, expr.elems[0], locals_)
            )
            if first is T.VOID:
                raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                              "list elements cannot be void"))
            for i, e in enumerate(expr.elems[1:], start=2):
                et = self._check_expr_expect(fn, e, locals_, first)
                if et != first:
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 1,
                        f"list element {i} must be {first}, found `{et}`",
                    ))
            return T.list_of(first)
        if isinstance(expr, A.TupleLit):
            thint = self._type_hints.get(id(expr))
            hints = thint.elems if isinstance(thint, T.TupleType) and len(thint.elems) == len(expr.elems) else None
            elems: List[T.Type] = []
            for i, e in enumerate(expr.elems):
                et = (self._check_expr_expect(fn, e, locals_, hints[i]) if hints
                      else self._check_expr(fn, e, locals_))
                if et is T.VOID:
                    raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                                  "tuple elements cannot be void"))
                elems.append(et)
            return T.TupleType(elems)
        if isinstance(expr, A.DictLit):
            hint = self._type_hints.get(id(expr))
            dhint = hint if isinstance(hint, T.DictType) else None
            if not expr.keys:
                if dhint is None:
                    raise CompileError(Diagnostic(
                        self.filename, expr.line, expr.col, 2,
                        "cannot infer the type of {}; give the variable a Dict type",
                    ))
                return dhint
            kt = self._check_expr(fn, expr.keys[0], locals_)
            if kt is not T.INT and kt is not T.STRING:
                raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                              f"Dict keys must be int or string, found `{kt}`"))
            vt = (self._check_expr_expect(fn, expr.values[0], locals_, dhint.value) if dhint
                  else self._check_expr(fn, expr.values[0], locals_))
            if vt is T.VOID:
                raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                              "Dict values cannot be void"))
            for i, (k, v) in enumerate(zip(expr.keys[1:], expr.values[1:], strict=True), start=2):
                if self._check_expr(fn, k, locals_) != kt:
                    raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                                  f"dict key {i} must be {kt}"))
                if self._check_expr_expect(fn, v, locals_, vt) != vt:
                    raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                                  f"dict value {i} must be {vt}"))
            return T.DictType(kt, vt)
        if isinstance(expr, A.Index):
            obj_ty = self._check_expr(fn, expr.obj, locals_)
            if isinstance(obj_ty, T.TupleType):
                ix = expr.index
                neg = isinstance(ix, A.UnaryOp) and ix.op == "-" and isinstance(ix.operand, A.IntLit)
                if not (isinstance(ix, A.IntLit) or neg):
                    raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                                  "tuple index must be an integer literal"))
                raw = -ix.operand.value if neg else ix.value  # type: ignore[union-attr]
                n = len(obj_ty.elems)
                pos = raw + n if raw < 0 else raw
                if not 0 <= pos < n:
                    raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                                  f"tuple index {raw} out of range for {obj_ty}"))
                self.result.tuple_index[id(expr)] = pos
                return obj_ty.elems[pos]
            if isinstance(obj_ty, T.DictType):
                self._check_dict_key(fn, obj_ty, expr.index, locals_, expr.line, expr.col)
                return obj_ty.value
            if not (isinstance(obj_ty, T.ListType) or obj_ty is T.STRING):
                raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                              f"cannot index `{obj_ty}`"))
            if self._check_expr(fn, expr.index, locals_) is not T.INT:
                raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                              "index must be int"))
            return obj_ty.elem if isinstance(obj_ty, T.ListType) else T.STRING
        if isinstance(expr, A.Slice):
            obj_ty = self._check_expr(fn, expr.obj, locals_)
            if not (isinstance(obj_ty, T.ListType) or obj_ty is T.STRING):
                raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                              f"cannot slice `{obj_ty}`"))
            for bound in (expr.start, expr.stop):
                if bound is not None and self._check_expr(fn, bound, locals_) is not T.INT:
                    raise CompileError(Diagnostic(self.filename, expr.line, expr.col, 1,
                                                  "slice bounds must be int"))
            return obj_ty
        if isinstance(expr, A.CallValue) and isinstance(expr.callee, A.FieldAccess):
            # `e.m(...)`: a method call if e's class has a method m (spec 33).
            recv = expr.callee
            recv_ty = self._check_expr(fn, recv.obj, locals_)
            if isinstance(recv_ty, T.ClassType) and recv.field in recv_ty.methods:
                return self._check_method_call(fn, id(expr), recv_ty, recv.field, expr.args,
                                               expr.line, expr.col, locals_)
            if isinstance(recv_ty, T.ClassType) and recv_ty.field_index(recv.field) is None:
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, len(recv.field),
                    f"{recv_ty} has no field or method `{recv.field}`",
                    help=did_you_mean(recv.field, self._members(recv_ty)),
                ))
            _, fty = self._field_of(recv_ty, recv.field, recv.line, recv.col)
            self.result.expr_types[id(recv)] = fty
            return self._check_fn_value_call(fn, fty, expr.args, "function value",
                                             expr.line, expr.col, locals_)
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
            # The fiber runtime returns results through rw_spawn_<T> for
            # these types only.
            if ret_ty not in (T.INT, T.FLOAT, T.BOOL, T.STRING, T.BYTES, T.VOID):
                raise CompileError(Diagnostic(
                    self.filename, expr.line, expr.col, 5,
                    f"cannot spawn a function returning `{ret_ty}`",
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
        if isinstance(expr, (A.SomeExpr, A.NoneExpr, A.OkExpr, A.ErrExpr)):
            return self._check_variant(fn, expr, locals_)
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
                    help=did_you_mean(call.callee, self._module_funcs(real_module)),
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
                    help=did_you_mean(call.callee, [*self._module_funcs(self.current_module),
                                                    *self._from_env, *_BUILTIN_FUNCS,
                                                    *self.class_map]),
                ))
        self.result.call_resolution[id(call)] = key
        return key

    def _bind_args(self, args: List[A.Expr], params: List[A.Param], what: str,
                   line: int, col: int) -> List[A.Expr]:
        """Resolve keyword arguments and defaults into one positional list in
        parameter order (spec 41). Too many positional arguments are left for
        the caller's arity check."""
        positional = [a for a in args if not isinstance(a, A.KeywordArg)]
        keywords = [a for a in args if isinstance(a, A.KeywordArg)]
        if not keywords and all(p.default is None for p in params[len(positional):]):
            return positional
        if len(positional) > len(params):
            return positional
        slots: List[Optional[A.Expr]] = list(positional) + [None] * (len(params) - len(positional))
        names = [p.name for p in params]
        for kw in keywords:
            if kw.name not in names:
                raise CompileError(Diagnostic(self.filename, kw.line, kw.col, len(kw.name),
                                              f"`{what}` got an unexpected keyword argument `{kw.name}`"))
            i = names.index(kw.name)
            if slots[i] is not None:
                raise CompileError(Diagnostic(self.filename, kw.line, kw.col, len(kw.name),
                                              f"`{what}` got multiple values for argument `{kw.name}`"))
            slots[i] = kw.value
        out: List[A.Expr] = []
        for p, a in zip(params, slots, strict=True):
            if a is None:
                if p.default is None:
                    raise CompileError(Diagnostic(self.filename, line, col, len(what),
                                                  f"`{what}` missing argument `{p.name}`"))
                a = p.default
            out.append(a)
        return out

    def _reject_keywords(self, args: List[A.Expr], what: str) -> None:
        for a in args:
            if isinstance(a, A.KeywordArg):
                raise CompileError(Diagnostic(
                    self.filename, a.line, a.col, len(a.name),
                    f"`{what}` does not take keyword arguments",
                ))

    def _check_call(self, fn: A.FuncDef, call: A.Call, locals_: Dict[str, T.Type]) -> T.Type:
        # Calls through function values (spec 21) come first: locals shadow
        # functions, builtins, and import qualifiers.
        if call.module is not None and call.module in locals_:
            self._note_use(call.module)
            recv_ty = locals_[call.module]
            if isinstance(recv_ty, T.ClassType) and call.callee in recv_ty.methods:
                # `x.m(...)`: a method call (spec 33).
                return self._check_method_call(fn, id(call), recv_ty, call.callee, call.args,
                                               call.line, call.col, locals_)
            if isinstance(recv_ty, T.ClassType) and recv_ty.field_index(call.callee) is None:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"{recv_ty} has no field or method `{call.callee}`",
                    help=did_you_mean(call.callee, self._members(recv_ty)),
                ))
            # `r.f(...)`: call the function stored in field f of record r.
            cls, fty = self._field_of(recv_ty, call.callee, call.line, call.col)
            self.result.field_fn_calls[id(call)] = cls
            return self._check_fn_value_call(fn, fty, call.args, call.callee,
                                             call.line, call.col, locals_)
        if call.module is None and isinstance(locals_.get(call.callee), T.FunctionType):
            self._note_use(call.callee)
            self.result.local_fn_calls.add(id(call))
            self.result.local_fn_call_types[id(call)] = locals_[call.callee]
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
        if call.callee in _BUILTIN_FUNCS:
            self._reject_keywords(call.args, call.callee)
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
        # Builtin: assert_eq(a, b) for int / float / bool / string (spec 68).
        if call.callee == "assert_eq":
            if len(call.args) != 2:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"assert_eq takes 2 arguments, got {len(call.args)}",
                ))
            lt = self._check_expr(fn, call.args[0], locals_)
            rt = self._check_expr_expect(fn, call.args[1], locals_, lt)
            if lt not in (T.INT, T.FLOAT, T.BOOL, T.STRING):
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"assert_eq compares int, float, bool or string values, found `{lt}`",
                ))
            if rt != lt:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"assert_eq arguments differ in type: `{lt}` and `{rt}`",
                    help=conversion_hint(lt, rt),
                ))
            return T.VOID
        if call.callee == "len":
            if len(call.args) != 1:
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len takes exactly 1 argument, got {len(call.args)}",
                ))
            at = self._check_expr(fn, call.args[0], locals_)
            if at is not T.STRING and at is not T.BYTES and not isinstance(
                at, (T.ListType, T.DictType, T.ChannelType)
            ):
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, 3,
                    f"len argument must be string, Bytes, a List or a Dict, found `{at}`",
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
        if call.callee in _DICT_BUILTINS:
            return self._check_dict_builtin(fn, call, locals_)
        if call.callee in ("chan_new", "chan_send", "chan_recv", "chan_close"):
            return self._check_chan_builtin(fn, call, locals_)
        if call.callee in ("await_all", "await_any"):
            if len(call.args) != 1:
                raise CompileError(Diagnostic(self.filename, call.line, call.col, len(call.callee),
                                              f"{call.callee} takes 1 argument, got {len(call.args)}"))
            lt = self._check_expr(fn, call.args[0], locals_)
            if not (isinstance(lt, T.ListType) and isinstance(lt.elem, T.FutureType)
                    and lt.elem.inner is not T.VOID):
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(call.callee),
                    f"{call.callee} argument must be a List of Futures with a value, found `{lt}`",
                ))
            inner = lt.elem.inner
            return T.list_of(inner) if call.callee == "await_all" else T.TupleType([T.INT, inner])
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
                at = self._check_expr_expect(fn, arg, locals_, want)
                if at != want:
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
            hint = self._type_hints.get(id(call))
            return hint if isinstance(hint, T.ListType) else T.LIST_INT
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
        """`ClassName(args...)`: one positional argument per field (spec 20);
        fields may also be passed by keyword (spec 41)."""
        field_params = [A.Param(fname, A.TypeName("int", 0, 0), call.line, call.col)
                        for fname, _ in cls.fields]
        call.args = self._bind_args(call.args, field_params, str(cls), call.line, call.col)
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

    def _value_names(self, locals_: Dict[str, T.Type]) -> List[str]:
        """Names a bare identifier could refer to here (for did-you-mean)."""
        return [*locals_, *self.const_map, *self._from_env,
                *self._module_funcs(self.current_module)]

    def _module_funcs(self, module: str) -> List[str]:
        return [name for (mod, name) in self.result.functions
                if mod == module and "." not in name and "$" not in name]

    @staticmethod
    def _members(cls: T.ClassType) -> List[str]:
        return [*(f for f, _ in cls.fields), *cls.methods]

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
                help=did_you_mean(field, [f for f, _ in obj_ty.fields]),
            ))
        return obj_ty, obj_ty.fields[idx][1]

    def _check_value_match(self, fn: A.FuncDef, stmt: A.ValueMatch,
                           locals_: Dict[str, T.Type], ret_ty: T.Type) -> bool:
        """match on an int / string / bool with literal, `_`, and binding
        patterns (spec 38). Returns whether every path returns."""
        tt = self._check_expr(fn, stmt.target, locals_)
        if tt not in (T.INT, T.STRING, T.BOOL):
            raise CompileError(Diagnostic(
                self.filename, stmt.line, stmt.col, 5,
                f"value patterns can only match int, string, or bool, found `{tt}`",
            ))
        seen: set = set()
        catch_all = False
        all_return = True
        for arm in stmt.arms:
            if catch_all:
                raise CompileError(Diagnostic(self.filename, arm.line, arm.col, 4,
                                              "unreachable case after a catch-all pattern"))
            for p in arm.patterns:
                pt = self._check_expr(fn, p, locals_)
                if pt != tt:
                    raise CompileError(Diagnostic(
                        self.filename, arm.line, arm.col, 4,
                        f"pattern of type `{pt}` cannot match a `{tt}`",
                    ))
                key = p.value  # type: ignore[attr-defined]
                if key in seen:
                    raise CompileError(Diagnostic(self.filename, arm.line, arm.col, 4,
                                                  f"duplicate pattern {key!r}"))
                seen.add(key)
            arm_locals = dict(locals_)
            if not arm.patterns:
                catch_all = True
                if arm.bind is not None:
                    if arm.bind in locals_:
                        raise CompileError(Diagnostic(
                            self.filename, arm.line, arm.col, len(arm.bind),
                            f"variable `{arm.bind}` already declared",
                        ))
                    arm_locals[arm.bind] = tt
            all_return = self._check_block(fn, arm.body, arm_locals, ret_ty) and all_return
        exhaustive = catch_all or (tt is T.BOOL and seen == {True, False})
        return exhaustive and all_return

    def _declare_by_assignment(self, fn: A.FuncDef, stmt: A.Assign,
                               locals_: Dict[str, T.Type]) -> bool:
        """`x = e` with `x` not in scope declares `x` with e's type (spec 32)."""
        value = stmt.value
        # A compound assignment (`x += 1`) desugars to `x = x + 1`; it never
        # declares.
        if isinstance(value, A.BinOp) and isinstance(value.left, A.Name) and value.left.name == stmt.name:
            raise CompileError(Diagnostic(
                self.filename, stmt.line, stmt.col, len(stmt.name),
                f"undefined variable: {stmt.name}",
                help=did_you_mean(stmt.name, self._value_names(locals_)),
            ))
        untyped = (
            isinstance(value, (A.NoneExpr, A.OkExpr, A.ErrExpr))
            or (isinstance(value, A.ListLit) and not value.elems)
            or self._is_builtin_call(value, "list_new", locals_)
            or self._is_builtin_call(value, "dict_new", locals_)
            or (isinstance(value, A.DictLit) and not value.keys)
        )
        if untyped:
            raise CompileError(Diagnostic(
                self.filename, stmt.line, stmt.col, len(stmt.name),
                f"cannot infer the type of `{stmt.name}`; declare it as `{stmt.name}: T = ...`",
            ))
        ty = self._check_expr(fn, value, locals_)
        if ty is T.VOID:
            raise CompileError(Diagnostic(
                self.filename, stmt.line, stmt.col, len(stmt.name),
                f"cannot assign a void value to `{stmt.name}`",
            ))
        locals_[stmt.name] = ty
        self.result.local_types[(self.current_module, fn.name, stmt.name)] = ty
        self.result.decl_types[id(stmt)] = ty
        return False

    def _check_dict_builtin(self, fn: A.FuncDef, call: A.Call, locals_: Dict[str, T.Type]) -> T.Type:
        """dict_new / dict_get / dict_has / dict_remove / dict_keys / dict_values
        (spec 31)."""
        name = call.callee
        arity = {"dict_new": 0, "dict_keys": 1, "dict_values": 1}.get(name, 2)
        if len(call.args) != arity:
            raise CompileError(Diagnostic(
                self.filename, call.line, call.col, len(name),
                f"{name} takes {arity} argument{'s' if arity != 1 else ''}, got {len(call.args)}",
            ))
        if name == "dict_new":
            hint = self._type_hints.get(id(call))
            if not isinstance(hint, T.DictType):
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(name),
                    "cannot infer the type of dict_new(); give the variable a Dict type",
                ))
            return hint
        dt = self._check_expr(fn, call.args[0], locals_)
        if not isinstance(dt, T.DictType):
            raise CompileError(Diagnostic(
                self.filename, call.line, call.col, len(name),
                f"{name} argument 1 must be a Dict, found `{dt}`",
            ))
        if name == "dict_keys":
            return T.list_of(dt.key)
        if name == "dict_values":
            return T.list_of(dt.value)
        kt = self._check_expr(fn, call.args[1], locals_)
        if kt != dt.key:
            raise CompileError(Diagnostic(
                self.filename, call.line, call.col, len(name),
                f"{name} argument 2 must be {dt.key}, found `{kt}`",
            ))
        return T.option_of(dt.value) if name == "dict_get" else T.BOOL

    def _check_chan_builtin(self, fn: A.FuncDef, call: A.Call, locals_: Dict[str, T.Type]) -> T.Type:
        """chan_new / chan_send / chan_recv / chan_close (spec 44)."""
        name = call.callee
        arity = {"chan_new": 1, "chan_send": 2, "chan_recv": 1, "chan_close": 1}[name]
        if len(call.args) != arity:
            raise CompileError(Diagnostic(
                self.filename, call.line, call.col, len(name),
                f"{name} takes {arity} argument{'s' if arity != 1 else ''}, got {len(call.args)}",
            ))
        if name == "chan_new":
            if self._check_expr(fn, call.args[0], locals_) is not T.INT:
                raise CompileError(Diagnostic(self.filename, call.line, call.col, len(name),
                                              "chan_new capacity must be int"))
            hint = self._type_hints.get(id(call))
            if not isinstance(hint, T.ChannelType):
                raise CompileError(Diagnostic(
                    self.filename, call.line, call.col, len(name),
                    "cannot infer the type of chan_new(); give the variable a Channel type",
                ))
            return hint
        ct = self._check_expr(fn, call.args[0], locals_)
        if not isinstance(ct, T.ChannelType):
            raise CompileError(Diagnostic(self.filename, call.line, call.col, len(name),
                                          f"{name} argument 1 must be a Channel, found `{ct}`"))
        if name == "chan_send":
            vt = self._check_expr_expect(fn, call.args[1], locals_, ct.elem)
            if vt != ct.elem:
                raise CompileError(Diagnostic(self.filename, call.line, call.col, len(name),
                                              f"chan_send argument 2 must be {ct.elem}, found `{vt}`"))
            return T.VOID
        if name == "chan_recv":
            return T.option_of(ct.elem)
        return T.VOID

    def _check_dict_key(self, fn: A.FuncDef, dt: T.DictType, key: A.Expr,
                        locals_: Dict[str, T.Type], line: int, col: int) -> None:
        kt = self._check_expr(fn, key, locals_)
        if kt != dt.key:
            raise CompileError(Diagnostic(self.filename, line, col, 1,
                                          f"{dt} key must be {dt.key}, found `{kt}`"))

    def _check_variant(self, fn: A.FuncDef, expr: A.Expr, locals_: Dict[str, T.Type]) -> T.Type:
        """`Some(x)` / `None` / `Ok(x)` / `Err(e)` (spec 30). The expected type,
        if any, fixes the payload types; otherwise the argument does, and an
        undetermined part defaults to int."""
        hint = self._type_hints.get(id(expr))
        if isinstance(expr, A.NoneExpr):
            return hint if isinstance(hint, T.OptionType) else T.OPTION_INT
        assert isinstance(expr, (A.SomeExpr, A.OkExpr, A.ErrExpr))
        if isinstance(expr, A.SomeExpr):
            name, want = "Some", hint.inner if isinstance(hint, T.OptionType) else None
        elif isinstance(expr, A.OkExpr):
            name, want = "Ok", hint.ok if isinstance(hint, T.ResultType) else None
        else:
            name, want = "Err", hint.err if isinstance(hint, T.ResultType) else None
        at = (self._check_expr_expect(fn, expr.arg, locals_, want) if want is not None
              else self._check_expr(fn, expr.arg, locals_))
        if at is T.VOID:
            raise CompileError(Diagnostic(self.filename, expr.line, expr.col, len(name),
                                          f"{name} argument cannot be void"))
        if want is not None and at != want:
            raise CompileError(Diagnostic(
                self.filename, expr.line, expr.col, len(name),
                f"{name} argument must be {want}, found `{at}`",
            ))
        if isinstance(expr, A.SomeExpr):
            return T.option_of(at)
        if isinstance(hint, T.ResultType):
            return hint
        return T.result_of(at, T.INT) if isinstance(expr, A.OkExpr) else T.result_of(T.INT, at)

    def _check_method_call(self, fn: A.FuncDef, call_id: int, cls: T.ClassType, name: str,
                           args: List[A.Expr], line: int, col: int,
                           locals_: Dict[str, T.Type]) -> T.Type:
        """Check `recv.name(args)` against the method's parameters after
        `self` (spec 33)."""
        key = cls.methods[name]
        sig = self.result.functions[key]
        self.result.method_calls[call_id] = key
        args[:] = self._bind_args(args, sig.node.params[1:], name, line, col)
        ftype = T.FunctionType([pt for _, pt in sig.params[1:]], sig.return_type)
        return self._check_fn_value_call(fn, ftype, args, name, line, col, locals_)

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
        self._reject_keywords(args, what)
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
                    help=conversion_hint(pty, at),
                ))
        return callee_ty.ret

    def _check_user_call(self, fn: A.FuncDef, call: A.Call, locals_: Dict[str, T.Type]) -> T.Type:
        """Resolve and type-check a call to a user-defined function (qualified
        or not). Builtins must already have been dispatched."""
        key = self._resolve_user_func(call)
        sig = self.result.functions[key]
        call.args = self._bind_args(call.args, sig.node.params, call.callee, call.line, call.col)
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
                    help=conversion_hint(pty, at),
                ))
        return sig.return_type


def analyze(module: A.Module, filename: str = "<input>") -> SemaResult:
    """Analyze a single module (no imports). Convenience wrapper used by tests."""
    program = LoadedProgram(root=module, root_name="<root>")
    return Sema(program, filename).analyze()


def analyze_program(program: LoadedProgram, filename: str = "<input>") -> SemaResult:
    return Sema(program, filename).analyze()
