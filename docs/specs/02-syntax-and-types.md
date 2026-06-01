# rw 構文と型システム

## リテラルと基本型

| 型 | リテラル例 | LLVM 表現 |
|---|---|---|
| `int` | `42`, `-7`, `0` | `i64` |
| `float` | `3.14`, `-0.5` | `double` |
| `bool` | `true`, `false` | `i1`(関数 ABI 上は `i8`) |
| `string` | `"hello"` | `{i64 len, i8* ptr}`(不変、連結不可) |
| `Future[T]` | リテラルなし | `i8*`(不透明) |

## 演算子

- 算術: `+ - * / %`(同じ型同士のみ)
- 比較: `== != < <= > >=`
- 論理: `and`, `or`, `not`
- 条件式(三項): `then if cond else els`(最も低い優先度、右結合)
- 代入: `=`(再代入可、ただし型は不変)

### 条件式(三項演算子)

Python 互換の `then if cond else els`。`cond` は `bool`、`then` と `els` は
同じ型で、その型が式全体の型になる(暗黙の型昇格はしない)。選ばれたブランチ
のみが評価される。

```python
larger: int = a if a > b else b
label: string = "even" if a % 2 == 0 else "odd"
# 右結合: a if p else (b if q else c)
sign: int = 1 if n > 0 else 0 if n == 0 else -1
```

詳細は [`14-ternary-expr.md`](14-ternary-expr.md) を参照。

## 関数定義

```python
def add(a: int, b: int) -> int:
    return a + b

def greet(name: string) -> void:
    print(name)
```

- 引数と戻り値の型注釈は **必須**
- 戻り値なしは `-> void`
- ローカル変数も型注釈必須:
  ```python
  x: int = 1 + 2
  ```

## 制御構文

```python
if x > 0:
    print("positive")
elif x == 0:
    print("zero")
else:
    print("negative")

while i < 10:
    i = i + 1
```

条件で値を選ぶときは、文の `if`/`else` の代わりに条件式(三項演算子)も使える:

```python
larger: int = a if a > b else b
```

`for ... in range(...)` のカウントループも利用できる(詳細は
[`13-for-range-loop.md`](13-for-range-loop.md))。`for <var> in <list>` の
イテレータ形式は未対応。

## 非同期構文

```python
def fetch(n: int) -> int:
    return n * 2

f: Future[int] = spawn fetch(21)
result: int = await f
print(result)
```

- `spawn 式` は **関数呼び出しを別スレッドで実行** し、`Future[T]` を返す
- `await 式` は `Future[T]` を完了まで待ち `T` を取り出す
- `spawn` の対象は **関数呼び出しのみ**(任意の式ではない)

## main 関数

```python
def main() -> int:
    print("hello")
    return 0
```

`main` は必須。戻り値はプロセス終了コード(`i64` → `i32` に切り詰め)。

## コメント

```python
# 行コメント。複数行は # を連ねる。
```

## 予約語

```
def return if elif else while and or not true false void
spawn await Future
int float bool string
# 将来用に予約(MVP では構文エラー):
extern class import for in as None
```

## 字句構造の注意点

- インデントはスペース 4 個推奨だが、ファイル内で一貫していれば 2 でも tab でもよい
- タブとスペースの混在は **同じブロック内で禁止**(Lexer がエラー)
- 空行・コメント行はインデント計算から除外
