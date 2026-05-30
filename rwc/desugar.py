"""Desugaring pass: lower syntactic-sugar AST nodes to core nodes.

Runs after parsing and before sema. Currently lowers `For` (range-based
loops) into `VarDecl` + `While` + `Assign` using only core AST nodes, so
sema and irgen need no knowledge of `For`.
"""

from __future__ import annotations

from typing import List

from . import ast_nodes as A


class _Desugarer:
    def __init__(self) -> None:
        self._tmp_counter = 0

    def _fresh(self, base: str) -> str:
        n = self._tmp_counter
        self._tmp_counter += 1
        return f"__for_{base}_{n}"

    def module(self, mod: A.Module) -> A.Module:
        for fn in mod.functions:
            fn.body = self._block(fn.body)
        return mod

    def _block(self, stmts: List[A.Stmt]) -> List[A.Stmt]:
        out: List[A.Stmt] = []
        for s in stmts:
            out.extend(self._stmt(s))
        return out

    def _stmt(self, s: A.Stmt) -> List[A.Stmt]:
        if isinstance(s, A.For):
            return self._lower_for(s)
        if isinstance(s, A.If):
            s.then_body = self._block(s.then_body)
            s.else_body = self._block(s.else_body)
            return [s]
        if isinstance(s, A.While):
            s.body = self._block(s.body)
            return [s]
        if isinstance(s, A.MatchStmt):
            if s.some_block is not None:
                s.some_block = self._block(s.some_block)
            if s.none_block is not None:
                s.none_block = self._block(s.none_block)
            if s.ok_block is not None:
                s.ok_block = self._block(s.ok_block)
            if s.err_block is not None:
                s.err_block = self._block(s.err_block)
            return [s]
        return [s]

    def _int_type(self, ln: int, col: int) -> A.TypeName:
        return A.TypeName("int", ln, col)

    def _lower_for(self, f: A.For) -> List[A.Stmt]:
        ln, col = f.line, f.col
        stop_name = self._fresh("stop")
        step_name = self._fresh("step")

        # Recursively desugar the body first (nested fors).
        body = self._block(f.body)

        out: List[A.Stmt] = []
        # __stop = <stop>; __step = <step>; <var> = <start>
        out.append(A.VarDecl(stop_name, self._int_type(ln, col), f.stop, ln, col))
        out.append(A.VarDecl(step_name, self._int_type(ln, col), f.step, ln, col))
        out.append(A.VarDecl(f.var, self._int_type(ln, col), f.start, ln, col))

        step_pos = A.BinOp(">", A.Name(step_name, ln, col), A.IntLit(0, ln, col), ln, col)
        step_neg = A.BinOp("<", A.Name(step_name, ln, col), A.IntLit(0, ln, col), ln, col)
        lt = A.BinOp("<", A.Name(f.var, ln, col), A.Name(stop_name, ln, col), ln, col)
        gt = A.BinOp(">", A.Name(f.var, ln, col), A.Name(stop_name, ln, col), ln, col)
        asc = A.BinOp("and", step_pos, lt, ln, col)
        desc = A.BinOp("and", step_neg, gt, ln, col)
        cond = A.BinOp("or", asc, desc, ln, col)

        # body + (var = var + __step)
        incr = A.Assign(
            f.var,
            A.BinOp("+", A.Name(f.var, ln, col), A.Name(step_name, ln, col), ln, col),
            ln, col,
        )
        while_body = list(body) + [incr]
        out.append(A.While(cond, while_body, ln, col))
        return out


def desugar_module(mod: A.Module) -> A.Module:
    return _Desugarer().module(mod)
