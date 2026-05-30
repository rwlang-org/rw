# rw `for ... in range(...)` ループ (構文糖, while への desugar)

## Context

rw はコンパイラ・ランタイム・サンプル・テストが一体となって育つ小さな言語で、
これまで型 (string / Bytes / List[int] / Option / Result) と並行・I/O (fibers /
scheduler / netpoller + TCP) を 1 サブプロジェクト 1 PR の粒度で積み上げてきた。

一方、制御フローは初期の `if` / `while` から拡張されておらず、最も使用頻度の
高い「カウントループ」を書くたびに以下のような定型を手で書く必要がある:

```rw
i: int = 0
while i < n:
    # body
    i = i + 1
```

このサブプロジェクトは、制御フロー強化のロードマップの第一歩として
`for <var> in range(...)` を導入する。設計の核心は **`range` を値として持つ
型を作らず、`for` 専用の構文要素として扱い、sema で `while` ループへ desugar
する** こと。これにより型システム (`types.py`) への波及をゼロに保ち、変更を
lexer / parser / sema の 3 層 + 例題に閉じ込める。

制御フローのロードマップ (このサブプロジェクトはその 1 つ目):

1. **このサブプロジェクト**: `for ... in range(start, stop[, step])`
2. `break` / `continue` (`for` / `while` 共通)
3. `for <var> in <list>` などのイテレータ形式
4. `if` を式として使う / 三項相当

## Goals

- 新しい文 `for <ident> in range(<args>):` を導入
  - `range(stop)` — start=0, step=1
  - `range(start, stop)` — step=1
  - `range(start, stop, step)`
- start / stop / step は **任意の `int` 式** (変数・関数呼び出しを含む)
- 半開区間 `[start, stop)`、ループ変数はループ本体スコープの `int`
- 負の `step` に対応 (`stop` を下回るまで降順に反復)
- `step == 0` は **0 回ループ** (本体を実行せず即終了)。desugar した
  ループ条件が step==0 で両辺 false になる性質をそのまま利用する
- `for` ノードを `while` + 代入の AST に desugar し、sema / irgen は既存の
  `while` 処理をそのまま使う (両層とも無改修)

## Non-Goals

- **`range` を値として扱う**こと。`x = range(0, 10)` や関数引数への `range`
  は構文エラー。`range` は `for ... in` の直後でのみ受理する
  (parser で「`range` can only appear in a for-loop header」エラー)
- `for <var> in <list/string/Bytes>` のイテレータ形式 (別 PR)
- `break` / `continue` (別 PR、制御フロー ロードマップの 2 つ目)
- `range` を `int` 以外 (`List` など) で使うこと
- ループ変数への再代入禁止の厳密なチェック (最小限の扱い)
- `step` の定数畳み込みによる条件簡約 (LLVM の最適化に委ねる)

## 構文

```
for_stmt   := "for" IDENT "in" "range" "(" range_args ")" ":" NEWLINE block
range_args := expr                       # stop
            | expr "," expr              # start, stop
            | expr "," expr "," expr     # start, stop, step
```

`range` は予約語にしない (識別子 `range` をパーサが for ヘッダ位置で照合する)。
予約語化すると変数名 `range` が使えなくなる副作用があるため、識別子マッチで
扱う。`for` / `in` は lexer に予約済み (`KW_FOR` / `KW_IN`)。

### 使用例

```rw
def main() -> int:
    total: int = 0
    for i in range(0, 10):       # 0,1,...,9
        total = total + i
    for j in range(10, 0, -1):   # 10,9,...,1
        total = total + j
    return total
```

## 内部設計: sema による while への desugar

`for v in range(a, b, s):` を、sema が以下と等価な AST に変換する。引数の
**二重評価を防ぐため一時変数に束縛**する (start/stop/step が副作用を持つ
関数呼び出しでも 1 回だけ評価する):

```
__stop = b
__step = s
v = a                     # ループ変数 (ユーザー可視, int)
while (__step > 0 and v < __stop) or (__step < 0 and v > __stop):
    <body>
    v = v + __step
```

- 一時変数名はユーザー識別子と衝突しない内部名 (`__for_stop_N` 等, N は連番)
- ループ条件は step の符号で両側に分岐させる汎用形。定数畳み込みはせず、
  LLVM の最適化に委ねる (Non-Goal)
- `range(stop)` / `range(start, stop)` は不足引数を `0` / `1` のリテラル
  ノードで補完してから上記に展開
- desugar 後はすべて既存の AST ノード (`While` / `Assign` / `BinOp` / `If`)
  なので、sema / irgen は無改修で動く

## step == 0 の挙動

`step` は任意式なのでコンパイル時に 0 と判定できないケースがある。trap は
設けず、desugar したループ条件 `(step>0 and v<stop) or (step<0 and v>stop)`
が step==0 で両辺 false になる性質をそのまま利用し、**0 回ループ**
(本体を実行せず即終了) とする。これにより runtime は無改修で済む。

## 触るレイヤー

| レイヤー | ファイル | 変更 |
|---|---|---|
| Lexer | `rwc/lexer.py` | `KW_FOR` / `KW_IN` は予約済み (L70-71, L129-130)。追加不要 |
| AST | `rwc/ast_nodes.py` | `For` ノード追加 (var, start, stop, step, body)。`Stmt` Union に追加 |
| Parser | `rwc/parser.py` | `parse_for()` 追加、文ディスパッチに `KW_FOR` 追加。`range(...)` を for ヘッダ位置でのみ受理 |
| Desugar | `rwc/desugar.py` (新規) | parser 直後・sema 前に走る独立パス。`For` を `VarDecl`/`While`/`Assign` に書き換える。CLI のパイプラインに 1 行挿入 |
| Sema | `rwc/sema.py` | **無改修**。desugar 後は既存ノードのみなので型チェック・`local_types` 登録が自動で効く |
| irgen | `rwc/irgen.py` | **無改修**。desugar 済み while をそのまま処理 |
| Runtime | `runtime/` | **無改修**。step==0 は 0 回ループで処理 |
| Examples | `examples/for_count.rw` (+ `.expected`) | 新サンプル 1 |
| Tests | `tests/test_e2e.py` ほか | parametrize に `for_count` 追加。parser/desugar の unit test |

実質 AST / parser / desugar(新規) + 例題で、sema・irgen・runtime は無改修。
`incremental-language-extensions` の「1 PR 4 層まで」に収まる。

## 検証

- 例題 `examples/for_count.rw` を `rwc build` → 実行 → `.expected` と一致
- positive テスト:
  - 昇順 `range(0, 5)` が 0..4
  - `range(5)` (1 引数) が 0..4
  - 降順 `range(5, 0, -1)` が 5..1
  - step=2 `range(0, 10, 2)` が 0,2,4,6,8
  - 空ループ `range(5, 5)` / `range(0, -3)` は本体 0 回
  - step==0 `range(0, 10, 0)` は本体 0 回 (即終了)
  - range 引数に変数・式を使う
- negative テスト:
  - `for` 外で `range(...)` → 構文エラー
  - `x = range(0, 5)` → 構文エラー
  - range 引数が非 int → 型エラー (sema)
  - `range()` 引数 0 個 / 4 個以上 → 構文エラー
- desugar の unit test: `For` ノードが `VarDecl`/`While`/`Assign` に展開される

## リスクと対処

- **二重評価**: start/stop/step を一時変数に束縛して回避 (上記 desugar)
- **内部名の衝突**: 連番付き内部名 (`__for_stop_N`) でユーザー識別子と分離
- **負 step の境界**: 条件を `(step>0 and v<stop) or (step<0 and v>stop)` と
  両側に分け、step の符号で正しく終了
- **step==0**: 上記条件が両辺 false になり 0 回ループで安全に終了 (trap 不要)
- **desugar の挿入漏れ**: `compile_source` / `emit_ir` / `emit_ast` の 3 経路
  すべてで parse 直後に desugar を呼ぶ (driver.py)。漏れると for が irgen に
  生のまま渡り "unknown stmt" になるため、3 経路を e2e で踏むテストで担保
