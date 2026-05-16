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
- 代入: `=`(再代入可、ただし型は不変)

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

`for` は MVP では未対応(イテレータ概念が必要なため)。

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
