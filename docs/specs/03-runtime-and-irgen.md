# rw ランタイム ABI と IR 生成方針

## ランタイム ABI

ヘッダ `runtime/runtime.h`:

```c
#ifndef RW_RUNTIME_H
#define RW_RUNTIME_H
#include <stdint.h>

typedef struct { int64_t len; const char *ptr; } rw_str;
typedef struct rw_future rw_future_t;

/* print */
void rw_print_i64(int64_t v);
void rw_print_f64(double v);
void rw_print_bool(int8_t v);
void rw_print_str(rw_str s);

/* 文字列ヘルパ */
rw_str rw_str_from_cstr(const char *cstr, int64_t len);

/* spawn / await(戻り値型ごとに分離) */
rw_future_t *rw_spawn_i64 (int64_t (*fn)(void *), void *args);
rw_future_t *rw_spawn_f64 (double  (*fn)(void *), void *args);
rw_future_t *rw_spawn_bool(int8_t  (*fn)(void *), void *args);
rw_future_t *rw_spawn_str (rw_str  (*fn)(void *), void *args);
rw_future_t *rw_spawn_void(void    (*fn)(void *), void *args);

int64_t rw_await_i64 (rw_future_t *f);
double  rw_await_f64 (rw_future_t *f);
int8_t  rw_await_bool(rw_future_t *f);
rw_str  rw_await_str (rw_future_t *f);
void    rw_await_void(rw_future_t *f);

/* プロセス init / shutdown(main 冒頭・末尾で rwc が挿入) */
void rw_init(void);
void rw_shutdown(void);

#endif
```

実装方針(runtime.c):
- `rw_spawn_*` は内部で `pthread_create` を呼び、`rw_future_t` に
  スレッド ID と結果格納域を保持する
- `rw_await_*` は `pthread_join` 後、結果を返して `rw_future_t` を `free` する
- `rw_print_*` は単純に `printf` 系を呼ぶ
- `rw_init` / `rw_shutdown` は MVP では空でよい(将来のスレッドプール用)

## `spawn f(a, b)` の IR 展開

rw コード:
```python
fut: Future[int] = spawn add(3, 4)
```

rwc が生成するもの:

1. **クロージャ構造体** を匿名で定義:
   ```llvm
   %closure_add_0 = type { i64, i64 }
   ```

2. **トランポリン関数** を生成(呼び出しサイトごとにユニーク):
   ```llvm
   define i64 @rw_trampoline_add_0(i8* %args) {
       %p  = bitcast i8* %args to %closure_add_0*
       %ap = getelementptr %closure_add_0, %closure_add_0* %p, i32 0, i32 0
       %bp = getelementptr %closure_add_0, %closure_add_0* %p, i32 0, i32 1
       %a  = load i64, i64* %ap
       %b  = load i64, i64* %bp
       %r  = call i64 @rw_user_add(i64 %a, i64 %b)
       call void @free(i8* %args)
       ret i64 %r
   }
   ```

3. **呼び出し側**(spawn 式の展開):
   ```llvm
   %args = call i8* @malloc(i64 16)
   ; %a, %b を struct にストア
   %fut  = call %rw_future_t* @rw_spawn_i64(
       i64 (i8*)* @rw_trampoline_add_0, i8* %args)
   ```

## `await fut` の IR 展開

戻り値型は Sema で既知。対応する `rw_await_*` を直接呼ぶ:
```llvm
%v = call i64 @rw_await_i64(%rw_future_t* %fut)
```

## main 関数

rwc は `def main() -> int:` を必須とし、生成 IR では:

```llvm
define i32 @main() {
    call void @rw_init()
    %r   = call i64 @rw_user_main()
    call void @rw_shutdown()
    %r32 = trunc i64 %r to i32
    ret i32 %r32
}
```

`@rw_user_main` はユーザー定義 `main` を改名したもの。

## メモリ管理(MVP)

| 対象 | 方針 |
|---|---|
| 文字列リテラル | `.rodata` に置く。`rw_str` は長さ+ポインタ。解放しない |
| クロージャ構造体 | `malloc` / トランポリン末尾で `free` |
| Future | `rw_await_*` 内で `free` |
| ユーザー定義型 | MVP では存在しない |

GC は導入しない。文字列を動的生成しない限りリークは発生しない。

将来 `list[T]` や文字列連結など動的なヒープ確保を伴う機能を入れる際、
メモリ管理方針(ARC / マーク&スイープ GC / 所有権)を選ぶ必要がある。
**現時点では決定を保留する**。判断材料がそろうのは:

- ユーザーが何を書きたいか(Web サーバー? 数値計算? Python ライブラリ連携?)
- 並行性能(fiber と GC の相性、ARC のアトミック retain/release のコスト)
- 標準ライブラリの形(文字列・配列・dict が言語仕様にどう収まるか)

がはっきりしてからになる。`docs/specs/06-memory-tbd.md` をいずれ作成して
論点を整理する予定。

## エラー処理(MVP)

例外なし、Result 型なし。実行時に死ぬ条件:
- 整数ゼロ除算 → LLVM `sdiv` の未定義動作に任せる
- スレッド生成失敗 → `rw_spawn_*` 内で `perror` + `exit(1)`
- malloc 失敗 → 同上

## 将来予約(MVP では未実装エラー)

```python
extern "c" def name(arg: int) -> int
```

この構文は Lexer/Parser では受理し、Sema で「未実装」エラーを出す。
将来 Phase 2+ で実装するときの後方互換のため。
