"""LLVM IR generation using llvmlite.ir.

Generates a module for a checked rw program. User function `main` is renamed
to `@rw_user_main` and a real C `@main` is synthesized that calls
`rw_init` / `rw_user_main` / `rw_shutdown` and returns the i32-truncated result.

This file covers the *synchronous* subset (functions, literals, binops,
if/while, print, calls). spawn/await are handled in the same file but
delegated to a dedicated routine in Step 8.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from llvmlite import ir

from . import ast_nodes as A
from . import types as T
from .loader import LoadedProgram
from .sema import FuncKey, SemaResult


# ---------- LLVM type helpers ----------

I8 = ir.IntType(8)
I32 = ir.IntType(32)
I64 = ir.IntType(64)
F64 = ir.DoubleType()
I8P = I8.as_pointer()
RW_STR_TY = ir.LiteralStructType([I64, I8P])  # { i64 len, i8* ptr }
RW_LIST_INT_TY = ir.LiteralStructType([I64, I64, I64.as_pointer()])  # {len, cap, data*}
RW_OPTION_INT_TY = ir.LiteralStructType([I64, I64])  # {tag, payload}


def llvm_type_of(t: T.Type) -> ir.Type:
    if t is T.INT:
        return I64
    if t is T.FLOAT:
        return F64
    if t is T.BOOL:
        return I8  # ABI: pass bool as i8
    if t is T.STRING:
        return RW_STR_TY
    if t is T.BYTES:
        return RW_STR_TY
    if isinstance(t, T.ListType):
        # Every List[T] has the same {len, cap, data*} layout (spec 22).
        return RW_LIST_INT_TY
    if isinstance(t, T.OptionType):
        # {tag, value}; identical to RW_OPTION_INT_TY for Option[int] (spec 30).
        return ir.LiteralStructType([I64, llvm_type_of(t.inner)])
    if isinstance(t, T.ResultType):
        # {tag, ok, err} (spec 30).
        return ir.LiteralStructType([I64, llvm_type_of(t.ok), llvm_type_of(t.err)])
    if t is T.VOID:
        return ir.VoidType()
    if isinstance(t, T.FutureType):
        return I8P  # opaque pointer
    if isinstance(t, T.ClassType):
        return I8P  # pointer to a heap record (spec 20)
    if isinstance(t, T.DictType):
        return I8P  # pointer to a heap hash table (spec 31)
    if isinstance(t, T.ChannelType):
        return I8P  # pointer to a runtime channel (spec 44)
    if t is T.MUTEX:
        return I8P  # pointer to a runtime mutex (spec 46)
    if isinstance(t, T.TupleType):
        return ir.LiteralStructType([llvm_type_of(e) for e in t.elems])  # by value (spec 37)
    if isinstance(t, T.FunctionType):
        # Pointer to the function's own LLVM type (spec 21).
        return ir.FunctionType(llvm_type_of(t.ret), [llvm_type_of(p) for p in t.params]).as_pointer()
    raise RuntimeError(f"no LLVM mapping for type {t}")


def record_struct_type(cls: T.ClassType) -> ir.LiteralStructType:
    """In-memory layout of a record: its fields in declaration order. Record
    fields are themselves i8*, so a layout never contains another by value."""
    return ir.LiteralStructType([llvm_type_of(fty) for _, fty in cls.fields])


# ---------- IR generator ----------

class IRGen:
    def __init__(self, program: "LoadedProgram", sema: SemaResult, checked: bool = True) -> None:
        self.program = program
        self.sema = sema
        # Run-time checks for integer division (spec 42); `--unchecked` turns
        # them off.
        self.checked = checked
        self.mod = ir.Module(name=sema.filename)
        # Use a generic target triple; the driver picks the host triple at codegen.
        self.mod.triple = ""
        self._string_const_counter = 0
        self._trampoline_counter = 0
        # Map (module, name) -> ir.Function (LLVM symbol is renamed per module).
        self.funcs: Dict[FuncKey, ir.Function] = {}
        # All (module, A.Module) pairs: entry first (module=None), then imports.
        self._all_modules: List[Tuple[Optional[str], A.Module]] = [
            (None, program.root)
        ] + [(name, mod) for name, mod in program.modules.items()]
        self._declare_runtime()

    @staticmethod
    def _symbol(key: FuncKey) -> str:
        """LLVM symbol for a user function. Entry-module functions keep the
        historical `rw_user_<name>`; imported functions are qualified as
        `rw_user_<module>.<name>`. `.` cannot appear in an rw identifier, so
        the qualified form never collides with an entry function (`lib_add`)
        or another module (`a_b.c` vs `a.b_c`)."""
        module, name = key
        if module is None:
            return f"rw_user_{name}"
        return f"rw_user_{module}.{name}"

    # ---------- runtime declarations ----------
    def _declare_runtime(self) -> None:
        m = self.mod
        # print
        self._rw_print_i64 = ir.Function(m, ir.FunctionType(ir.VoidType(), [I64]), "rw_print_i64")
        self._rw_print_f64 = ir.Function(m, ir.FunctionType(ir.VoidType(), [F64]), "rw_print_f64")
        self._rw_print_bool = ir.Function(m, ir.FunctionType(ir.VoidType(), [I8]), "rw_print_bool")
        self._rw_print_str = ir.Function(m, ir.FunctionType(ir.VoidType(), [RW_STR_TY]), "rw_print_str")
        # assertion failure
        self._rw_panic = ir.Function(m, ir.FunctionType(ir.VoidType(), [RW_STR_TY]), "rw_panic")
        # string ops
        self._rw_str_len = ir.Function(
            m, ir.FunctionType(I64, [RW_STR_TY]), "rw_str_len")
        self._rw_str_eq = ir.Function(
            m, ir.FunctionType(I8, [RW_STR_TY, RW_STR_TY]), "rw_str_eq")
        self._rw_str_concat = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [RW_STR_TY, RW_STR_TY]), "rw_str_concat")
        # string search / conversion (spec 18)
        self._rw_str_find = ir.Function(
            m, ir.FunctionType(I64, [RW_STR_TY, RW_STR_TY]), "rw_str_find")
        self._rw_str_slice = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [RW_STR_TY, I64, I64]), "rw_str_slice")
        self._rw_str_starts_with = ir.Function(
            m, ir.FunctionType(I8, [RW_STR_TY, RW_STR_TY]), "rw_str_starts_with")
        self._rw_str_from_int = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [I64]), "rw_str_from_int")
        # conversions and string ordering (spec 26)
        self._rw_str_from_float = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [F64]), "rw_str_from_float")
        self._rw_str_cmp = ir.Function(
            m, ir.FunctionType(I64, [RW_STR_TY, RW_STR_TY]), "rw_str_cmp")
        # timers and clocks (spec 39)
        self._rw_sleep_ms = ir.Function(m, ir.FunctionType(ir.VoidType(), [I64]), "rw_sleep_ms")
        self._rw_time_ms = ir.Function(m, ir.FunctionType(I64, []), "rw_time_ms")
        self._rw_mono_ns = ir.Function(m, ir.FunctionType(I64, []), "rw_mono_ns")
        # channels (spec 44)
        self._rw_chan_new = ir.Function(m, ir.FunctionType(I8P, [I64, I64]), "rw_chan_new")
        self._rw_chan_send = ir.Function(m, ir.FunctionType(ir.VoidType(), [I8P, I8P]), "rw_chan_send")
        self._rw_chan_recv = ir.Function(m, ir.FunctionType(I8, [I8P, I8P]), "rw_chan_recv")
        self._rw_chan_close = ir.Function(m, ir.FunctionType(ir.VoidType(), [I8P]), "rw_chan_close")
        self._rw_chan_len = ir.Function(m, ir.FunctionType(I64, [I8P]), "rw_chan_len")
        # Mutex (spec 46)
        self._rw_mutex_new = ir.Function(m, ir.FunctionType(I8P, []), "rw_mutex_new")
        self._rw_mutex_lock = ir.Function(m, ir.FunctionType(ir.VoidType(), [I8P]), "rw_mutex_lock")
        self._rw_mutex_unlock = ir.Function(m, ir.FunctionType(ir.VoidType(), [I8P]), "rw_mutex_unlock")
        self._rw_mutex_try_lock = ir.Function(m, ir.FunctionType(I8, [I8P]), "rw_mutex_try_lock")
        # await_any (spec 45)
        self._rw_await_any = ir.Function(
            m, ir.FunctionType(I64, [RW_LIST_INT_TY.as_pointer()]), "rw_await_any")
        # random numbers (spec 43)
        self._rw_rand_int = ir.Function(m, ir.FunctionType(I64, [I64, I64]), "rw_rand_int")
        self._rw_rand_float = ir.Function(m, ir.FunctionType(F64, []), "rw_rand_float")
        self._rw_rand_seed = ir.Function(m, ir.FunctionType(ir.VoidType(), [I64]), "rw_rand_seed")
        self._rw_rand_bytes = ir.Function(m, ir.FunctionType(RW_STR_TY, [I64]), "rw_rand_bytes")
        # OS API (spec 40)
        self._rw_set_args = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [I32, I8P.as_pointer()]), "rw_set_args")
        self._rw_os_args = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [RW_LIST_INT_TY.as_pointer()]), "rw_os_args")
        self._opt_str_ty = llvm_type_of(T.option_of(T.STRING))
        self._rw_os_getenv = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [self._opt_str_ty.as_pointer(), RW_STR_TY]),
            "rw_os_getenv")
        self._rw_os_setenv = ir.Function(
            m, ir.FunctionType(I8, [RW_STR_TY, RW_STR_TY]), "rw_os_setenv")
        self._rw_os_exit = ir.Function(m, ir.FunctionType(ir.VoidType(), [I64]), "rw_os_exit")
        self._rw_os_getcwd = ir.Function(m, ir.FunctionType(RW_STR_TY, []), "rw_os_getcwd")
        # address-based I/O entry points (spec 50)
        self._rw_dial = ir.Function(m, ir.FunctionType(I64, [RW_STR_TY]), "rw_dial")
        self._rw_listen = ir.Function(m, ir.FunctionType(I64, [RW_STR_TY]), "rw_listen")
        self._rw_accept = ir.Function(m, ir.FunctionType(I64, [I64]), "rw_accept")
        self._rw_open = ir.Function(m, ir.FunctionType(I64, [RW_STR_TY, RW_STR_TY]), "rw_open")
        self._rw_local_addr = ir.Function(m, ir.FunctionType(RW_STR_TY, [I64]), "rw_local_addr")
        self._rw_peer_addr = ir.Function(m, ir.FunctionType(RW_STR_TY, [I64]), "rw_peer_addr")
        self._rw_write_all = ir.Function(m, ir.FunctionType(I64, [I64, RW_STR_TY]), "rw_write_all")
        # timeouts (spec 53)
        self._rw_dial_timeout = ir.Function(m, ir.FunctionType(I64, [RW_STR_TY, I64]), "rw_dial_timeout")
        self._rw_set_read_timeout = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [I64, I64]), "rw_set_read_timeout")
        self._rw_set_write_timeout = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [I64, I64]), "rw_set_write_timeout")
        # I/O errors (spec 51)
        self._rw_last_error = ir.Function(m, ir.FunctionType(I64, []), "rw_last_error")
        self._rw_error_name = ir.Function(m, ir.FunctionType(RW_STR_TY, [I64]), "rw_error_name")
        self._rw_error_message = ir.Function(m, ir.FunctionType(RW_STR_TY, [I64]), "rw_error_message")
        self._rw_os_chdir = ir.Function(m, ir.FunctionType(I8, [RW_STR_TY]), "rw_os_chdir")
        # string builtins (spec 28); split/join take the list by pointer.
        s2s = ir.FunctionType(RW_STR_TY, [RW_STR_TY])
        self._rw_str_strip = ir.Function(m, s2s, "rw_str_strip")
        self._rw_str_lower = ir.Function(m, s2s, "rw_str_lower")
        self._rw_str_upper = ir.Function(m, s2s, "rw_str_upper")
        self._rw_str_replace = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [RW_STR_TY, RW_STR_TY, RW_STR_TY]), "rw_str_replace")
        self._rw_str_contains = ir.Function(
            m, ir.FunctionType(I8, [RW_STR_TY, RW_STR_TY]), "rw_str_contains")
        self._rw_str_ends_with = ir.Function(
            m, ir.FunctionType(I8, [RW_STR_TY, RW_STR_TY]), "rw_str_ends_with")
        self._rw_str_split = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [RW_LIST_INT_TY.as_pointer(), RW_STR_TY, RW_STR_TY]),
            "rw_str_split")
        self._rw_str_join = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [RW_LIST_INT_TY.as_pointer(), RW_STR_TY]), "rw_str_join")
        # List[int] ops — pointer-out ABI (see runtime.h for the
        # rationale: 24-byte struct is past arm64's "return in regs"
        # threshold, so we go through stack-allocated buffers).
        list_ptr = RW_LIST_INT_TY.as_pointer()
        self._rw_list_int_new = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [list_ptr]), "rw_list_int_new")
        self._rw_list_int_push = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [list_ptr, list_ptr, I64]),
            "rw_list_int_push")
        self._rw_list_int_at = ir.Function(
            m, ir.FunctionType(I64, [list_ptr, I64]), "rw_list_int_at")
        self._rw_list_int_len = ir.Function(
            m, ir.FunctionType(I64, [list_ptr]), "rw_list_int_len")
        # Option[int] ops — pointer-out for the output struct, matching
        # the List helpers' calling convention.
        option_ptr = RW_OPTION_INT_TY.as_pointer()
        self._rw_list_int_at_opt = ir.Function(
            m, ir.FunctionType(ir.VoidType(),
                               [option_ptr, list_ptr, I64]),
            "rw_list_int_at_opt")
        # List[T] for non-int T (spec 22): size-generic, element by pointer.
        self._rw_list_new = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [list_ptr]), "rw_list_new")
        self._rw_list_push = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [list_ptr, list_ptr, I8P, I64]), "rw_list_push")
        self._rw_list_at = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [I8P, list_ptr, I64, I64]), "rw_list_at")
        # Dict[K, V] (spec 31): keys and values by pointer.
        self._rw_dict_new = ir.Function(m, ir.FunctionType(I8P, [I64, I64]), "rw_dict_new")
        self._rw_dict_set = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [I8P, I8P, I8P]), "rw_dict_set")
        self._rw_dict_get = ir.Function(m, ir.FunctionType(I8, [I8P, I8P, I8P]), "rw_dict_get")
        self._rw_dict_at = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [I8P, I8P, I8P]), "rw_dict_at")
        self._rw_dict_has = ir.Function(m, ir.FunctionType(I8, [I8P, I8P]), "rw_dict_has")
        self._rw_dict_remove = ir.Function(m, ir.FunctionType(I8, [I8P, I8P]), "rw_dict_remove")
        self._rw_dict_len = ir.Function(m, ir.FunctionType(I64, [I8P]), "rw_dict_len")
        self._rw_dict_keys = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [list_ptr, I8P]), "rw_dict_keys")
        self._rw_dict_values = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [list_ptr, I8P]), "rw_dict_values")
        # Indexing and slicing (spec 29). RW_SLICE_NONE marks a missing bound.
        self._rw_str_index = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [RW_STR_TY, I64]), "rw_str_index")
        self._rw_str_slice_py = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [RW_STR_TY, I64, I64]), "rw_str_slice_py")
        self._rw_list_slice = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [list_ptr, list_ptr, I64, I64, I64]), "rw_list_slice")
        self._rw_str_to_int = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [option_ptr, RW_STR_TY]), "rw_str_to_int")
        # TCP open ops (runtime/net/tcp.c)
        self._rw_tcp_listen = ir.Function(
            m, ir.FunctionType(I64, [I64]), "rw_tcp_listen")
        self._rw_tcp_accept = ir.Function(
            m, ir.FunctionType(I64, [I64]), "rw_tcp_accept")
        # Generic fd I/O (runtime/io.c)
        self._rw_read = ir.Function(
            m, ir.FunctionType(ir.VoidType(),
                               [RW_STR_TY.as_pointer(), I64, I64]),
            "rw_read")
        self._rw_write = ir.Function(
            m, ir.FunctionType(I64, [I64, RW_STR_TY]), "rw_write")
        self._rw_close = ir.Function(
            m, ir.FunctionType(I64, [I64]), "rw_close")
        self._rw_file_open = ir.Function(
            m, ir.FunctionType(I64, [RW_STR_TY, RW_STR_TY]), "rw_file_open")
        # math intrinsics (lowered by clang; no -lm / runtime link needed)
        self._llvm_sqrt = m.declare_intrinsic("llvm.sqrt", [F64])
        self._llvm_floor = m.declare_intrinsic("llvm.floor", [F64])
        self._llvm_ceil = m.declare_intrinsic("llvm.ceil", [F64])
        self._llvm_exp = m.declare_intrinsic("llvm.exp", [F64])
        self._llvm_log = m.declare_intrinsic("llvm.log", [F64])
        self._llvm_sin = m.declare_intrinsic("llvm.sin", [F64])
        self._llvm_cos = m.declare_intrinsic("llvm.cos", [F64])
        self._llvm_fabs = m.declare_intrinsic("llvm.fabs", [F64])
        self._llvm_pow = m.declare_intrinsic("llvm.pow", [F64])  # double (double, double)
        # lifecycle
        self._rw_init = ir.Function(m, ir.FunctionType(ir.VoidType(), []), "rw_init")
        self._rw_shutdown = ir.Function(m, ir.FunctionType(ir.VoidType(), []), "rw_shutdown")
        # malloc/free
        self._malloc = ir.Function(m, ir.FunctionType(I8P, [I64]), "malloc")
        self._free = ir.Function(m, ir.FunctionType(ir.VoidType(), [I8P]), "free")
        # spawn / await: declared lazily as needed
        self._spawn_funcs: Dict[str, ir.Function] = {}
        self._await_funcs: Dict[str, ir.Function] = {}
        # Preemption at safe points (spec 23): a counter of pending requests
        # owned by the runtime, and the slow path that yields.
        self._rw_preempt_flag = ir.GlobalVariable(m, I32, "rw_preempt_flag")
        self._rw_safepoint = ir.Function(m, ir.FunctionType(ir.VoidType(), []), "rw_safepoint")
        # Drop a Future without awaiting it (spec 19).
        self._rw_detach = ir.Function(
            m, ir.FunctionType(ir.VoidType(), [I8P]), "rw_detach")

    def _decl_spawn(self, ret_ty: T.Type) -> ir.Function:
        key = repr(ret_ty)
        if key in self._spawn_funcs:
            return self._spawn_funcs[key]
        if ret_ty is T.INT:
            name, ret_llvm = "rw_spawn_i64", I64
        elif ret_ty is T.FLOAT:
            name, ret_llvm = "rw_spawn_f64", F64
        elif ret_ty is T.BOOL:
            name, ret_llvm = "rw_spawn_bool", I8
        elif ret_ty is T.STRING or ret_ty is T.BYTES:
            name, ret_llvm = "rw_spawn_str", RW_STR_TY
        elif ret_ty is T.VOID:
            name, ret_llvm = "rw_spawn_void", ir.VoidType()
        else:
            raise RuntimeError(f"cannot spawn function returning {ret_ty}")
        # trampoline fn signature: ret_llvm (i8*)
        tramp_ty = ir.FunctionType(ret_llvm, [I8P]).as_pointer()
        fty = ir.FunctionType(I8P, [tramp_ty, I8P])
        fn = ir.Function(self.mod, fty, name)
        self._spawn_funcs[key] = fn
        return fn

    def _decl_await(self, ret_ty: T.Type) -> ir.Function:
        key = repr(ret_ty)
        if key in self._await_funcs:
            return self._await_funcs[key]
        if ret_ty is T.INT:
            name, ret_llvm = "rw_await_i64", I64
        elif ret_ty is T.FLOAT:
            name, ret_llvm = "rw_await_f64", F64
        elif ret_ty is T.BOOL:
            name, ret_llvm = "rw_await_bool", I8
        elif ret_ty is T.STRING or ret_ty is T.BYTES:
            name, ret_llvm = "rw_await_str", RW_STR_TY
        elif ret_ty is T.VOID:
            name, ret_llvm = "rw_await_void", ir.VoidType()
        else:
            raise RuntimeError(f"cannot await Future[{ret_ty}]")
        fty = ir.FunctionType(ret_llvm, [I8P])
        fn = ir.Function(self.mod, fty, name)
        self._await_funcs[key] = fn
        return fn

    # ---------- entry ----------
    def generate(self) -> ir.Module:
        # First pass: declare LLVM function symbols for every module.
        for mod_name, mod in self._all_modules:
            for ast_fn in mod.functions:
                key: FuncKey = (mod_name, ast_fn.name)
                sig = self.sema.functions[key]
                param_tys = [llvm_type_of(pt) for _, pt in sig.params]
                ret_ll = llvm_type_of(sig.return_type)
                fty = ir.FunctionType(ret_ll, param_tys)
                fn = ir.Function(self.mod, fty, self._symbol(key))
                for i, (pname, _) in enumerate(sig.params):
                    fn.args[i].name = pname
                self.funcs[key] = fn

        # Second pass: emit bodies.
        for mod_name, mod in self._all_modules:
            for ast_fn in mod.functions:
                self._emit_function((mod_name, ast_fn.name), ast_fn)

        # Synthesize the real C @main.
        self._emit_c_main()
        return self.mod

    # ---------- C main shim ----------
    def _emit_c_main(self) -> None:
        fty = ir.FunctionType(I32, [I32, I8P.as_pointer()])
        main = ir.Function(self.mod, fty, "main")
        bb = main.append_basic_block("entry")
        b = ir.IRBuilder(bb)
        b.call(self._rw_set_args, list(main.args))  # for os_args() (spec 40)
        b.call(self._rw_init, [])
        r = b.call(self.funcs[(None, "main")], [])
        b.call(self._rw_shutdown, [])
        r32 = b.trunc(r, I32)
        b.ret(r32)

    # ---------- per-function ----------
    def _emit_function(self, key: FuncKey, ast_fn: A.FuncDef) -> None:
        fn = self.funcs[key]
        entry = fn.append_basic_block("entry")
        b = ir.IRBuilder(entry)
        # Allocate locals for each parameter; copy in.
        locals_: Dict[str, ir.AllocaInstr] = {}
        sig = self.sema.functions[key]
        for (pname, pty), llvm_arg in zip(sig.params, fn.args):
            slot = b.alloca(llvm_type_of(pty), name=pname)
            b.store(llvm_arg, slot)
            locals_[pname] = slot

        ctx = FunctionCtx(builder=b, function=fn, locals=locals_,
                          return_ty=sig.return_type, gen=self, func_key=key)
        self._emit_safepoint(ctx)
        self._emit_block(ast_fn.body, ctx)

        # If the block didn't terminate, add a synthetic terminator (for void) or unreachable.
        if not ctx.builder.block.is_terminated:
            self._emit_cleanups(ctx, 0)
            if sig.return_type is T.VOID:
                ctx.builder.ret_void()
            else:
                ctx.builder.unreachable()

    # ---------- statements ----------
    def _emit_block(self, stmts: List[A.Stmt], ctx: "FunctionCtx") -> None:
        for s in stmts:
            if ctx.builder.block.is_terminated:
                break
            self._emit_stmt(s, ctx)

    def _emit_stmt(self, stmt: A.Stmt, ctx: "FunctionCtx") -> None:
        b = ctx.builder
        if isinstance(stmt, A.VarDecl):
            ty = self.sema.decl_types[id(stmt)]
            slot = ctx.alloca(llvm_type_of(ty), name=stmt.name)
            val = self._emit_expr(stmt.value, ctx)
            b.store(val, slot)
            ctx.locals[stmt.name] = slot
            return
        if isinstance(stmt, A.Assign):
            decl_ty = self.sema.decl_types.get(id(stmt))
            if decl_ty is not None:
                # This assignment declares the variable (spec 32).
                ctx.locals[stmt.name] = ctx.alloca(llvm_type_of(decl_ty), name=stmt.name)
            slot = ctx.locals[stmt.name]
            val = self._emit_expr(stmt.value, ctx)
            b.store(val, slot)
            return
        if isinstance(stmt, A.ForEach):
            self._emit_foreach(stmt, ctx)
            return
        if isinstance(stmt, A.ValueMatch):
            self._emit_value_match(stmt, ctx)
            return
        if isinstance(stmt, A.TupleAssign):
            v = self._emit_expr(stmt.value, ctx)
            for i, name in enumerate(stmt.names):
                if name == "_":
                    continue
                decl = self.sema.unpack_decls.get((id(stmt), i))
                if decl is not None:
                    ctx.locals[name] = ctx.alloca(llvm_type_of(decl), name=name)
                b.store(b.extract_value(v, i), ctx.locals[name])
            return
        if isinstance(stmt, A.IndexAssign):
            dt = self.sema.expr_types[id(stmt.obj)]
            assert isinstance(dt, T.DictType)
            d = self._emit_expr(stmt.obj, ctx)
            key = self._spill(ctx, self._emit_expr(stmt.index, ctx), dt.key)
            val = self._spill(ctx, self._emit_expr(stmt.value, ctx), dt.value)
            b.call(self._rw_dict_set, [d, key, val])
            return
        if isinstance(stmt, A.FieldAssign):
            obj_ty = self.sema.expr_types[id(stmt.obj)]
            assert isinstance(obj_ty, T.ClassType)
            ptr = self._field_ptr(self._emit_expr(stmt.obj, ctx), obj_ty, stmt.field, b)
            b.store(self._emit_expr(stmt.value, ctx), ptr)
            return
        if isinstance(stmt, A.ExprStmt):
            val = self._emit_expr(stmt.expr, ctx)
            if isinstance(stmt.expr, A.SpawnExpr):
                # The Future is discarded, so nobody will await it: detach
                # the fiber so it is freed when it finishes (spec 19).
                b.call(self._rw_detach, [val])
            return
        if isinstance(stmt, A.Return):
            if stmt.value is None:
                self._emit_cleanups(ctx, 0)
                b.ret_void()
            else:
                v = self._emit_expr(stmt.value, ctx)  # before the cleanups run
                self._emit_cleanups(ctx, 0)
                b.ret(v)
            return
        if isinstance(stmt, A.Defer):
            ctx.cleanups.append(("defer", stmt.stmt, len(ctx.loop_stack)))
            return
        if isinstance(stmt, A.With):
            self._emit_with(stmt, ctx)
            return
        if isinstance(stmt, A.If):
            cond_i8 = self._emit_expr(stmt.cond, ctx)
            cond_i1 = b.icmp_unsigned("!=", cond_i8, ir.Constant(I8, 0))
            then_bb = ctx.function.append_basic_block("then")
            else_bb = ctx.function.append_basic_block("else")
            end_bb = ctx.function.append_basic_block("endif")
            b.cbranch(cond_i1, then_bb, else_bb)
            # then
            b.position_at_end(then_bb)
            self._emit_block(stmt.then_body, ctx)
            if not b.block.is_terminated:
                b.branch(end_bb)
            # else
            b.position_at_end(else_bb)
            self._emit_block(stmt.else_body, ctx)
            if not b.block.is_terminated:
                b.branch(end_bb)
            # end
            b.position_at_end(end_bb)
            return
        if isinstance(stmt, A.While):
            cond_bb = ctx.function.append_basic_block("while.cond")
            body_bb = ctx.function.append_basic_block("while.body")
            # `continue` and the end of the body go to the update step, if any.
            update_bb = ctx.function.append_basic_block("while.update") if stmt.update else cond_bb
            end_bb = ctx.function.append_basic_block("while.end")
            b.branch(cond_bb)
            b.position_at_end(cond_bb)
            # Every iteration (including `continue`) passes the loop head.
            self._emit_safepoint(ctx)
            cond_i8 = self._emit_expr(stmt.cond, ctx)
            cond_i1 = b.icmp_unsigned("!=", cond_i8, ir.Constant(I8, 0))
            b.cbranch(cond_i1, body_bb, end_bb)
            b.position_at_end(body_bb)
            ctx.loop_stack.append((update_bb, end_bb))
            self._emit_block(stmt.body, ctx)
            ctx.loop_stack.pop()
            if not b.block.is_terminated:
                b.branch(update_bb)
            if stmt.update:
                b.position_at_end(update_bb)
                self._emit_block(stmt.update, ctx)
                if not b.block.is_terminated:
                    b.branch(cond_bb)
            b.position_at_end(end_bb)
            return
        if isinstance(stmt, A.Break):
            self._emit_cleanups(ctx, len(ctx.loop_stack))
            b.branch(ctx.loop_stack[-1][1])
            return
        if isinstance(stmt, A.Continue):
            self._emit_cleanups(ctx, len(ctx.loop_stack))
            b.branch(ctx.loop_stack[-1][0])
            return
        if isinstance(stmt, A.Assert):
            cond_i8 = self._emit_expr(stmt.cond, ctx)
            cond_i1 = b.icmp_unsigned("!=", cond_i8, ir.Constant(I8, 0))
            success_bb = ctx.function.append_basic_block("assert.ok")
            fail_bb = ctx.function.append_basic_block("assert.fail")
            b.cbranch(cond_i1, success_bb, fail_bb)
            b.position_at_end(fail_bb)
            if stmt.msg is not None:
                msg_val = self._emit_expr(stmt.msg, ctx)
            else:
                msg_val = self._emit_string_literal("assertion failed", b)
            b.call(self._rw_panic, [msg_val])
            b.unreachable()
            b.position_at_end(success_bb)
            return
        if isinstance(stmt, A.MatchStmt):
            v = self._emit_expr(stmt.target, ctx)
            target_ty = self.sema.expr_types[id(stmt.target)]
            tag = b.extract_value(v, 0)
            arm1_bb = ctx.function.append_basic_block("match.arm1")
            arm0_bb = ctx.function.append_basic_block("match.arm0")
            end_bb = ctx.function.append_basic_block("match.end")
            # tag == 1 -> arm1, default (tag == 0) -> arm0.
            sw = b.switch(tag, arm0_bb)
            sw.add_case(ir.Constant(I64, 1), arm1_bb)
            # Payloads (spec 30): Option {tag, value}; Result {tag, ok, err}.
            b.position_at_end(arm1_bb)
            if stmt.style == "option":
                assert isinstance(target_ty, T.OptionType)
                self._emit_arm(stmt.some_var, stmt.some_block, b.extract_value(v, 1),
                               target_ty.inner, ctx, end_bb)
                b.position_at_end(arm0_bb)
                self._emit_arm(None, stmt.none_block, None, None, ctx, end_bb)
            else:  # "result"
                assert isinstance(target_ty, T.ResultType)
                self._emit_arm(stmt.ok_var, stmt.ok_block, b.extract_value(v, 1),
                               target_ty.ok, ctx, end_bb)
                b.position_at_end(arm0_bb)
                self._emit_arm(stmt.err_var, stmt.err_block, b.extract_value(v, 2),
                               target_ty.err, ctx, end_bb)
            b.position_at_end(end_bb)
            return
        raise RuntimeError(f"unsupported stmt: {type(stmt).__name__}")

    def _emit_cleanup(self, entry, ctx: "FunctionCtx") -> None:
        kind, payload, _ = entry
        b = ctx.builder
        if kind == "defer":
            self._emit_stmt(payload, ctx)
            return
        value, rtype = payload
        if rtype is T.MUTEX:
            b.call(self._rw_mutex_unlock, [value])
        else:
            b.call(self.funcs[rtype.methods["close"]], [value])

    def _emit_cleanups(self, ctx: "FunctionCtx", min_depth: int) -> None:
        """Emit, innermost first, the pending cleanups pushed at loop depth
        >= min_depth (0 = all of them, for a return). They stay on the stack:
        other paths still need them."""
        for entry in reversed(ctx.cleanups):
            if entry[2] >= min_depth:
                self._emit_cleanup(entry, ctx)

    def _emit_with(self, stmt: A.With, ctx: "FunctionCtx") -> None:
        b = ctx.builder
        rtype = self.sema.with_types[id(stmt)]
        value = self._emit_expr(stmt.expr, ctx)
        if rtype is T.MUTEX:
            b.call(self._rw_mutex_lock, [value])
        saved = None
        if stmt.var is not None:
            slot = ctx.alloca(llvm_type_of(rtype), name=stmt.var)
            b.store(value, slot)
            saved = ctx.locals.get(stmt.var)
            ctx.locals[stmt.var] = slot
        entry = ("with", (value, rtype), len(ctx.loop_stack))
        ctx.cleanups.append(entry)
        self._emit_block(stmt.body, ctx)
        ctx.cleanups.pop()
        if not b.block.is_terminated:
            self._emit_cleanup(entry, ctx)
        if stmt.var is not None:
            if saved is None:
                ctx.locals.pop(stmt.var, None)
            else:
                ctx.locals[stmt.var] = saved

    def _emit_value_match(self, stmt: A.ValueMatch, ctx: "FunctionCtx") -> None:
        """Test arms in order; the first whose pattern equals the value runs
        (spec 38). A catch-all arm always matches."""
        b = ctx.builder
        tt = self.sema.expr_types[id(stmt.target)]
        v = self._emit_expr(stmt.target, ctx)
        end_bb = ctx.function.append_basic_block("vmatch.end")
        for arm in stmt.arms:
            body_bb = ctx.function.append_basic_block("vmatch.arm")
            next_bb = ctx.function.append_basic_block("vmatch.next")
            if arm.patterns:
                hit = None
                for p in arm.patterns:
                    pv = self._emit_expr(p, ctx)
                    if tt is T.STRING:
                        eq = b.icmp_unsigned("!=", b.call(self._rw_str_eq, [v, pv]), ir.Constant(I8, 0))
                    else:
                        eq = b.icmp_signed("==", v, pv)
                    hit = eq if hit is None else b.or_(hit, eq)
                b.cbranch(hit, body_bb, next_bb)
            else:
                b.branch(body_bb)
            b.position_at_end(body_bb)
            saved = None
            if arm.bind is not None:
                slot = ctx.alloca(llvm_type_of(tt), name=arm.bind)
                b.store(v, slot)
                saved = ctx.locals.get(arm.bind)
                ctx.locals[arm.bind] = slot
            self._emit_block(arm.body, ctx)
            if arm.bind is not None:
                if saved is None:
                    ctx.locals.pop(arm.bind, None)
                else:
                    ctx.locals[arm.bind] = saved
            if not b.block.is_terminated:
                b.branch(end_bb)
            b.position_at_end(next_bb)
        b.branch(end_bb)  # no arm matched
        b.position_at_end(end_bb)

    def _emit_arm(self, var_name, block, payload, payload_ty, ctx, end_bb):
        """Emit one match arm. var_name=None means no payload binding."""
        b = ctx.builder
        if var_name is not None:
            slot = ctx.alloca(llvm_type_of(payload_ty), name=var_name)
            b.store(payload, slot)
            saved = ctx.locals.get(var_name)
            ctx.locals[var_name] = slot
            self._emit_block(block, ctx)
            if saved is not None:
                ctx.locals[var_name] = saved
            else:
                ctx.locals.pop(var_name, None)
        else:
            self._emit_block(block, ctx)
        if not b.block.is_terminated:
            b.branch(end_bb)

    # ---------- expressions ----------
    def _field_ptr(
        self, obj: ir.Value, cls: T.ClassType, field: str, b: ir.IRBuilder
    ) -> ir.Value:
        """Pointer to `field` inside the record `obj` (an i8*)."""
        idx = cls.field_index(field)
        assert idx is not None
        rec = b.bitcast(obj, record_struct_type(cls).as_pointer())
        return b.gep(rec, [ir.Constant(I32, 0), ir.Constant(I32, idx)])

    def _emit_constructor(self, call: A.Call, cls: T.ClassType, ctx: "FunctionCtx") -> ir.Value:
        """malloc a record and store each field (spec 20)."""
        b = ctx.builder
        values = [self._emit_expr(a, ctx) for a in call.args]
        rec_ptr_ty = record_struct_type(cls).as_pointer()
        # sizeof via the GEP-on-null idiom (as in _emit_spawn).
        size = b.ptrtoint(b.gep(ir.Constant(rec_ptr_ty, None), [ir.Constant(I32, 1)]), I64)
        size = b.select(b.icmp_unsigned(">", size, ir.Constant(I64, 0)), size, ir.Constant(I64, 1))
        raw = b.call(self._malloc, [size])
        rec = b.bitcast(raw, rec_ptr_ty)
        for i, v in enumerate(values):
            b.store(v, b.gep(rec, [ir.Constant(I32, 0), ir.Constant(I32, i)]))
        return raw

    def _emit_safepoint(self, ctx: "FunctionCtx") -> None:
        """If any preemption request is pending, call rw_safepoint(), which
        yields when the request is for this worker (spec 23). The fast path
        is one relaxed atomic load and a branch."""
        b = ctx.builder
        pending = b.load_atomic(self._rw_preempt_flag, "monotonic", 4)
        slow = ctx.function.append_basic_block("preempt")
        cont = ctx.function.append_basic_block("preempt.cont")
        b.cbranch(b.icmp_unsigned("!=", pending, ir.Constant(I32, 0)), slow, cont)
        b.position_at_end(slow)
        b.call(self._rw_safepoint, [])
        b.branch(cont)
        b.position_at_end(cont)

    def _sizeof(self, ty: ir.Type, b: ir.IRBuilder) -> ir.Value:
        """sizeof(ty) as an i64, via the GEP-on-null idiom."""
        return b.ptrtoint(b.gep(ir.Constant(ty.as_pointer(), None), [ir.Constant(I32, 1)]), I64)

    # ---------- lists and strings: literals, indexing, slicing (spec 29) ----------

    _SLICE_NONE = ir.Constant(I64, -(2 ** 63))

    def _list_push_value(self, ctx: "FunctionCtx", lst: ir.Value, list_ty: T.ListType,
                         elem: ir.Value) -> ir.Value:
        """Return lst with elem appended (copy-on-push)."""
        b = ctx.builder
        in_slot = ctx.alloca(RW_LIST_INT_TY)
        b.store(lst, in_slot)
        out_slot = ctx.alloca(RW_LIST_INT_TY)
        if list_ty is T.LIST_INT:
            b.call(self._rw_list_int_push, [out_slot, in_slot, elem])
        else:
            elem_llvm = llvm_type_of(list_ty.elem)
            elem_slot = ctx.alloca(elem_llvm)
            b.store(elem, elem_slot)
            b.call(self._rw_list_push,
                   [out_slot, in_slot, b.bitcast(elem_slot, I8P), self._sizeof(elem_llvm, b)])
        return b.load(out_slot)

    def _list_get(self, ctx: "FunctionCtx", list_slot: ir.Value, list_ty: T.ListType,
                  idx: ir.Value) -> ir.Value:
        """Bounds-checked element `idx` (already non-negative-normalised)."""
        b = ctx.builder
        if list_ty is T.LIST_INT:
            return b.call(self._rw_list_int_at, [list_slot, idx])
        elem_llvm = llvm_type_of(list_ty.elem)
        elem_slot = ctx.alloca(elem_llvm)
        b.call(self._rw_list_at,
               [b.bitcast(elem_slot, I8P), list_slot, idx, self._sizeof(elem_llvm, b)])
        return b.load(elem_slot)

    def _emit_list_lit(self, expr: A.ListLit, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        list_ty = self.sema.expr_types[id(expr)]
        assert isinstance(list_ty, T.ListType)
        slot = ctx.alloca(RW_LIST_INT_TY)
        b.call(self._rw_list_int_new if list_ty is T.LIST_INT else self._rw_list_new, [slot])
        lst: ir.Value = b.load(slot)
        for e in expr.elems:
            lst = self._list_push_value(ctx, lst, list_ty, self._emit_expr(e, ctx))
        return lst

    # ---------- Dict[K, V] (spec 31) ----------

    def _spill(self, ctx: "FunctionCtx", value: ir.Value, ty: T.Type) -> ir.Value:
        """Store value in an entry-block slot and return it as an i8*."""
        slot = ctx.alloca(llvm_type_of(ty))
        ctx.builder.store(value, slot)
        return ctx.builder.bitcast(slot, I8P)

    def _chan_recv(self, ctx: "FunctionCtx", ch: ir.Value, ct: T.ChannelType) -> ir.Value:
        """Receive into an Option[T] value: tag = got a value (spec 44)."""
        b = ctx.builder
        val_llvm = llvm_type_of(ct.elem)
        slot = ctx.alloca(val_llvm)
        b.store(ir.Constant(val_llvm, None), slot)
        ok = b.call(self._rw_chan_recv, [ch, b.bitcast(slot, I8P)])
        opt = ir.Constant(llvm_type_of(T.option_of(ct.elem)), None)
        opt = b.insert_value(opt, b.zext(ok, I64), 0)
        return b.insert_value(opt, b.load(slot), 1)

    def _emit_chan_builtin(self, call: A.Call, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        if call.callee == "chan_new":
            ct = self.sema.expr_types[id(call)]
            assert isinstance(ct, T.ChannelType)
            cap = self._emit_expr(call.args[0], ctx)
            return b.call(self._rw_chan_new, [cap, self._sizeof(llvm_type_of(ct.elem), b)])
        ch = self._emit_expr(call.args[0], ctx)
        ct = self.sema.expr_types[id(call.args[0])]
        assert isinstance(ct, T.ChannelType)
        if call.callee == "chan_send":
            v = self._spill(ctx, self._emit_expr(call.args[1], ctx), ct.elem)
            return b.call(self._rw_chan_send, [ch, v])
        if call.callee == "chan_recv":
            return self._chan_recv(ctx, ch, ct)
        return b.call(self._rw_chan_close, [ch])
    def _emit_await_many(self, call: A.Call, ctx: "FunctionCtx") -> ir.Value:
        """await_all: await each Future in order into a List[T].
        await_any: (index, value) of the first Future to finish (spec 45)."""
        b = ctx.builder
        lt = self.sema.expr_types[id(call.args[0])]
        assert isinstance(lt, T.ListType) and isinstance(lt.elem, T.FutureType)
        inner = lt.elem.inner
        await_fn = self._decl_await(inner)
        slot = ctx.alloca(RW_LIST_INT_TY)
        b.store(self._emit_expr(call.args[0], ctx), slot)
        if call.callee == "await_any":
            idx = b.call(self._rw_await_any, [slot])
            value = b.call(await_fn, [self._list_get(ctx, slot, lt, idx)])
            tup = ir.Constant(llvm_type_of(T.TupleType([T.INT, inner])), None)
            return b.insert_value(b.insert_value(tup, idx, 0), value, 1)
        out_ty = T.list_of(inner)
        out = ctx.alloca(RW_LIST_INT_TY)
        b.call(self._rw_list_int_new if out_ty is T.LIST_INT else self._rw_list_new, [out])
        n = b.call(self._rw_list_int_len, [slot])
        idx_slot = ctx.alloca(I64)
        b.store(ir.Constant(I64, 0), idx_slot)
        cond_bb = ctx.function.append_basic_block("await_all.cond")
        body_bb = ctx.function.append_basic_block("await_all.body")
        end_bb = ctx.function.append_basic_block("await_all.end")
        b.branch(cond_bb)
        b.position_at_end(cond_bb)
        i = b.load(idx_slot)
        b.cbranch(b.icmp_signed("<", i, n), body_bb, end_bb)
        b.position_at_end(body_bb)
        value = b.call(await_fn, [self._list_get(ctx, slot, lt, i)])
        b.store(self._list_push_value(ctx, b.load(out), out_ty, value), out)
        b.store(b.add(i, ir.Constant(I64, 1)), idx_slot)
        b.branch(cond_bb)
        b.position_at_end(end_bb)
        return b.load(out)

    def _dict_keys_list(self, ctx: "FunctionCtx", d: ir.Value) -> ir.Value:
        out = ctx.alloca(RW_LIST_INT_TY)
        ctx.builder.call(self._rw_dict_keys, [out, d])
        return ctx.builder.load(out)

    def _emit_dict_builtin(self, call: A.Call, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        if call.callee == "dict_new":
            dt = self.sema.expr_types[id(call)]
            assert isinstance(dt, T.DictType)
            kind = ir.Constant(I64, 0 if dt.key is T.INT else 1)
            return b.call(self._rw_dict_new, [kind, self._sizeof(llvm_type_of(dt.value), b)])
        d = self._emit_expr(call.args[0], ctx)
        dt = self.sema.expr_types[id(call.args[0])]
        assert isinstance(dt, T.DictType)
        if call.callee in ("dict_keys", "dict_values"):
            out = ctx.alloca(RW_LIST_INT_TY)
            fn = self._rw_dict_keys if call.callee == "dict_keys" else self._rw_dict_values
            b.call(fn, [out, d])
            return b.load(out)
        key = self._spill(ctx, self._emit_expr(call.args[1], ctx), dt.key)
        if call.callee == "dict_has":
            return b.call(self._rw_dict_has, [d, key])
        if call.callee == "dict_remove":
            return b.call(self._rw_dict_remove, [d, key])
        # dict_get -> Option[V]: tag = found, value = the copied value (or 0).
        val_llvm = llvm_type_of(dt.value)
        val_slot = ctx.alloca(val_llvm)
        b.store(ir.Constant(val_llvm, None), val_slot)
        found = b.call(self._rw_dict_get, [d, key, b.bitcast(val_slot, I8P)])
        opt = ir.Constant(llvm_type_of(T.option_of(dt.value)), None)
        opt = b.insert_value(opt, b.zext(found, I64), 0)
        return b.insert_value(opt, b.load(val_slot), 1)

    def _emit_index(self, expr: A.Index, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        obj = self._emit_expr(expr.obj, ctx)
        idx = self._emit_expr(expr.index, ctx)
        obj_ty = self.sema.expr_types[id(expr.obj)]
        if isinstance(obj_ty, T.DictType):
            val_slot = ctx.alloca(llvm_type_of(obj_ty.value))
            b.call(self._rw_dict_at, [obj, self._spill(ctx, idx, obj_ty.key),
                                      b.bitcast(val_slot, I8P)])
            return b.load(val_slot)
        if obj_ty is T.STRING:
            return b.call(self._rw_str_index, [obj, idx])
        assert isinstance(obj_ty, T.ListType)
        slot = ctx.alloca(RW_LIST_INT_TY)
        b.store(obj, slot)
        n = b.call(self._rw_list_int_len, [slot])
        neg = b.icmp_signed("<", idx, ir.Constant(I64, 0))
        idx = b.select(neg, b.add(idx, n), idx)  # xs[-1] is the last element
        return self._list_get(ctx, slot, obj_ty, idx)

    def _emit_slice(self, expr: A.Slice, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        obj = self._emit_expr(expr.obj, ctx)
        start = self._emit_expr(expr.start, ctx) if expr.start is not None else self._SLICE_NONE
        stop = self._emit_expr(expr.stop, ctx) if expr.stop is not None else self._SLICE_NONE
        obj_ty = self.sema.expr_types[id(expr.obj)]
        if obj_ty is T.STRING:
            return b.call(self._rw_str_slice_py, [obj, start, stop])
        assert isinstance(obj_ty, T.ListType)
        in_slot = ctx.alloca(RW_LIST_INT_TY)
        b.store(obj, in_slot)
        out_slot = ctx.alloca(RW_LIST_INT_TY)
        size = self._sizeof(llvm_type_of(obj_ty.elem), b)
        b.call(self._rw_list_slice, [out_slot, in_slot, start, stop, size])
        return b.load(out_slot)

    def _emit_foreach(self, stmt: A.ForEach, ctx: "FunctionCtx") -> None:
        """for var in <list|string>: an index loop over a value evaluated
        once; `continue` goes to the increment."""
        b = ctx.builder
        it_ty = self.sema.expr_types[id(stmt.iterable)]
        elem_ty = self.sema.foreach_types[id(stmt)]
        it_val = self._emit_expr(stmt.iterable, ctx)
        if isinstance(it_ty, T.ChannelType):
            self._emit_chan_foreach(stmt, ctx, it_val, it_ty)
            return
        if isinstance(it_ty, T.DictType):
            # Iterate over a snapshot of the keys (spec 31).
            it_val = self._dict_keys_list(ctx, it_val)
            it_ty = T.list_of(it_ty.key)
        it_slot = ctx.alloca(llvm_type_of(it_ty))
        b.store(it_val, it_slot)
        if it_ty is T.STRING:
            n = b.call(self._rw_str_len, [it_val])
        else:
            n = b.call(self._rw_list_int_len, [it_slot])
        idx_slot = ctx.alloca(I64)
        b.store(ir.Constant(I64, 0), idx_slot)
        var_slot = ctx.alloca(llvm_type_of(elem_ty), name=stmt.var)

        cond_bb = ctx.function.append_basic_block("for.cond")
        body_bb = ctx.function.append_basic_block("for.body")
        update_bb = ctx.function.append_basic_block("for.update")
        end_bb = ctx.function.append_basic_block("for.end")
        b.branch(cond_bb)
        b.position_at_end(cond_bb)
        self._emit_safepoint(ctx)
        i = b.load(idx_slot)
        b.cbranch(b.icmp_signed("<", i, n), body_bb, end_bb)

        b.position_at_end(body_bb)
        if it_ty is T.STRING:
            elem = b.call(self._rw_str_index, [b.load(it_slot), i])
        else:
            assert isinstance(it_ty, T.ListType)
            elem = self._list_get(ctx, it_slot, it_ty, i)
        b.store(elem, var_slot)
        saved = ctx.locals.get(stmt.var)
        ctx.locals[stmt.var] = var_slot
        ctx.loop_stack.append((update_bb, end_bb))
        self._emit_block(stmt.body, ctx)
        ctx.loop_stack.pop()
        if saved is None:
            del ctx.locals[stmt.var]
        else:
            ctx.locals[stmt.var] = saved
        if not b.block.is_terminated:
            b.branch(update_bb)

        b.position_at_end(update_bb)
        b.store(b.add(b.load(idx_slot), ir.Constant(I64, 1)), idx_slot)
        b.branch(cond_bb)
        b.position_at_end(end_bb)

    def _emit_chan_foreach(self, stmt: A.ForEach, ctx: "FunctionCtx", ch: ir.Value,
                           ct: T.ChannelType) -> None:
        """for v in ch: receive until the channel is closed and drained."""
        b = ctx.builder
        val_llvm = llvm_type_of(ct.elem)
        var_slot = ctx.alloca(val_llvm, name=stmt.var)
        cond_bb = ctx.function.append_basic_block("chan.recv")
        body_bb = ctx.function.append_basic_block("chan.body")
        end_bb = ctx.function.append_basic_block("chan.end")
        b.branch(cond_bb)
        b.position_at_end(cond_bb)
        self._emit_safepoint(ctx)
        ok = b.call(self._rw_chan_recv, [ch, b.bitcast(var_slot, I8P)])
        b.cbranch(b.icmp_unsigned("!=", ok, ir.Constant(I8, 0)), body_bb, end_bb)
        b.position_at_end(body_bb)
        saved = ctx.locals.get(stmt.var)
        ctx.locals[stmt.var] = var_slot
        ctx.loop_stack.append((cond_bb, end_bb))
        self._emit_block(stmt.body, ctx)
        ctx.loop_stack.pop()
        if saved is None:
            del ctx.locals[stmt.var]
        else:
            ctx.locals[stmt.var] = saved
        if not b.block.is_terminated:
            b.branch(cond_bb)
        b.position_at_end(end_bb)

    def _emit_generic_list_op(self, call: A.Call, ctx: "FunctionCtx") -> ir.Value:
        """list_push / list_at for List[T] with T != int (spec 22)."""
        b = ctx.builder
        list_ty = self.sema.expr_types[id(call.args[0])]
        assert isinstance(list_ty, T.ListType)
        elem_llvm = llvm_type_of(list_ty.elem)
        in_slot = ctx.alloca(RW_LIST_INT_TY)
        b.store(self._emit_expr(call.args[0], ctx), in_slot)
        elem_slot = ctx.alloca(elem_llvm)
        size = self._sizeof(elem_llvm, b)
        if call.callee == "list_push":
            b.store(self._emit_expr(call.args[1], ctx), elem_slot)
            out_slot = ctx.alloca(RW_LIST_INT_TY)
            b.call(self._rw_list_push, [out_slot, in_slot, b.bitcast(elem_slot, I8P), size])
            return b.load(out_slot)
        idx = self._emit_expr(call.args[1], ctx)
        b.call(self._rw_list_at, [b.bitcast(elem_slot, I8P), in_slot, idx, size])
        return b.load(elem_slot)

    def _emit_expr(self, expr: A.Expr, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        cdef = self.sema.const_refs.get(id(expr))
        if cdef is not None:
            # A module constant: emit its literal in place (spec 35).
            return self._emit_expr(cdef.value, ctx)
        ref = self.sema.func_refs.get(id(expr))
        if ref is not None:
            # A function used as a value (a Name or `mod.f`): the function
            # symbol is its pointer (spec 21).
            return self.funcs[ref]
        if isinstance(expr, A.FieldAccess):
            obj_ty = self.sema.expr_types[id(expr.obj)]
            assert isinstance(obj_ty, T.ClassType)
            return b.load(self._field_ptr(self._emit_expr(expr.obj, ctx), obj_ty, expr.field, b))
        if isinstance(expr, A.IntLit):
            return ir.Constant(I64, expr.value)
        if isinstance(expr, A.FloatLit):
            return ir.Constant(F64, expr.value)
        if isinstance(expr, A.BoolLit):
            return ir.Constant(I8, 1 if expr.value else 0)
        if isinstance(expr, A.StringLit):
            return self._emit_string_literal(expr.value, b)
        if isinstance(expr, A.TupleLit):
            agg = ir.Constant(llvm_type_of(self.sema.expr_types[id(expr)]), None)
            for i, e in enumerate(expr.elems):
                agg = b.insert_value(agg, self._emit_expr(e, ctx), i)
            return agg
        if isinstance(expr, A.Index) and id(expr) in self.sema.tuple_index:
            return b.extract_value(self._emit_expr(expr.obj, ctx), self.sema.tuple_index[id(expr)])
        if isinstance(expr, A.ListLit):
            return self._emit_list_lit(expr, ctx)
        if isinstance(expr, A.DictLit):
            # rw_dict_new, then one rw_dict_set per entry (spec 36).
            dt = self.sema.expr_types[id(expr)]
            assert isinstance(dt, T.DictType)
            kind = ir.Constant(I64, 0 if dt.key is T.INT else 1)
            d = b.call(self._rw_dict_new, [kind, self._sizeof(llvm_type_of(dt.value), b)])
            for k, v in zip(expr.keys, expr.values):
                key = self._spill(ctx, self._emit_expr(k, ctx), dt.key)
                val = self._spill(ctx, self._emit_expr(v, ctx), dt.value)
                b.call(self._rw_dict_set, [d, key, val])
            return d
        if isinstance(expr, A.Index):
            return self._emit_index(expr, ctx)
        if isinstance(expr, A.Slice):
            return self._emit_slice(expr, ctx)
        if isinstance(expr, A.CallValue) and id(expr) in self.sema.method_calls:
            # `e.m(args)` -> Class.m(e, args) (spec 33).
            assert isinstance(expr.callee, A.FieldAccess)
            recv = self._emit_expr(expr.callee.obj, ctx)
            fn = self.funcs[self.sema.method_calls[id(expr)]]
            return b.call(fn, [recv] + [self._emit_expr(a, ctx) for a in expr.args])
        if isinstance(expr, A.CallValue):
            callee = self._emit_expr(expr.callee, ctx)
            return b.call(callee, [self._emit_expr(a, ctx) for a in expr.args])
        if isinstance(expr, A.Name):
            slot = ctx.locals[expr.name]
            return b.load(slot)
        if isinstance(expr, A.UnaryOp):
            v = self._emit_expr(expr.operand, ctx)
            ty = self.sema.expr_types[id(expr.operand)]
            if expr.op == "-":
                if ty is T.INT:
                    return b.neg(v)
                else:
                    return b.fneg(v)
            if expr.op == "not":
                # v is i8 with 0/1; flip lsb
                return b.xor(v, ir.Constant(I8, 1))
            if expr.op == "~":
                # v is i64; bitwise complement
                return b.not_(v)
            raise RuntimeError(f"bad unary {expr.op}")
        if isinstance(expr, A.BinOp):
            return self._emit_binop(expr, ctx)
        if isinstance(expr, A.Call):
            return self._emit_call(expr, ctx)
        if isinstance(expr, A.SpawnExpr):
            return self._emit_spawn(expr, ctx)
        if isinstance(expr, A.AwaitExpr):
            return self._emit_await(expr, ctx)
        if isinstance(expr, (A.SomeExpr, A.NoneExpr, A.OkExpr, A.ErrExpr)):
            # Start from all-zero (tag 0) and insert the tag and the payload
            # (spec 30): Some/Ok have tag 1; Some/Ok store at 1, Err at 2.
            zero = ir.Constant(llvm_type_of(self.sema.expr_types[id(expr)]), None)
            if isinstance(expr, A.NoneExpr):
                return zero
            v = self._emit_expr(expr.arg, ctx)
            if isinstance(expr, A.ErrExpr):
                return ctx.builder.insert_value(zero, v, 2)
            tagged = ctx.builder.insert_value(zero, ir.Constant(I64, 1), 0)
            return ctx.builder.insert_value(tagged, v, 1)
        if isinstance(expr, A.IfExpr):
            return self._emit_if_expr(expr, ctx)
        raise RuntimeError(f"unsupported expr: {type(expr).__name__}")

    def _emit_if_expr(self, expr: A.IfExpr, ctx: "FunctionCtx") -> ir.Value:
        # `then if cond else els` lowered to cbranch + phi, mirroring the
        # short-circuit and/or emission. Both branches are sema-checked to
        # share one type, so the phi node uses that type.
        b = ctx.builder
        cond = self._emit_expr(expr.cond, ctx)
        cond_i1 = b.icmp_unsigned("!=", cond, ir.Constant(I8, 0))
        then_bb = ctx.function.append_basic_block("tern.then")
        else_bb = ctx.function.append_basic_block("tern.else")
        end_bb = ctx.function.append_basic_block("tern.end")
        b.cbranch(cond_i1, then_bb, else_bb)

        b.position_at_end(then_bb)
        then_val = self._emit_expr(expr.then, ctx)
        then_bb_end = b.block
        b.branch(end_bb)

        b.position_at_end(else_bb)
        else_val = self._emit_expr(expr.els, ctx)
        else_bb_end = b.block
        b.branch(end_bb)

        b.position_at_end(end_bb)
        result_ty = llvm_type_of(self.sema.expr_types[id(expr)])
        phi = b.phi(result_ty)
        phi.add_incoming(then_val, then_bb_end)
        phi.add_incoming(else_val, else_bb_end)
        return phi

    def _emit_binop(self, expr: A.BinOp, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        op = expr.op

        # Short-circuit logical ops
        if op == "and":
            lhs = self._emit_expr(expr.left, ctx)
            lhs_i1 = b.icmp_unsigned("!=", lhs, ir.Constant(I8, 0))
            rhs_bb = ctx.function.append_basic_block("and.rhs")
            end_bb = ctx.function.append_basic_block("and.end")
            cur_bb = b.block
            b.cbranch(lhs_i1, rhs_bb, end_bb)
            b.position_at_end(rhs_bb)
            rhs = self._emit_expr(expr.right, ctx)
            rhs_bb_end = b.block
            b.branch(end_bb)
            b.position_at_end(end_bb)
            phi = b.phi(I8)
            phi.add_incoming(ir.Constant(I8, 0), cur_bb)
            phi.add_incoming(rhs, rhs_bb_end)
            return phi
        if op == "or":
            lhs = self._emit_expr(expr.left, ctx)
            lhs_i1 = b.icmp_unsigned("!=", lhs, ir.Constant(I8, 0))
            rhs_bb = ctx.function.append_basic_block("or.rhs")
            end_bb = ctx.function.append_basic_block("or.end")
            cur_bb = b.block
            b.cbranch(lhs_i1, end_bb, rhs_bb)
            b.position_at_end(rhs_bb)
            rhs = self._emit_expr(expr.right, ctx)
            rhs_bb_end = b.block
            b.branch(end_bb)
            b.position_at_end(end_bb)
            phi = b.phi(I8)
            phi.add_incoming(ir.Constant(I8, 1), cur_bb)
            phi.add_incoming(rhs, rhs_bb_end)
            return phi

        l = self._emit_expr(expr.left, ctx)
        r = self._emit_expr(expr.right, ctx)
        lty = self.sema.expr_types[id(expr.left)]
        is_float = lty is T.FLOAT
        is_int = lty is T.INT
        is_str = lty is T.STRING
        is_strlike = lty is T.STRING or lty is T.BYTES

        if op in ("/", "%") and is_int and self.checked:
            return self._checked_int_div(op, l, r, expr, ctx)
        if op in ("+", "-", "*", "/", "%"):
            if is_int:
                table = {
                    "+": b.add, "-": b.sub, "*": b.mul,
                    "/": b.sdiv, "%": b.srem,
                }
                return table[op](l, r)
            if is_float:
                table = {
                    "+": b.fadd, "-": b.fsub, "*": b.fmul,
                    "/": b.fdiv, "%": b.frem,
                }
                return table[op](l, r)
            if is_str and op == "+":
                return b.call(self._rw_str_concat, [l, r])
            raise RuntimeError(f"arith op {op} on {lty}")

        if op in ("&", "|", "^", "<<", ">>"):
            if is_int:
                table = {
                    "&": b.and_, "|": b.or_, "^": b.xor,
                    "<<": b.shl, ">>": b.ashr,
                }
                return table[op](l, r)
            raise RuntimeError(f"bitwise op {op} on {lty}")

        if op in ("<", "<=", ">", ">=", "==", "!="):
            if is_int:
                pred = {
                    "<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "==", "!=": "!=",
                }[op]
                i1 = b.icmp_signed(pred, l, r)
            elif is_float:
                pred = {
                    "<": "<", "<=": "<=", ">": ">", ">=": ">=",
                    "==": "==", "!=": "!=",
                }[op]
                i1 = b.fcmp_ordered(pred, l, r)
            elif lty is T.BOOL and op in ("==", "!="):
                i1 = b.icmp_unsigned(op, l, r)
            elif is_str and op in ("<", "<=", ">", ">="):
                cmp = b.call(self._rw_str_cmp, [l, r])
                i1 = b.icmp_signed(op, cmp, ir.Constant(I64, 0))
            elif is_strlike and op in ("==", "!="):
                eq_i8 = b.call(self._rw_str_eq, [l, r])
                i1 = b.icmp_unsigned("!=", eq_i8, ir.Constant(I8, 0))
                if op == "!=":
                    i1 = b.xor(i1, ir.Constant(ir.IntType(1), 1))
            else:
                raise RuntimeError(f"cmp op {op} on {lty}")
            return b.zext(i1, I8)

        raise RuntimeError(f"unknown binop {op}")

    def _emit_conversion(self, call: A.Call, ctx: "FunctionCtx") -> ir.Value:
        """int(x) / float(x) / str(x) (spec 26)."""
        b = ctx.builder
        v = self._emit_expr(call.args[0], ctx)
        at = self.sema.expr_types[id(call.args[0])]
        if call.callee == "int":
            if at is T.FLOAT:
                return b.fptosi(v, I64)
            if at is T.BOOL:
                return b.zext(v, I64)
            return v
        if call.callee == "float":
            return b.sitofp(v, F64) if at is T.INT else v
        if at is T.INT:
            return b.call(self._rw_str_from_int, [v])
        if at is T.FLOAT:
            return b.call(self._rw_str_from_float, [v])
        if at is T.BOOL:
            is_true = b.icmp_unsigned("!=", v, ir.Constant(I8, 0))
            return b.select(is_true, self._emit_string_literal("true", b),
                            self._emit_string_literal("false", b))
        return v

    def _emit_call(self, call: A.Call, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        if (
            call.module is None
            and call.callee in ("int", "float", "str")
            and id(call) not in self.sema.call_resolution
            and id(call) not in self.sema.local_fn_calls
            and id(call) not in self.sema.constructor_calls
        ):
            return self._emit_conversion(call, ctx)
        method_key = self.sema.method_calls.get(id(call))
        if method_key is not None:
            # `x.m(args)` with x a local -> Class.m(x, args) (spec 33).
            assert call.module is not None
            recv = b.load(ctx.locals[call.module])
            return b.call(self.funcs[method_key],
                          [recv] + [self._emit_expr(a, ctx) for a in call.args])
        if id(call) in self.sema.local_fn_calls:
            callee = b.load(ctx.locals[call.callee])
            return b.call(callee, [self._emit_expr(a, ctx) for a in call.args])
        rec_cls = self.sema.field_fn_calls.get(id(call))
        if rec_cls is not None:
            assert call.module is not None
            obj = b.load(ctx.locals[call.module])
            callee = b.load(self._field_ptr(obj, rec_cls, call.callee, b))
            return b.call(callee, [self._emit_expr(a, ctx) for a in call.args])
        cls = self.sema.constructor_calls.get(id(call))
        if cls is not None:
            return self._emit_constructor(call, cls, ctx)
        # User-function calls (qualified or not) are recorded by sema in
        # call_resolution; builtins are not. This is the discriminator.
        key = self.sema.call_resolution.get(id(call))
        if key is not None:
            fn = self.funcs[key]
            args = [self._emit_expr(a, ctx) for a in call.args]
            return ctx.builder.call(fn, args)
        if call.callee == "print":
            arg_ast = call.args[0]
            v = self._emit_expr(arg_ast, ctx)
            aty = self.sema.expr_types[id(arg_ast)]
            if aty is T.INT:
                ctx.builder.call(self._rw_print_i64, [v])
            elif aty is T.FLOAT:
                ctx.builder.call(self._rw_print_f64, [v])
            elif aty is T.BOOL:
                ctx.builder.call(self._rw_print_bool, [v])
            elif aty is T.STRING:
                ctx.builder.call(self._rw_print_str, [v])
            else:
                raise RuntimeError(f"cannot print {aty}")
            # `print` is void; produce a poison-ish placeholder is unsafe; print is only
            # used as a statement expression; return a zero i64 sentinel never used.
            return ir.Constant(I64, 0)
        if call.callee == "len":
            arg_ast = call.args[0]
            v = self._emit_expr(arg_ast, ctx)
            aty = self.sema.expr_types[id(arg_ast)]
            if isinstance(aty, T.DictType):
                return ctx.builder.call(self._rw_dict_len, [v])
            if isinstance(aty, T.ChannelType):
                return ctx.builder.call(self._rw_chan_len, [v])
            if isinstance(aty, T.ListType):
                # rw_list_int_len takes a pointer to a stack copy; it only
                # reads `len`, so it serves every List[T].
                slot = ctx.alloca(RW_LIST_INT_TY)
                ctx.builder.store(v, slot)
                return ctx.builder.call(self._rw_list_int_len, [slot])
            return ctx.builder.call(self._rw_str_len, [v])
        if call.callee in ("bytes_from_str", "str_from_bytes"):
            # Both are noops at the IR level: the value carries the
            # same {len, ptr} layout, only the sema type changes.
            return self._emit_expr(call.args[0], ctx)
        if call.callee in ("chan_new", "chan_send", "chan_recv", "chan_close"):
            return self._emit_chan_builtin(call, ctx)
        if call.callee in ("await_all", "await_any"):
            return self._emit_await_many(call, ctx)
        if call.callee in ("dict_new", "dict_get", "dict_has", "dict_remove", "dict_keys", "dict_values"):
            return self._emit_dict_builtin(call, ctx)
        if call.callee == "os_args":
            out = ctx.alloca(RW_LIST_INT_TY)
            ctx.builder.call(self._rw_os_args, [out])
            return ctx.builder.load(out)
        if call.callee == "os_getenv":
            out = ctx.alloca(self._opt_str_ty)
            ctx.builder.call(self._rw_os_getenv, [out, self._emit_expr(call.args[0], ctx)])
            return ctx.builder.load(out)
        if call.callee == "str_split":
            sv, sep = (self._emit_expr(a, ctx) for a in call.args)
            out_slot = ctx.alloca(RW_LIST_INT_TY)
            ctx.builder.call(self._rw_str_split, [out_slot, sv, sep])
            return ctx.builder.load(out_slot)
        if call.callee == "str_join":
            lv = self._emit_expr(call.args[0], ctx)
            sep = self._emit_expr(call.args[1], ctx)
            in_slot = ctx.alloca(RW_LIST_INT_TY)
            ctx.builder.store(lv, in_slot)
            return ctx.builder.call(self._rw_str_join, [in_slot, sep])
        direct = {
            "str_find": self._rw_str_find,
            "str_slice": self._rw_str_slice,
            "str_starts_with": self._rw_str_starts_with,
            "str_from_int": self._rw_str_from_int,
            "str_strip": self._rw_str_strip,
            "str_lower": self._rw_str_lower,
            "str_upper": self._rw_str_upper,
            "str_replace": self._rw_str_replace,
            "str_contains": self._rw_str_contains,
            "str_ends_with": self._rw_str_ends_with,
            "sleep": self._rw_sleep_ms,
            "time_ms": self._rw_time_ms,
            "mono_ns": self._rw_mono_ns,
            "rand_int": self._rw_rand_int,
            "rand_float": self._rw_rand_float,
            "rand_seed": self._rw_rand_seed,
            "rand_bytes": self._rw_rand_bytes,
            "mutex_new": self._rw_mutex_new,
            "mutex_lock": self._rw_mutex_lock,
            "mutex_unlock": self._rw_mutex_unlock,
            "mutex_try_lock": self._rw_mutex_try_lock,
            "os_setenv": self._rw_os_setenv,
            "os_exit": self._rw_os_exit,
            "os_getcwd": self._rw_os_getcwd,
            "os_chdir": self._rw_os_chdir,
            "dial": self._rw_dial,
            "listen": self._rw_listen,
            "accept": self._rw_accept,
            "open": self._rw_open,
            "local_addr": self._rw_local_addr,
            "peer_addr": self._rw_peer_addr,
            "last_error": self._rw_last_error,
            "write_all": self._rw_write_all,
            "dial_timeout": self._rw_dial_timeout,
            "set_read_timeout": self._rw_set_read_timeout,
            "set_write_timeout": self._rw_set_write_timeout,
            "error_name": self._rw_error_name,
            "error_message": self._rw_error_message,
        }
        if call.callee in direct:
            fn = direct[call.callee]
            args = [self._emit_expr(a, ctx) for a in call.args]
            return ctx.builder.call(fn, args)
        if call.callee == "str_to_int":
            sv = self._emit_expr(call.args[0], ctx)
            out_slot = ctx.alloca(RW_OPTION_INT_TY)
            ctx.builder.call(self._rw_str_to_int, [out_slot, sv])
            return ctx.builder.load(out_slot)
        if call.callee == "list_new" and self.sema.expr_types[id(call)] is not T.LIST_INT:
            slot = ctx.alloca(RW_LIST_INT_TY)
            ctx.builder.call(self._rw_list_new, [slot])
            return ctx.builder.load(slot)
        if call.callee in ("list_push", "list_at") and self.sema.expr_types[id(call.args[0])] is not T.LIST_INT:
            return self._emit_generic_list_op(call, ctx)
        if call.callee == "list_new":
            # rw_list_int_new(rw_list_int *out) writes to a stack slot;
            # we then load the value back out as a SSA struct.
            slot = ctx.alloca(RW_LIST_INT_TY)
            ctx.builder.call(self._rw_list_int_new, [slot])
            return ctx.builder.load(slot)
        if call.callee == "list_push":
            lv = self._emit_expr(call.args[0], ctx)
            vv = self._emit_expr(call.args[1], ctx)
            # Copy the input list onto the stack, call push with an
            # output slot, then load the new list back.
            in_slot = ctx.alloca(RW_LIST_INT_TY)
            ctx.builder.store(lv, in_slot)
            out_slot = ctx.alloca(RW_LIST_INT_TY)
            ctx.builder.call(self._rw_list_int_push, [out_slot, in_slot, vv])
            return ctx.builder.load(out_slot)
        if call.callee == "list_at":
            lv = self._emit_expr(call.args[0], ctx)
            iv = self._emit_expr(call.args[1], ctx)
            slot = ctx.alloca(RW_LIST_INT_TY)
            ctx.builder.store(lv, slot)
            return ctx.builder.call(self._rw_list_int_at, [slot, iv])
        if call.callee == "list_at_opt":
            lv = self._emit_expr(call.args[0], ctx)
            iv = self._emit_expr(call.args[1], ctx)
            in_slot = ctx.alloca(RW_LIST_INT_TY)
            ctx.builder.store(lv, in_slot)
            out_slot = ctx.alloca(RW_OPTION_INT_TY)
            ctx.builder.call(self._rw_list_int_at_opt, [out_slot, in_slot, iv])
            return ctx.builder.load(out_slot)
        if call.callee == "tcp_listen":
            v = self._emit_expr(call.args[0], ctx)
            return ctx.builder.call(self._rw_tcp_listen, [v])
        if call.callee == "tcp_accept":
            v = self._emit_expr(call.args[0], ctx)
            return ctx.builder.call(self._rw_tcp_accept, [v])
        if call.callee == "read":
            fd_v = self._emit_expr(call.args[0], ctx)
            mx_v = self._emit_expr(call.args[1], ctx)
            out_slot = ctx.alloca(RW_STR_TY)
            ctx.builder.call(self._rw_read, [out_slot, fd_v, mx_v])
            return ctx.builder.load(out_slot)
        if call.callee == "write":
            fd_v = self._emit_expr(call.args[0], ctx)
            b_v  = self._emit_expr(call.args[1], ctx)
            return ctx.builder.call(self._rw_write, [fd_v, b_v])
        if call.callee == "close":
            v = self._emit_expr(call.args[0], ctx)
            return ctx.builder.call(self._rw_close, [v])
        if call.callee == "file_open":
            path_v = self._emit_expr(call.args[0], ctx)
            mode_v = self._emit_expr(call.args[1], ctx)
            return ctx.builder.call(self._rw_file_open, [path_v, mode_v])
        # math intrinsics
        _math_unary = {
            "sqrt": "_llvm_sqrt", "floor": "_llvm_floor", "ceil": "_llvm_ceil",
            "exp": "_llvm_exp", "log": "_llvm_log", "sin": "_llvm_sin",
            "cos": "_llvm_cos", "fabs": "_llvm_fabs",
        }
        if call.callee in _math_unary:
            v = self._emit_expr(call.args[0], ctx)
            intr = getattr(self, _math_unary[call.callee])
            return ctx.builder.call(intr, [v])
        if call.callee == "pow":
            base_v = self._emit_expr(call.args[0], ctx)
            exp_v = self._emit_expr(call.args[1], ctx)
            return ctx.builder.call(self._llvm_pow, [base_v, exp_v])
        # Should be unreachable: sema records every user call in call_resolution
        # and rejects unknown names.
        raise RuntimeError(f"irgen: unresolved call to `{call.callee}`")

    # ---------- spawn / await ----------
    def _emit_spawn(self, expr: A.SpawnExpr, ctx: "FunctionCtx") -> ir.Value:
        """Generate a closure struct + trampoline + rw_spawn_* call."""
        b = ctx.builder
        call = expr.call
        key = self.sema.call_resolution[id(call)]
        sig = self.sema.functions[key]
        ret_ty = sig.return_type

        # 1. Build a closure struct type with the LLVM types of each argument.
        arg_llvm_types = [llvm_type_of(pt) for _, pt in sig.params]
        closure_ty = ir.LiteralStructType(arg_llvm_types) if arg_llvm_types else ir.LiteralStructType([])
        closure_ptr_ty = closure_ty.as_pointer()

        # 2. Evaluate the call args in the *caller* and pack them into a heap struct.
        evaluated = [self._emit_expr(a, ctx) for a in call.args]
        # malloc enough bytes
        # llvmlite has no portable sizeof, so use gep on null trick.
        null = ir.Constant(closure_ptr_ty, None)
        size_ptr = b.gep(null, [ir.Constant(I32, 1)])
        size_i64 = b.ptrtoint(size_ptr, I64)
        # When the closure has no fields, sizeof is 0; malloc(0) is implementation-defined.
        # Guard by passing at least 1 byte.
        one = ir.Constant(I64, 1)
        ugt = b.icmp_unsigned(">", size_i64, ir.Constant(I64, 0))
        size_to_alloc = b.select(ugt, size_i64, one)
        raw = b.call(self._malloc, [size_to_alloc])
        closure = b.bitcast(raw, closure_ptr_ty)
        for i, v in enumerate(evaluated):
            field_ptr = b.gep(closure, [ir.Constant(I32, 0), ir.Constant(I32, i)])
            b.store(v, field_ptr)

        # 3. Generate (or reuse) a trampoline for this callee.
        tramp = self._get_or_make_trampoline(key, sig)

        # 4. Call rw_spawn_<retty>(tramp, raw)
        spawn_fn = self._decl_spawn(ret_ty)
        # tramp's type must match the spawn argument; bitcast if needed.
        tramp_param_ty = spawn_fn.function_type.args[0]
        tramp_cast = b.bitcast(tramp, tramp_param_ty)
        fut = b.call(spawn_fn, [tramp_cast, raw])
        return fut  # i8*

    def _emit_await(self, expr: A.AwaitExpr, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        fut = self._emit_expr(expr.target, ctx)
        target_ty = self.sema.expr_types[id(expr.target)]
        assert isinstance(target_ty, T.FutureType)
        await_fn = self._decl_await(target_ty.inner)
        return b.call(await_fn, [fut])

    def _get_or_make_trampoline(self, key: FuncKey, sig) -> ir.Function:
        # Each callee gets exactly one trampoline reused at every spawn site.
        module, callee = key
        name = f"rw_trampoline_{callee}" if module is None else f"rw_trampoline_{module}.{callee}"
        if name in self.mod.globals:
            return self.mod.get_global(name)
        ret_ty = sig.return_type
        arg_llvm_types = [llvm_type_of(pt) for _, pt in sig.params]
        closure_ty = ir.LiteralStructType(arg_llvm_types) if arg_llvm_types else ir.LiteralStructType([])
        closure_ptr_ty = closure_ty.as_pointer()
        ret_llvm = llvm_type_of(ret_ty)
        fty = ir.FunctionType(ret_llvm, [I8P])
        fn = ir.Function(self.mod, fty, name)
        bb = fn.append_basic_block("entry")
        b = ir.IRBuilder(bb)
        closure = b.bitcast(fn.args[0], closure_ptr_ty)
        # Load each field
        loaded = []
        for i, _ in enumerate(arg_llvm_types):
            field_ptr = b.gep(closure, [ir.Constant(I32, 0), ir.Constant(I32, i)])
            loaded.append(b.load(field_ptr))
        result = b.call(self.funcs[key], loaded)
        # Free the closure memory.
        b.call(self._free, [fn.args[0]])
        if ret_ty is T.VOID:
            b.ret_void()
        else:
            b.ret(result)
        return fn

    # ---------- literals ----------
    def _panic_if(self, cond: ir.Value, message: str, ctx: "FunctionCtx") -> None:
        """Branch to a block that calls rw_panic(message) when cond holds."""
        b = ctx.builder
        fail = ctx.function.append_basic_block("check.fail")
        ok = ctx.function.append_basic_block("check.ok")
        b.cbranch(cond, fail, ok)
        b.position_at_end(fail)
        b.call(self._rw_panic, [self._emit_string_literal(message, b)])
        b.unreachable()
        b.position_at_end(ok)

    def _checked_int_div(self, op: str, l: ir.Value, r: ir.Value, expr: A.BinOp,
                         ctx: "FunctionCtx") -> ir.Value:
        """Integer `/` and `%` without LLVM undefined behaviour (spec 42):
        dividing by zero panics, INT64_MIN / -1 panics (the quotient does not
        fit), and INT64_MIN % -1 is 0."""
        b = ctx.builder
        where = f"in {ctx.func_key[1]} (line {expr.line})"
        self._panic_if(b.icmp_signed("==", r, ir.Constant(I64, 0)),
                       f"division by zero {where}", ctx)
        minus_one = b.icmp_signed("==", r, ir.Constant(I64, -1))
        if op == "/":
            overflow = b.and_(b.icmp_signed("==", l, ir.Constant(I64, -(2 ** 63))), minus_one)
            self._panic_if(overflow, f"integer overflow in division {where}", ctx)
            return b.sdiv(l, r)
        # x % -1 == 0 for every x; avoid srem's INT64_MIN % -1 trap.
        safe_r = b.select(minus_one, ir.Constant(I64, 1), r)
        return b.select(minus_one, ir.Constant(I64, 0), b.srem(l, safe_r))

    def _emit_string_literal(self, s: str, b: ir.IRBuilder) -> ir.Value:
        data = s.encode("utf-8")
        arr_ty = ir.ArrayType(I8, len(data))
        name = f".str.{self._string_const_counter}"
        self._string_const_counter += 1
        g = ir.GlobalVariable(self.mod, arr_ty, name=name)
        g.global_constant = True
        g.linkage = "private"
        g.initializer = ir.Constant(arr_ty, bytearray(data))
        # Bitcast to i8* and construct {i64, i8*}
        ptr = b.bitcast(g, I8P)
        # Build constant {len, ptr} as a struct value.
        agg = ir.Constant(RW_STR_TY, [ir.Constant(I64, len(data)), ir.Constant(I8P, None)])
        # We can't put a non-constant pointer into a constant aggregate at module-level
        # use insertvalue at runtime.
        agg = b.insert_value(agg, ir.Constant(I64, len(data)), 0)
        agg = b.insert_value(agg, ptr, 1)
        return agg


class FunctionCtx:
    def __init__(
        self,
        builder: ir.IRBuilder,
        function: ir.Function,
        locals: Dict[str, ir.AllocaInstr],
        return_ty: T.Type,
        gen: "IRGen",
        func_key: FuncKey,
    ) -> None:
        self.builder = builder
        self.function = function
        self.locals = locals
        self.return_ty = return_ty
        self.gen = gen
        # (module, name) of the function being emitted. Used to key local_types.
        self.func_key = func_key
        # Stack of enclosing loops as (cond_bb, end_bb); continue -> cond_bb,
        # break -> end_bb. Pushed/popped around each While body emission.
        self.loop_stack: list = []
        # Pending cleanups (spec 47), innermost last: ("defer", stmt, depth)
        # or ("with", (value, type), depth), where depth is len(loop_stack)
        # when pushed. Emitted before return / break / continue leaves them.
        self.cleanups: list = []

    def alloca(self, ty: ir.Type, name: str = "") -> ir.AllocaInstr:
        """Allocate a stack slot at the start of the entry block. An alloca
        emitted where a local is declared would run on every loop iteration
        and grow the (64 KiB) fiber stack without bound."""
        entry = self.function.entry_basic_block
        ab = ir.IRBuilder(entry)
        ab.position_at_start(entry)
        return ab.alloca(ty, name=name)


def generate(program: LoadedProgram, sema: SemaResult, checked: bool = True) -> ir.Module:
    return IRGen(program, sema, checked).generate()
