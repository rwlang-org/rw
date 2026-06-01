"""LLVM IR generation using llvmlite.ir.

Generates a module for a checked rw program. User function `main` is renamed
to `@rw_user_main` and a real C `@main` is synthesized that calls
`rw_init` / `rw_user_main` / `rw_shutdown` and returns the i32-truncated result.

This file covers the *synchronous* subset (functions, literals, binops,
if/while, print, calls). spawn/await are handled in the same file but
delegated to a dedicated routine in Step 8.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from llvmlite import ir

from . import ast_nodes as A
from . import types as T
from .sema import SemaResult


# ---------- LLVM type helpers ----------

I8 = ir.IntType(8)
I32 = ir.IntType(32)
I64 = ir.IntType(64)
F64 = ir.DoubleType()
I8P = I8.as_pointer()
RW_STR_TY = ir.LiteralStructType([I64, I8P])  # { i64 len, i8* ptr }
RW_LIST_INT_TY = ir.LiteralStructType([I64, I64, I64.as_pointer()])  # {len, cap, data*}
RW_OPTION_INT_TY = ir.LiteralStructType([I64, I64])  # {tag, payload}
RW_RESULT_INT_INT_TY = ir.LiteralStructType([I64, I64])  # {tag, payload} (named alias)


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
    if t is T.LIST_INT:
        return RW_LIST_INT_TY
    if t is T.OPTION_INT:
        return RW_OPTION_INT_TY
    if t is T.RESULT_INT_INT:
        return RW_RESULT_INT_INT_TY
    if t is T.VOID:
        return ir.VoidType()
    if isinstance(t, T.FutureType):
        return I8P  # opaque pointer
    raise RuntimeError(f"no LLVM mapping for type {t}")


# ---------- IR generator ----------

class IRGen:
    def __init__(self, module: A.Module, sema: SemaResult) -> None:
        self.ast_module = module
        self.sema = sema
        self.mod = ir.Module(name=sema.filename)
        # Use a generic target triple; the driver picks the host triple at codegen.
        self.mod.triple = ""
        self._string_const_counter = 0
        self._trampoline_counter = 0
        # Map user function name -> ir.Function (LLVM symbol may be renamed).
        self.funcs: Dict[str, ir.Function] = {}
        self._declare_runtime()

    # ---------- runtime declarations ----------
    def _declare_runtime(self) -> None:
        m = self.mod
        # print
        self._rw_print_i64 = ir.Function(m, ir.FunctionType(ir.VoidType(), [I64]), "rw_print_i64")
        self._rw_print_f64 = ir.Function(m, ir.FunctionType(ir.VoidType(), [F64]), "rw_print_f64")
        self._rw_print_bool = ir.Function(m, ir.FunctionType(ir.VoidType(), [I8]), "rw_print_bool")
        self._rw_print_str = ir.Function(m, ir.FunctionType(ir.VoidType(), [RW_STR_TY]), "rw_print_str")
        # string ops
        self._rw_str_len = ir.Function(
            m, ir.FunctionType(I64, [RW_STR_TY]), "rw_str_len")
        self._rw_str_eq = ir.Function(
            m, ir.FunctionType(I8, [RW_STR_TY, RW_STR_TY]), "rw_str_eq")
        self._rw_str_concat = ir.Function(
            m, ir.FunctionType(RW_STR_TY, [RW_STR_TY, RW_STR_TY]), "rw_str_concat")
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
        # lifecycle
        self._rw_init = ir.Function(m, ir.FunctionType(ir.VoidType(), []), "rw_init")
        self._rw_shutdown = ir.Function(m, ir.FunctionType(ir.VoidType(), []), "rw_shutdown")
        # malloc/free
        self._malloc = ir.Function(m, ir.FunctionType(I8P, [I64]), "malloc")
        self._free = ir.Function(m, ir.FunctionType(ir.VoidType(), [I8P]), "free")
        # spawn / await: declared lazily as needed
        self._spawn_funcs: Dict[str, ir.Function] = {}
        self._await_funcs: Dict[str, ir.Function] = {}

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
        # First pass: declare LLVM function symbols.
        for ast_fn in self.ast_module.functions:
            sig = self.sema.functions[ast_fn.name]
            param_tys = [llvm_type_of(pt) for _, pt in sig.params]
            ret_ll = llvm_type_of(sig.return_type)
            fty = ir.FunctionType(ret_ll, param_tys)
            # Rename user main to rw_user_main, others keep their names with rw_user_ prefix
            # to avoid colliding with libc/runtime symbols.
            sym = f"rw_user_{ast_fn.name}"
            fn = ir.Function(self.mod, fty, sym)
            for i, (pname, _) in enumerate(sig.params):
                fn.args[i].name = pname
            self.funcs[ast_fn.name] = fn

        # Second pass: emit bodies.
        for ast_fn in self.ast_module.functions:
            self._emit_function(ast_fn)

        # Synthesize the real C @main.
        self._emit_c_main()
        return self.mod

    # ---------- C main shim ----------
    def _emit_c_main(self) -> None:
        fty = ir.FunctionType(I32, [])
        main = ir.Function(self.mod, fty, "main")
        bb = main.append_basic_block("entry")
        b = ir.IRBuilder(bb)
        b.call(self._rw_init, [])
        r = b.call(self.funcs["main"], [])
        b.call(self._rw_shutdown, [])
        r32 = b.trunc(r, I32)
        b.ret(r32)

    # ---------- per-function ----------
    def _emit_function(self, ast_fn: A.FuncDef) -> None:
        fn = self.funcs[ast_fn.name]
        entry = fn.append_basic_block("entry")
        b = ir.IRBuilder(entry)
        # Allocate locals for each parameter; copy in.
        locals_: Dict[str, ir.AllocaInstr] = {}
        sig = self.sema.functions[ast_fn.name]
        for (pname, pty), llvm_arg in zip(sig.params, fn.args):
            slot = b.alloca(llvm_type_of(pty), name=pname)
            b.store(llvm_arg, slot)
            locals_[pname] = slot

        ctx = FunctionCtx(builder=b, function=fn, locals=locals_, return_ty=sig.return_type, gen=self)
        self._emit_block(ast_fn.body, ctx)

        # If the block didn't terminate, add a synthetic terminator (for void) or unreachable.
        if not ctx.builder.block.is_terminated:
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
            ty = self.sema.local_types[(ctx.function_name, stmt.name)]
            slot = b.alloca(llvm_type_of(ty), name=stmt.name)
            val = self._emit_expr(stmt.value, ctx)
            b.store(val, slot)
            ctx.locals[stmt.name] = slot
            return
        if isinstance(stmt, A.Assign):
            slot = ctx.locals[stmt.name]
            val = self._emit_expr(stmt.value, ctx)
            b.store(val, slot)
            return
        if isinstance(stmt, A.ExprStmt):
            self._emit_expr(stmt.expr, ctx)
            return
        if isinstance(stmt, A.Return):
            if stmt.value is None:
                b.ret_void()
            else:
                v = self._emit_expr(stmt.value, ctx)
                b.ret(v)
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
            end_bb = ctx.function.append_basic_block("while.end")
            b.branch(cond_bb)
            b.position_at_end(cond_bb)
            cond_i8 = self._emit_expr(stmt.cond, ctx)
            cond_i1 = b.icmp_unsigned("!=", cond_i8, ir.Constant(I8, 0))
            b.cbranch(cond_i1, body_bb, end_bb)
            b.position_at_end(body_bb)
            self._emit_block(stmt.body, ctx)
            if not b.block.is_terminated:
                b.branch(cond_bb)
            b.position_at_end(end_bb)
            return
        if isinstance(stmt, A.MatchStmt):
            v = self._emit_expr(stmt.target, ctx)
            tag = b.extract_value(v, 0)
            payload = b.extract_value(v, 1)
            arm1_bb = ctx.function.append_basic_block("match.arm1")
            arm0_bb = ctx.function.append_basic_block("match.arm0")
            end_bb = ctx.function.append_basic_block("match.end")
            # tag == 1 -> arm1, default (tag == 0) -> arm0.
            sw = b.switch(tag, arm0_bb)
            sw.add_case(ir.Constant(I64, 1), arm1_bb)
            if stmt.style == "option":
                var1, block1 = stmt.some_var, stmt.some_block
                var0, block0 = None, stmt.none_block
            else:  # "result"
                var1, block1 = stmt.ok_var, stmt.ok_block
                var0, block0 = stmt.err_var, stmt.err_block
            # arm1 (tag == 1)
            b.position_at_end(arm1_bb)
            self._emit_arm(var1, block1, payload, ctx, end_bb)
            # arm0 (tag == 0)
            b.position_at_end(arm0_bb)
            self._emit_arm(var0, block0, payload, ctx, end_bb)
            b.position_at_end(end_bb)
            return
        raise RuntimeError(f"unsupported stmt: {type(stmt).__name__}")

    def _emit_arm(self, var_name, block, payload, ctx, end_bb):
        """Emit one match arm. var_name=None means no payload binding."""
        b = ctx.builder
        if var_name is not None:
            slot = b.alloca(I64, name=var_name)
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
    def _emit_expr(self, expr: A.Expr, ctx: "FunctionCtx") -> ir.Value:
        b = ctx.builder
        if isinstance(expr, A.IntLit):
            return ir.Constant(I64, expr.value)
        if isinstance(expr, A.FloatLit):
            return ir.Constant(F64, expr.value)
        if isinstance(expr, A.BoolLit):
            return ir.Constant(I8, 1 if expr.value else 0)
        if isinstance(expr, A.StringLit):
            return self._emit_string_literal(expr.value, b)
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
            raise RuntimeError(f"bad unary {expr.op}")
        if isinstance(expr, A.BinOp):
            return self._emit_binop(expr, ctx)
        if isinstance(expr, A.Call):
            return self._emit_call(expr, ctx)
        if isinstance(expr, A.SpawnExpr):
            return self._emit_spawn(expr, ctx)
        if isinstance(expr, A.AwaitExpr):
            return self._emit_await(expr, ctx)
        if isinstance(expr, A.SomeExpr):
            v = self._emit_expr(expr.arg, ctx)
            base = ir.Constant(RW_OPTION_INT_TY,
                               [ir.Constant(I64, 1), ir.Constant(I64, 0)])
            return ctx.builder.insert_value(base, v, 1)
        if isinstance(expr, A.NoneExpr):
            return ir.Constant(RW_OPTION_INT_TY,
                               [ir.Constant(I64, 0), ir.Constant(I64, 0)])
        if isinstance(expr, A.OkExpr):
            v = self._emit_expr(expr.arg, ctx)
            base = ir.Constant(RW_RESULT_INT_INT_TY,
                               [ir.Constant(I64, 1), ir.Constant(I64, 0)])
            return ctx.builder.insert_value(base, v, 1)
        if isinstance(expr, A.ErrExpr):
            v = self._emit_expr(expr.arg, ctx)
            base = ir.Constant(RW_RESULT_INT_INT_TY,
                               [ir.Constant(I64, 0), ir.Constant(I64, 0)])
            return ctx.builder.insert_value(base, v, 1)
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
            elif is_strlike and op in ("==", "!="):
                eq_i8 = b.call(self._rw_str_eq, [l, r])
                i1 = b.icmp_unsigned("!=", eq_i8, ir.Constant(I8, 0))
                if op == "!=":
                    i1 = b.xor(i1, ir.Constant(ir.IntType(1), 1))
            else:
                raise RuntimeError(f"cmp op {op} on {lty}")
            return b.zext(i1, I8)

        raise RuntimeError(f"unknown binop {op}")

    def _emit_call(self, call: A.Call, ctx: "FunctionCtx") -> ir.Value:
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
            if aty is T.LIST_INT:
                # rw_list_int_len takes a pointer to a stack copy.
                slot = ctx.builder.alloca(RW_LIST_INT_TY)
                ctx.builder.store(v, slot)
                return ctx.builder.call(self._rw_list_int_len, [slot])
            return ctx.builder.call(self._rw_str_len, [v])
        if call.callee in ("bytes_from_str", "str_from_bytes"):
            # Both are noops at the IR level: the value carries the
            # same {len, ptr} layout, only the sema type changes.
            return self._emit_expr(call.args[0], ctx)
        if call.callee == "list_new":
            # rw_list_int_new(rw_list_int *out) writes to a stack slot;
            # we then load the value back out as a SSA struct.
            slot = ctx.builder.alloca(RW_LIST_INT_TY)
            ctx.builder.call(self._rw_list_int_new, [slot])
            return ctx.builder.load(slot)
        if call.callee == "list_push":
            lv = self._emit_expr(call.args[0], ctx)
            vv = self._emit_expr(call.args[1], ctx)
            # Copy the input list onto the stack, call push with an
            # output slot, then load the new list back.
            in_slot = ctx.builder.alloca(RW_LIST_INT_TY)
            ctx.builder.store(lv, in_slot)
            out_slot = ctx.builder.alloca(RW_LIST_INT_TY)
            ctx.builder.call(self._rw_list_int_push, [out_slot, in_slot, vv])
            return ctx.builder.load(out_slot)
        if call.callee == "list_at":
            lv = self._emit_expr(call.args[0], ctx)
            iv = self._emit_expr(call.args[1], ctx)
            slot = ctx.builder.alloca(RW_LIST_INT_TY)
            ctx.builder.store(lv, slot)
            return ctx.builder.call(self._rw_list_int_at, [slot, iv])
        if call.callee == "list_at_opt":
            lv = self._emit_expr(call.args[0], ctx)
            iv = self._emit_expr(call.args[1], ctx)
            in_slot = ctx.builder.alloca(RW_LIST_INT_TY)
            ctx.builder.store(lv, in_slot)
            out_slot = ctx.builder.alloca(RW_OPTION_INT_TY)
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
            out_slot = ctx.builder.alloca(RW_STR_TY)
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
        fn = self.funcs[call.callee]
        args = [self._emit_expr(a, ctx) for a in call.args]
        return ctx.builder.call(fn, args)

    # ---------- spawn / await ----------
    def _emit_spawn(self, expr: A.SpawnExpr, ctx: "FunctionCtx") -> ir.Value:
        """Generate a closure struct + trampoline + rw_spawn_* call."""
        b = ctx.builder
        call = expr.call
        target_fn = self.funcs[call.callee]
        sig = self.sema.functions[call.callee]
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
        tramp = self._get_or_make_trampoline(call.callee, sig)

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

    def _get_or_make_trampoline(self, callee: str, sig) -> ir.Function:
        # Each callee gets exactly one trampoline reused at every spawn site.
        name = f"rw_trampoline_{callee}"
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
        result = b.call(self.funcs[callee], loaded)
        # Free the closure memory.
        b.call(self._free, [fn.args[0]])
        if ret_ty is T.VOID:
            b.ret_void()
        else:
            b.ret(result)
        return fn

    # ---------- literals ----------
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
    ) -> None:
        self.builder = builder
        self.function = function
        self.locals = locals
        self.return_ty = return_ty
        self.gen = gen
        self.function_name = function.name.removeprefix("rw_user_")


def generate(module: A.Module, sema: SemaResult) -> ir.Module:
    return IRGen(module, sema).generate()
