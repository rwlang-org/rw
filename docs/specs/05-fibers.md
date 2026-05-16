# rw fiber ランタイム

このドキュメントは、当初の `OS スレッド + Future` 方式(`docs/specs/03-runtime-and-irgen.md`)を
**緑色スレッド(stackful coroutine + 小さいスタック)** に置き換えた新ランタイムを
説明する。コンパイラ側の ABI(`rw_spawn_*` / `rw_await_*` / `rw_future_t`)は
完全に保たれているので、ユーザーが書く rw コードは何も変わらない。

## モチベーション

`pthread_create` ベースの旧実装は **1 万 spawn を超えると破綻**する:

- macOS のデフォルトスレッドスタックは 512KB、Linux は 8MB
- カーネル側の `task_struct` / `kthread` も 1 本あたり数 KB
- `ulimit -u` のデフォルトが数千〜1 万のホストでは `pthread_create` が EAGAIN を返す
- コンテキストスイッチコストが OS スケジューラ経由で μs オーダー

これは「Web サーバー(C10k)を rw で書きたい」「数千ワーカーを spawn したい」
というユースケースに合わなかった。fiber 版では:

- **1 fiber あたり 64KB + ガード 2 ページ**(arm64 で 16K + 16K)
- **コンテキストスイッチはアセンブリ 1 関数**で、コール 1 回 + ロード/ストア数十命令
- **OS リソースを使わない**(`pthread_create` を呼ばない)
- 実測: macOS arm64 で **10 万 fiber が 437ms** で完走

## 用語

| 用語 | 意味 |
|---|---|
| **fiber** | 軽量スレッド。1 本の OS スレッド上で多重化される実行コンテキスト |
| **fiber context** | callee-saved レジスタの保存領域(`rw_fiber_ctx`) |
| **scheduler** | READY キューを持ち、yield/完了時に次の fiber を選んで swap する小さなループ |
| **trampoline** | 新規 fiber を初回起動するときに「`x0 = arg` を準備してから `entry` にジャンプ」する小さなアセンブリ関数 |
| **ハンドル** | spawn された fiber を識別する不透明ポインタ(`rw_future_t` の正体) |

## コンテキストスイッチ ABI

`runtime/fiber/fiber.h`:

```c
#define RW_FIBER_CTX_WORDS 21

typedef struct {
    uint64_t regs[RW_FIBER_CTX_WORDS];
} rw_fiber_ctx;

void rw_fiber_swap(rw_fiber_ctx *old, rw_fiber_ctx *new);
```

### arm64 のレジスタレイアウト

| word index | 内容 |
|---|---|
| 0..9 | x19..x28(integer callee-saved) |
| 10 | x29(FP) |
| 11 | x30(LR; 復帰先) |
| 12 | sp |
| 13..20 | d8..d15(FP callee-saved の下半分) |

`stp` / `ldp` のペアロード/ストアを使うことで命令数を最小化している。
詳細は `runtime/fiber/fiber_arm64.S` を参照。

### 新規 fiber の起動

`rw_fiber_swap` は「callee-saved を保存・復帰」しかしない。新規 fiber の
引数 `arg` は ABI 上 `x0` に置く必要があるが、`x0` は callee-saved では
ないので swap では保存されない。

解決:**`x19 = entry`, `x20 = arg`, `lr = trampoline` を仕込む**。
`rw_fiber_swap` から最初にこの fiber に切り替わったとき、`ret` で
`trampoline` に飛び、`trampoline` が `mov x0, x20; blr x19` を実行する。

## スケジューラ

`runtime/fiber/sched.h`:

```c
void rw_sched_init(void);
void rw_sched_shutdown(void);
void rw_sched_yield(void);

rw_fiber_handle *rw_sched_spawn_i64 (int64_t (*fn)(void *), void *arg);
/* ... f64 / bool / str / void も同様 ... */

int64_t rw_sched_join_i64 (rw_fiber_handle *h);
/* ... f64 / bool / str / void も同様 ... */
```

### 動作

- **スレッドは 1 本のまま**(マルチコアは将来の D-6 で対応)
- ready キューは FIFO の単方向リスト
- `rw_sched_yield()` は現在の fiber をキュー末尾に積み、次の fiber に
  swap する。次が無ければ呼び出し元(main または別 fiber)に戻る
- `rw_sched_join_*(h)` は対象 fiber が DONE になるまで yield を繰り返し、
  DONE になったら結果を取り出してハンドルを `free` する

### スタックレイアウト

| ページ | 用途 |
|---|---|
| `[base, base + page]` | low guard(PROT_NONE) |
| `[base + page, base + page + 64K]` | usable stack(下から上に伸びる) |
| `[base + page + 64K, base + 2page + 64K]` | high guard(PROT_NONE) |

ガードページは `mmap` + `mprotect(PROT_NONE)` で確保。スタックオーバーフロー
は SIGSEGV になるので silent corruption しない。

ページサイズは `sysconf(_SC_PAGESIZE)` で実行時取得(macOS arm64 = 16K、
Linux x86_64 = 4K)。

## ユーザー ABI(変更なし)

`runtime/runtime.h` の以下は **シグネチャ完全互換**:

```c
rw_future_t *rw_spawn_i64 (int64_t (*fn)(void *), void *args);
int64_t      rw_await_i64 (rw_future_t *f);
/* ...他の型も同様... */
```

実装は `runtime/runtime.c` で fiber スケジューラに委譲するシムになっている。
コンパイラ(`rwc`)が吐く LLVM IR は一切変える必要がない。

## await のセマンティクス

旧 pthread 版では `rw_await_*` は `pthread_join` で**呼び出しスレッドを
ブロック**していた。fiber 版では:

- 呼び出し fiber は **`rw_sched_yield()` を繰り返す**だけ
- その間、他の READY な fiber が走り続ける
- 対象 fiber が完了したら結果を取り出して戻る

つまり「await はもう協調的な待ち」であり、await 中に他の spawn 済み fiber
がブロックされない。

ただし**現状は完全に協調**で、長時間 CPU を握る fiber は他の fiber を
飢えさせる。割り込みベースのプリエンプションは将来課題(D-6 で検討)。

## ターゲット別の実装

| OS / arch | アセンブリファイル | 状況 |
|---|---|---|
| macOS arm64 | `fiber/fiber_arm64.S` | 動作確認済み |
| Linux aarch64 | `fiber/fiber_arm64.S` | 同じファイル、シンボル名のアンダースコアプレフィックスのみ条件付き |
| Linux x86_64 | `fiber/fiber_x86_64.S` | **動作確認済み(Docker linux/amd64 で全テスト緑)** |
| Windows | - | 対象外 |

x86_64 の実装メモ(System V AMD64 ABI):

- callee-saved 整数レジスタ: `rbx`, `rbp`, `r12`, `r13`, `r14`, `r15`, `rsp`
- 浮動小数 callee-saved は無し(XMM は全て caller-saved)
- リターンアドレスはスタック上。新規 fiber では `rw_fiber_ctx_init` が
  スタックトップに `&rw_fiber_trampoline` を書き込む
- トランポリン: `r12 = entry`, `r13 = arg` から `mov %r13, %rdi; call *%r12`

## 検証

- `runtime/fiber/test_pingpong.c`: 3 fiber の往復で swap 自体を検証
- `runtime/fiber/test_sched.c`: 1000 fiber で sum of squares が正しいことを検証
- `runtime/fiber/test_c10k.c`: 10 万 fiber で 1+2+…+N が正しいことを検証

加えて `tests/test_e2e.py` の `spawn_basic` / `spawn_many` / `spawn_string`
が fiber バックエンドでも緑のまま動く。

## 既知の制限

1. **スレッドは 1 本だけ**:CPU バウンドの並列実行はまだ得られない。
   work-stealing マルチコアスケジューラは別フェーズ。
2. **プリエンプションなし**:無限ループや重い計算をする fiber は他をブロックする。
3. **I/O 自動 yield なし**:`read`/`recv` 等の syscall が直接ブロックすると
   スケジューラ全体が止まる。epoll/kqueue 連携は別フェーズ。
4. **デバッガ表示**:lldb のスタックトレースは「現在の fiber 1 本分」しか
   見えない。複数 fiber 同時の状態取得は別 fiber を `info threads` で
   個別に検査できない。

## 今後

| ID | 内容 | 効果 |
|---|---|---|
| ~~D-4~~ | ~~Linux x86_64 アセンブリ追加~~ | **完了** |
| D-5 | I/O 多重化(epoll/kqueue 連携) | C10k Web サーバーが書ける |
| D-6 | work-stealing マルチコア | CPU バウンドでも真の並列 |
| D-7 | プリエンプション(タイマー or 安全点) | 行儀の悪い fiber を強制切替 |
