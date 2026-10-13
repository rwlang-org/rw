# rw ランタイム設計

この文書は、rw のランタイム（`runtime/` 以下の C コードと `runtime/librw.a`）と、コンパイラ `rwc` がランタイムに対して何を約束しているかを説明します。対象読者は、ランタイムを読む人、ランタイムに機能を足す人、ランタイムのバグを追う人です。

記述の正はコードです。設計の経緯や却下した代替案は `docs/specs/` の各仕様書にあり、本文では「詳細: spec NN」の形で参照します。仕様書とコードが食い違う箇所は、末尾の「付録: 仕様書・コメントとの差分」にまとめています。

この文書の英語版は [`docs/runtime.md`](../runtime.md) にあります。

## 目次

1. [全体像](#1-全体像)
2. [値表現と ABI](#2-値表現と-abi)
3. [ファイバとスケジューラ](#3-ファイバとスケジューラ)
4. [I/O](#4-io)
5. [タイマー・同期・シグナル・プロセス・ファイルシステム](#5-タイマー同期シグナルプロセスファイルシステム)
6. [メモリ管理](#6-メモリ管理)
7. [コンパイラ側の保証](#7-コンパイラ側の保証)
8. [ランタイム拡張のチェックリスト](#8-ランタイム拡張のチェックリスト)
9. [テストとデバッグ](#9-テストとデバッグ)
10. [付録: 仕様書・コメントとの差分](#付録-仕様書コメントとの差分)

---

## 1. 全体像

### 1.1 コンパイルパイプライン

```
 .rw ソース
    │  rwc/lexer.py        トークン列
    │  rwc/parser.py       AST
    │  rwc/desugar.py      糖衣構文の除去（範囲 for → VarDecl + While + Assign）
    │      ※ rwc/loader.py が import 先のモジュールも同じ 3 段で読み込む
    │  rwc/sema.py         型検査、呼び出し解決、クロージャの捕捉解析
    │  rwc/irgen.py        llvmlite で LLVM IR を生成
    ▼
 一時ファイル *.ll
    │  clang -O<level> x.ll runtime/librw.a -o out -L<gc>/lib -lgc -lpthread -lm
    ▼
 ネイティブ実行ファイル
```

`rwc/driver.py` の `compile_source` がこの流れをまとめています。

- `librw.a` が無ければ、最初のビルド時に `make -C runtime` を実行して作ります。
- 最適化レベルは `-O` フラグ、`$RW_OPT_LEVEL`、既定値 2 の順で決まり、clang にそのまま渡します（詳細: spec 70）。
- Boehm GC のリンクフラグは `scripts/gc-prefix.sh` が見つけたプレフィクスから組み立てます。プレフィクスが `/usr` と `/usr/local` 以外なら `-L<prefix>/lib -Wl,-rpath,<prefix>/lib` を付け、最後に `-lgc` を足します（詳細: spec 67）。
- `-lm` は、`llvm.pow` などの組み込み関数がバックエンドで libm 呼び出しに下ろされる場合に備えたものです。Linux では明示的なリンクが必要です。

### 1.2 librw.a の中身

`runtime/Makefile` は以下をまとめて静的ライブラリにします。CFLAGS は `-O2 -Wall -Wextra -fPIC -std=c11 -D_GNU_SOURCE` に GC のインクルードパスを加えたものです。

| ファイル | 役割 |
|---|---|
| `runtime.c` / `runtime.h` | 公開 ABI の宣言。print、assert、文字列、リスト、辞書、OS API、乱数、ライフサイクル（`rw_init` / `rw_shutdown`）、spawn / await の薄いラッパ |
| `io.c` | 汎用 `read` / `write` / `write_all` / `close` / `file_open`、エラー名の表、タイムアウト設定 |
| `aio.c` | ブロッキング syscall を肩代わりするスレッドプール |
| `timer.c` | タイマースレッドと最小ヒープ、`sleep`、時計 |
| `chan.c` | チャネルとミューテックス |
| `signal.c` | セルフパイプによる `signal_wait` |
| `fs.c` | ファイルシステムのビルトイン |
| `process.c` | 子プロセス（`posix_spawnp`） |
| `gc.c` / `rwgc.h` | Boehm GC のラッパ。`<gc/gc.h>` を include するのはこのファイルだけ |
| `fiber/fiber.c`, `fiber/fiber_{arm64,x86_64}.S` | コンテキストの初期化と切り替え |
| `fiber/sched.c` | M:N スケジューラ、プリエンプション、ファイバの寿命管理 |
| `fiber/runq.c` | P ごとの有界リングとグローバルキュー |
| `fiber/park.c` | 待ちリスト用の `atomic_flag` スピンロック |
| `net/netpoller.c` | netpoller の共通部分、fd ごとの poll descriptor |
| `net/netpoller_kqueue.c` / `net/netpoller_epoll.c` | OS ごとのイベントループ（macOS / Linux） |
| `net/tcp.c` | `tcp_listen` / `tcp_accept` |
| `net/addr.c` | アドレス文字列による `dial` / `listen` / `accept` / `open`、UDP、ソケットオプション |

アセンブリは `uname -m` で、netpoller の実装は `uname -s` で選ばれます。対応するのは arm64 / aarch64 / x86_64 と Darwin / Linux です。

### 1.3 生成される C main

irgen はユーザーの `main` を `@rw_user_main` に改名し、C の `main` を別途合成します（`IRGen._emit_c_main`）。

```c
int main(int argc, char **argv) {
    rw_set_args(argc, argv);   /* os_args() 用 (spec 40) */
    rw_init();
    int64_t r = rw_user_main();
    rw_shutdown();
    return (int32_t)r;         /* i64 を i32 に切り詰める */
}
```

`rw_init` と `rw_shutdown` は次の順で各サブシステムを起動・停止します。停止は起動の逆順です。

```
rw_init:      signal(SIGPIPE, SIG_IGN)
              rw_sched_init      GC 初期化 → ワーカー起動 → モニタ起動
              rw_netpoller_init  kqueue/epoll を開き netpoller スレッドを起動
              rw_aio_init        aio プールを起動
              rw_timer_init      タイマースレッドを起動

rw_shutdown:  rw_timer_shutdown → rw_aio_shutdown → rw_netpoller_shutdown → rw_sched_shutdown
```

`rw_user_main` はファイバではなくメインスレッドの上で直接動きます。メインスレッドはワーカーではないので、メインから呼ばれたブロッキング操作は park せずにスレッドごと止まります（4.7 節）。

`rw_sched_shutdown` は終了フラグを立ててワーカーを起こし、全ワーカーとモニタを `pthread_join` した後、キューに残った未実行のファイバを解放します。park したまま、あるいは実行中のまま残ったファイバは解放しません（プロセス終了で回収されます。詳細: spec 19）。ワーカーが終了フラグを見るのはファイバの合間だけなので、セーフポイントを通らないファイバが走り続けていると終了処理は待たされます。

### 1.4 実行時のスレッド

```
                      ┌──────────────────────────────────────┐
  main ───────────────┤ rw_user_main（ファイバではない）       │  GC 登録: あり（GC_INIT）
                      └──────────────────────────────────────┘
                        │ spawn → globq
                        ▼
  worker M0..Mn-1   ┌────────┐ ┌────────┐     ┌────────┐       GC 登録: あり
  (RW_WORKERS)      │ M0 / P0│ │ M1 / P1│ ... │ Mn / Pn│       各 M がファイバを実行
                    └────────┘ └────────┘     └────────┘
                        ▲  rw_sched_enqueue_ready（globq に push + cond_signal）
        ┌───────────────┼──────────────┬──────────────┬───────────────┐
  monitor            netpoller        aio pool        timer           signal
  1 ms ごとに        kqueue/epoll     RW_AIO_THREADS  最小ヒープ      セルフパイプを読む
  preempt を要求     で待機           ブロッキング    期限で起床       （初回 signal_wait で起動）
  GC 登録: なし      GC 登録: なし    GC 登録: あり   GC 登録: なし    GC 登録: なし
```

| スレッド | 本数 | 作成箇所 | 役割 | GC 登録 |
|---|---|---|---|---|
| main | 1 | プロセス | `rw_user_main` を実行。ファイバではない | あり（`GC_INIT`） |
| worker (M) | `RW_WORKERS`、既定はオンライン CPU 数、上限 64 | `rw_sched_init` | P のキューからファイバを取り出して実行 | あり（`worker_main` 冒頭） |
| monitor | 0 か 1（`RW_PREEMPT_MS=0` なら起動しない） | `rw_sched_init` | 長く走るファイバへの preempt 要求 | なし |
| netpoller | 1 | `rw_netpoller_init` | fd の準備完了を待ち、waiter を起こす | なし |
| aio pool | `RW_AIO_THREADS`、既定 4、上限 64 | `rw_aio_init` | ブロッキング read/write、`getaddrinfo`、`stat`、`waitpid` など | あり（読み込みバッファやディレクトリ一覧を割り当てるため） |
| timer | 1 | `rw_timer_init` | `sleep` とタイムアウトのコールバック | なし |
| signal | 0 か 1 | 初回の `rw_signal_wait`（`pthread_once`）、detach 済み | セルフパイプを読んで待ち手を起こす | なし |

GC に登録しないスレッドは GC ヒープに触れてはいけません。これは拡張時の規則でもあります（8 章）。

---

## 2. 値表現と ABI

### 2.1 型と LLVM 表現

`rwc/irgen.py` の `llvm_type_of` が rw の型を LLVM 型に対応づけます。

| rw の型 | LLVM 表現 | C 側の型 | 置き場所 |
|---|---|---|---|
| `int` | `i64` | `int64_t` | 値 |
| `float` | `double` | `double` | 値 |
| `bool` | `i8` | `int8_t` | 値（0 / 1） |
| `string`, `Bytes` | `{i64 len, i8* ptr}` | `rw_str` | 値。中身のバイト列は GC ヒープ（atomic）か定数 |
| `List[T]` | `{i64 len, i64 cap, i64* data}` | `rw_list_int` / `rw_list` | 構造体は値、`data` は GC ヒープ |
| `Option[T]` | `{i64 tag, T value}` | `rw_option_int` など | 値。tag は 0 = None、1 = Some |
| `Result[T, E]` | `{i64 tag, T ok, E err}` | — | 値。tag は 1 = Ok、0 = Err |
| タプル | 要素を並べた構造体 | — | 値 |
| レコード（クラス） | `i8*` | — | GC ヒープ上の構造体へのポインタ |
| `Dict[K, V]` | `i8*` | `rw_dict *` | GC ヒープ |
| `Channel[T]` | `i8*` | `rw_chan *` | GC ヒープ |
| `Mutex` | `i8*` | `rw_mutex *` | GC ヒープ |
| `Future[T]` | `i8*` | `rw_future_t *`（実体は `rw_fiber_handle *`） | uncollectable な GC メモリ |
| 関数値 | `{i8* code, i8* env}` | — | 値。`env` は GC ヒープか null |
| `void` | `void` | `void` | — |

### 2.2 文字列と Bytes

`rw_str` は `{int64_t len; const char *ptr;}` の 2 ワード構造体で、NUL 終端しません。`string` と `Bytes` は同じ表現です（詳細: spec 08）。

- 文字列は不変です。そのため `rw_str_slice`、`str_strip` 系、添字アクセス `rw_str_index` は元のバッファを共有するゼロコピーのスライスを返します。Boehm GC は内部ポインタを認識するので、スライスだけが残っても元のバッファは回収されません。
- 新しいバイト列（連結、変換、読み込みバッファ、`rand_bytes`）は `rw_gc_alloc_atomic` で確保します。中にポインタが無いので GC は走査しません。
- 文字列リテラルは `.str.N` という private な定数グローバルを指します。GC ヒープの外なので回収の対象になりません。
- 長さ 0 の文字列は `ptr == NULL` のことがあります。C 側で `ptr` を参照する前に `len` を確認してください。
- C API に渡すときは NUL 終端のコピーを `malloc` で作り、使い終わったら `free` します（`io.c` の `file_open_impl`、`fs.c` の `cpath`、`process.c` の `cstr`）。

### 2.3 リスト

`List[T]` はどの要素型でも `{len, cap, data}` の 3 ワードです（詳細: spec 09, 22）。`List[int]` は `rw_list_int`、それ以外は要素サイズを引数に取る `rw_list` の関数を使います。

- リストは不変です。`list_push` は毎回新しいバッファを確保して既存要素をコピーし、新しいリストを返します（copy-on-push）。容量は 4 から始まり倍々に増えますが、バッファは毎回作り直すので、push を繰り返して長さ n のリストを作るコストは O(n²) です。
- `List[int]` のデータは atomic、それ以外の要素型のデータは走査対象のメモリに置きます。要素が文字列やレコードを指しうるからです。
- 範囲外アクセスは `rw_list_int_at` / `rw_list_at` が `abort` します。`list_at_opt` は Option を返します。
- 3 ワードの構造体は arm64 でレジスタ返しの上限を超え、sret になります。llvmlite の宣言に sret 属性を付けずに済むよう、リストを扱う関数はすべて**ポインタ渡し**です（2.10 節）。

### 2.4 辞書

`rw_dict` は CPython の compact dict と同じ構成の挿入順序付きハッシュ表です（詳細: spec 31, 36）。

```
rw_dict ─┬─ entries: [ {hash, live, key, value} ... ]   挿入順。GC 走査あり
         └─ index:   [ -1 | entries 内の位置 ... ]       線形探索、2 の冪、負荷 ≤ 2/3。atomic
```

- キーの種類は `key_kind` で区別します。0 は `int`（splitmix64 でハッシュ）、1 は `string`（FNV-1a）です。
- エントリのサイズは `16 + key_size + val_size`（値は 8 バイト境界に切り上げ）です。
- 削除は `live` を 0 にするだけで、index のスロットは墓石として残ります。`entries` が埋まったとき、生きているエントリが半分以上なら容量を倍にし、そうでなければ同じ容量で詰め直します。
- キーと値はすべてポインタで受け渡します（`rw_dict_set(d, &key, &val)`）。

### 2.5 Option / Result / タプル

すべて値渡しの構造体で、ヒープを使いません（詳細: spec 10, 11, 30, 37）。

- `Option[T]` は `{i64 tag, T value}` です。`Option[int]` の `rw_option_int` と `Option[float]` の `rw_option_float` は 16 バイトなので値で返せますが、ランタイム関数は他と揃えて出力ポインタ形式にしています。
- `Result[T, E]` は `{i64 tag, T ok, E err}` です。`match` は tag が 1 なら Ok 側、それ以外なら Err 側に分岐します。
- タプルは要素型の構造体をそのまま並べたものです。`await_any` は `(int, T)` のタプルを返します。

### 2.6 レコード

レコードは GC ヒープ上の構造体へのポインタ（`i8*`）です（詳細: spec 20）。

- 構造体のフィールドは宣言順に並び、各フィールドは `llvm_type_of` の結果をそのまま埋め込みます。文字列フィールドは 16 バイトの `rw_str` が入り、レコード型のフィールドはポインタなので、レイアウトが再帰することはありません。
- コンストラクタは `rw_gc_alloc(sizeof)`（0 バイトなら 1 バイト）で確保し、各フィールドを格納します。サイズは null に対する GEP で求めます。
- フィールドアクセスは `bitcast` と `gep` で行います（`IRGen._field_ptr`）。

### 2.7 関数値とクロージャ

関数値は `{i8* code, i8* env}` の 2 ワードです（詳細: spec 21, 71）。

- `code` の型は `R (i8* env, P1, ..., Pn)` で、第 1 引数は常に環境です。
- **トップレベル関数**を値として使うと、環境を無視して本体を呼ぶ internal なサンク `rw_fnval_<name>` を作り、`{サンク, null}` を値とします。
- **クロージャ**は `rw_closure_<name>` という関数を作ります。捕捉した変数は生成時点の値を GC ヒープ上の環境構造体にコピーします（値による捕捉）。捕捉が無ければ `env` は null です。
- クロージャ本体の冒頭で環境から各変数をローカルの alloca にコピーします。自分自身を名前で参照するローカル関数には、同じ `{code, env}` を指すスロットを用意します。
- 呼び出しは `code` を正しい関数型にキャストし、`env` を第 1 引数として渡します（`IRGen._call_fn_value`）。

### 2.8 ハンドル型

`Future`、`Channel`、`Mutex`、`Dict` は不透明なポインタです。

- `Future[T]` の実体は `rw_fiber_handle *` です。`runtime.c` で `rw_future_t *` と相互にキャストします。
- `Channel[T]` は要素サイズを作成時に受け取り、値をポインタで受け渡します。
- `Mutex` はファイバ対応のロックです（5.3 節）。
- 内部に `pthread_mutex_t` を持つ `rw_chan` と `rw_mutex` は、GC で回収されても `pthread_mutex_destroy` を呼びません。macOS と Linux ではこれでリソースは漏れませんが、移植時には気を付けてください。

### 2.9 シンボル命名

| シンボル | 意味 |
|---|---|
| `rw_*` | ランタイムが公開する C 関数・変数（`rw_str_concat`、`rw_preempt_flag` など） |
| `rw_user_<name>` | エントリモジュールのユーザー関数 |
| `rw_user_<module>.<name>` | import したモジュールの関数。`.` は rw の識別子に現れないので衝突しない |
| `rw_trampoline_<callee>` | spawn 用トランポリン。呼び出し先ごとに 1 個 |
| `rw_closure_<name>` | クロージャのコード |
| `rw_fnval_<name>` | トップレベル関数を値にするためのサンク（internal） |
| `.str.N` | 文字列リテラル（private 定数） |
| `main` | 合成した C エントリポイント |

ランタイム内部専用の関数は `static` にするか、`rw_sched_*`、`rw_net_*`、`rw_aio_*`、`rw_gc_*`、`rw_timer_*` のようにサブシステム名を付けます。

### 2.10 irgen からの呼び出し規約

irgen はランタイム関数を `_declare_runtime` でまとめて宣言し、次の規則で呼び出します。

| 種類 | 規約 | 例 |
|---|---|---|
| スカラ（int/float/bool）と `rw_str` | 値渡し・値返し。`rw_str` は 16 バイトで 2 レジスタに収まる | `rw_str rw_str_concat(rw_str, rw_str)` |
| リスト（24 バイト） | 入力は `const rw_list *`、出力は呼び出し側の alloca を指す `rw_list *out`（第 1 引数） | `void rw_list_push(rw_list *out, const rw_list *l, const void *elem, int64_t elem_size)` |
| Option / 複合結果 | 出力ポインタ | `void rw_str_to_int(rw_option_int *out, rw_str)`、`void rw_recv_from(rw_datagram *out, ...)` |
| 汎用要素（任意の型 T） | 要素へのポインタとサイズ | `rw_list_at(void *out, const rw_list *, i, elem_size)`、`rw_chan_send(c, const void *)` |
| ハンドル | `i8*` | `rw_dict *`、`rw_chan *`、`rw_future_t *` |

出力ポインタの先や一時的な入力は、すべて `FunctionCtx.alloca` でエントリブロックに確保したスロットです（7.4 節）。この判断の背景は `runtime.h` の `rw_list_int` のコメントにあります。

**直接呼び出し表**: 引数をそのまま評価して渡すだけで済むビルトインは、`IRGen._emit_call` の `direct` 辞書に `"rw 名": 宣言済み関数` の形で並んでいます（`str_find`、`sleep`、`dial`、`fs_kind`、`gc_collect` など）。リストを受け取る・出力ポインタが要る・ジェネリックなビルトインは、`_emit_call` の個別の分岐、`_emit_generic_list_op`、`_emit_dict_builtin`、`_emit_chan_builtin` などが扱います。

**spawn の ABI**: `spawn f(a, b)` は次のように展開されます。

```
caller:                                        rw_trampoline_f(i8* args):
  args = rw_gc_alloc(sizeof {A, B})              a, b = load args
  store a, b → args                              r = rw_user_f(a, b)
  fut = rw_spawn_<T>(rw_trampoline_f, args)      rw_gc_free(args)
                                                 return r
```

`rw_spawn_<T>` と `rw_await_<T>` は戻り値の型ごとに `i64` / `f64` / `bool` / `str` / `void` の 5 種類があり、`Bytes` は `str` を使います。これ以外の型（レコード、リストなど）を返す関数は spawn できず、irgen が `RuntimeError` を出します。式文として書いた `spawn`（Future を捨てる形）の直後には `rw_detach(fut)` を挿入します（詳細: spec 19）。

---

## 3. ファイバとスケジューラ

### 3.1 ファイバのスタック

各ファイバは `mmap` で確保した専用の領域で動きます（詳細: spec 05）。

```
 低アドレス                                                   高アドレス
 ┌────────────┬────────────────────────────────────┬────────────┐
 │ guard page │ 使えるスタック 64 KiB              │ guard page │
 │ PROT_NONE  │ ← sp はここから下に伸びる     top ─┤ PROT_NONE  │
 └────────────┴────────────────────────────────────┴────────────┘
 region       region+page                          region+page+64K
```

- 使える部分は `RW_FIBER_STACK_USABLE` = 64 KiB で、ページサイズの倍数に切り上げます。ページサイズは `sysconf(_SC_PAGESIZE)` で取得します（macOS/arm64 は 16 KiB、Linux/x86_64 は 4 KiB）。
- 両端のガードページは `mprotect(PROT_NONE)` で保護します。スタックが溢れると、黙ってメモリを壊す代わりに SIGSEGV になります。
- スタックは伸長しません。深い再帰や大きなローカル配列は 64 KiB に収まる必要があります。irgen がすべての alloca をエントリブロックに置く理由もここにあります（7.4 節）。
- `fiber_stack_top(g)` は使える部分の最高アドレスで、GC はこれをスタックの底として扱います。

### 3.2 コンテキストスイッチ

`rw_fiber_swap(old, new)` は callee-saved レジスタを `*old` に保存し、`*new` から復元して `ret` します。`rw_fiber_ctx` は 21 ワードの配列です。

| インデックス | arm64 | x86_64 |
|---|---|---|
| 0..9 | x19..x28 | 0: rbx, 1: rbp, 2: r12, 3: r13, 4: r14, 5: r15 |
| 10 | x29 (fp) | — |
| 11 | x30 (lr)。再開先 | — |
| 12 | **sp** | — |
| 6 | — | **rsp** |
| 13..20 | d8..d15 | — |

sp の位置はアーキテクチャで異なります（arm64 は 12、x86_64 は 6）。`sched.c` の `saved_sp()` はこれに合わせて分岐しており、GC が停止中のファイバのスタックを走査するときに使います。

新しいファイバは `rw_fiber_ctx_init` で初期化します。

- arm64: `entry` を x19、`arg` を x20 に入れ、lr を `rw_fiber_trampoline` にします。トランポリンは `x0 = x20` として `blr x19` を実行し、戻ってきたら `brk #0xdead` で止まります。
- x86_64: 戻り番地がスタック上にあるので、16 バイト境界に揃えたスタック頂上の直下に `&rw_fiber_trampoline` を書き、rsp をそこに向けます。`entry` は r12、`arg` は r13 です。戻ってきたら `ud2` で止まります。

ファイバの入口関数（`fiber_entry_<T>`）は決して return しません。最後は `finalize_fiber` からスケジューラに切り替えます。

### 3.3 M:N スケジューラの構成

Go の G-M-P モデルを簡略化した構成です（詳細: spec 06）。

```
          spawn（main から）/ preempt されたファイバ / 他スレッドからの起床
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │ globq（mutex 付き連結リスト）  │
                    └──────────────────────────────┘
                       ▲ 溢れ(半分+1)    │ 補充(最大 128)
       ┌───────────────┴──┐     ┌──────▼───────────┐
       │ P0: ring[256]    │◀───▶│ P1: ring[256]    │ ...   ← try_steal は他の P から半分を奪う
       └───────┬──────────┘steal└──────┬───────────┘
               │                       │
           M0 (worker)              M1 (worker)
           sched_ctx               sched_ctx
           current                 current
```

| 用語 | 実体 | 説明 |
|---|---|---|
| G | `rw_fiber_handle` | ファイバ。ctx、スタック領域、状態、結果、待ちリスト、回収フラグ、`last_error` を持つ |
| M | `rw_M` | ワーカースレッド。自分のスケジューラ ctx（`sched_ctx`）、実行中の G、P へのポインタ、プリエンプション用カウンタ、park の commit を持つ |
| P | `rw_P` | 256 要素の有界リング。持ち主の M だけが put/get し、他の M は CAS で grab（盗む）できる |
| globq | `rw_globq` | mutex で守った連結リスト。溢れ分、main からの spawn、他スレッドからの起床、preempt されたファイバが入る |

ワーカーと P は 1 対 1 で、`RW_WORKERS` 本（1〜64）作ります。スレッドローカル変数 `tls_m` は、ワーカー上なら自分の `rw_M`、それ以外（main や netpoller）なら NULL です。この値が「ファイバの中にいるか」の唯一の判定基準です。`rw_sched_current_fiber()` はワーカー上でファイバを実行中ならそのハンドルを、そうでなければ NULL を返します。

**キューへの投入（`enqueue_ready`）**:

- ワーカー上から呼ばれた場合（`tls_m != NULL`）は自分の P のリングに入れます。リングが満杯なら、半分とその G をまとめて globq に移します。ワーカーが 2 本以上あれば、寝ているワーカーを 1 本だけ `pthread_cond_signal` で起こします。
- ワーカー以外（main、netpoller、aio、timer、signal の各スレッド）から呼ばれた場合は globq に入れて 1 本起こします。

### 3.4 次に走らせるファイバの選び方

`find_runnable(m)` は次の順で探します。

1. 終了フラグが立っていれば NULL を返す（ワーカー終了）。
2. `m->tick % 61 == 0` なら、先に globq から 1 個取る。
3. 自分の P のリングから取る。
4. globq から最大 128 個まとめて取り、1 個を実行し残りを自分のリングに入れる。
5. 乱数（xorshift64）で選んだ開始位置から他の P を順に見て、半分を盗む。
6. 何も無ければ `g_sched_mu` を取り、終了フラグと globq を再確認してから `g_sched_cv` で寝る。

手順 2 の 61 回に 1 回という間隔は Go の `schedule()` に倣ったものです。これが無いと、preempt されてもローカルキューに戻され続けるファイバや、自分のリングを使い切らないワーカーがある限り、main から globq に入れたファイバがいつまでも走りません（詳細: spec 23）。

### 3.5 状態遷移

`rw_fiber_handle.state` は `READY` / `RUNNING` / `WAITING` / `DONE` の 4 値です。

```
              spawn_common
                  │ enqueue_ready
                  ▼
   ┌──────────▶ READY ◀─────────────────────────────────┐
   │              │ worker_main が取り出す                │
   │              ▼                                       │ 起床側が rw_sched_enqueue_ready
   │           RUNNING ──── rw_sched_park_commit ───▶ WAITING
   │              │                                  （commit が起床経路を登録）
   │ yield /      │
   │ preempt      │ fn(arg) が return → finalize_fiber
   └──────────────┤
                  ▼
                 DONE ──(REAP_FINISHED と REAP_RELEASED が揃う)──▶ free_handle
```

- `READY ⇄ RUNNING` の遷移はスケジューラの直列化された経路で起きるので relaxed で十分です。
- `RUNNING → DONE` だけが同期の役割を持ちます。`finalize_fiber` は結果を書いた後に `DONE` を release ストアし、join 側は acquire ロードで `DONE` を見てから結果を読みます。

ファイバがスケジューラに戻ってくると、`worker_main` は理由を次のように判定します。

| 判定 | 意味 | 処理 |
|---|---|---|
| `m->park_commit != NULL` | park した | commit を呼ぶ。以後このファイバには触れない |
| `m->finished == g` | 終了した | `reap` を `REAP_FINISHED` と交換し、すでに `REAP_RELEASED` なら解放 |
| `state == RUNNING` かつ `m->preempted` | セーフポイントで preempt された | globq の末尾に入れる |
| `state == RUNNING` | 自発的に yield した | 自分の P に戻す |

終了の判定に `g->state` ではなく M ローカルの `m->finished` を使うのは、`state` が他スレッドから書き換えられうるからです。

### 3.6 spawn / await / detach と回収

**spawn**（`spawn_common`）は次の順で処理します。

1. ハンドルを `rw_gc_alloc_uncollectable` で確保する（6.2 節）。
2. `join_mu` / `join_cv` / `wait_lock` を初期化する。
3. スタック領域を `mmap` し、ガードページを保護する。
4. ctx を初期化する。
5. GC 用の生存ファイバ一覧に登録する（`track_fiber`）。
6. `enqueue_ready` でキューに入れる。

新しいファイバはすぐには走りません。

**await**（`rw_sched_join_<T>` → `wait_done`）は呼び出し元によって動きが変わります。

- ファイバから呼ぶ場合は、対象の待ちリストに park します（`park_on` → `join_commit`）。commit は対象の `wait_lock` を取って再確認し、すでに `DONE` なら自分をすぐキューに戻し、そうでなければ待ちリストに自分を積みます。`finalize_fiber` は `DONE` を公開した後に `wait_lock` を取って待ちリストを丸ごと外し、各待ち手を `enqueue_ready` します。
- メインスレッドから呼ぶ場合は、ハンドルごとの `join_cv` で寝ます。`finalize_fiber` は `DONE` の後に `join_cv` を broadcast します。

結果を読んだ後は `release_handle` を呼びます。

**detach** は Future を捨てる操作で、`release_handle` だけを行います。

**回収のプロトコル**（詳細: spec 19）: ハンドルの解放は、ワーカー側と所有者側のうち後から来た方が行います。

```
worker_main（ファイバが終了してスタックを離れた後）:
    if (exchange(&g->reap, REAP_FINISHED) == REAP_RELEASED) free_handle(g);

release_handle（join が結果を読んだ後、または detach）:
    if (exchange(&h->reap, REAP_RELEASED) == REAP_FINISHED) free_handle(h);
```

どちらも atomic な交換なので、相手の値を見るのはちょうど一方だけです。ワーカー側が `REAP_FINISHED` を書くのは、ファイバが `rw_fiber_swap` でスタックを完全に離れた後に限ります。そのため、使用中のスタックを `munmap` することはありません。`free_handle` は生存一覧から外し、mutex/cond を破棄し、スタックを `munmap` し、ハンドルを `rw_gc_free` します。

`rw_sched_live_fibers()` は生成数から解放数を引いたカウンタで、テスト専用です。

### 3.7 park と park_commit（publish after save）

ファイバを止めて後で起こしてもらう操作は、すべて `rw_sched_park_commit(commit, arg)` を通します（詳細: spec 24）。

```
ファイバのスタック上:                         ワーカーのスケジューラスタック上:
  state = WAITING
  m->park_commit = commit, m->park_arg = arg
  rw_gc_switch_begin
  rw_fiber_swap(&me->ctx, &m->sched_ctx) ───▶ （swap が戻った = ctx の保存が完了）
                                              rw_gc_switch_end
                                              commit(me, arg)
                                                └ ここで初めて me を起床側に公開する
                                                  （netpoller に登録、aio キューに積む、
                                                   待ちリストに入れる、ロックを外す）
                                              以後 me には触れない
```

**なぜ必要か**: 「起床経路に自分を登録してから swap する」という順序だと、登録と swap の間にイベントが起きたとき、起床側がまだ保存途中の ctx を持つファイバをキューに戻してしまいます。別のワーカーがそれを盗んで再開すると、半分しか書かれていないレジスタで飛んで PC=0 付近で落ちます。あるいは完全に再開して RUNNING にした後、元のワーカーがそれを見てもう一度キューに入れると、同じファイバが 2 本並行して走ります。`runtime/fiber/test_park_race.c` はこの競合を再現するテストです。

同じ理由で、`rw_sched_yield` もファイバを自分でキューに戻しません。キューに戻すのは swap が戻った後の `worker_main` です。

**commit の約束**:

- commit はワーカーのスケジューラスタック上で、park したファイバと同じ OS スレッドで動きます。そのため、ファイバ側で取った `pthread_mutex` を commit で外すことは正当です（チャネル、ミューテックス、シグナルがこの性質を使っています）。
- commit が起床経路を用意できなかった場合は、自分で `rw_sched_enqueue_ready(self)` を呼ばなければなりません。
- commit はブロックしてはいけません。

現在の commit の一覧です。

| 呼び出し元 | commit |
|---|---|
| `await`（ファイバから） | `join_commit`: 対象の待ちリストに積む。すでに DONE なら即キューへ |
| netpoller 待ち | `commit_wait`: waiter に自分を設定して kqueue/epoll に登録する。タイムアウト済み・クローズ済み・登録失敗なら即キューへ |
| aio | `commit_task`: スタック上のタスクをプールのキューに積む |
| `sleep` | `commit_sleep`: タイマーヒープに積む |
| チャネル | `unlock_commit`: チャネルの mutex を外す |
| ミューテックス | `mutex_unlock_commit`: 内部 mutex を外す |
| `signal_wait` | `commit_wait`: 待ち手一覧に積み、`g_mu` を外す |

### 3.8 セーフポイントによるプリエンプション

ファイバは基本的に協調的にスケジュールされます。I/O をせずに計算し続けるファイバがワーカーを独占しないよう、コンパイラが挿入するセーフポイントで切り替えます（詳細: spec 23）。

**コンパイラ側**（`IRGen._emit_safepoint`）: 関数の入口、クロージャの入口、`while` ループの先頭（`continue` も含めて毎回通る。範囲 `for` は desugar で `while` になる）、`for x in list` とチャネルに対する `for` のループ先頭に、次のコードを置きます。

```llvm
%p = load atomic i32, i32* @rw_preempt_flag monotonic, align 4
%req = icmp ne i32 %p, 0
br i1 %req, label %preempt, label %preempt.cont
preempt:
  call void @rw_safepoint()
  br label %preempt.cont
```

**ランタイム側**:

```
monitor（1 ms ごと）                          ワーカー M_i
  各 M について:                                worker_main: ファイバを再開するたびに tick++,
    tick が前回と同じで running == 1 のまま       running = 1
    RW_PREEMPT_MS 以上経過していたら
      M_i.preempt = 1                        セーフポイント: rw_preempt_flag != 0
      rw_preempt_flag++  ─────────────────────▶ rw_safepoint():
    （同じファイバには 1 スライスごとに再要求）     exchange(M_i.preempt, 0) が 1 なら
                                                  rw_preempt_flag--, preempted = 1
                                                  rw_sched_yield()
                                               worker_main: preempted なら globq 末尾へ
```

- `rw_preempt_flag` は「未処理の要求数」の全体カウンタです。どこか 1 つの M に要求があれば全ワーカーのセーフポイントが遅い経路に入りますが、`rw_safepoint` は自分の M の `preempt` だけを見て、無ければすぐ戻ります。
- ファイバが park や終了でセーフポイントに着く前に M を離れた場合、`worker_main` が M の `preempt` を消して `rw_preempt_flag` を減らします。古い要求が次のファイバを早く preempt することはありません。
- preempt されたファイバは、Go と同じく globq の末尾に入ります。
- スライスは `RW_PREEMPT_MS`（既定 10、0〜60000）で設定し、0 にするとモニタスレッドを起動しません。
- セーフポイントは通常の C 関数呼び出しなので、LLVM はその前後でスタックと callee-saved レジスタが整合していると扱います。シグナルによる非同期プリエンプションは行いません。長い直線コードや、ランタイムの C 関数の中に長く留まるコードは中断されません。メインスレッドも preempt の対象外です。

---

## 4. I/O

### 4.1 汎用 read / write / close

I/O はハンドルの種類によらず fd に対する汎用の `read` / `write` / `close` で行います（`io.c`。詳細: spec 15）。TCP 専用の I/O 関数はありません。

| ビルトイン | ランタイム関数 | 戻り値 |
|---|---|---|
| `read(fd, max)` | `void rw_read(rw_str *out, fd, max)` | 読んだバイト列。EOF とエラーは空。区別は `last_error()` |
| `write(fd, b)` | `int64_t rw_write(fd, b)` | 書いたバイト数（部分書き込みあり）か -1 |
| `write_all(fd, b)` | `int64_t rw_write_all(fd, b)` | `b.len` か -1。部分書き込みを繰り返し、タイムアウトの期限は全体で 1 つ（詳細: spec 52） |
| `close(fd)` | `int64_t rw_close(fd)` | 0 か -1。先に `rw_net_close_fd` で待ち手を起こす |
| `file_open(path, mode)` | `int64_t rw_file_open(path, mode)` | fd か -1。mode は `r` / `w` / `a` |

データグラムソケットに空の `write` をすると、空のデータグラムを送ります（詳細: spec 69）。

### 4.2 振り分けの規則

```
                    rw_read / rw_write(fd)
                             │
                   needs_offload(fd)?  ── fstat ──┐
                             │                    │
         ┌───────────────────┴───────────────┐    │
   ソケット、または                         それ以外（通常ファイル、
   O_NONBLOCK の FIFO                       ブロッキングの pipe / tty / 文字デバイス、
         │                                  rw が作っていない stdin など）
         ▼                                        ▼
   read(2)/write(2) を直接実行               rw_aio_read / rw_aio_write
   EAGAIN なら rw_net_block                        │
         │                                  ファイバ上? ─ yes → タスクを積んで park
   ファイバ上? ─ yes → rw_net_wait（park）           │                （aio プール）
         │                                        no → その場で同期 read/write
         no → poll(2) を 50 ms 刻みで（メインスレッド）
```

- **ソケット**は常にノンブロッキングで、netpoller を使います。
- **ノンブロッキングの FIFO**（rw が子プロセス用に作るパイプ）も netpoller を使います。有限サイズの aio プールに回すと、ブロックした read がキューで後ろに並んだ write を待ってデッドロックしうるからです（詳細: spec 61）。
- **それ以外の fd** はカーネル内でブロックしてワーカーを占有するので、aio プールに回します（詳細: spec 16, 25）。
- `print` は C の stdio を使い、呼んだスレッドで書きます。標準出力のパイプが詰まるとワーカーが止まります（spec 25 の Non-Goal）。

### 4.3 netpoller

netpoller は 1 本の専用スレッドで、OS のイベント通知を待ちます（詳細: spec 12）。

| | macOS（`netpoller_kqueue.c`） | Linux（`netpoller_epoll.c`） |
|---|---|---|
| 待機 | `kevent` | `epoll_wait` |
| 登録 | `EV_ADD \| EV_ONESHOT`、(fd, filter) ごとに knote 1 個 | `EPOLLONESHOT \| EPOLLRDHUP`、`EPOLL_CTL_MOD` を試し、失敗したら `ADD` |
| 方向の判定 | `EVFILT_READ` / `EVFILT_WRITE`。EOF やエラーも同じ filter でフラグ付きで届く | `EPOLLIN/ERR/HUP/RDHUP` → 読み、`EPOLLOUT/ERR/HUP` → 書き |
| 終了の通知 | `EVFILT_USER` + `NOTE_TRIGGER` | `eventfd` |

どちらも ONESHOT で登録し、通知のたびに `rw_netpoller_ready(pd, mask)` を呼びます。`rw_netpoller_ready` は残っている関心を再登録します。

### 4.4 poll descriptor と waiter

fd ごとに `rw_pollfd` が 1 個あり、一度作ったら解放しません。fd 番号は再利用されるので、descriptor もそのまま使い回します。

```c
struct rw_pollfd {
    rw_wait_lock   lock;
    int            fd;
    uint64_t       next_id;
    rw_net_waiter *head[2];        /* [RW_NET_READ], [RW_NET_WRITE] の待ち手リスト */
    int64_t        timeout_ms[2];  /* 方向ごとのタイムアウト (spec 53) */
    int            closed;         /* rw_close 済み */
};
```

- descriptor は 1024 個ずつのチャンクで遅延確保します。チャンクは 4096 個まであり、fd 番号の上限は約 400 万です。チャンクの公開は CAS で行います。
- `rw_net_waiter` は待っているファイバのスタック上に置くノードで、`{id, g, timedout, closed, linked, next}` を持ちます。`g` は park の commit が動くまで NULL です。
- descriptor に触るのは park するファイバ、netpoller スレッド、タイマースレッドの 3 者で、いずれも `lock` を取ってから触ります。そのため「準備完了」と「タイムアウト」のどちらか一方だけが waiter を再開します。

`rw_net_wait(fd, dir, deadline)` の流れは次のとおりです。

```
1. lock を取る。closed なら RW_NET_CLOSED を返す。
2. waiter に id を振ってリストに積み、lock を外す（この時点では g = NULL）。
3. deadline があれば rw_timer_call_at(deadline, wait_timeout, pd, id<<1|dir) を登録する。
4. rw_sched_park_commit(commit_wait)
     commit_wait（ctx の保存後）: lock を取る
       timedout / closed / linked でない → 外して即キューへ
       そうでなければ w->g = self として kqueue/epoll に登録（失敗したら即キューへ）
5. 再開後: lock を取り、まだリンクされていれば外す。
   戻り値は closed → RW_NET_CLOSED、timedout → RW_NET_TIMEDOUT、それ以外 → 0
```

戻り値 0 は「準備できたかもしれない」という意味で、偽の起床もありえます。呼び出し側は syscall をやり直し、また `EAGAIN` なら待ち直します。`rw_netpoller_ready` は、まだ commit 前（`g == NULL`）の waiter をリストに残します。その waiter は自分の commit で登録します。

### 4.5 タイムアウト

`set_read_timeout` / `set_write_timeout` は descriptor の `timeout_ms` を設定します（詳細: spec 53）。I/O ビルトインは最初の `EAGAIN` で `rw_net_deadline` を呼んで期限を決め、その呼び出しの間ずっと同じ期限を使います。

タイマーのコールバック `wait_timeout(pd, tag)` は、タグから waiter の id と方向を取り出し、lock の下でリストからその id を探します。

- **見つからない**: 待ちはもう終わっています。何もしません。waiter はスタック上のノードなので、ポインタではなく id で探すことで、終わった待ちのノード（すでに無効なスタック領域）に触れずに済みます。
- **見つかり `g != NULL`**（park 済み）: `timedout = 1` にしてリストから外し、キューに入れます。
- **見つかり `g == NULL`**（commit 前）: `timedout = 1` にするだけです。後で commit がそれを見て即座にキューに戻します。

このように、準備完了・タイムアウト・クローズの起床はすべて「lock の下で `g` を取り出して NULL にした側だけがキューに入れる」という形になっています。同じファイバが二重に再開されることはありません。

`dial_timeout` の期限は接続処理だけを対象とし、名前解決は含みません。

### 4.6 close による起床と rw_net_reset_fd

カーネルは close された fd の登録を黙って捨てます。そのままだと、その fd で待っていたファイバは永久に眠ります。`rw_close` は `close(2)` の前に `rw_net_close_fd(fd)` を呼び、descriptor に `closed = 1` を付け、両方向の waiter すべてに `closed = 1` を立てて起こします（commit 前の waiter には commit が気付きます）。起こされた操作は `EBADF` で失敗します。別のファイバが listen ソケットを close すると accept ループが止まる、という使い方ができます（詳細: spec 57）。

`closed` の印とタイムアウトは fd 番号に付いているので、同じ番号が新しいハンドルに再利用されたときに消す必要があります。**fd を作るランタイムの経路は、すべて `rw_net_reset_fd(fd)` を呼ばなければなりません。** 呼ばないと、新しいハンドルが古いハンドルの closed 印やタイムアウトを引き継ぎます。現在の呼び出し箇所は `rw_file_open`、`rw_tcp_listen`、`rw_tcp_accept`、`rw_dial_timeout`（と `connect_fd`）、`rw_listen`、`rw_proc_start`（親側の 3 本のパイプ）です。

### 4.7 メインスレッドでの経路

メインスレッドはファイバではないので park できません。各操作は次のように振る舞います。

| 操作 | メインスレッドでの動作 |
|---|---|
| netpoller 系（`rw_net_block`） | `poll(2)` を最大 50 ms 刻みで繰り返し、毎回 descriptor の `closed` を確認する。別スレッドの close は `poll` を起こさないため |
| aio 系 | その場で同期的に `read` / `write` / 関数呼び出し |
| `connect` | 期限が無ければブロッキング connect。期限があればノンブロッキングにして `poll` |
| `sleep` | `nanosleep`（`EINTR` なら残り時間で再試行） |
| `await` | ハンドルの `join_cv` で寝る |
| チャネル・ミューテックス | waiter ごとの `pthread_cond_t` で寝る |
| `signal_wait` | 共有の `g_cv` で寝る |

`rw_net_wait` 自体をファイバ外から呼ぶと `abort` します。ファイバ外からはかならず `rw_net_block` を経由します。

### 4.8 aio プール

`aio.c` は固定サイズのスレッドプールで、ブロッキング syscall を肩代わりします（詳細: spec 16）。

```
fiber:                               aio thread:
  rw_aio_task t（ファイバのスタック上）  queue_pop()
  t.waiter = me                         run_task(t)
  park_commit(commit_task, &t) ──────▶    READ:  buf = rw_gc_alloc_atomic(max); read()
     commit: queue_push(&t)                WRITE: write()
                                           CALL:  t->fn(t->arg)
                                         t->err = errno
  再開後: rw_errno = t.err ◀──────────  rw_sched_enqueue_ready(t->waiter)
```

- タスクは park したファイバのスタック上にあります。ファイバは完了まで park したままなので、スタックは生きています。
- 結果の格納は `rw_sched_enqueue_ready` より前に起き、その後に再開したファイバが結果を読みます。netpoller と同じ順序関係です。
- `rw_aio_call(fn, arg)` は任意の関数をプールで実行します。名前解決（`getaddrinfo`）、ファイルシステムの各操作、`waitpid` がこれを使います。ファイバ外から呼ぶとその場で `fn(arg)` を実行します。
- プールの各スレッドはブロッキング中の fd 1 個につき 1 本ふさがります。`RW_AIO_THREADS`（既定 4）を超える数の同時ブロッキング読み込みは、互いの後ろに並びます。

### 4.9 エラー処理: rw_errno と last_error

I/O ビルトインは、成功なら 0、失敗なら errno の値を `rw_set_last_error` で記録します。`last_error()` はそれを返します（詳細: spec 51）。

- 記録先は**ファイバごと**です（`rw_fiber_handle.last_error`）。ファイバは M の間を移動するので、スレッドローカルには置けません。ファイバ外（メインスレッド）ではスレッドローカルの `tls_last_error` を使います。
- 名前解決の失敗は `RW_ENOHOST`（10001）で、errno の範囲外の値です。`error_name` / `error_message` は名前と説明文を返します。errno の番号は OS ごとに違うので、プログラムは名前で比較します。
- **park しうるコードは `errno` を直接触らず、`rw_errno` を使います。** `rw_errno` は `(*rw_errno_ptr())` で、`rw_errno_ptr` は別の翻訳単位（`sched.c`）にある非 const 関数です。glibc は `__errno_location` を const と宣言しているので、コンパイラは park の前に計算した errno のアドレスを再利用してよいことになります。するとファイバが別スレッドで再開した後に、元のスレッドの errno を読んでしまいます。
- aio プールでのエラーは、プールのスレッドの errno を `t.err` に写し、再開後に呼び出し側の `rw_errno` に戻します。
- 公開エントリ関数は、入口で `rw_errno = 0` とし、出口で一度だけ `rw_set_last_error` を呼ぶ形に揃えています。失敗経路で `close` などを呼ぶときは、`close_keep_errno` のように errno を退避して戻します。

### 4.10 アドレスによる dial / listen / accept / open

`net/addr.c` はハンドルの種類をアドレス文字列で選ぶ共通の入口を提供します（詳細: spec 50, 69）。

| アドレス | dial | listen | open |
|---|---|---|---|
| `tcp://host:port` | 名前解決して順に connect | `host` が空なら IPv4 の全インタフェース、それ以外は名前解決して bind + listen(128) | — |
| `udp://host:port` | connect 済み UDP ソケット | bind のみ（ノンブロッキング） | — |
| `unix:///path` | ストリーム型 Unix ソケット | bind + listen | — |
| `file:///path` または素のパス | — | — | `rw_file_open` |

- IPv6 リテラルは `[v6]:port` と書きます。ポートは 5 桁以内の数字で 65535 以下です。
- dial / accept で得たソケットはノンブロッキングです。listen ソケットは作成時はブロッキングで、`tcp_accept_impl` が最初の accept でノンブロッキングにします。
- `connect_fd` は、ファイバ上か期限がある場合はノンブロッキング connect と書き込み可能待ちを行い、`SO_ERROR` で結果を確かめます。`EINTR` で中断された connect は裏で進行し続けるので、connect を呼び直さず、ノンブロッキングと同じように完了を待ちます（6.7 節）。
- `recv_from` / `send_to` は `udp://` のアドレス文字列で送信元・宛先を表します。`send_to` は宛先を名前解決し、ソケットと同じアドレスファミリの最初の候補を使います。
- `local_addr` / `peer_addr` は `tcp://`、`udp://`、`unix://` 形式の文字列を返します。
- ソケットオプション（`shutdown`、`set_nodelay`、`set_keepalive`、`set_backlog`）は 0 か -1 を返し、`last_error` を記録します（詳細: spec 56）。

**名前解決**: `getaddrinfo` はブロックするので、ファイバ上では `rw_aio_call` でプールに回して park します（`resolve` → `do_resolve`）。失敗すると `rw_errno = RW_ENOHOST` になります。

### 4.11 SIGPIPE

`rw_init` は `signal(SIGPIPE, SIG_IGN)` を実行します。切断済みの相手への write は、プロセスを殺す代わりに -1 / `EPIPE` を返します。macOS では、dial / accept で得たソケットにも `SO_NOSIGPIPE` を設定します。子プロセスは `posix_spawnattr_setsigdefault` で SIGPIPE を既定の動作に戻してから起動します（詳細: spec 51, 61）。

---

## 5. タイマー・同期・シグナル・プロセス・ファイルシステム

### 5.1 タイマー

`timer.c` は専用スレッド 1 本と、`(deadline, seq)` で順序付けた二分最小ヒープを持ちます（詳細: spec 39）。

- エントリは「ファイバを起こす」（`sleep`）か「`fn(arg, tag)` を呼ぶ」（`rw_timer_call_at`。netpoller のタイムアウトが使う）のどちらかです。同じ期限のエントリは `seq` の順（FIFO）で発火します。
- `rw_sleep_ms` は期限をファイバのスタックに置き、`commit_sleep` でヒープに積みます。ファイバ外では `nanosleep` します。
- タイマースレッドは期限の来たエントリを取り出し、**ロックを外してから**ファイバを起こすかコールバックを呼びます。コールバックはブロックしてはいけません。
- 待機には `pthread_cond_timedwait` を使います。Linux では条件変数を `CLOCK_MONOTONIC` にします。macOS には `pthread_condattr_setclock` が無いので、単調時計の期限を `CLOCK_REALTIME` に換算します。
- ヒープは `realloc` で伸ばす C のメモリで、GC ヒープの外にあります。
- `time_ms()` は壁時計（Unix 時刻のミリ秒）、`mono_ns()` は単調時計（ナノ秒）です。

### 5.2 チャネル

`Channel[T]` は容量 0（ランデブー）か N の FIFO で、状態はチャネルごとの `pthread_mutex_t` で守ります（詳細: spec 44）。

```
send:  受信待ちがいる       → 値を受信者の領域に直接コピーして起こす
       バッファに空きがある → リングバッファに入れる
       それ以外             → 送信 waiter（自分のスタック上）を積んでブロック
recv:  バッファに値がある   → 取り出す。送信待ちがいれば、空いた枠にその値を入れて起こす
       送信待ちがいる       → 送信者から直接受け取る（ランデブー）
       closed               → 0 を返す
       それ以外             → 受信 waiter を積んでブロック
close: 待っている受信者と送信者を全員 ok = 0 で起こす
```

- ブロックするときは、ファイバなら `rw_sched_park_commit(unlock_commit, c)` で park し、commit がチャネルの mutex を外します。起こす側は mutex を取らないと waiter を完了できないので、保存途中のファイバをキューに戻すことはありません。ファイバ外なら waiter ごとの条件変数で寝ます。
- closed のチャネルへの送信と、待っている間に close された送信は `abort` します。
- バッファとチャネル本体は走査対象の GC メモリです。

### 5.3 ミューテックス

`Mutex` はチャネルと同じ waiter の仕組みを使うファイバ対応のロックです（詳細: spec 46）。

- `unlock` はロックを最初の waiter に直接渡します。横入り（barging）が無いので、待ち手は FIFO 順にロックを得ます。
- ロックされていないミューテックスの `unlock` は `abort` します。
- `with m:` は入口で `mutex_lock`、出口のすべての経路で `mutex_unlock` を呼びます（7.5 節）。

### 5.4 await_all / await_any

- `await_all(futures)` は irgen が展開するループで、各 Future を順に `rw_await_<T>` し、結果をリストに push します。
- `await_any(futures)` は `rw_await_any` で最初に終わった Future の添字を求め、その Future を `rw_await_<T>` して `(index, value)` を返します（詳細: spec 45）。

`rw_await_any` は、終了済みの Future の中から `finish_seq` が最小のもの、つまり実際に最初に終わったものを選びます。添字が最小のものを選ぶわけではありません。`finish_seq` は `finalize_fiber` が `DONE` を公開する直前に全体のカウンタから振る番号です。ファイバは同時に複数の待ちリストに載れないので、どれも終わっていなければ `rw_sleep_ms(1)` を挟んでポーリングします。ファイバ上ならその間は park します。

`await_any` は選ばれた Future だけを await（解放）します。他の Future はそのまま残ります。

### 5.5 シグナル

`signal_wait(names)` は、名前で指定したシグナル（`SIGINT`、`SIGTERM`、`SIGHUP`、`SIGQUIT`、`SIGUSR1`、`SIGUSR2`）のどれかが届くまで待ち、届いたシグナルの名前を返します（詳細: spec 57）。

```
シグナルハンドラ（SA_RESTART）          signal スレッド                      待ち手
  write(self_pipe[1], signo) ───────▶ read(self_pipe[0])
  （errno を保存・復元）                 lock g_mu
                                        g_pending[sig]++
                                        待ち手リストを丸ごと取る
                                        broadcast g_cv（ファイバ外の待ち手）
                                        unlock
                                        各ファイバを enqueue_ready ──────▶ 再開して自分の集合の
                                                                          pending を確認。あれば 1 消費、
                                                                          無ければ再び待つ
```

- 初回の呼び出しで、セルフパイプと detach 済みのスレッドを `pthread_once` で作ります。
- ハンドラは、そのシグナルが初めて待たれたときに `sigaction` で登録し、以後そのシグナルの既定動作は失われます。
- 誰も待っていない間に届いたシグナルは `g_pending` に残るので、2 回の待ちの間に届いたものも失われません。
- ファイバの待ち手は `commit_wait` で一覧に入り、同じ commit で `g_mu` を外します。

### 5.6 子プロセス

`process.c` は `posix_spawnp` で子プロセスを起動します（詳細: spec 61）。マルチスレッドのプロセスで `fork` の後に任意のコードを走らせるのは安全ではないので、`posix_spawnp` を使います。

- `proc_start(argv)` は `[pid, stdin_w, stdout_r, stderr_r]` を返します。失敗すると空リストを返し、`last_error` を記録します。
- パイプの作成から spawn までを `g_spawn_mu` で直列化します。macOS には移植性のある `pipe2(O_CLOEXEC)` が無いため、パイプのすべての端に `FD_CLOEXEC` を付けます。こうしておけば、あるファイバが起動した子が、別のファイバが準備中のパイプを継承することはありません。子の側では `dup2` で 0/1/2 に複製したものだけが残ります。
- 子は SIGPIPE と、rw がハンドラを入れうるシグナルを既定の動作に戻し、シグナルマスクを空にして起動します。
- 親側の 3 本の端には `rw_net_reset_fd` を呼んでからノンブロッキングにします。そのため、netpoller 経由で読み書きされます（4.2 節）。
- `proc_wait(pid)` は `waitpid` を aio プールで実行します（`EINTR` なら再試行）。戻り値は終了コードで、シグナルで終了した場合は 128 + シグナル番号、エラーなら -1 です。
- `proc_kill(pid, sig)` はシグナル名で `kill` します。

### 5.7 ファイルシステム

`fs.c` のビルトイン（`fs_kind`、`fs_size`、`fs_mtime_ms`、`fs_read_dir`、`fs_rename`、`fs_remove`、`fs_mkdir`）は、ファイバから呼ぶと `rw_aio_call` でプールで実行します（詳細: spec 59）。遅いファイルシステムやネットワークファイルシステムでは、ディレクトリやメタデータの syscall もブロックするからです。

- すべて `last_error` を記録します。失敗時の戻り値は -1、空文字列、空リストのいずれかです。
- パスに NUL を含む場合は `EINVAL` です。
- `fs_read_dir` は名前をソートしたリストを返します。リストは aio スレッド上で GC ヒープに確保します。aio スレッドを GC に登録している理由の 1 つがこれです。

---

## 6. メモリ管理

### 6.1 Boehm GC の採用

rw の値は Boehm-Demers-Weiser の保守的 GC（bdw-gc 8.0 以上）が管理します（詳細: spec 67）。採用した理由は次の 3 点です。

- コンパイラが型マップ、スタックマップ、書き込みバリアを出さなくてよい。
- 文字列スライスのような内部ポインタをそのまま扱える。
- スレッドに対応している。

GC は `rwgc.h` の関数で包み、`<gc/gc.h>` を include するのは `gc.c` だけです。`gc.c` は `GC_THREADS` と `GC_NO_THREAD_REDIRECTS` を定義して include します。スレッドの登録は明示的に行います。

### 6.2 割り当ての種類

| 関数 | GC の扱い | 置くもの |
|---|---|---|
| `rw_gc_alloc_atomic` | 走査しない。初期化しない | 文字列のバイト列（連結、変換、`read` のバッファ、`rand_bytes`、`str_from_*`）、`List[int]` のデータ、辞書の index 配列、`proc_start` の結果 |
| `rw_gc_alloc` | 走査する。ゼロ初期化 | 汎用リストのデータ、`str_split` の結果、辞書本体とエントリ、チャネルとバッファ、ミューテックス、`os_args`、ディレクトリ一覧、レコード（irgen）、spawn の引数ブロック（irgen）、クロージャの環境（irgen） |
| `rw_gc_alloc_uncollectable` | 走査するが回収しない。`rw_gc_free` で明示的に解放 | ファイバのハンドル。結果、引数ブロックへのポインタ、切り替え時に保存したレジスタを保持する |
| `rw_gc_realloc` / `rw_gc_free` | — | 明示的な再確保（`fs_read_dir` の一覧の拡張）と解放（失敗した `read` のバッファ、spawn の引数ブロック、ファイバのハンドル） |

いずれもメモリ不足なら `"rw: out of memory"` を出して `abort` します。

### 6.3 GC ヒープの外にあるもの

次のものは GC ヒープの外にあります。どれも rw の値を唯一参照するものではありません。

- C の一時領域（NUL 終端のパス、`posix_spawn` の argv）: `malloc` / `free`
- netpoller の descriptor 表: `calloc`、解放しない
- タイマーのヒープ: `realloc`
- ファイバのスタック: `mmap`
- aio タスク、netpoller の waiter、チャネルの waiter、シグナルの waiter: 待っているファイバのスタック上

### 6.4 スレッドの登録

| スレッド | 登録 |
|---|---|
| main | `rw_sched_init` → `rw_gc_init` の `GC_INIT` で登録される。GC はワーカーより先にメインスレッドで初期化しなければならない |
| worker | `worker_main` の冒頭で `rw_gc_register_thread(&m->stack_base)`。終了時に登録解除 |
| aio pool | `worker_main`（aio 側）の冒頭で登録。終了時に解除 |
| netpoller / timer / monitor / signal | 登録しない。GC ヒープに触れない |

`rw_gc_register_thread` は GC がそのスレッドに与えるハンドルと、pthread スタックの底（最高アドレス）を返します。ワーカーは両方を `rw_M` に保持し、切り替えのたびに使います。

### 6.5 ファイバスタックの走査

GC は自分が知っているスレッドのスタックしか走査しません。ファイバは独自の `mmap` スタックで動くので、次の 2 つの仕組みで GC に見せます。

1. **実行中のファイバ**: スレッドは切り替えのたびに `GC_set_stackbottom` で自分のスタックの底を設定し直します（`rw_gc_switch_begin`）。設定先は、これから走るファイバの `fiber_stack_top` か、ワーカー自身の pthread スタックの底です。GC はスレッドが実行中のスタックの `[sp, 底]` を走査します。
2. **止まっているファイバ**: `GC_set_push_other_roots` のフックを既定のフック（スレッドスタックを積む）の後ろにつなぎ、生存ファイバ一覧（`g_all`）をたどって、終わっていないファイバそれぞれの `[保存した sp, スタック頂上]` を `GC_push_all_eager` で積みます（`push_fiber_stacks`）。実行中のファイバの保存 sp は古い値ですが、スタックの範囲内なので、余計に走査するだけで済みます。

ファイバが保存したレジスタは、uncollectable なハンドルの中の `ctx` にあります。ハンドル自体が走査されるので、レジスタにしか無いポインタも根として扱われます。

### 6.6 切り替えと stop-the-world の排他

切り替えでは、スタックポインタの変更と、記録したスタックの底の変更が 2 段階で起きます。その間で GC が世界を止めると、でたらめな範囲を走査してしまいます。そこで `gc.c` は「切り替え中」のカウンタで排他します。

```
rw_gc_switch_begin(handle, new_base)      ← 古いスタック上、rw_fiber_swap の直前
    rw_gc_enter():  g_stopping の間は待つ → g_switching++ → g_stopping を再確認
    GC_set_stackbottom(handle, new_base)
rw_fiber_swap(...)
rw_gc_switch_end()                        ← 新しいスタック上、swap の直後
    rw_gc_leave():  g_switching--

GC_EVENT_PRE_STOP_WORLD:   g_stopping = 1; g_switching が 0 になるまで sched_yield
GC_EVENT_POST_START_WORLD: g_stopping = 0
```

- `rw_gc_switch_end` を呼ぶ場所は、swap した先によって決まります。worker_main（ファイバから戻ったとき）、`rw_sched_yield` と `rw_sched_park_commit`（ファイバが再開したとき）、新しいファイバの入口関数 `fiber_entry_<T>` の冒頭です。`finalize_fiber` は begin だけを呼び、対応する end は worker_main 側にあります。
- GC が世界を止めている間に begin を呼んだスレッドは、世界が再開するまで待ちます。
- 切り替えの経路はメモリを割り当てません。GC を実行中のスレッドが持つアロケーションロックを必要としないので、デッドロックしません。
- 同じ排他（`rw_gc_enter` / `rw_gc_leave`）で生存ファイバ一覧の変更（`track_fiber` / `untrack_fiber`）も守ります。フックが半端につながった一覧を見ることはありません。

**rw_fiber_swap を呼ぶすべての箇所は、`rw_gc_switch_begin` と `rw_gc_switch_end` で挟まなければなりません。**

### 6.7 EINTR への配慮

Linux の Boehm GC はシグナルでスレッドを止めます。ハンドラは `SA_RESTART` なので、多くのブロッキング syscall は自動的に再開されます。それでも `EINTR` を返しうる呼び出しは、ランタイムが明示的に再試行しています。

| 箇所 | 対応 |
|---|---|
| `rw_net_block` の `poll` | `EINTR` ならループを続ける |
| `do_wait` の `waitpid` | 再試行 |
| `rw_sleep_ms` の `nanosleep` | 残り時間で再試行 |
| `connect_fd` の `connect` | 中断された connect は裏で進行するので、呼び直さずに書き込み可能を待つ |
| `accept` / `recvfrom` / `sendto` | `EINTR` なら再試行 |
| kqueue / epoll のループ、signal スレッドの `read` | `EINTR` なら継続 |

ランタイムに新しいブロッキング syscall を足すときは、`EINTR` の扱いを決めてください。

### 6.8 ビルトイン

| ビルトイン | ランタイム関数 | 動作 |
|---|---|---|
| `gc_collect()` | `rw_gc_collect` → `GC_gcollect` | 全体回収を今すぐ実行する |
| `gc_heap_size() -> int` | `rw_gc_heap_size` → `GC_get_heap_size` | GC ヒープのバイト数 |

### 6.9 依存の検出

bdw-gc はシステムの依存です（`brew install bdw-gc` / `apt install libgc-dev`）。`scripts/gc-prefix.sh` は `include/gc/gc.h` があるプレフィクスを次の順で探して出力します。

1. `$RW_GC_PREFIX`（設定されていればそのまま使う）
2. `pkg-config --variable=prefix bdw-gc`
3. `brew --prefix bdw-gc`
4. `/opt/homebrew/opt/bdw-gc`、`/usr/local/opt/bdw-gc`、`/usr/local`、`/usr`

`runtime/Makefile` はこれをインクルードパスに、`rwc`（`driver.gc_link_flags`）と C テストはリンクフラグに使います。

---

## 7. コンパイラ側の保証

ランタイムの正しさは、irgen が出す IR がいくつかの性質を満たすことを前提にしています。

### 7.1 最適化レベル

既定は `-O2` です（詳細: spec 70）。IR は次の理由で最適化しても安全です。

- セーフポイントは `rw_preempt_flag` を monotonic な atomic ロードで読みます。LLVM はこのロードをループの外に出せません。
- irgen は `nsw` / `nuw` を付けません。符号付き整数のオーバーフローはラップし、最適化器が「オーバーフローしない」と仮定することはありません。
- 保守的 GC は、最適化器によるポインタの扱い（内部ポインタ、レジスタにだけある値）を許容します。

`-O0` が必要なのは主に IR のデバッグのときです。CI では 1 ジョブが `RW_OPT_LEVEL=0` で全テストを流します。

### 7.2 セーフポイントの位置

関数の入口、クロージャの入口、すべてのループ先頭にセーフポイントを置きます（3.8 節）。終わらない rw の計算は、かならずどこかでセーフポイントを通ります。

### 7.3 検査付き整数除算

整数の `/` と `%` は LLVM の未定義動作を避けるために検査を入れます（詳細: spec 42）。

- 0 による除算: `rw_panic("division by zero in <関数> (line N)")`
- `INT64_MIN / -1`: 商が収まらないので `rw_panic("integer overflow in division ...")`
- `x % -1`: 常に 0 を返す（`srem` の `INT64_MIN % -1` のトラップを避ける）

`rwc build --unchecked` / `rwc run --unchecked` / `rwc emit-ir --unchecked` でこの検査を外せます。

### 7.4 エントリブロックの alloca

`FunctionCtx.alloca` は、すべてのスタックスロットを関数のエントリブロックの先頭に確保します。ローカル変数を宣言した場所で alloca すると、ループの各反復で alloca が実行されてスタックが伸び続け、64 KiB のファイバスタックをすぐに使い切るからです。出力ポインタ用の一時スロットも同じ規則に従います。

### 7.5 with / defer のクリーンアップスタック

`FunctionCtx.cleanups` は未実行のクリーンアップを内側が最後になるように積んだスタックです（詳細: spec 47）。

- 要素は `("defer", 文, 深さ)` か `("with", (値, 型), 深さ)` で、深さは積んだ時点のループの深さです。
- `return` は値を評価した**後**に、すべてのクリーンアップを内側から順に出力します。
- `break` / `continue` は、現在のループの深さ以上で積んだクリーンアップだけを出力します。
- `with` ブロックを普通に抜けるときは、自分のクリーンアップを出力します。
- `with` の対象が `Mutex` なら `mutex_unlock`、それ以外の型なら `close` メソッドを呼びます。

クリーンアップは「出力」されるだけで、スタックからは取り除きません。同じクリーンアップを他の経路でも出力する必要があるからです。例外機構は無いので、`abort` や `rw_panic` で終わる経路ではクリーンアップは動きません。

---

## 8. ランタイム拡張のチェックリスト

新しいビルトインやサブシステムを足すときは、次の項目を確認してください。

**ビルトインの追加**

- [ ] `rwc/sema.py` の `_BUILTIN_FUNCS` に名前を足す。単純なシグネチャなら `_SIMPLE_BUILTIN_SIGS` に `(引数型, 戻り値型)` を足す。それ以外は sema に個別の型検査を書く
- [ ] `rwc/irgen.py` の `_declare_runtime` で C 関数を宣言し、引数をそのまま渡すだけなら `_emit_call` の `direct` 表に足す。リストや出力ポインタを使うなら個別の分岐を書く
- [ ] `runtime/runtime.h` にプロトタイプを足す（内部向けならサブシステムのヘッダ）
- [ ] 新しい `.c` ファイルを作ったら `runtime/Makefile` の `OBJS` と依存規則に足す
- [ ] 24 バイト以上の構造体は値で渡さず、ポインタ渡し・出力ポインタにする（2.10 節）

**fd とメモリ**

- [ ] fd を作る経路では、ファイバに渡す前にかならず `rw_net_reset_fd(fd)` を呼ぶ（4.6 節）
- [ ] ソケットとノンブロッキングにしたい fd には `rw_set_nonblocking` を呼ぶ。ブロッキングのまま残す fd は aio プールに回る
- [ ] rw の値になるメモリは `rw_gc_alloc*` で確保する。ポインタを含まないものは `rw_gc_alloc_atomic`、含むものは `rw_gc_alloc`。C の一時領域は `malloc` / `free` でよい
- [ ] rw の値を、GC から見えない場所（`malloc` した領域、未登録スレッドの変数など）にだけ置かない

**スレッド**

- [ ] GC ヒープに触れる新しいスレッドは `rw_gc_register_thread` で登録し、終了時に `rw_gc_unregister_thread` を呼ぶ
- [ ] 新しいスレッドの起動と停止を `rw_init` / `rw_shutdown` の順序に組み込む
- [ ] 別スレッドからファイバを起こすときは `rw_sched_enqueue_ready` を使う

**ファイバとブロッキング**

- [ ] `rw_fiber_swap` を呼ぶなら、前後を `rw_gc_switch_begin` / `rw_gc_switch_end` で挟む
- [ ] ファイバを止めるときは `rw_sched_park_commit` を使い、起床側への公開（登録、キュー投入、ロック解除）は commit の中で行う。commit が起床経路を用意できなければ自分で `rw_sched_enqueue_ready(self)` を呼ぶ
- [ ] ブロッキング syscall をワーカー上で直接呼ばない。ノンブロッキング fd なら `rw_net_block`、それ以外は `rw_aio_call`
- [ ] ファイバ外（メインスレッド）での動作を決める（`rw_sched_current_fiber() == NULL` の分岐）
- [ ] 起床が複数の経路（準備完了・タイムアウト・クローズなど）から来うるなら、ロックの下で「取り出した側だけがキューに入れる」形にする

**エラー**

- [ ] 公開関数は成功・失敗のどちらでも `rw_set_last_error` を呼ぶ
- [ ] park しうるコードでは `errno` ではなく `rw_errno` を使う
- [ ] aio プールで実行した処理の errno は、タスクに写して呼び出し側に戻す
- [ ] `EINTR` の扱いを決める（6.7 節）

**テスト**

- [ ] C レベルのテストを `runtime/fiber/test_*.c` に書き、`tests/test_runtime_c.py` の `C_TESTS` に登録する
- [ ] rw レベルの動作は `examples/*.rw` と `.rw.expected`、または `tests/test_e2e_*.py` で確かめる
- [ ] 競合を含む変更は `RW_WORKERS` を 2 以上にして何度も実行する

---

## 9. テストとデバッグ

### 9.1 テストの配置

| 場所 | 内容 |
|---|---|
| `runtime/fiber/test_*.c` | ランタイムを C から直接叩くテスト |
| `tests/test_runtime_c.py` | `make -C runtime` の後、`C_TESTS` に挙げた C テストをビルドして実行する。登録されているのは `test_detach`、`test_shutdown`、`test_preempt`、`test_park_race`、`test_dict`、`test_timer`、`test_chan`、`test_mutex`、`test_await_any`、`test_net_timeout` |
| `tests/test_e2e.py` | `examples/*.rw` をコンパイル・実行し、`.rw.expected` と比較する。出力を決定的にするため `RW_WORKERS=1` で実行し、スケジューリングの不具合で止まらないようタイムアウトを付ける |
| `tests/test_e2e_*.py` | サーバ、タイムアウト、GC、プロセス、UDP などの e2e テスト。`tests/_net.py` はポート 0 で listen させ、プログラムに束縛したアドレスを出力させる補助関数 |
| `tests/test_irgen.py` ほか | コンパイラ各段の単体テスト |

`runtime/fiber/` には、`test_runq`、`test_steal`、`test_sched`、`test_pingpong`、`test_wait`、`test_c10k`、`test_tcp_loopback`、`test_netpoller_pipe`、`test_list_int`、`test_option`、`test_str_ops` など、`C_TESTS` に登録されていない C テストもあります。これらは pytest から実行されないので、手でビルドしてください（コンパイル方法は `tests/test_runtime_c.py` と同じです）。

よく使うコマンドは次のとおりです。

```sh
make -C runtime
uv run --frozen pytest tests/test_runtime_c.py -q
uv run --frozen pytest -q
```

### 9.2 環境変数

以下の変数はすべてコード上で参照されていることを確認済みです。

| 変数 | 読む場所 | 既定値 | 意味 |
|---|---|---|---|
| `RW_WORKERS` | `sched.c` `parse_workers` | オンライン CPU 数 | ワーカー数（1〜64）。範囲外や数字以外は無視して既定値 |
| `RW_PREEMPT_MS` | `sched.c` `parse_preempt_ns` | 10 | プリエンプションのスライス（0〜60000 ms）。0 で無効（モニタスレッドを起動しない） |
| `RW_AIO_THREADS` | `aio.c` `rw_aio_init` | 4 | aio プールのスレッド数（1〜64） |
| `RW_OPT_LEVEL` | `driver.py` `resolve_opt_level` | 2 | `-O` を指定しないときの最適化レベル（0〜3） |
| `RW_GC_PREFIX` | `scripts/gc-prefix.sh` | 自動検出 | bdw-gc のインストールプレフィクス |

最初の 3 つは実行時（生成したプログラム）に、後の 2 つはコンパイル時（`rwc` と `make`）に効きます。

### 9.3 デバッグの手がかり

- **再現性を上げる**: `RW_WORKERS=1` で並列性を消し、`RW_PREEMPT_MS=0` でプリエンプションを止めると、実行順がほぼ決まります。逆に競合を探すときは `RW_WORKERS` を増やして繰り返し実行します。
- **IR を読む**: `rwc emit-ir x.rw` で生成した IR を表示できます。`RW_OPT_LEVEL=0`（または `-O0`）でビルドすると、デバッガで見るコードが IR に近くなります。
- **PC=0 付近の SIGSEGV、ハング、同じファイバの二重実行**: ファイバを公開する順序の誤り（publish before save）を疑います。新しく park する経路が `rw_sched_park_commit` の commit の外でファイバを公開していないか確認してください（3.7 節）。
- **ガードページ付近の SIGSEGV**: 64 KiB のファイバスタックの溢れです。深い再帰か、エントリブロックの外に alloca が出ていないかを確認してください。
- **GC 後に値が壊れる**: 走査されないメモリ（`malloc`、atomic 領域）にポインタを置いていないか、未登録のスレッドが GC ヒープを触っていないか、`rw_fiber_swap` が `rw_gc_switch_begin/end` で挟まれているかを確認してください。
- **I/O が永久に戻らない**: fd を作った経路が `rw_net_reset_fd` を呼んでいるか、ブロッキングの fd を netpoller に回していないかを確認してください。
- **ファイバの解放漏れ**: C テストでは `rw_sched_live_fibers()` が 0 に戻るかで確かめられます。

---

## 付録: 仕様書・コメントとの差分

コードと食い違っている仕様書や、古くなったソースコメントの一覧です。本文はコードに従っています。

| 場所 | 記述 | 現在のコード |
|---|---|---|
| spec 12 | メインスレッドの `tcp_accept` はブロッキング accept でカーネル内で眠る | 常にノンブロッキング accept で、メインスレッドでは `rw_net_block` の 50 ms 刻みの `poll`（spec 57 で変更済み）。`net/tcp.c` 冒頭のコメントも古い記述のまま |
| spec 05 | 使えるスタックは「低位から高位へ伸びる」 | sp は領域の最高アドレスから始まり、低位に向かって伸びる |
| spec 05 / 06 | ハンドルは `calloc`、join 後に `free` | `rw_gc_alloc_uncollectable` で確保し、`free_handle` で `rw_gc_free`（spec 67） |
| spec 16、`aio.h` | 読み込みバッファは `malloc` したもの | `rw_gc_alloc_atomic`（spec 67） |
| spec 20 | コンストラクタは `malloc` | `rw_gc_alloc` |
| spec 25 | ソケット以外の fd はすべて aio プール | ノンブロッキングの FIFO は netpoller（spec 61 の追記どおり） |
| spec 23 | セーフポイントは関数の先頭とループ先頭（`for` は `while` に desugar） | 範囲 `for` は desugar されるが、`for x in list` とチャネルの `for` は irgen が個別に出力し、それぞれにセーフポイントを置く。クロージャの入口にも置く |
| `sched.h`、`sched.c` 冒頭、`runq.h` | 「スレッド 1 本、協調的、ロック不要」「Commit 2 では単一スレッド」 | M:N のマルチワーカーでワークスティーリングあり |
| `sched.h` | spawn の `arg` は `malloc` した構造体 | irgen が `rw_gc_alloc` で確保し、トランポリンが `rw_gc_free` |
| `gc.c` 冒頭 | 止まっているファイバは `[sp, top]` と保存レジスタを積む | フックが積むのはスタック範囲だけ。保存レジスタは uncollectable なハンドルの走査で拾われる（結果は同じ） |
| `tests/test_runtime_c.py` | — | `runtime/fiber/test_*.c` 22 本のうち 10 本だけが登録されている |
