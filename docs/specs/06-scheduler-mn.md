# rw fiber ランタイム: M:N スケジューラ

このドキュメントは `05-fibers.md` の単一スレッド協調スケジューラを
**M:N (複数 OS スレッドで多数の fiber を多重化)** に拡張した実装を
説明する。公開 ABI (`rw_spawn_*` / `rw_await_*` / `rw_init` /
`rw_shutdown` / `rw_str`) は完全に互換で、`rwc/irgen.py` には一切
変更がない。

## 用語 (Go の GMP と同じ)

| 記号 | 意味 |
|---|---|
| **G** | fiber 1 つ。`rw_fiber_handle` で表現 |
| **M** | OS スレッド (pthread) 1 本。`rw_M` |
| **P** | 論理プロセッサ。256 スロットの有界リングを持つ。`rw_P` |
| **globq** | グローバルキュー。mutex 保護のリンクリスト。`rw_globq` |

M と P は 1:1 でペアになる。M 数 = P 数 = ワーカ数で、デフォルトは
`sysconf(_SC_NPROCESSORS_ONLN)`、上限 64。環境変数 `RW_WORKERS` で
上書きできる。

メインスレッドは **ワーカではなくオーケストレータ**。`rw_init` が
ワーカ群を spawn し、メインは `rw_user_main` を同期的に呼ぶだけ。

## G の状態機械

```
                       spawn
                         |
                         v
                      READY -----> RUNNING
                       ^               |
              unpark   |               | yield / park
                       |               v
                    WAITING <------ (park on wait list)
                                       |
                                       v
                                     DONE  (release-store; 結果公開済み)
```

- `READY`: いずれかのキュー (P-local ring または globq) に乗っている
- `RUNNING`: 現在ある M で実行中
- `WAITING`: 他 G の `wait_head` リストに park 中。どのキューにも居ない
- `DONE`: 完了。結果は trampoline が公開済み

`state` は `_Atomic int`。`RUNNING -> DONE` だけが **release/acquire**
ペアの同期エッジ。trampoline が結果書き込み後に release-store し、
join 側が acquire-load してから結果を読む。

## 実行フロー

### spawn

```
rw_sched_spawn_*(fn, arg)
  └─ spawn_common
      ├─ handle を calloc + mmap stack + rw_fiber_ctx_init
      ├─ join_mu / join_cv / wait_lock 初期化
      └─ enqueue_ready(h)
            ├─ tls_m があれば m->p の ring に push
            │   + 他 M が park 中なら cond_signal で 1 つ起こす
            └─ tls_m が NULL (main 由来) なら globq に push + cond_signal
```

### worker のメインループ

```
worker_main(m):
  tls_m = m
  loop:
    g = find_runnable(m)
    if g == NULL: break   (shutdown)
    g->state = RUNNING
    m->current = g
    rw_fiber_swap(&m->sched_ctx, &g->ctx)
    ──── 戻ってきた時点で g は yield/park/done のいずれか ────
    if g->state == RUNNING:
        enqueue_ready(g)     # yield だった → 戻す
    m->current = NULL
```

「ring に push するのは swap **後**」が重要 (後述)。

### find_runnable

```
find_runnable(m):
  loop:
    if g_shutdown: return NULL
    g = rw_runq_get(m->p)          # 自分の P から
    if g: return g
    g = globq から最大 CAP/2 個 refill して 1 つ取る
    if g: return g
    g = try_steal(m)               # 他 P から半分 steal
    if g: return g
    park on g_sched_cv (with shutdown re-check under lock)
```

### work-stealing (`try_steal`)

```
try_steal(m):
  offset = xorshift64(m) % nworkers   # 各 M ごとに別 PRNG
  for i in 0..nworkers-1:
    idx = (offset + i) % nworkers
    if idx == m->id: continue
    n = rw_runq_grab(g_ps[idx], batch, CAP/2)
    if n > 0:
        return batch[0] と batch[1..n-1] を自 P に push
  return NULL
```

`rw_runq_grab` は 1 回の CAS で victim の `head` を進めて
`ceil(n/2)` 個取り出す。

### fiber 内 await (`park_on`)

```
wait_done(target):
  if tls_m:                       # fiber が他 fiber を await
    while target->state != DONE:
      park_on(target):
        wait_lock 取得
        if target->state == DONE: 解放して return
        me->state = WAITING
        me を target->wait_head に push
        wait_lock 解放
        rw_fiber_swap(&me->ctx, &m->sched_ctx)
        ──── trampoline が起こしたら戻ってくる ────
  else:                           # main が fiber を await
    pthread_mutex_lock(&target->join_mu)
    while target->state != DONE:
      pthread_cond_wait(&target->join_cv, ...)
    pthread_mutex_unlock(...)
```

### trampoline 完了 (`finalize_fiber`)

```
finalize_fiber(h):
  atomic_store(h->state, DONE, release)   # 結果を公開
  pthread_mutex_lock(h->join_mu)
  pthread_cond_broadcast(h->join_cv)      # main 側 joiner を起こす
  pthread_mutex_unlock(h->join_mu)
  wait_lock 取得
    waiters = h->wait_head
    h->wait_head = NULL
  wait_lock 解放
  for w in waiters:
    enqueue_ready(w)                      # WAITING -> READY に戻す
  rw_fiber_swap(&h->ctx, &m->sched_ctx)
```

## 同期ポイント

| 対象 | writer | reader | ordering |
|---|---|---|---|
| `h->state` -> DONE | trampoline | joiner | release / acquire |
| `h->result.*` | trampoline (DONE より前) | joiner (DONE 確認後) | state の release/acquire に便乗 |
| `p->ring` / `head` / `tail` | owner (put/get), stealer (grab) | 同左 | Go 風: tail は release-store、head は CAS |
| `g_shutdown` | shutdown | worker loop | release / acquire |
| `wait_head` | parker (CAS で push), trampoline (atomic 取り出し) | 同左 | `wait_lock` (atomic_flag spinlock) で保護 |

## 落とし穴と対処

### 1. yield 時の "ctx 公開タイミング" 競合

最初は `rw_sched_yield` の中で「自分を ring に push してから swap」
していた。これは **重大なバグ** で、ring に乗った瞬間に他 M が
`rw_runq_grab` で取り、swap-in しようとする。しかし `rw_fiber_swap`
は自分の ctx を保存している途中なので、stealer は **半分書きの
ctx を読み込んで PC=0 にジャンプ** する。

修正: yield は ring に何もしないで swap だけする。「ring に戻す」
判定は `worker_main` 内で、swap が完全に戻ってきた **後** に行う。
これで swap による ctx 保存が完了してから stealer に渡る。

### 2. park 中の cond_wait 競合

joiner が「state を読む → cond_wait」の間に trampoline が DONE を
書いて broadcast を投げると、joiner は signal を取り逃がして永久に
寝てしまう。対処: `join_mu` を取った状態で state を再チェックし、
DONE なら cond_wait しない。trampoline 側も `join_mu` を取って
broadcast するので、joiner が「mu を取る前に DONE 公開」したら
joiner はその時点で抜ける、「mu を取った後に DONE 公開」したら
broadcast を取りこぼさない。

### 3. wait list park の競合

joiner が「state チェック → wait_head に push」の間に trampoline が
DONE 公開 + wait_head 取り出しを終えると、joiner は誰にも起こされず
寝る。対処: `wait_lock` の中で state を再チェック。

## RW_WORKERS

| 値 | 挙動 |
|---|---|
| 設定なし | `sysconf(_SC_NPROCESSORS_ONLN)`、上限 64 |
| `1` | デターミニスティック実行。`tests/test_e2e.py` はこれを固定 |
| `2`〜`64` | 指定通り |
| 範囲外 / 非数値 | デフォルトにフォールバック |

## 観測される並列スピードアップ

`runtime/fiber/test_steal.c` (200 CPU-bound fibers を 1 つの primer
fiber から spawn、つまり初期は全部 1 つの P に積まれる) を macOS
arm64 で実測:

| RW_WORKERS | elapsed | 1 スレッド比 |
|---|---|---|
| 1 | 505ms | 1.00x |
| 2 | 259ms | 1.95x |
| 4 | 139ms | 3.62x |
| 8 |  82ms | 6.15x |

work-stealing なしだと N=1 と同じ速度に張り付くはずで、これが
「不均等配置でもスケールする」ことを示す。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `runtime/fiber/sched.{c,h}` | M:N スケジューラ本体 |
| `runtime/fiber/runq.{c,h}` | 256 スロット有界リング + globq |
| `runtime/fiber/park.{c,h}` | wait list 用 atomic_flag spinlock |
| `runtime/fiber/fiber.{c,h}` | `rw_fiber_ctx_init` (変更なし) |
| `runtime/fiber/fiber_{arm64,x86_64}.S` | `rw_fiber_swap` (変更なし) |

## テスト

| バイナリ | 検証内容 |
|---|---|
| `test_sched` | 1000 fiber の spawn/join、合計値の正しさ |
| `test_c10k` | 10 万 fiber、所要時間ベンチ |
| `test_pingpong` | `rw_fiber_swap` の単体動作 |
| `test_runq` | 有界リング単体: FIFO / overflow / grab / globq |
| `test_wait` | fiber→fiber await、ネスト await、fan-out |
| `test_steal` | unbalanced 配置から並列スピードアップを確認 |
| `test_shutdown` | init/shutdown を 35 サイクル、リーク・デッドロックなし |
| `tests/test_e2e.py` | コンパイラ + ランタイム結合 (RW_WORKERS=1 固定) |
