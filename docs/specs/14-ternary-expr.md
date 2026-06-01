# rw 条件式 / 三項演算子 (`then if cond else els`)

## Context

rw はコンパイラ・ランタイム・サンプル・テストが一体で育つ小さな言語で、型
(string / Bytes / List[int] / Option / Result) と並行・I/O、そして制御フロー
(`if` / `while` / `for ... in range`) を 1 サブプロジェクト 1 PR の粒度で
積み上げてきた。

`for ... in range` の spec ([[13-for-range-loop]]) で示した制御フロー ロード
マップの 4 番目「`if` を式として使う / 三項相当」に対応するのがこのサブ
プロジェクトである。これまで「条件で値を選ぶ」には文の `if`/`else` で一旦
変数に代入する必要があった:

```rw
larger: int = 0
if a > b:
    larger = a
else:
    larger = b
```

このサブプロジェクトは Python 互換の条件式 `then if cond else els` を導入し、
上記を 1 行の式で書けるようにする:

```rw
larger: int = a if a > b else b
```

設計の核心は、**短絡 `and` / `or` が既に使っている cbranch + phi の IR
パターンをそのまま流用する**こと。新ノードは式 1 つ (`IfExpr`) のみで、
desugar も lexer 変更も不要。変更は parser / sema / irgen + テスト・例題に
閉じる。

## Goals

- 新しい式 `then if cond else els` を導入 (Python の三項演算子と同一構文)
- 三項演算子は **最も低い優先度**。`x: int = 1 if c else 2` のように代入の
  右辺・関数引数・`return` 値など式が来る任意の位置で使える
- `cond` は `bool`、`then` / `els` は **同じ型**であることを要求し、その型を
  式全体の型とする
- ネストは **右結合**: `a if p else b if q else c` は
  `a if p else (b if q else c)` と解釈する
- 評価は短絡的: 選ばれたブランチのみを評価する (cbranch により他方は実行
  されない)

## Non-Goals

- **if 式 / block 式** (式志向構文, #109 RFC)。文ブロックを式として値を返す
  仕組みは別の話で、ここでは扱わない
- **C 風の三項記号構文** `cond ? a : b`。Python 互換の `a if c else b` のみ
- **ブランチ間の暗黙の型昇格** (`int` ↔ `float` など)。両ブランチは同型必須。
  混在は sema で型エラー
- **ネストの定数畳み込みや条件簡約**。素直に cbranch + phi で生成し、最適化は
  LLVM (および将来の最適化レベル #55) に委ねる

## 構文

```
expr     := ternary
ternary  := or_expr [ "if" or_expr "else" ternary ]   # 右結合
```

- `if` / `else` は lexer に予約済み (`KW_IF` / `KW_ELSE`)。**lexer 変更は不要**
- `then` 側と `cond` 側は `or_expr` (三項より高い優先度) を読み、`else` 側は
  再び `ternary` を読むため右結合になる
- 三項演算子は `parse_expr` と `parse_or` の間の `parse_ternary` 層で処理する

### 文の `if` との非衝突

文の `if` (`if cond:` で始まる複文) と式の `if` は次の理由で衝突しない:

- 文の `if` は `parse_stmt` の **行頭ディスパッチ**でのみ分岐する
- 式の `if` は `parse_ternary` 内、つまり **式を読み始めた後**にのみ現れる

したがって `x: int = 1 if c else 2` の `if` は式として、行頭の `if c:` は文と
して、それぞれ曖昧なく解釈される。

### 使用例

```rw
def classify(n: int) -> int:
    # ネストした条件式 (右結合)
    return 1 if n > 0 else 0 if n == 0 else -1

def main() -> int:
    a: int = 10
    b: int = 20
    larger: int = a if a > b else b            # int ブランチ
    label: string = "even" if a % 2 == 0 else "odd"  # string ブランチ
    ok: bool = true if larger == 20 else false       # bool ブランチ
    print(larger)            # 20
    print(label)             # even
    print(ok)                # true
    print(classify(-3))      # -1
    return 0
```

## 型付け (sema)

`_infer_expr` の `IfExpr` 分岐で以下を検査する:

1. `cond` を型推論し、`bool` でなければ型エラー
   (`conditional expression requires bool condition, found ...`)
2. `then` / `els` を型推論し、両者が**不一致**なら型エラー
   (`conditional expression branches must have the same type, found ... and ...`)
3. 一致した型を式全体の型として返す。`_check_expr` 経由で
   `expr_types[id(expr)]` に登録され、irgen が phi の型決定に使う

## IR 生成 (irgen)

`IfExpr` を、短絡 `and` / `or` と同形の **cbranch + phi** に lower する:

```
  cond_i1 = (cond != 0)            ; bool は i8、!=0 で i1 化
  br i1 cond_i1, label tern.then, label tern.else
tern.then:
  <then を評価>
  br label tern.end
tern.else:
  <els を評価>
  br label tern.end
tern.end:
  %r = phi <ty> [ then_val, tern.then ], [ else_val, tern.else ]
```

- phi の型は `llvm_type_of(sema.expr_types[id(expr)])` から引く。`and` / `or`
  は結果が常に `i8` で固定だが、三項は int / float / bool / string いずれも
  取り得るため、sema が推論した型を使う点だけが異なる
- 選ばれたブランチのみが実行される (cbranch による短絡)

## 触るレイヤー

| レイヤー | ファイル | 変更 |
|---|---|---|
| Lexer | `rwc/lexer.py` | `KW_IF` / `KW_ELSE` は予約済み。**無改修** |
| AST | `rwc/ast_nodes.py` | `IfExpr` ノード追加 (then, cond, els)。`Expr` Union に追加 |
| Parser | `rwc/parser.py` | `parse_ternary()` を `parse_expr` と `parse_or` の間に挿入。右結合 |
| Sema | `rwc/sema.py` | `_infer_expr` に `IfExpr` 分岐追加 (cond は bool、両ブランチ同型) |
| irgen | `rwc/irgen.py` | `_emit_expr` に `IfExpr` 分岐 + `_emit_if_expr` (cbranch + phi) |
| Runtime | `runtime/` | **無改修** |
| Desugar | `rwc/desugar.py` | **無改修** (desugar 不要) |
| Examples | `examples/ternary.rw` (+ `.expected`) | 新サンプル 1 |
| Tests | `tests/test_*.py` | parser / sema / irgen の unit test、e2e parametrize に `ternary` 追加 |

`incremental-language-extensions` の「1 PR 4 層まで」(parser / sema / irgen +
例題) に収まる。

## 検証

- 例題 `examples/ternary.rw` を `rwc build` → 実行 → `.expected` と一致
- positive (parser): `1 if c else 2` が `IfExpr` にパース、ネストが右結合
- positive (sema): int / string / bool の同型ブランチが通る
- positive (irgen): 生成 IR に `br i1` と `phi` が出る
- negative (sema): `cond` が非 bool → 型エラー、ブランチ型不一致 → 型エラー
- negative (parser): `else` 欠落 → 構文エラー

## リスクと対処

- **文 if と式 if の取り違え**: 文の `if` は行頭ディスパッチ、式の `if` は
  `parse_ternary` 内でのみ現れるため衝突しない。parser テストで既存の `if`
  文が壊れていないことも担保する
- **phi の型ミスマッチ**: phi の型は固定値ではなく sema が推論した型
  (`expr_types`) から引く。両ブランチ同型は sema が保証済みなので、then 側の
  値と else 側の値は同じ LLVM 型になる
- **「ついでに」誘惑**: if 式 RFC (#109) や定数畳み込みには手を出さない
  (Non-Goals に明記)
