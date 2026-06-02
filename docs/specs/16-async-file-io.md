# rw 非同期ファイル I/O（抽象 + thread pool バックエンド）

## Context

rw はファイル I/O ([[15-file-io]]) を fd 汎用の `read` / `write` / `close` で
提供している。実装は `runtime/io.c` の `rw_read` / `rw_write` で、`EAGAIN` のとき
fiber を netpoller に park する completion-ish なロジックを持つ。

しかしこのロジックは **ノンブロッキングソケット**にしか効かない。正規ファイルの
fd は `read(2)` / `write(2)` で `EAGAIN` を返さず、データが揃うまでカーネルが
ブロックして完了する。その間、その `read` を呼んだ fiber が乗っている
**ワーカースレッド M が 1 本まるごとブロックされる**。fiber を 100k 走らせる
ランタイムにとって、ファイル read 1 つでワーカーが止まるのは弱点である。

このサブプロジェクトは、ファイル I/O を「別スレッドにオフロードして fiber を
park し、完了したら起こす」非同期モデルにする。fiber はファイル read 中も
ワーカーを明け渡し、他の fiber が同じワーカーで走り続けられる。

### ロードマップ上の位置

ユーザーの最終目標は「ファイル I/O で io_uring を使えるようにする」こと。
io_uring は **completion モデル**（read 操作をカーネルに submit し、完了通知を
受ける）で、現在の readiness ベース netpoller とは根本的に異なる。そこで:

1. **このサブプロジェクト (PR 1)**: 非同期ファイル I/O の**抽象インターフェース**
   を定義し、最初の実体を **thread pool バックエンド**（全 OS 共通）で実装する。
2. **次のサブプロジェクト (PR 2)**: io_uring を **Linux でのより高速な実体**と
   して抽象の下に差し替え追加する。macOS は thread pool のまま。

抽象を先に置くことで、両 OS で「ファイル I/O が fiber をブロックしない」同じ
挙動を先に達成し、io_uring 導入時には完成済みの park/完了プロトコルへ実体を
差し込むだけにできる。

## Goals

- 非同期ファイル I/O の抽象 `runtime/aio.h` / `runtime/aio.c` を導入:
  - `void rw_aio_read(rw_str *out, int64_t fd, int64_t max)`
  - `int64_t rw_aio_write(int64_t fd, rw_str b)`
- 最初のバックエンドを **thread pool**（固定数の pthread + タスクキュー）で実装。
  worker が `read(2)` / `write(2)` を実行し、完了後に呼び出し fiber を起こす。
- `runtime/io.c` の `rw_read` / `rw_write` で `fstat(fd)` により fd 種別を判定し:
  - **正規ファイル (`S_ISREG`)** → `rw_aio_read` / `rw_aio_write`（thread pool）
  - **それ以外（ソケット等）** → 従来の netpoller 経路（`EAGAIN`→park、無改修）
- fiber を park して完了で起こすのに、既存スケジューラの
  `rw_sched_park_current()` / `rw_sched_enqueue_ready()` をそのまま使う。
- `rw_init` / `rw_shutdown` で thread pool のライフサイクルを管理。
- 呼び出し側（rw コード・sema・irgen）は**一切変更しない**。`read`/`write` が
  fd 種別に応じて透過的に最適経路を選ぶ。

## Non-Goals

- **io_uring 本体の実装** — PR 2 で行う。本 PR は抽象 + thread pool のみ。
- ファイル I/O 以外（ソケット）の経路変更 — netpoller はそのまま。
- `read` 以外のファイル操作（seek/stat/truncate 等）の非同期化。
- fixed buffer / バッチ submit / ゼロコピーなど io_uring 固有の最適化。
- thread pool のサイズ自動調整・ワークスティーリング（固定数で十分）。
- メインスレッド（fiber 外）での非同期化 — park できないので同期 `read(2)`
  にフォールバックする。

## アーキテクチャ

```
rw_read(fd, max)  ──┐  io.c で fstat(fd) 判定
rw_write(fd, b)   ──┤
                    ├── S_ISREG（ファイル） ──→ rw_aio_read / rw_aio_write (aio.c)
                    │                              │ fiber 上なら:
                    │                              │   task を submit → rw_sched_park_current()
                    │                              │   worker が read(2) → 結果格納
                    │                              │   → rw_sched_enqueue_ready(handle)
                    │                              │ fiber 外なら: 同期 read(2)
                    └── それ以外（socket 等） ──→ 従来の EAGAIN→rw_net_park_*（無改修）
```

### コンポーネント

**`runtime/aio.h`** — 公開インターフェース（プロトタイプは runtime.h に集約せず
aio.h に置く。net/tcp.h と異なり aio は独立した抽象なので自前ヘッダを持つ）:
- `void rw_aio_init(void);` / `void rw_aio_shutdown(void);`
- `void rw_aio_read(rw_str *out, int64_t fd, int64_t max);`
- `int64_t rw_aio_write(int64_t fd, rw_str b);`

**`runtime/aio.c`** — thread pool バックエンド:
- 固定数（既定 4、`RW_AIO_THREADS` 環境変数で上書き可）の worker pthread。
- ロック付きタスクキュー（`pthread_mutex_t` + `pthread_cond_t`）。
- タスク構造体: 操作種別（READ/WRITE）、fd、バッファ/サイズ、結果格納先、
  待っている `rw_fiber_handle *`。
- `rw_aio_read` / `rw_aio_write` の流れ（fiber 上）:
  1. タスクを構築（結果を書き戻すスロットと自分の fiber handle を持たせる）
  2. キューに push して condvar で worker を起こす
  3. `rw_sched_park_current()` で park（WAITING になりワーカーを明け渡す）
  4. worker が `read(2)`/`write(2)` を実行 → 結果をタスクのスロットに格納 →
     `rw_sched_enqueue_ready(task->handle)` で fiber を ready に戻す
  5. park から戻った fiber がタスクのスロットから結果を読み取り、呼び出し元へ
- fiber 外（`rw_sched_current_fiber() == NULL`）では park できないので、その場で
  同期 `read(2)`/`write(2)` を実行して返す（netpoller の park 同様の安全策）。

**`runtime/io.c`**（変更）:
- `rw_read` / `rw_write` の冒頭で `fstat(fd)` し、`S_ISREG` なら `rw_aio_*` に委譲。
  それ以外は既存ロジック（ソケット向け EAGAIN+netpoller park）をそのまま実行。
- `fstat` 失敗時は安全側で既存ロジックにフォールバック。

**`runtime/runtime.c`**（変更）:
- `rw_init` で `rw_netpoller_init()` の隣に `rw_aio_init()`。
- `rw_shutdown` で `rw_aio_shutdown()`（worker を止め join）。

### データフローと所有権

- `rw_aio_read` のバッファ: 既存 `rw_read` と同じく `out->ptr` に malloc した
  バッファの所有権を呼び出し元へ渡す（n>0 のとき）。malloc は aio.c 側で行い、
  worker が read 後に len を設定する。EOF/エラーは len=0 / ptr=NULL。
- タスク構造体は呼び出し fiber のスタック上に確保し、ポインタをキューに積む。
  fiber は完了まで park しているのでスタックは生存している（park 中も fiber の
  スタックは保持される）。worker は完了時にスロットへ書き、handle を enqueue。
- 結果の可視性: worker のスロット書き込みは `rw_sched_enqueue_ready` の前に行い、
  enqueue/park 復帰の happens-before（既存 netpoller と同じ acquire/release
  規約）に乗せる。

## 並行性の正しさ

このプロトコルは既存の 2 つの「park して別スレッドが起こす」経路と**完全に
同型**にする。実コードを確認済み（`runtime/fiber/sched.c`,
`runtime/net/netpoller*.c`）:

- **netpoller** (`rw_net_park_read`): `rw_netpoller_register_read(fd, h)` で
  kqueue/epoll に登録 → `rw_sched_park_current()`。poll スレッドがイベント時に
  `rw_sched_enqueue_ready(h)`。
- **join** (`park_on`): `wait_lock` 内で wait list に自分を入れ `state=WAITING`
  → 解放 → `rw_fiber_swap`。完了側 `finalize_fiber` が `state=DONE`(release) →
  `wait_lock` で list を一括 take → 各 waiter を `enqueue_ready`。
- **aio (本 PR)**: handle を確定 → タスクを submit → `rw_sched_park_current()`。
  worker が `read(2)`/`write(2)` 完了後、結果をスロットに格納 →
  `rw_sched_enqueue_ready(task->handle)`。

`rw_sched_enqueue_ready` は「別スレッドから安全に呼べる」と sched.h に明記され、
`enqueue_ready` は `state=READY` にして ready キューへ push する
(`sched.c:206`)。`rw_sched_park_current` は `state=WAITING` にして
`rw_fiber_swap` でスケジューラへ戻る (`sched.c:339`)。worker_main は swap から
戻った fiber を **`state==RUNNING` のときだけ** ready へ戻し、WAITING/DONE は
戻さない (`sched.c:373`)。これが鉄則「WAITING の fiber は ready queue に居て
はならない」を担保する。

### ctx 公開タイミングの競合について

stackful coroutine スケジューラには「`rw_sched_park_current` 内の
`rw_fiber_swap`（ctx 保存）が完了する前に、別スレッドが `enqueue_ready` →
別ワーカーが steal → half-written ctx を resume して PC=0 SEGV」という古典的
競合がある（[[stackful-coroutine-scheduling]]）。

本 PR の aio はこの競合を**新規に持ち込まない**。理由: 上記のとおり netpoller /
join と**同一のプロトコル**（handle 確定 → 登録/submit → park、相手スレッドは
park 後に enqueue）を使い、独自の publish-before-save パターンを作らないから。
worker が park より先に enqueue する可能性の有無・その安全性は、netpoller の
register→park（登録直後に fd が ready なら poll スレッドが即 enqueue しうる）と
**完全に同じ条件**であり、本 PR が既存コードの安全性水準を上下させることはない。

したがって本 PR の責務は「netpoller と寸分違わぬ順序でプロトコルを書く」こと。
もし将来この同型競合が顕在化するなら、それは netpoller・join・aio に共通の
スケジューラ層の課題であり、`park.c` の `wait_lock` 相当を park_current に
組み込む別タスクとして 3 経路まとめて対処する（本 PR のスコープ外）。

### 検証で competition を炙り出す

[[stackful-coroutine-scheduling]] の検証手順に従い、複数 fiber が並行で
ファイル I/O する e2e を **`RW_WORKERS=1` と `RW_WORKERS≥2`（steal が起きる）の
両方**で回し、SEGV/ハングなく結果が揃うことを確認する。これは aio 経路が
既存スケジューラと正しく協調できている証拠になる。

## 触るレイヤー

| レイヤー | ファイル | 変更 |
|---|---|---|
| Runtime (新規) | `runtime/aio.h` / `runtime/aio.c` | 抽象 + thread pool バックエンド |
| Runtime | `runtime/io.c` | `rw_read`/`rw_write` に `fstat` 判定を追加し、ファイルは aio に委譲 |
| Runtime | `runtime/runtime.c` | `rw_init`/`rw_shutdown` に aio init/shutdown |
| Runtime | `runtime/Makefile` | `aio.o` を OBJS とビルドルールに追加 |
| Compiler | `rwc/` | **無改修**（`read`/`write` の組み込みは不変、内部経路だけ変わる） |
| Examples | 既存 `examples/file_io.rw` で回帰確認 | 新規サンプルは任意 |
| Tests | `tests/` | 既存 e2e（file_io / tcp）が緑のまま。並行ファイル read の e2e を 1 本追加 |

`rwc/` を一切触らないのが本 PR の特徴。言語仕様は変わらず、ランタイムの
ファイル I/O 実装が同期ブロッキングから非同期オフロードに変わるだけ。

## 検証

```sh
make -C runtime
uv run pytest -q                       # 既存 169 + 新規が全緑
uv run rwc run examples/file_io.rw     # round-trip が従来どおり一致
```

- 回帰: `examples/file_io.rw`（round-trip）と `tests/test_e2e_tcp.py`（ソケット
  経路が無改修で動く）が緑。
- 非同期性の確認 (新規 e2e): 複数 fiber がそれぞれファイルを read/write する
  rw サンプルを `spawn` で並行実行し、全 fiber の結果が正しく揃うこと。
  thread pool 経由で正しく park/再開できている証拠になる。
  （タイミング依存にならないよう、出力は決定的な内容にする。）
- メインスレッド経路: fiber 外から `read`/`write` を呼ぶ既存サンプル（main 直下
  での file_io）が同期フォールバックで動くこと（file_io.rw が該当）。

## リスクと対処

- **park/enqueue の競合 (ctx 公開タイミング)**: netpoller / join と**同一
  プロトコル**（handle 確定 → submit → `park_current`、worker は結果格納後
  `enqueue_ready`）を厳守し、独自の publish-before-save を作らない。本 PR は
  既存の安全性水準を変えない（上記「並行性の正しさ」参照）。実装時に
  `RW_WORKERS≥2` + steal 負荷で SEGV/ハングがないことを必ず確認する。
- **fiber スタック上のタスク生存**: park 中も fiber スタックは保持されるため、
  タスクをスタックに置きポインタを渡して安全。worker は完了書き込みのみ行い、
  タスクを free しない（呼び出し fiber が所有）。
- **fstat のコスト**: ファイル/ソケット判定に毎回 `fstat` を 1 回呼ぶ。read 1 回
  あたり syscall 1 増だが、ブロッキング read の代替としては無視できる。PR2 の
  io_uring 化でも同じ判定を流用できる。
- **メインスレッドのブロック**: fiber 外では同期 read にフォールバックするため、
  メインスレッドは従来どおりブロックしうる（Non-Goal）。ただし netpoller 同様、
  ワーカー M と netpoller/aio スレッドは別なので並行タスクは進む。
- **「ついでに」誘惑**: io_uring・fixed buffer・seek 等には手を出さない
  (Non-Goals)。本 PR は抽象 + thread pool の最小実装に閉じる。
