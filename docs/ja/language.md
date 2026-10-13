# rw 言語リファレンス

rw は、Python 風の表面構文を持つ静的型付きのコンパイル言語です。コンパイラ
`rwc`（Python 製）が rw のソースを LLVM IR に変換し、clang が C ランタイム
`librw.a` と Boehm GC をリンクしてネイティブ実行ファイルを作ります。

このドキュメントは言語全体のリファレンスです。記述の根拠はコンパイラの実装
（`rwc/lexer.py`、`rwc/parser.py`、`rwc/desugar.py`、`rwc/sema.py`、
`rwc/loader.py`、`rwc/cli.py`）です。設計の経緯は `docs/specs/` の各仕様書に
あり、各節の末尾に「詳細: spec NN」として示します。仕様書と実装が食い違う
場合は、このドキュメントは実装に従っています。

英語版は [../language.md](../language.md) にあります。

## 目次

1. [概要](#1-概要)
2. [字句構造](#2-字句構造)
3. [型](#3-型)
4. [宣言と文](#4-宣言と文)
5. [式と演算子](#5-式と演算子)
6. [並行処理](#6-並行処理)
7. [モジュールと import](#7-モジュールと-import)
8. [組み込み関数リファレンス](#8-組み込み関数リファレンス)
9. [エラーと実行時チェック](#9-エラーと実行時チェック)
10. [仕様書の索引](#10-仕様書の索引)

---

## 1. 概要

### 1.1 プログラムの構造

rw のプログラムは 1 つ以上のモジュール（`.rw` ファイル）からなります。
コンパイラに渡すファイルをエントリモジュールと呼び、エントリモジュールは
`def main() -> int` を定義しなければなりません。`main` は引数を取らず、
戻り値がプロセスの終了ステータスになります（OS が下位 8 ビットに切り詰める
ので、`return 300` の終了ステータスは 44 です）。

```rw
def main() -> int:
    print("hello, rw")
    return 0
```

モジュールのトップレベルに書けるのは次の 5 種類だけです。`import` は他の
すべてより前に置きます。

| 要素 | 例 |
|---|---|
| import | `import json`, `from path import join` |
| 定数 | `MAX: int = 10` |
| 型エイリアス | `type Score = Dict[string, int]` |
| レコード | `class Point:` |
| 関数 | `def add(a: int, b: int) -> int:` |

トップレベルに文（代入や式）は書けません。可変のグローバル変数はありません。

### 1.2 コンパイルと実行

`rwc` のサブコマンドは次のとおりです（リポジトリでは `uv run rwc ...` で
起動します）。

| コマンド | 説明 |
|---|---|
| `rwc build FILE [-o OUT] [-O N] [--unchecked]` | 実行ファイルを作る。`-o` を省略すると `FILE` から拡張子を除いたパス |
| `rwc run FILE [-O N] [--unchecked]` | 一時ディレクトリでビルドして実行し、その終了ステータスを返す。プログラムへ引数は渡せない |
| `rwc check [-q] FILE...` | 型検査だけを行う。IR 生成もリンクもしない。成功したファイルごとに `FILE: ok` を表示（`-q` で抑止）。1 つでも失敗すれば終了ステータス 1 |
| `rwc test [PATH...] [-k SUBSTR] [--timeout SEC] [-v]` | `test_*.rw` / `*_test.rw` 内の `test_*` 関数を実行する（[9.4](#94-テストrwc-test-と-assert_eq)） |
| `rwc emit-ir FILE [--unchecked]` | 生成した LLVM IR を標準出力に書く |
| `rwc emit-ast FILE` | エントリモジュールの AST を表示する |
| `rwc --version` | バージョンを表示する |

オプション:

- `-O N`（`-O0` ～ `-O3`）: clang に渡す LLVM の最適化レベル。省略時は
  環境変数 `RW_OPT_LEVEL`、それもなければ 2 です。範囲外の値はエラーです。
  `build` と `run` だけが受け付けます。
- `--unchecked`: 整数除算の実行時チェック（[9.2](#92-実行時チェック)）を
  省きます。`build`、`run`、`emit-ir` が受け付けます。

コンパイラは最初のエラーで止まり、ファイル名・行・桁・キャレット付きの
診断を表示します。名前の綴り間違いには `help: did you mean ...?`、型の
不一致には `float(...)` などの変換方法が添えられます。

C ランタイム `runtime/librw.a` がなければ、初回のビルド時に `make -C runtime`
で作ります。リンクには `clang`（なければ `cc`）と Boehm GC が必要です。

詳細: [spec 48](../specs/48-rwc-check.md)、[spec 49](../specs/49-did-you-mean.md)、
[spec 68](../specs/68-rwc-test.md)、[spec 70](../specs/70-optimization-levels.md)

### 1.3 環境変数

| 変数 | 読む側 | 意味 |
|---|---|---|
| `RW_OPT_LEVEL` | コンパイラ | `-O` を省略したときの最適化レベル（0–3、既定 2） |
| `RW_STDLIB` | コンパイラ | 標準ライブラリのディレクトリ（既定はリポジトリの `stdlib/`） |
| `RW_GC_PREFIX` | コンパイラ（`scripts/gc-prefix.sh`） | Boehm GC のインストール先。pkg-config や brew による探索より優先 |
| `RW_WORKERS` | 実行時 | ファイバーを実行するワーカースレッド数（1–64、既定は CPU 数） |
| `RW_PREEMPT_MS` | 実行時 | プリエンプションのタイムスライス（ミリ秒、0–60000、既定 10、0 で無効） |
| `RW_AIO_THREADS` | 実行時 | ブロッキング I/O を肩代わりするスレッドプールの大きさ（1–64、既定 4） |

実行時に読む 3 つの変数は、範囲外や数値でない値を無視して既定値を使います。
`RW_OPT_LEVEL` に数字以外を書くと無視されますが、4 以上の数字はエラーです。

---

## 2. 字句構造

### 2.1 コメントと行

- ソースは UTF-8 のテキストです。
- `#` から行末まではコメントです。複数行コメントはありません。
- 文は 1 行に 1 つです。括弧の中でも改行で式を続けることはできず、
  行継続の `\` もありません。長いリストや引数列も 1 行に書きます。
- 空行とコメントだけの行はインデントの計算から除外されます。

### 2.2 インデント

ブロックは Python と同じくインデントで表します。`:` で終わる行の次の行を
1 段深くし、元の深さに戻るとブロックが閉じます。

- インデント幅は自由ですが、同じブロック内では揃えます。閉じたときに
  外側のどの深さとも一致しなければエラーです。
- 1 行のインデントにタブと空白を混ぜるとエラーです。
- 空のブロックは書けません。`pass` はありません。

### 2.3 識別子

英字または `_` で始まり、英字・数字・`_` が続きます。大文字と小文字は
区別されます。キーワードは識別子に使えません。

### 2.4 キーワード

字句解析器が予約しているキーワードは次の 43 個です。

```
def return if elif else while for in break continue match case
assert with defer lambda and or not true false
spawn await class type import from as extern
void int float bool string Bytes List Option Result Future
Some None Ok Err
```

`extern` は予約されているだけで、構文としての意味はありません。

### 2.5 文脈で特別扱いされる名前

次の名前はキーワードではありませんが、特定の位置で特別な意味を持ちます。

| 名前 | 意味を持つ位置 |
|---|---|
| `Dict`, `Callable`, `Channel`, `tuple` / `Tuple` | 型の位置で `[` が続くとき |
| `Mutex` | 型名として |
| `range` | `for x in range(...)` のヘッダ内だけ |
| `self` | メソッドの第 1 引数 |
| `_` | タプル分解で値を捨てる、値の `match` でワイルドカード |
| `str` など組み込み関数名 | 関数呼び出しとして（[8 章](#8-組み込み関数リファレンス)） |

`int(...)` と `float(...)` はキーワードですが、`(` が続くと変換関数として
扱われます。

### 2.6 リテラル

**整数**（`int`、64 ビット符号付き）

| 形式 | 例 |
|---|---|
| 10 進 | `0`, `42`, `1_000_000` |
| 16 進 | `0xff`, `0XFF` |
| 8 進 | `0o17` |
| 2 進 | `0b1010` |

- `_` は数字と数字の間に 1 つずつ置けます。接頭辞の直後、先頭、末尾、連続は
  エラーです。
- 負の数のリテラルはなく、`-5` は単項マイナスです。
- 先頭に 0 が付いた 10 進（`007`）はエラーです。8 進は `0o7` と書きます。
  0 だけの並び（`000`）は使えます。

**浮動小数点数**（`float`、IEEE 754 倍精度）

`3.14`、`1e10`、`2.5E-3`、`1_000.5` のように書きます。小数点の前後には
どちらも数字が必要です（`.5` や `5.` は書けません）。指数部があれば小数点は
なくても `float` になります。

**文字列**（`string`）

二重引用符で囲みます。単一引用符、三重引用符、raw 文字列はありません。
文字列は 1 行で閉じなければなりません。使えるエスケープは次の 6 つだけで、
それ以外はエラーです。

| エスケープ | 意味 |
|---|---|
| `\n` | 改行 |
| `\t` | タブ |
| `\r` | 復帰 |
| `\\` | バックスラッシュ |
| `\"` | 二重引用符 |
| `\0` | NUL バイト |

**f-string**

`f"..."` の中の `{式}` は、その式に `str()` を適用した結果に置き換わります。
`{{` と `}}` はそれぞれ `{`、`}` そのものです。テキスト部分では通常の
エスケープが使えます。式の中に文字列リテラルや括弧を書けます
（`f"{d["k"]}"`）。書式指定（`{x:.2f}`）や `!r`、`=` はありません。
`f"a{x}b"` は `"a" + str(x) + "b"` に展開されるので、式の型は `str()` が
受け付ける `int`、`float`、`bool`、`string` に限られます。

**真偽値と None**

`true`、`false`（小文字）。`None` は `Option[T]` の値のない側です
（[3.7](#37-optiont-と-resultt-e)）。

詳細: [spec 34](../specs/34-fstrings.md)

### 2.7 演算子と区切り記号

```
+  -  *  /  %  &  |  ^  ~  <<  >>
== !=  <  <=  >  >=
=  +=  -=  *=  /=  %=  &=  |=  ^=
(  )  [  ]  {  }  ,  :  .  ->
```

`**`、`//`、`<<=`、`>>=`、`++`、`--`、`!` はありません。

---

## 3. 型

### 3.1 型の一覧

| 型 | 例 | 意味論 |
|---|---|---|
| `int` | `42` | 64 ビット符号付き整数。値 |
| `float` | `3.14` | 倍精度浮動小数点数。値 |
| `bool` | `true` | 値 |
| `string` | `"hi"` | 不変のバイト列。値 |
| `Bytes` | `bytes_from_str("hi")` | 不変のバイト列。値 |
| `List[T]` | `[1, 2, 3]` | 不変のリスト。値 |
| `Dict[K, V]` | `{"a": 1}` | 挿入順を保つハッシュ表。参照 |
| `tuple[T1, ..., Tn]` | `("ann", 31)` | 2 要素以上の組。値 |
| `Option[T]` | `Some(1)`, `None` | 値があるかもしれない。値 |
| `Result[T, E]` | `Ok(1)`, `Err("bad")` | 成功か失敗。値 |
| `Callable[[P...], R]` | `add`, `lambda x: x + 1` | 関数値（クロージャを含む） |
| `Future[T]` | `spawn f()` | 実行中のファイバーの結果 |
| `Channel[T]` | `chan_new(8)` | ファイバー間のチャネル。参照 |
| `Mutex` | `mutex_new()` | ロック。参照 |
| レコード | `Point(1, 2)` | `class` で宣言した型。参照 |
| `void` | — | 戻り値がないことを表す。戻り値の型と `Callable` の戻り値、`Future[void]` にだけ書ける |

`void` は変数、引数、フィールド、リスト要素、`Option` などの中身には
使えません。

暗黙の型変換はありません。`int` と `float` を混ぜた演算も書けないので、
`float(n)` のように明示的に変換します。

### 3.2 int、float、bool

- `int` の `+`、`-`、`*` はオーバーフローすると 2 の補数で折り返します。
- `int` の `/` は 0 方向への切り捨て、`%` の符号は被除数と同じです
  （`-7 / 2 == -3`、`-7 % 2 == -1`。Python とは異なります）。0 による除算は
  実行時エラーです（[9.2](#92-実行時チェック)）。
- `float` の `%` は C の `fmod` と同じで、符号は被除数に従います。
  `1.0 / 0.0` は `inf` です。
- `bool` は `true` / `false` だけで、整数とは相互に変換されません
  （`int(true)` は 1）。

### 3.3 string と Bytes

`string` も `Bytes` も不変のバイト列で、長さやインデックスはバイト単位です。
UTF-8 として正しいかどうかは検査されず、大文字小文字の変換や空白の判定は
ASCII だけが対象です。

- `string`: `+` で連結、`==` / `!=` で比較、`<` などでバイトの辞書順比較、
  `s[i]`（1 バイトの `string`）、`s[a:b]`、`for c in s`（1 バイトずつ）。
- `Bytes`: I/O 用のバイト列です。`==` / `!=` と `len` だけが使え、
  インデックスやスライスはありません。`bytes_from_str` / `str_from_bytes` で
  `string` と相互に変換します。

詳細: [spec 07](../specs/07-string-builtins.md)、[spec 08](../specs/08-bytes-type.md)、
[spec 18](../specs/18-string-search.md)、[spec 28](../specs/28-string-builtins.md)

### 3.4 List[T]

`List[T]` は任意の `void` 以外の型 `T` のリストで、入れ子にもできます
（`List[List[string]]`）。リストは不変です。

- 作り方: リテラル `[e1, ..., en]`、または `list_new()`。
- 要素の追加: `list_push(xs, v)` は `v` を末尾に加えた**新しい**リストを
  返し、`xs` は変わりません。`xs = list_push(xs, v)` と書きます。
- `xs[i]`（負の `i` は末尾から数える。範囲外は実行時エラー）、
  `xs[a:b]`（新しいリスト）、`for x in xs`、`len(xs)`。
- `xs[i] = v` はコンパイルエラーです。

空のリテラル `[]` と `list_new()` は、期待される型
（[3.14](#314-期待型による型付け)）から要素の型を決めます。期待型が
なければ `List[int]` です。

```rw
def main() -> int:
    names: List[string] = []
    names = list_push(names, "ann")
    names = list_push(names, "bob")
    for n in names:
        print(n)
    print(names[-1])
    print(len(names[1:]))
    return 0
```

詳細: [spec 09](../specs/09-list-type.md)、[spec 22](../specs/22-generic-list.md)、
[spec 29](../specs/29-list-syntax.md)

### 3.5 Dict[K, V]

キー `K` は `int` か `string`、値 `V` は `void` 以外の任意の型です。辞書は
ヒープ上のオブジェクトで、代入や引数渡しは参照を共有します。

- 作り方: リテラル `{k: v, ...}`、または `dict_new()`。`{}` と `dict_new()`
  は期待型が必要で、ないとエラーです。リテラルでキーが重複すると最後の
  値が残ります。
- `d[k]`（キーがなければ実行時エラー）、`d[k] = v`、`d[k] += 1` などの
  複合代入。
- `dict_get` / `dict_has` / `dict_remove` / `dict_keys` / `dict_values`、
  `len(d)`。
- `for k in d` はキーを挿入順に回ります。開始時のキーのスナップショットを
  回るので、ループ中に辞書を変更しても安全です。再代入したキーは位置を
  保ち、削除して追加し直したキーは末尾に移ります。
- 複数のファイバーからの同時更新は同期されません。`Mutex` で守ります。

```rw
def main() -> int:
    counts: Dict[string, int] = {}
    for w in str_split("a b a c a", " "):
        if dict_has(counts, w):
            counts[w] += 1
        else:
            counts[w] = 1
    for w in counts:
        print(w + " " + str(counts[w]))
    return 0
```

詳細: [spec 31](../specs/31-dict.md)、[spec 36](../specs/36-dict-literals.md)

### 3.6 タプル

`tuple[T1, ..., Tn]`（`Tuple[...]` とも書ける）は 2 要素以上の不変の組です。

- リテラル `(e1, ..., en)`。`return a, b` もタプルを返します。`(e)` は
  ただの括弧です。1 要素や空のタプルはありません。
- 要素の取得は定数インデックス `t[0]`、`t[-1]` だけです。範囲外の定数は
  コンパイルエラーです。
- 分解代入 `a, b = t`（[4.5](#45-代入)）。
- タプル同士の `==` や `print` はできません。

```rw
def divmod_(a: int, b: int) -> tuple[int, int]:
    return a / b, a % b

def main() -> int:
    q, r = divmod_(17, 5)
    pair = ("ann", 31)
    print(f"{pair[0]} is {pair[1]}, q={q} r={r}")
    return 0
```

詳細: [spec 37](../specs/37-tuples.md)

### 3.7 Option[T] と Result[T, E]

`Option[T]` は `Some(x)` か `None`、`Result[T, E]` は `Ok(x)` か `Err(e)` です。
`T` と `E` は `void` 以外の任意の型で、入れ子にできます。中身を取り出すには
`match` を使います（[4.10](#410-match)）。`==` と `print` は使えず、
`unwrap` のようなメソッドもありません。

`None`、`Some(...)`、`Ok(...)`、`Err(...)` は期待型があればその型になります。
期待型がないときは、`Some(x)` は `Option[x の型]`、`None` は `Option[int]`、
`Ok(x)` は `Result[x の型, int]`、`Err(e)` は `Result[int, e の型]` です。
ただし型注釈のない宣言 `x = None`、`x = Ok(1)`、`x = Err(...)` は推論できず
エラーになります。

```rw
def parse(s: string) -> Result[int, string]:
    match str_to_int(s):
        case Some(n):
            return Ok(n)
        case None:
            return Err("not a number: " + s)

def main() -> int:
    match parse("x1"):
        case Ok(n):
            print(n)
        case Err(msg):
            print(msg)
    return 0
```

詳細: [spec 10](../specs/10-option-type.md)、[spec 11](../specs/11-result-type.md)、
[spec 30](../specs/30-generic-option-result.md)

### 3.8 Callable（関数値）

`Callable[[P1, ..., Pn], R]` は関数値の型です。引数がなければ
`Callable[[], R]`、戻り値がなければ `R` に `void` を書きます。型は構造的に
比較されます（引数と戻り値の型が同じなら同じ型）。

関数値になるもの:

- トップレベル関数の名前（呼び出さずに書く）、`from` で取り込んだ関数名、
  `mod.f`
- 入れ子の `def` と `lambda`（[4.15](#415-クロージャ入れ子の-def-と-lambda)）

関数値は変数、引数、戻り値、レコードのフィールド、リストの要素に入れられ、
`h(x)`、`r.handler(x)`、`pick()(x)` のように呼べます。組み込み関数と
メソッドは値にできません。関数値の呼び出しにキーワード引数は使えません。
関数値どうしの `==` はありません。

詳細: [spec 21](../specs/21-function-values.md)、[spec 71](../specs/71-closures.md)

### 3.9 Future、Channel、Mutex

- `Future[T]`: `spawn f(...)` の結果です。`await` で値を取り出します。
  1 つの `Future` を `await` できるのは 1 回だけです。
- `Channel[T]`: ファイバー間で値を送るチャネルです。参照です。
- `Mutex`: ロックです。参照です。

使い方は [6 章](#6-並行処理) を見てください。

### 3.10 レコード（class）

`class` はフィールドを持つ名前付きのレコード型を宣言します。レコードは
ヒープ上のオブジェクトで、代入や引数渡しは参照をコピーします。ある名前を
通じた書き換えは、同じレコードを指す他の名前からも見えます。

```rw
class Point:
    x: int
    y: int

    def norm1(self) -> int:
        return self.x + self.y

    def shift(self, dx: int = 1) -> void:
        self.x += dx

def main() -> int:
    p = Point(1, 2)
    q = p
    q.x = 10
    print(p.x)            # 10: p と q は同じレコード
    p.shift(dx=5)
    print(p.norm1())      # 17
    r = Point(y=4, x=3)   # フィールド名をキーワードにして渡せる
    print(r.x)
    return 0
```

- フィールドは `name: Type` を 1 行に 1 つ書きます。同じモジュールの後方で
  宣言したクラスも型として使えます（`next: Option[Node]` のような再帰も可）。
- コンストラクタ `Name(e1, ..., en)` はフィールドごとに 1 つの引数を宣言順に
  取ります。キーワード引数でも渡せます。フィールドの既定値はありません。
- `e.field` で読み、`e.field = v` / `e.field += v` で書きます。
- メソッドについては [4.14](#414-レコードとメソッド) を見てください。
- 「値のないレコード」はありません。必要なら `Option[Point]` を使います。
- レコードの `==`、`print`、`str()` はできません。

詳細: [spec 20](../specs/20-records.md)、[spec 33](../specs/33-methods.md)

### 3.11 型エイリアス

`type Name = Type` はモジュールのトップレベルで型に別名を付けます。

```rw
type Handler = Callable[[string], string]
type Score = Dict[string, int]
```

エイリアスはそのモジュールの中だけで有効で、import できません。前方参照は
できず、後のエイリアスから前のエイリアスを参照することはできます。

### 3.12 値の意味論と参照の意味論

| 値として振る舞う型 | 参照として振る舞う型 |
|---|---|
| `int`, `float`, `bool`, `string`, `Bytes`, `List[T]`, `tuple[...]`, `Option[T]`, `Result[T, E]` | レコード、`Dict[K, V]`, `Channel[T]`, `Mutex` |

値の型は不変なので、代入や引数渡しでコピーされたように振る舞い、受け取った
側の操作が元の変数に影響することはありません。ただし、値の型の中に入れた
レコードや辞書は参照のままです（`List[Point]` の要素の `Point` を書き換えると、
同じレコードを指す他の場所からも見えます）。参照の型は 1 つのオブジェクトを
共有し、書き換えは全員から見えます。この区別はクロージャのキャプチャ
（[4.15](#415-クロージャ入れ子の-def-と-lambda)）にもそのまま当てはまります。

メモリは Boehm GC が回収します。明示的な解放はありません。

詳細: [spec 67](../specs/67-garbage-collection.md)

### 3.13 等値、順序、print

| 操作 | 使える型 |
|---|---|
| `==`, `!=` | `int`, `float`, `bool`, `string`, `Bytes`（両辺が同じ型） |
| `<`, `<=`, `>`, `>=` | `int`, `float`, `string`（両辺が同じ型。文字列はバイトの辞書順） |
| `print(x)` | `int`, `float`, `bool`, `string` |
| `str(x)` | `int`, `float`, `bool`, `string` |
| `assert_eq(a, b)` | `int`, `float`, `bool`, `string` |

リスト、辞書、タプル、Option、Result、レコード、関数値は比較も表示も
できません。必要なら自分で関数を書きます。

`print` は値と改行を標準出力に書きます。`float` の表示は C の `%g`（有効
数字 6 桁）なので、`print(1.0 / 3.0)` は `0.333333` です。`str(float)` は
読み戻すと同じ値になる最短の表記（`%.15g`、`%.16g`、`%.17g` の順に試す）を
返すので、全桁が必要なら `print(str(x))` または f-string を使います。
どちらも `2.0` は `2`、`1e100` は `1e+100` と表示します。`bool` は
`true` / `false` です。

### 3.14 期待型による型付け

次の式は、それ自身だけでは型が決まらないため、文脈が期待する型を使います。

| 式 | 期待型がないとき |
|---|---|
| `[]`, `list_new()` | `List[int]` |
| `{}`, `dict_new()` | エラー |
| `chan_new(n)` | エラー |
| `None`, `Some(x)`, `Ok(x)`, `Err(e)` | [3.7](#37-optiont-と-resultt-e) の既定 |
| `lambda ...` | エラー |

期待型を与える文脈は、型注釈付きの宣言、既存変数への代入、`return`、関数・
メソッド・コンストラクタの引数、リストやタプルや辞書リテラルの要素、
`list_push` の第 1 引数、条件式の両方の枝です。

---

## 4. 宣言と文

### 4.1 関数

```rw
def greet(name: string, greeting: string = "hello", times: int = 1) -> string:
    out = ""
    for i in range(times):
        out += greeting + ", " + name + "\n"
    return out
```

- 引数と戻り値の型注釈は必須です。戻り値がなければ `-> void` と書きます。
- 既定値はリテラル（`int`、`float`、`string`、`bool`、負の数値）に限られ、
  型が引数と一致しなければなりません。既定値のある引数は後ろにまとめます。
  既定値は省略した呼び出しごとにその場に展開されます。
- 呼び出しでは位置引数の後に `name=value` のキーワード引数を書けます。
  未知のキーワード、同じ引数への二重指定、必須引数の欠落はエラーです。
  キーワード引数はユーザー定義の関数、メソッド、コンストラクタ、`spawn` で
  使えます。組み込み関数と関数値には使えません。
- `void` 以外を返す関数は、すべての経路で `return` しなければなりません。
  コンパイラは `if`/`else` の両方の枝、`match` のすべての腕を見て判断します。
  `while` や `for` は（`while true:` でも）return を保証しないので、ループの
  後にも `return` が必要です。
- 同じモジュールの関数は定義順に関係なく呼べます。オーバーロードはありません。
- 組み込み関数と同じ名前の関数は、修飾なしで呼ぶと組み込み関数が選ばれます。
  組み込み関数の名前は避けてください。

詳細: [spec 41](../specs/41-default-keyword-args.md)

### 4.2 変数

```rw
def main() -> int:
    count: int = 0            # 型注釈付きの宣言
    names = ["a", "b"]        # 右辺から型を推論（List[string]）
    count = count + len(names)
    print(count)
    return 0
```

- `x: T = e` は型 `T` の変数を宣言します。
- まだ宣言されていない名前への `x = e` は、`e` の型で変数を宣言します。
  右辺が型を決めない場合（`[]`、`{}`、`None`、`list_new()`、`dict_new()`、
  単独の `Ok(...)` / `Err(...)`、`void` の式）はエラーなので、型注釈を付けます。
- 宣言後の代入は同じ型でなければなりません。型を広げる代入はありません。
- 同じスコープで同じ名前を 2 回宣言するとエラーです。
- スコープ: 関数本体が 1 つのスコープで、`if`/`elif`/`else`、`while`、
  `for x in ...`、`match` の腕、`with` の本体はそれぞれ内側のスコープを
  作ります。内側で宣言した変数はブロックの外では見えず、外で同じ名前を
  別の型で宣言し直せます。
- 例外として、`for i in range(...)` のループ変数は外側のスコープに宣言され、
  ループの後でも参照できます（値はループを抜けた時点のもの）。
  `for x in xs` のループ変数はループの中だけで有効です。
- ローカル変数は同じ名前の関数や定数を隠せます。

詳細: [spec 32](../specs/32-local-inference.md)

### 4.3 定数

```rw
MAX_BODY: int = 1048576
GREETING: string = "hello"
RATIO: float = -0.5
```

- モジュールのトップレベルに `NAME: T = リテラル` と書きます。値は `int`、
  `float`、`string`、`bool` のリテラル（負の数値を含む）だけです。
- 定数はコンパイル時に値へ置き換えられます。代入するとエラーですが、
  型注釈付きの宣言で同じ名前のローカル変数を作って隠すことはできます。
- 他のモジュールからは `mod.NAME` または `from mod import NAME` で使えます。
- 名前は関数、クラス、エイリアス、他の定数、組み込み関数と重複できません。

詳細: [spec 35](../specs/35-constants.md)

### 4.4 型エイリアスとレコード

[3.11](#311-型エイリアス) と [3.10](#310-レコードclass) を見てください。

### 4.5 代入

| 形 | 意味 |
|---|---|
| `x = e` | 変数への代入（未宣言なら宣言） |
| `r.f = e` | レコードのフィールドへの代入。`a.b.c = e` のような連鎖も可 |
| `d[k] = e` | 辞書への代入。リストと文字列へのインデックス代入はエラー |
| `x OP= e` | `x = x OP e`。`OP` は `+ - * / % & \| ^` |
| `r.f OP= e`, `d[k] OP= e` | フィールドと辞書要素の複合代入 |
| `a, b = e` | タプルの分解 |

- 複合代入は同じ型の規則に従うので、`s += "!"` も書けます。未宣言の変数に
  `x += 1` とするとエラーです。
- フィールドの複合代入の左辺は名前とフィールドアクセスだけからなる経路、
  辞書要素の複合代入は加えてキーがリテラルか単純な経路でなければなりません
  （左辺を 2 回評価するため）。
- 分解代入は右辺のタプルと同じ数の名前を書きます。未宣言の名前は要素の型で
  宣言され、既存の名前は型が一致しなければなりません。`_` は値を捨てます。
- 代入は式ではなく、連鎖代入（`a = b = 0`）もありません。

詳細: [spec 27](../specs/27-compound-assignment.md)、[spec 37](../specs/37-tuples.md)

### 4.6 if / elif / else

```rw
def sign(n: int) -> string:
    if n > 0:
        return "positive"
    elif n == 0:
        return "zero"
    else:
        return "negative"

def main() -> int:
    print(sign(-3))
    return 0
```

条件は `bool` でなければなりません。整数や文字列を真偽値として扱う
暗黙の変換はありません。

### 4.7 while

```rw
def main() -> int:
    i = 0
    while i < 3:
        print(i)
        i += 1
    return 0
```

`while ... else` はありません。

### 4.8 for

**範囲ループ**

```rw
def main() -> int:
    for i in range(3):            # 0, 1, 2
        print(i)
    for j in range(10, 0, -3):    # 10, 7, 4, 1
        print(j)
    return 0
```

- `range(stop)`、`range(start, stop)`、`range(start, stop, step)`。引数は
  任意の `int` の式で、ループ開始時に 1 回だけ評価されます。
- 半開区間 `[start, stop)` を `step` ずつ進みます。負の `step` は降順、
  `step == 0` は 1 回も回りません。
- ループ変数は `int` です。同じ名前の `int` 変数が既にあれば再利用します
  （別の型ならエラー）。ループ後も参照できます（[4.2](#42-変数)）。
- `range` は `for` のヘッダの中でだけ使えます。

**要素ループ**

```rw
def main() -> int:
    for name in ["ann", "bob"]:
        print(name)
    for ch in "hi":
        print(ch)
    return 0
```

| 反復対象 | ループ変数 |
|---|---|
| `List[T]` | `T` |
| `string` | 1 バイトの `string` |
| `Dict[K, V]` | `K`（挿入順） |
| `Channel[T]` | `T`（閉じられて空になるまで受信） |

反復対象の式は 1 回だけ評価されます。タプルへの分解（`for k, v in ...`）や
`enumerate` / `zip` はありません。

詳細: [spec 13](../specs/13-for-range-loop.md)、[spec 29](../specs/29-list-syntax.md)

### 4.9 break / continue

最も内側のループを抜ける、または次の反復に進みます。ループの外で使うと
エラーです。範囲ループの `continue` でもループ変数の更新は行われます。
クロージャの本体から外側のループには届きません。

### 4.10 match

`match` には 2 つの形があり、最初の `case` で決まります。

**Option / Result の分解**

```rw
def main() -> int:
    d: Dict[string, int] = {"a": 1}
    match dict_get(d, "a"):
        case Some(v):
            print(v)
        case None:
            print("missing")
    return 0
```

- `case Some(name):` と `case None:`、または `case Ok(name):` と
  `case Err(name):` の組を両方書きます（順序は自由）。片方の欠落、重複、
  Option と Result の腕の混在はエラーです。
- 束縛した名前はその腕の中だけで有効で、中身の型を持ちます。
- `Some(_)` のような入れ子のパターンはありません。

**値の match**

```rw
def describe(code: int) -> string:
    match code:
        case 200 | 204:
            return "ok"
        case 404:
            return "not found"
        case other:
            return "status " + str(other)

def main() -> int:
    print(describe(500))
    return 0
```

- 対象は `int`、`string`、`bool` です。
- パターンはリテラル（負の整数を含む）、`|` でつないだ複数のリテラル、
  `_`（何にでも一致）、名前（何にでも一致し、その値を束縛）です。
- 腕は上から順に試され、最初に一致した腕だけが実行されます。どれにも一致
  しなければ何も実行しません。
- 型の合わないパターン、重複したリテラル、何にでも一致する腕の後の腕は
  エラーです。
- 何にでも一致する腕がある（または `bool` で `true` と `false` を両方書いた）
  うえで全腕が return する場合だけ、`match` 全体が return するとみなされます。

詳細: [spec 10](../specs/10-option-type.md)、[spec 11](../specs/11-result-type.md)、
[spec 38](../specs/38-value-match.md)

### 4.11 assert

```rw
def main() -> int:
    n = 3
    assert n > 0
    assert n < 10, "n is too large"
    return 0
```

条件は `bool`、メッセージは省略可能な `string` です。失敗すると
`rw: assertion failed: <メッセージ>` を標準エラー出力に書き、終了
ステータス 1 で終わります（メッセージを省略すると `assertion failed`）。

### 4.12 with / defer

```rw
class Res:
    name: string

    def close(self) -> void:
        print("close " + self.name)

def work() -> int:
    defer print("deferred 1")
    defer print("deferred 2")
    with Res("r1") as r:
        print("using " + r.name)
        return 7

def main() -> int:
    print(work())   # using r1 / close r1 / deferred 2 / deferred 1 / 7
    return 0
```

- `defer 呼び出し` は関数が終わるとき（どの `return` でも、末尾に達しても）に
  その呼び出しを実行します。後に登録したものから先に実行し、実行が到達した
  `defer` だけが対象です。
- `defer` は関数本体の最上位にだけ書けます（`if` やループの中は不可）。
  後ろには呼び出し式が必要です。
- `defer` の呼び出しの引数は、登録時ではなく関数を抜けるときに評価されます
  （Go とは異なります）。
- `with 式 [as 名前]:` の式は `Mutex`（入るときにロックし、出るときに
  アンロック）か、引数のない `close(self)` メソッドを持つレコード（出るときに
  `close` を呼ぶ）です。`as` の名前は新しい名前でなければなりません。
- 本体から `return`、`break`、`continue` で抜けるときも後始末が先に実行
  されます。`return` の値は後始末より前に評価されます。

詳細: [spec 47](../specs/47-with-defer.md)

### 4.13 return

`return 式` は値を返し、`return` だけの文は `void` 関数から戻ります。
`return a, b` はタプルを返します。`void` 関数で値を返すこと、`void` 以外の
関数で値なしの `return` はエラーです。

### 4.14 レコードとメソッド

`class` の本体には、フィールドの間や後にメソッドを書けます。

- メソッドは `def name(self, 引数...) -> R:` と書き、第 1 引数は必ず型注釈の
  ない `self` です。`self` はそのクラスの型を持ちます。
- `obj.m(args)` で呼びます。`obj` はローカル変数、フィールドの連鎖、要素、
  呼び出し結果など、そのクラス型の任意の式です。import したクラスの
  メソッドも呼べます。
- メソッドから `self.other()` で他のメソッドを呼んだり、`self.f` を読み書き
  したりできます。既定値とキーワード引数も使えます。
- 同じ名前のフィールドとメソッドは持てません。メソッドを値として取り出す
  こと、`spawn obj.m()`、継承、静的メソッド、`__init__` はありません。
- 関数値を持つフィールドも `r.handler(x)` の形で呼べます。

詳細: [spec 33](../specs/33-methods.md)

### 4.15 クロージャ（入れ子の def と lambda）

```rw
def make_adder(n: int) -> Callable[[int], int]:
    def add(x: int) -> int:
        return x + n          # n をキャプチャ
    return add

def apply(f: Callable[[int], int], v: int) -> int:
    return f(v)

def main() -> int:
    inc = make_adder(1)
    print(inc(41))                         # 42
    print(apply(lambda x: x * 2, 21))      # 42
    double: Callable[[int], int] = lambda x: x * 2
    print(double(4))
    return 0
```

- 関数本体の中の `def` 文はクロージャを定義し、`Callable[[P...], R]` 型の
  ローカル変数に束縛します。引数と戻り値の型注釈が必要で、既定値は書けません。
  自分自身の名前で再帰呼び出しできます。
- `lambda a, b: 式` は本体が 1 つの式の関数です。引数と戻り値の型は、文脈が
  期待する `Callable` 型（引数、`Callable` 型で注釈した変数、戻り値、
  レコードのフィールド）から決まり、文脈がなければエラーです
  （`f = lambda x: x` は書けません）。戻り値が `void` の `Callable` なら、
  本体の値は捨てられます。
- **キャプチャは値渡しです。** クロージャは、外側の関数のローカル変数
  （引数を含む）のうち、クロージャより前に宣言されて本体で読まれるものを、
  クロージャが作られた時点（`def` 文の実行時、`lambda` 式の評価時）で
  コピーします。後から外側の変数を変えても、クロージャの中の値は変わりません。
- キャプチャした変数にクロージャの中で代入するとコンパイルエラーです。
  共有して変更したい状態は、レコード、辞書、チャネルのような参照型に入れます。
- クロージャの中のクロージャは、両方の階層からキャプチャできます。
- `spawn` でクロージャを起動することはできません。

詳細: [spec 71](../specs/71-closures.md)

---

## 5. 式と演算子

### 5.1 優先順位

下にいくほど強く結合します。

| 順位 | 演算子 | 結合 |
|---|---|---|
| 1 | `lambda ...: ...` | — |
| 2 | `a if cond else b` | 右結合 |
| 3 | `or` | 左結合 |
| 4 | `and` | 左結合 |
| 5 | `not` | 前置 |
| 6 | `==` `!=` `<` `<=` `>` `>=` | 非結合（1 つの式に 1 つだけ） |
| 7 | `\|` | 左結合 |
| 8 | `^` | 左結合 |
| 9 | `&` | 左結合 |
| 10 | `<<` `>>` | 左結合 |
| 11 | `+` `-` | 左結合 |
| 12 | `*` `/` `%` | 左結合 |
| 13 | 単項 `-` `~`、`await`、`spawn` | 前置 |
| 14 | 呼び出し `f(...)`、インデックス `x[i]`、スライス `x[a:b]`、フィールド `x.f` | 左結合 |

ビット演算子は比較より強く結合します（Python と同じで、C とは異なります）。
`x & 1 == 1` は `(x & 1) == 1` です。比較の連鎖 `a < b < c` は構文エラー
なので、`a < b and b < c` と書きます。

### 5.2 算術演算子

| 演算子 | オペランド | 結果 |
|---|---|---|
| `+` `-` `*` `/` `%` | 同じ型の `int` 同士、または `float` 同士 | オペランドの型 |
| `+` | `string` 同士 | 連結した `string` |
| 単項 `-` | `int` または `float` | 同じ型 |

整数演算の詳細は [3.2](#32-intfloatbool) を見てください。

詳細: [spec 42](../specs/42-checked-division.md)

### 5.3 比較演算子

結果は `bool` です。使える型は [3.13](#313-等値順序print) の表のとおりで、
両辺は同じ型でなければなりません。

### 5.4 ビット演算子

`&`、`|`、`^`、`<<`、`>>`、単項 `~` は `int` だけに使えます。`>>` は算術
シフト（符号を保つ）です。シフト量が 0–63 の範囲外の場合の結果は未規定です。

### 5.5 論理演算子

`and`、`or`、`not` は `bool` だけを取り、`bool` を返します。`and` と `or` は
短絡評価です。

### 5.6 条件式

`then if cond else els` は `cond`（`bool`）が真なら `then`、偽なら `els` を
評価します。評価されるのは選ばれた側だけです。両方の枝は同じ型でなければ
ならず、それが式全体の型になります。右結合なので
`a if p else b if q else c` は `a if p else (b if q else c)` です。

詳細: [spec 14](../specs/14-ternary-expr.md)

### 5.7 インデックスとスライス

| 式 | 対象 | 結果 |
|---|---|---|
| `xs[i]` | `List[T]` | `T`。負の `i` は末尾から。範囲外は実行時エラー |
| `s[i]` | `string` | 1 バイトの `string`。負の `i` は末尾から。範囲外は実行時エラー |
| `d[k]` | `Dict[K, V]` | `V`。キーがなければ実行時エラー |
| `t[n]` | タプル | n 番目の要素。`n` は整数リテラル（負も可） |
| `xs[a:b]` | `List[T]` | 新しい `List[T]` |
| `s[a:b]` | `string` | `string`（コピーしない） |

スライスの境界はどちらも省略でき、負の値は末尾から数え、範囲外は
`[0, len]` に丸められます（Python と同じ）。ステップ（`[::2]`）はありません。
`Bytes` にはインデックスもスライスもありません。

### 5.8 呼び出し

| 形 | 意味 |
|---|---|
| `f(args)` | 同じモジュールの関数、`from` で取り込んだ関数、組み込み関数、レコードのコンストラクタ、関数値を持つローカル変数 |
| `mod.f(args)` | import したモジュールの関数、または `mod.Class(...)` |
| `x.m(args)` | メソッド呼び出し、または関数値のフィールドの呼び出し |
| `expr(args)` | 関数値を返す式の呼び出し（`pick()(x)`） |

関数値を持つローカル変数は、同じ名前の関数や組み込み関数より優先されます。
ローカル変数は、同じ名前のモジュール修飾子より優先されます。

### 5.9 f-string

[2.6](#26-リテラル) を見てください。

### 5.10 型変換

| 呼び出し | 受け付ける型 | 結果 |
|---|---|---|
| `int(x)` | `int`, `float`, `bool` | `float` は 0 方向に切り捨て、`bool` は 0 / 1。NaN や範囲外の `float` の結果は未規定 |
| `float(x)` | `int`, `float` | `float` |
| `str(x)` | `int`, `float`, `bool`, `string` | 10 進表記、最短の往復表記（[3.13](#313-等値順序print)）、`"true"` / `"false"`、そのまま |

文字列からの変換は失敗しうるので `Option` を返す関数を使います:
`str_to_int(s)`、`str_to_float(s)`。

詳細: [spec 26](../specs/26-conversions.md)

---

## 6. 並行処理

rw の並行処理の単位はファイバー（スタックを持つ軽量スレッド）です。
ファイバーは `RW_WORKERS` 個のワーカースレッド上で M:N スケジューリングされ、
並列に実行されます。`main` はファイバーではなくメインスレッドで動きます。
スケジューラ、ネットポーラ、I/O の扱いの詳細は
[ランタイムのドキュメント](../runtime.md) と [spec 05](../specs/05-fibers.md)、
[spec 06](../specs/06-scheduler-mn.md) を見てください。

### 6.1 spawn と await

```rw
def work(n: int) -> int:
    return n * n

def say(msg: string) -> void:
    print(msg)

def main() -> int:
    f: Future[int] = spawn work(7)
    print(await f)          # 49
    g = spawn say("hi")
    await g                 # Future[void]
    spawn say("detached")   # 文として書くと切り離される
    sleep(10)
    return 0
```

- `spawn f(args)` は引数をその場で評価し、`f` を新しいファイバーで実行して
  `Future[R]` を返します。`f` はユーザー定義関数（`mod.f` も可）でなければ
  ならず、組み込み関数、関数値、クロージャ、メソッドは起動できません。
  キーワード引数と既定値は使えます。
- 起動できる関数の戻り値の型は `int`、`float`、`bool`、`string`、`Bytes`、
  `void` だけです。引数の型に制限はありません（レコードも渡せます）。
- `await e` は `Future[T]` の完了を待って `T` を返します。ファイバーの中では
  そのファイバーだけが待ち（ワーカーは他のファイバーを実行します）、
  メインスレッドではブロックします。1 つの `Future` を `await` できるのは
  1 回だけです。
- **切り離し（detach）**: `spawn` を式文として書き、`Future` を捨てた
  ファイバーは切り離され、終了するとすぐに回収されます。`Future` を変数に
  入れたまま `await` しなければ、そのファイバーの資源は解放されません。
- `main` が戻るとプロセスは終了し、実行中のファイバーは待たれません。

詳細: [spec 19](../specs/19-fiber-lifecycle.md)

### 6.2 await_all と await_any

```rw
def slow(ms: int) -> int:
    sleep(ms)
    return ms

def main() -> int:
    futs: List[Future[int]] = [spawn slow(30), spawn slow(1)]
    i, v = await_any(futs)
    print(f"{i} finished first with {v}")
    print(await futs[0])
    more: List[Future[int]] = [spawn slow(2), spawn slow(3)]
    print(len(await_all(more)))
    return 0
```

- `await_all(fs: List[Future[T]]) -> List[T]`: すべてを待ち、結果をリストの
  順に返します。
- `await_any(fs: List[Future[T]]) -> tuple[int, T]`: 最初に終わった
  `Future` のインデックスと値を返します。他の `Future` は動き続け、後で
  `await` できます。空のリストは実行時エラーです。
- どちらも `Future[void]` には使えません。

詳細: [spec 45](../specs/45-await-all-any.md)

### 6.3 チャネル

```rw
def producer(ch: Channel[int]) -> void:
    for i in range(3):
        chan_send(ch, i)
    chan_close(ch)

def main() -> int:
    ch: Channel[int] = chan_new(1)
    spawn producer(ch)
    for v in ch:
        print(v)
    return 0
```

- `chan_new(capacity)` は期待型から要素の型を決めます。容量 0 はランデブー
  （受信者が受け取るまで送信が待つ）、N は N 個までバッファします。
- `chan_send(ch, v)` は満杯の間ブロックします。閉じたチャネルへの送信は
  実行時エラーです。
- `chan_recv(ch) -> Option[T]` は値が届くまでブロックします。閉じた後も
  バッファの値は届き、尽きると `None` を返します。
- `for v in ch:` は閉じられて空になるまで受信します。`len(ch)` はバッファ
  中の値の数です。
- ブロックするとファイバーは待機状態になり、ワーカーは他を実行します。
  メインスレッドからも送受信できます。`select` やタイムアウト付きの送受信は
  ありません。

詳細: [spec 44](../specs/44-channels.md)

### 6.4 Mutex

```rw
class Counter:
    n: int

def bump(c: Counter, m: Mutex) -> void:
    for i in range(1000):
        with m:
            c.n += 1

def main() -> int:
    c = Counter(0)
    m = mutex_new()
    fs: List[Future[void]] = [spawn bump(c, m), spawn bump(c, m)]
    for f in fs:
        await f
    print(c.n)    # 2000
    return 0
```

- `mutex_new()`、`mutex_lock(m)`、`mutex_unlock(m)`、
  `mutex_try_lock(m) -> bool`。`with m:` でロックとアンロックを対にできます。
- 待つファイバーは待機し、ロックは FIFO 順に渡されます。ロックされていない
  `Mutex` のアンロックは実行時エラーです。再帰ロックはできません。

詳細: [spec 46](../specs/46-mutex.md)

### 6.5 sleep と時計

- `sleep(ms)` は少なくとも `ms` ミリ秒待ちます。ファイバーでは待機状態に
  なり、メインスレッドでは `nanosleep` します。
- `time_ms()` は Unix 時刻（ミリ秒）、`mono_ns()` は経過時間の計測用の
  単調時計（ナノ秒）です。

詳細: [spec 39](../specs/39-sleep-and-clocks.md)

### 6.6 プリエンプション

コンパイラは関数の先頭と各ループの反復の先頭にセーフポイントを入れます。
タイムスライス（`RW_PREEMPT_MS`、既定 10 ms）より長く走り続けたファイバーは、
次のセーフポイントでワーカーを譲ります。C ランタイム内の長い処理や、
ループも呼び出しもない長い直線的なコードは中断されません。メインスレッドは
プリエンプトされません。

詳細: [spec 23](../specs/23-preemption.md)

### 6.7 I/O とファイバー

`read`、`write`、`accept`、`dial` などの I/O はファイバーの中ではそのファイバー
だけを待たせ、ワーカーは止まりません。ソケットと非ブロッキングのパイプは
ネットポーラ（kqueue / epoll）を、通常ファイルなどブロッキングの fd は
スレッドプール（`RW_AIO_THREADS`）を使います。`last_error()` の値は
ファイバーごとに保持されます。

詳細: [spec 12](../specs/12-netpoller-tcp.md)、[spec 16](../specs/16-async-file-io.md)、
[spec 25](../specs/25-blocking-fd-offload.md)、[spec 51](../specs/51-io-errors.md)、
[spec 53](../specs/53-io-timeouts.md)

---

## 7. モジュールと import

### 7.1 import の書き方

```rw
import geo                          # geo.f(...), geo.Vec(...), geo.PI
import geo as g                     # g.f(...)
from geo import Vec, area as circle_area, PI
```

- `import m` はモジュール `m` を読み込み、`m.f(...)`、`m.Class(...)`、
  型 `m.Class`、定数 `m.NAME`、関数値 `m.f` を使えるようにします。
- `import m as a` は修飾子を `a` にします。同じ修飾子を 2 回使うとエラーです。
- `from m import x [as y], ...` は関数、クラス、定数を修飾なしの名前で取り
  込みます。型エイリアスは取り込めません。取り込んだ名前が同じモジュールの
  関数・クラス・組み込み関数・他の取り込み名と衝突するとエラーです。
- `import` はモジュールの先頭、`def` / `type` / `class` / 定数より前に書きます。
- 名前空間はモジュールごとに分かれ、別のモジュールに同じ名前の関数が
  あっても衝突しません。
- 取り込んだモジュールで起きたエラーは、そのモジュールのファイル名と行で
  報告されます。

### 7.2 探索パス

`import foo` は次の順に `foo.rw` を探します。

1. エントリファイルと同じディレクトリ
2. 標準ライブラリのディレクトリ（リポジトリの `stdlib/`、環境変数
   `RW_STDLIB` で変更可能）

同名ならローカルのモジュールが標準ライブラリより優先されます。見つから
なければ、両方のパスを示してエラーになります。取り込まれたモジュールが
さらに import する場合も、探索の起点はエントリファイルのディレクトリです。

### 7.3 規則

- 取り込まれるモジュールは `main` を定義してはいけません。
- 循環 import（エントリファイル自身や自分自身の import を含む）はエラー
  です。エラーメッセージに循環の経路が表示されます。
- 1 つのモジュールは何度 import されても 1 回だけ読み込まれます。

### 7.4 標準ライブラリ

`stdlib/` には rw で書かれた次のモジュールがあります。各モジュールの API は
[標準ライブラリのドキュメント](../stdlib.md) を見てください。

| モジュール | 内容 | 仕様 |
|---|---|---|
| `bufio` | バッファ付きリーダ（`read_line`、`read_until`、`read_exact`） | [spec 54](../specs/54-stdlib-bufio.md) |
| `json` | JSON の解析と出力 | [spec 58](../specs/58-json.md) |
| `path` | POSIX パス文字列の操作 | [spec 59](../specs/59-fs-path.md) |
| `fs` | ファイルとディレクトリの操作 | [spec 59](../specs/59-fs-path.md) |
| `log` | レベル付きログ | [spec 60](../specs/60-log.md) |
| `process` | 子プロセス | [spec 61](../specs/61-process.md) |
| `url` | パーセントエンコーディングとクエリ文字列 | [spec 62](../specs/62-url-web-requests.md) |
| `template` | Mustache 風テンプレート | [spec 66](../specs/66-template.md) |

詳細: [spec 17](../specs/17-namespaced-imports.md)、[spec 54](../specs/54-stdlib-bufio.md)

---

## 8. 組み込み関数リファレンス

組み込み関数は 109 個あります。すべて修飾なしで呼び、キーワード引数は
使えず、値として取り出すこともできません。表の `T`、`K`、`V` は任意の型
（`void` を除く）を表します。

I/O 系の関数が返す「ハンドル」は `int` です。現在は OS の fd ですが、
不透明な値として扱ってください。失敗すると多くの関数は -1（`read` は空の
`Bytes`）を返し、`last_error()` に原因を残します（[9.3](#93-io-エラーlast_error)）。

### 8.1 出力・汎用

| 関数 | 説明 |
|---|---|
| `print(x: int \| float \| bool \| string) -> void` | 値と改行を標準出力に書く。`float` は `%g` |
| `len(x: string \| Bytes \| List[T] \| Dict[K, V] \| Channel[T]) -> int` | バイト数、要素数、エントリ数、チャネルのバッファ中の値の数 |
| `assert_eq(a: T, b: T) -> void` | `T` は `int`、`float`、`bool`、`string`。異なれば位置と両辺を標準エラー出力に書いて終了ステータス 1 で終わる |

### 8.2 型変換

| 関数 | 説明 |
|---|---|
| `int(x: int \| float \| bool) -> int` | `float` は 0 方向に切り捨て、`bool` は 0 / 1 |
| `float(x: int \| float) -> float` | |
| `str(x: int \| float \| bool \| string) -> string` | `float` は最短の往復表記 |

### 8.3 文字列

インデックスと長さはすべてバイト単位です。「空白」は ASCII の空白
（空白、`\t`、`\n`、`\r`、`\v`、`\f`）です。

| 関数 | 説明 |
|---|---|
| `str_find(s: string, sub: string) -> int` | 最初に現れる位置。なければ -1。`sub` が空なら 0 |
| `str_rfind(s: string, sub: string) -> int` | 最後に現れる位置。なければ -1。`sub` が空なら `len(s)` |
| `str_slice(s: string, start: int, end: int) -> string` | バイト `[start, end)`。両端は `[0, len(s)]` に丸め、`start >= end` なら `""`。負の値は末尾からではない |
| `str_starts_with(s: string, prefix: string) -> bool` | |
| `str_ends_with(s: string, suffix: string) -> bool` | |
| `str_contains(s: string, sub: string) -> bool` | `str_find(s, sub) >= 0` |
| `str_split(s: string, sep: string) -> List[string]` | `sep` で区切る。`"a,,b"` は `["a", "", "b"]`。`sep` が空なら `[s]` |
| `str_join(parts: List[string], sep: string) -> string` | 間に `sep` を挟んで連結 |
| `str_strip(s: string) -> string` | 前後の空白を除く |
| `str_lstrip(s: string) -> string` | 先頭の空白を除く |
| `str_rstrip(s: string) -> string` | 末尾の空白を除く |
| `str_replace(s: string, old: string, new: string) -> string` | 重ならない `old` をすべて左から置換。`old` が空なら `s` |
| `str_lower(s: string) -> string` | ASCII 英字を小文字に |
| `str_upper(s: string) -> string` | ASCII 英字を大文字に |
| `str_from_int(n: int) -> string` | 10 進表記（`str(n)` と同じ） |
| `str_to_int(s: string) -> Option[int]` | 省略可能な `-` と 1 つ以上の数字だけからなれば値。空、空白、`+`、余分な文字、オーバーフローは `None` |
| `str_to_float(s: string) -> Option[float]` | `strtod` の構文で解析。余りがある、または先頭が空白なら `None` |
| `byte_at(s: string, i: int) -> int` | 位置 `i` のバイト（0–255）。範囲外は -1 |
| `chr(cp: int) -> string` | コードポイント `cp` の UTF-8 表現。不正な値（負、サロゲート、U+10FFFF 超）は U+FFFD |
| `str_from_byte(b: int) -> string` | `b` の下位 8 ビットからなる 1 バイトの文字列 |

詳細: [spec 18](../specs/18-string-search.md)、[spec 28](../specs/28-string-builtins.md)、
[spec 55](../specs/55-string-extras.md)、[spec 58](../specs/58-json.md)、
[spec 62](../specs/62-url-web-requests.md)

### 8.4 Bytes

| 関数 | 説明 |
|---|---|
| `bytes_from_str(s: string) -> Bytes` | 同じバイト列の `Bytes` |
| `str_from_bytes(b: Bytes) -> string` | 同じバイト列の `string`（UTF-8 の検査はしない） |

### 8.5 リスト

| 関数 | 説明 |
|---|---|
| `list_new() -> List[T]` | 空のリスト。`T` は期待型から（なければ `int`） |
| `list_push(xs: List[T], v: T) -> List[T]` | `v` を末尾に加えた新しいリスト。`xs` は変わらない |
| `list_at(xs: List[T], i: int) -> T` | `i` 番目の要素。`i < 0` または範囲外は実行時エラー（負のインデックスは使えない） |
| `list_at_opt(xs: List[int], i: int) -> Option[int]` | 範囲外なら `None`。`List[int]` 専用 |

### 8.6 辞書

| 関数 | 説明 |
|---|---|
| `dict_new() -> Dict[K, V]` | 空の辞書。型は期待型から（なければエラー） |
| `dict_get(d: Dict[K, V], k: K) -> Option[V]` | 値、なければ `None` |
| `dict_has(d: Dict[K, V], k: K) -> bool` | |
| `dict_remove(d: Dict[K, V], k: K) -> bool` | 削除する。あったら `true` |
| `dict_keys(d: Dict[K, V]) -> List[K]` | キーを挿入順に |
| `dict_values(d: Dict[K, V]) -> List[V]` | 値を挿入順に |

### 8.7 数学

| 関数 | 説明 |
|---|---|
| `sqrt(x: float) -> float` | 平方根 |
| `floor(x: float) -> float` | 切り下げ |
| `ceil(x: float) -> float` | 切り上げ |
| `exp(x: float) -> float` | e の x 乗 |
| `log(x: float) -> float` | 自然対数 |
| `sin(x: float) -> float` | 正弦（ラジアン） |
| `cos(x: float) -> float` | 余弦（ラジアン） |
| `fabs(x: float) -> float` | 絶対値 |
| `pow(x: float, y: float) -> float` | x の y 乗 |

`int` を渡すときは `float(n)` で変換します。

### 8.8 時間

| 関数 | 説明 |
|---|---|
| `sleep(ms: int) -> void` | 少なくとも `ms` ミリ秒待つ |
| `time_ms() -> int` | Unix 時刻（ミリ秒） |
| `mono_ns() -> int` | 単調時計（ナノ秒） |

### 8.9 乱数

| 関数 | 説明 |
|---|---|
| `rand_int(lo: int, hi: int) -> int` | `[lo, hi]`（両端を含む）の一様乱数。`lo > hi` は実行時エラー |
| `rand_float() -> float` | `[0, 1)` の一様乱数 |
| `rand_seed(s: int) -> void` | 以後の `rand_int` / `rand_float` の系列を再現可能にする |
| `rand_bytes(n: int) -> Bytes` | OS の暗号論的乱数から `n` バイト。秘密の値にはこれを使う |

詳細: [spec 43](../specs/43-random.md)

### 8.10 並行処理

| 関数 | 説明 |
|---|---|
| `await_all(fs: List[Future[T]]) -> List[T]` | すべて待ち、結果をリスト順に |
| `await_any(fs: List[Future[T]]) -> tuple[int, T]` | 最初に終わったもののインデックスと値。空は実行時エラー |
| `chan_new(capacity: int) -> Channel[T]` | 型は期待型から。容量 0 はランデブー。負は実行時エラー |
| `chan_send(ch: Channel[T], v: T) -> void` | 満杯ならブロック。閉じていれば実行時エラー |
| `chan_recv(ch: Channel[T]) -> Option[T]` | 届くまでブロック。閉じて空なら `None` |
| `chan_close(ch: Channel[T]) -> void` | チャネルを閉じる |
| `mutex_new() -> Mutex` | |
| `mutex_lock(m: Mutex) -> void` | 取得できるまで待つ（FIFO） |
| `mutex_unlock(m: Mutex) -> void` | ロックされていなければ実行時エラー |
| `mutex_try_lock(m: Mutex) -> bool` | 待たずに取得を試みる |

### 8.11 OS

| 関数 | 説明 |
|---|---|
| `os_args() -> List[string]` | コマンドライン引数。先頭はプログラム名 |
| `os_getenv(name: string) -> Option[string]` | 未設定なら `None` |
| `os_setenv(name: string, value: string) -> bool` | 上書きで設定。失敗で `false` |
| `os_exit(code: int) -> void` | 標準出力と標準エラー出力をフラッシュして終了。`defer` は実行されず、ファイバーも待たない |
| `os_getcwd() -> string` | カレントディレクトリ |
| `os_chdir(path: string) -> bool` | 失敗で `false` |
| `signal_wait(names: List[string]) -> string` | 指定したシグナルのどれかが届くまで待ち、その名前を返す |

`signal_wait` が受け付ける名前は `"SIGINT"`、`"SIGTERM"`、`"SIGHUP"`、
`"SIGQUIT"`、`"SIGUSR1"`、`"SIGUSR2"` で、どれも認識できなければすぐ `""` を
返します。一度名前を挙げたシグナルは既定の動作（プロセス終了）を失い、
以後は `signal_wait` でだけ受け取ります。待つ者がいない間に届いたシグナルは
数えられ、後の呼び出しで 1 回ずつ返ります。

詳細: [spec 40](../specs/40-os-api.md)、[spec 57](../specs/57-signals-graceful-shutdown.md)

### 8.12 プロセス

| 関数 | 説明 |
|---|---|
| `proc_start(argv: List[string]) -> List[int]` | `argv[0]` を `PATH` から探して起動し、`[pid, stdin, stdout, stderr]`（親側のパイプのハンドル）を返す。失敗で `[]` |
| `proc_wait(pid: int) -> int` | 終了を待つ。終了ステータス、シグナル N で終わったら `128 + N`、エラーで -1 |
| `proc_kill(pid: int, sig: string) -> int` | シグナルを名前で送る（`"SIGTERM"`、`"SIGKILL"` など）。0、エラーで -1 |

シェルは介在しません。通常は標準ライブラリの `process` モジュールを使います。

詳細: [spec 61](../specs/61-process.md)

### 8.13 ファイルシステム

| 関数 | 説明 |
|---|---|
| `fs_kind(p: string) -> string` | `"file"`、`"dir"`、`"other"`。エラーで `""`。シンボリックリンクはたどる |
| `fs_size(p: string) -> int` | バイト数。エラーで -1 |
| `fs_mtime_ms(p: string) -> int` | 更新時刻（Unix ミリ秒）。エラーで -1 |
| `fs_read_dir(p: string) -> List[string]` | エントリ名をバイト順に。`.` と `..` は含まない |
| `fs_rename(src: string, dst: string) -> int` | 0、エラーで -1 |
| `fs_remove(p: string) -> int` | ファイルか空のディレクトリを削除。0、エラーで -1 |
| `fs_mkdir(p: string) -> int` | モード 0755 で作成。0、エラーで -1 |

詳細: [spec 59](../specs/59-fs-path.md)

### 8.14 I/O

| 関数 | 説明 |
|---|---|
| `read(h: int, max: int) -> Bytes` | 最大 `max` バイト読む。EOF、エラー、タイムアウトで空。EOF では `last_error()` が 0 |
| `write(h: int, b: Bytes) -> int` | 書いたバイト数（一部だけのこともある）。エラーで -1 |
| `write_all(h: int, b: Bytes) -> int` | すべて書く。`len(b)`、エラーで -1（一部は送られているかもしれない） |
| `close(h: int) -> int` | 閉じる。0、エラーで -1。そのハンドルで待っているファイバーは `EBADF` で起こされる |
| `open(addr: string, mode: string) -> int` | `file:///絶対パス` か `:` を含まないパスを開く。`mode` は `"r"`、`"w"`（作成・切り詰め）、`"a"`（作成・追記） |
| `file_open(path: string, mode: string) -> int` | 旧 API。`open` と同じモード |

ハンドル `0`、`1`、`2` は標準入力、標準出力、標準エラー出力です。

詳細: [spec 15](../specs/15-file-io.md)、[spec 50](../specs/50-dial-listen-open.md)、
[spec 52](../specs/52-write-all.md)

### 8.15 ネットワーク

アドレスは `tcp://host:port`、`udp://host:port`、`unix:///絶対パス` の形式です。
IPv6 は `tcp://[::1]:80` のように角括弧で囲みます。`listen` ではホストを
省略した `tcp://:port` で IPv4 のすべてのインターフェースに、ポート 0 で
空きポートにバインドします。

| 関数 | 説明 |
|---|---|
| `dial(addr: string) -> int` | 接続する（`tcp`、`unix`、`udp`）。`udp` は接続済みソケット |
| `dial_timeout(addr: string, ms: int) -> int` | 接続に `ms` ミリ秒の上限を付けた `dial`（名前解決は含まない） |
| `listen(addr: string) -> int` | バインドして待ち受ける（`tcp`、`unix` はバックログ 128。`udp` はバインドのみ） |
| `accept(ln: int) -> int` | 次の接続のハンドル（`tcp`、`unix`）。エラーで -1 |
| `local_addr(h: int) -> string` | 自分のアドレス（`dial` に渡せる形式）。エラーで `""` |
| `peer_addr(h: int) -> string` | 相手のアドレス。エラーで `""` |
| `recv_from(h: int, max: int) -> tuple[Bytes, string]` | UDP のデータグラム 1 つと送信元 `"udp://ip:port"`。エラーやタイムアウトでは両方空 |
| `send_to(h: int, data: Bytes, addr: string) -> int` | `addr`（`udp://`）にデータグラムを 1 つ送る。送ったバイト数、エラーで -1 |
| `shutdown(h: int, how: string) -> int` | 半クローズ。`how` は `"read"`、`"write"`、`"both"`。0 / -1 |
| `set_nodelay(h: int, on: bool) -> int` | `TCP_NODELAY`。0 / -1 |
| `set_keepalive(h: int, on: bool) -> int` | `SO_KEEPALIVE`。0 / -1 |
| `set_backlog(ln: int, n: int) -> int` | 待ち受けキューの長さ（`n < 1` は `EINVAL`、65535 で頭打ち）。0 / -1 |
| `set_read_timeout(h: int, ms: int) -> void` | `read`、`accept`、`recv_from` 1 回ごとの上限。`ms <= 0` で無効 |
| `set_write_timeout(h: int, ms: int) -> void` | `write`、`write_all`、`send_to` 1 回ごとの上限。`ms <= 0` で無効 |
| `tcp_listen(port: int) -> int` | 旧 API。IPv4 の全インターフェースで待ち受ける |
| `tcp_accept(ln: int) -> int` | 旧 API。`accept` と同じ |

タイムアウトはソケットにだけ効き、`last_error()` は `ETIMEDOUT` になります。
ハンドルを閉じたり、同じ fd 番号で新しいハンドルを作ったりすると解除されます。

詳細: [spec 50](../specs/50-dial-listen-open.md)、[spec 53](../specs/53-io-timeouts.md)、
[spec 56](../specs/56-socket-options.md)、[spec 69](../specs/69-udp.md)

### 8.16 エラー

| 関数 | 説明 |
|---|---|
| `last_error() -> int` | このファイバーで直前の I/O 系組み込み関数の結果。0 は成功、それ以外は errno 値 |
| `error_name(e: int) -> string` | `"ECONNREFUSED"` などの名前。0 なら `""`、未知の値は `"E<n>"` |
| `error_message(e: int) -> string` | `strerror` の説明文。0 なら `""` |
| `set_last_error(e: int) -> void` | `last_error()` の値を設定する。後始末の `close` などで上書きされた原因を戻すときに使う |

### 8.17 GC

| 関数 | 説明 |
|---|---|
| `gc_collect() -> void` | 今すぐ完全なガベージコレクションを行う |
| `gc_heap_size() -> int` | GC ヒープの大きさ（バイト） |

詳細: [spec 67](../specs/67-garbage-collection.md)

---

## 9. エラーと実行時チェック

rw に例外はありません。失敗しうる操作は `Option` / `Result` か -1 などの
番兵値で結果を返し、回復できない誤りはプロセスを終了させます（パニック）。
パニックを捕まえる手段はありません。

### 9.1 コンパイル時のエラー

型の不一致、未定義の名前、引数の数の誤り、`return` の欠落などは
コンパイル時に検出されます。コンパイラは最初のエラーで止まります。

### 9.2 実行時チェック

| 状況 | メッセージ（標準エラー出力） | 終わり方 |
|---|---|---|
| `assert` の失敗 | `rw: assertion failed: <メッセージ>` | 終了ステータス 1 |
| `assert_eq` の失敗 | `rw: assert_eq failed in <関数> (line N)` と両辺 | 終了ステータス 1 |
| 整数の 0 除算（`/`、`%`） | `rw: assertion failed: division by zero in <関数> (line N)` | 終了ステータス 1 |
| `INT64_MIN / -1` | `rw: assertion failed: integer overflow in division in <関数> (line N)` | 終了ステータス 1 |
| リストのインデックス範囲外（`xs[i]`、`list_at`） | `rw: list_at: index out of bounds` | abort（SIGABRT） |
| 文字列のインデックス範囲外 | `rw: string index out of range` | abort |
| 辞書にキーがない（`d[k]`） | `rw: dict key not found: <キー>` | abort |
| 閉じたチャネルへの送信 | `rw: send on a closed channel` | abort |
| ロックされていない `Mutex` のアンロック | `rw: unlock of a mutex that is not locked` | abort |
| `rand_int(lo, hi)` で `lo > hi` | `rw: rand_int: lo is greater than hi` | abort |
| 空のリストへの `await_any` | `rw: await_any of an empty list` | abort |
| `chan_new` の容量が負 | `rw: chan_new: negative capacity` | abort |

- 整数除算のチェックは `--unchecked` で省けます。省いた場合の 0 除算は
  未定義動作です。`INT64_MIN % -1` はチェックの有無にかかわらず 0 です。
- `+`、`-`、`*` のオーバーフローはチェックされず、折り返します。
- 浮動小数点の除算は IEEE 754 に従います（`inf`、`nan`）。
- `rwc run` は子プロセスの終了ステータスを返します。abort の場合は
  シグナルによる終了になります。

詳細: [spec 42](../specs/42-checked-division.md)

### 9.3 I/O エラー（last_error）

I/O 系の組み込み関数は失敗を -1 や空の `Bytes` で返し、原因を errno 形式で
ファイバーごとに保存します。`last_error()` は直前の I/O 系関数の結果
（成功なら 0）を返します。errno の番号は OS によって異なるので、
`error_name` の名前で比較します。

```rw
def main() -> int:
    c = dial("tcp://127.0.0.1:1")
    if c < 0:
        print(error_name(last_error()))    # 例: ECONNREFUSED
    fd = open("/no/such/file", "r")
    if fd < 0:
        print(error_message(last_error()))
    return 0
```

- `read` が空を返したとき、`last_error()` が 0 なら EOF、それ以外ならエラー
  です。
- 不正なアドレスや未対応の `open` モードは `EINVAL`、名前解決の失敗は
  rw 独自の `ENOHOST`（10001）です。
- `last_error()` を更新するのは I/O、ネットワーク、ファイルシステム、
  プロセスの組み込み関数です。それ以外の関数は値を変えません。
- `SIGPIPE` は無視されるので、閉じた相手への書き込みはプロセスを殺さず
  `EPIPE` などのエラーになります。

詳細: [spec 51](../specs/51-io-errors.md)

### 9.4 テスト（rwc test と assert_eq）

```rw
# test_math.rw
def add(a: int, b: int) -> int:
    return a + b

def test_add() -> void:
    assert_eq(add(2, 3), 5)
```

- テストファイルは `test_*.rw` または `*_test.rw` という名前の普通の
  モジュールで、`main` を定義しません。
- テストは引数のないトップレベル関数 `test_*` です。戻り値は無視されます。
- 各テストは別プロセスで並列に実行され、正常に戻れば成功、`assert` や
  `assert_eq` の失敗、実行時チェック、`os_exit(n)`（n ≠ 0）、クラッシュ、
  タイムアウトで失敗です。
- `assert_eq` は文字列をエスケープ付きの引用形式で表示し、複数行なら最初に
  異なる行も示します。`float` は `==` で比較し、17 桁で表示します。

詳細: [spec 68](../specs/68-rwc-test.md)

---

## 10. 仕様書の索引

各機能の設計の経緯は次の仕様書にあります。spec 01–06 は初期の MVP の
記述で、現在の実装とは異なる点があります（型推論がない、`for` がない、
など）。

| 分野 | 仕様書 |
|---|---|
| 全体・MVP | [01](../specs/01-overview.md), [02](../specs/02-syntax-and-types.md), [03](../specs/03-runtime-and-irgen.md), [04](../specs/04-diagnostics-and-testing.md) |
| ファイバーとスケジューラ | [05](../specs/05-fibers.md), [06](../specs/06-scheduler-mn.md), [19](../specs/19-fiber-lifecycle.md), [23](../specs/23-preemption.md), [24](../specs/24-park-after-save.md) |
| 文字列と Bytes | [07](../specs/07-string-builtins.md), [08](../specs/08-bytes-type.md), [18](../specs/18-string-search.md), [28](../specs/28-string-builtins.md), [55](../specs/55-string-extras.md) |
| コレクション | [09](../specs/09-list-type.md), [22](../specs/22-generic-list.md), [29](../specs/29-list-syntax.md), [31](../specs/31-dict.md), [36](../specs/36-dict-literals.md), [37](../specs/37-tuples.md) |
| Option / Result / match | [10](../specs/10-option-type.md), [11](../specs/11-result-type.md), [30](../specs/30-generic-option-result.md), [38](../specs/38-value-match.md) |
| 構文 | [13](../specs/13-for-range-loop.md), [14](../specs/14-ternary-expr.md), [27](../specs/27-compound-assignment.md), [32](../specs/32-local-inference.md), [34](../specs/34-fstrings.md), [35](../specs/35-constants.md), [41](../specs/41-default-keyword-args.md), [47](../specs/47-with-defer.md) |
| レコード・関数値・クロージャ | [20](../specs/20-records.md), [21](../specs/21-function-values.md), [33](../specs/33-methods.md), [71](../specs/71-closures.md) |
| 変換と演算 | [26](../specs/26-conversions.md), [42](../specs/42-checked-division.md) |
| モジュール | [17](../specs/17-namespaced-imports.md), [54](../specs/54-stdlib-bufio.md) |
| 並行処理 | [39](../specs/39-sleep-and-clocks.md), [44](../specs/44-channels.md), [45](../specs/45-await-all-any.md), [46](../specs/46-mutex.md) |
| I/O とネットワーク | [12](../specs/12-netpoller-tcp.md), [15](../specs/15-file-io.md), [16](../specs/16-async-file-io.md), [25](../specs/25-blocking-fd-offload.md), [50](../specs/50-dial-listen-open.md), [51](../specs/51-io-errors.md), [52](../specs/52-write-all.md), [53](../specs/53-io-timeouts.md), [56](../specs/56-socket-options.md), [69](../specs/69-udp.md) |
| OS・プロセス・ファイルシステム | [40](../specs/40-os-api.md), [43](../specs/43-random.md), [57](../specs/57-signals-graceful-shutdown.md), [59](../specs/59-fs-path.md), [61](../specs/61-process.md) |
| 標準ライブラリ・Web | [58](../specs/58-json.md), [60](../specs/60-log.md), [62](../specs/62-url-web-requests.md), [63](../specs/63-web-static-middleware.md), [64](../specs/64-web-keepalive.md), [65](../specs/65-web-chunked-requests.md), [66](../specs/66-template.md) |
| ツールとランタイム | [48](../specs/48-rwc-check.md), [49](../specs/49-did-you-mean.md), [67](../specs/67-garbage-collection.md), [68](../specs/68-rwc-test.md), [70](../specs/70-optimization-levels.md) |
