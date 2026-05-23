# rw netpoller + 最小 TCP API

## Context

ロードマップの **最終目標**「rw で echo server が書ける」を達成する PR。
これまでの言語拡張 (string / Bytes / List / Option / Result / match) と
ランタイム拡張 (M:N スケジューラ) を組み合わせ、**fiber が `recv` で
ブロックしているように見えて裏で kqueue/epoll が回る** という Go 風の
書き味を実現する。

これまでに揃った道具:

- M:N スケジューラ (#90): pthread worker + work-stealing + fiber 間 wait list
- 文字列 / Bytes (#91, #92): バイトデータの表現
- List[int] (#93): クライアント fd の配列が持てる
- Option / Result (#94, #95): エラー表現の土台 (ただし今回は使わない)

最後に必要なのは:

- **nonblocking I/O + fd readiness 監視**: `kqueue(2)` / `epoll(7)`
- **fiber を fd 監視に紐づけて park / wake**: netpoller スレッド
- **rw 言語からの TCP API**: `tcp_listen` / `tcp_accept` / `tcp_read` / `tcp_write` / `tcp_close`

ロードマップ:

1. 文字列 `len` / `==` / `+` (#91)
2. Bytes 型 (#92)
3. List[int] (#93)
4a. Option[int] + match (#94)
4b. Result[int, int] (#95)
4c. (将来) 真のジェネリクス化
5. **このサブプロジェクト**: netpoller + TCP API → echo server

## Goals

- ランタイムに netpoller スレッドを追加 (kqueue/epoll、専用 pthread 1 つ)
- fiber を fd readiness で park / wake する内部 API (`rw_net_park_read/write`)
- rw 言語に 5 つの TCP 組込み:
  - `tcp_listen(port: int) -> int`
  - `tcp_accept(listen_fd: int) -> int`
  - `tcp_read(fd: int, max: int) -> Bytes`
  - `tcp_write(fd: int, b: Bytes) -> int`
  - `tcp_close(fd: int) -> int`
- `examples/tcp_echo.rw` が動く (1 接続 + 10 並列接続を Python から検証)
- 公開既存 ABI と既存 example はすべて回帰なし

## Non-Goals

- IPv6 / UDP / TLS / Unix domain socket
- `tcp_listen` のホスト指定 (`0.0.0.0` 固定 IPv4)
- 詳細エラー (errno 取得 API)
- graceful shutdown / SIGINT ハンドラ (Ctrl-C で殺す前提)
- partial write の自動 retry (`tcp_write` は実際に書いたバイト数を返すだけ、
  全部書ききるのはユーザコードの責任)
- 同一 fd への複数 fiber 同時 park (protocol で 1 fd = 1 fiber 規約)
- Result 型でのエラー表現 (`tcp_read` は `Bytes` を返し len==0 で EOF/エラー
  両方を表現)
- 接続数の C10k ベンチ (最小 e2e のみ。1 接続成功 + 10 並列接続)
- ファイル I/O / pipe / TTY (今回 socket のみ、kqueue/epoll で監視可能な
  fd でも他種類はスコープ外)
- fd 上限の自動引き上げ (シェルで `ulimit -n` を上げる前提、ランタイムが
  `setrlimit` を呼ぶことはしない)
- main thread の fiber 化 (main は worker でも fiber でもない単独 thread
  のまま、`tcp_accept` を main から呼ぶと blocking accept で kernel sleep)

## 設計

### システム全体図

```
                                 +---------------------+
                                 |  netpoller thread   |
                                 |  kevent / epoll_wait|
                                 +----------+----------+
                                            |
                                            | enqueue_ready(fiber)
                                            v
+--------+   spawn    +------------------------------------+
|  main  | ---------> | ready queue (global + per-P rings) |
| thread |   accept   +-------+----------+-------+---------+
+--------+                    |          |       |
  (kernel sleep               |          |       |
   in blocking                v          v       v
   accept)                +--------+ +--------+ +--------+
                          | wkr M1 | | wkr M2 | | wkr Mn |
                          +---+----+ +---+----+ +---+----+
                              |          |          |
                              v          v          v
                          fiber 実行 (tcp_read / tcp_write / ...)
                              |
                              | EAGAIN → rw_net_park_read(fd)
                              |   ↓
                              | netpoller に登録、fiber WAITING、worker M は次の fiber へ
                              |
                              | (netpoller が ready を検知すると上の矢印に戻る)
```

スレッド本数:

| 種類 | 本数 | 役割 |
|---|---|---|
| main | 1 | `rw_user_main` 実行、`tcp_accept` で blocking sleep |
| worker M | `sysconf(_SC_NPROCESSORS_ONLN)`、上限 64 | fiber を `find_runnable` + `rw_fiber_swap` で実行 |
| netpoller | 1 | `kevent` / `epoll_wait` で fd ready を監視し、対応 fiber を `enqueue_ready` |

= **`nproc + 2` 本** (接続数に依存しない)。

### netpoller スレッドの動き

```c
void *rw_netpoller_main(void *arg) {
    while (!atomic_load(&g_netpoller_shutdown)) {
        // 1 回の syscall で最大 128 event を取得 (kernel が sleep してくれる)
        int n = kevent(g_kq, NULL, 0, events, 128, NULL);
        for (int i = 0; i < n; i++) {
            rw_fiber_handle *f = (rw_fiber_handle *)events[i].udata;
            // 1 fd = 1 fiber (protocol 規約)、ONESHOT なので再登録なし
            enqueue_ready(f);   // 既存の M:N スケジューラを再利用
        }
    }
    return NULL;
}
```

ONESHOT モード (`EV_ONESHOT` / `EPOLLONESHOT`) を使う理由:

- 一度 ready 通知したら kernel が自動的に監視から外す
- 同じ fd を再 park したいときは fiber が改めて `rw_net_park_*` を呼んで登録
- 「fiber が close する前に複数 fiber が park してしまう」race の窓を最小化

### park / wake API

```c
// runtime/net/netpoller.h
void rw_netpoller_init(void);
void rw_netpoller_shutdown(void);

int  rw_set_nonblocking(int fd);    // O_NONBLOCK を立てる (idempotent)

// Block current fiber until fd is readable / writable.
// Must be called from a fiber (tls_m != NULL).
void rw_net_park_read(int fd);
void rw_net_park_write(int fd);
```

`rw_net_park_read` の実装:

```c
void rw_net_park_read(int fd) {
    rw_M *m = tls_m;
    rw_fiber_handle *me = m->current;
    atomic_store_explicit(&me->state, RW_FIBER_WAITING, memory_order_relaxed);
    register_for_read(fd, me);     // kqueue / epoll に EV_ONESHOT で登録
    rw_fiber_swap(&me->ctx, &m->sched_ctx);
    // ここから戻った時点で netpoller がこの fiber を enqueue_ready している
}
```

`register_for_read` のプラットフォーム別実装:

```c
// netpoller_kqueue.c
static void register_for_read(int fd, rw_fiber_handle *f) {
    struct kevent kev;
    EV_SET(&kev, fd, EVFILT_READ, EV_ADD | EV_ONESHOT, 0, 0, f);
    kevent(g_kq, &kev, 1, NULL, 0, NULL);
}

// netpoller_epoll.c
static void register_for_read(int fd, rw_fiber_handle *f) {
    struct epoll_event ev = {
        .events = EPOLLIN | EPOLLONESHOT,
        .data.ptr = f,
    };
    // 既に登録済みなら MOD、未登録なら ADD。両方試して片方を成功とする。
    if (epoll_ctl(g_ep, EPOLL_CTL_MOD, fd, &ev) != 0) {
        epoll_ctl(g_ep, EPOLL_CTL_ADD, fd, &ev);
    }
}
```

epoll は ADD/MOD で挙動が違うので両方試行。kqueue は EV_ADD だけで十分。

### tcp_* helper の実装パターン

```c
// runtime/net/tcp.c

int64_t rw_tcp_listen(int64_t port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_ANY),
        .sin_port = htons((uint16_t)port),
    };
    if (bind(fd, (void*)&addr, sizeof(addr)) < 0) { close(fd); return -1; }
    if (listen(fd, 128) < 0)                       { close(fd); return -1; }
    return fd;
    // 注: listen fd は blocking のまま。main thread が tcp_accept で
    // 待つときに kernel sleep できるよう。fiber 経由で呼ばれた最初の
    // tcp_accept で nonblocking に切り替える。
}

int64_t rw_tcp_accept(int64_t listen_fd) {
    if (tls_m == NULL) {
        // main thread: blocking accept (kernel が main を寝かす、
        // worker M / netpoller は別 thread なので影響なし)
        int c = accept(listen_fd, NULL, NULL);
        if (c < 0) return -1;
        rw_set_nonblocking(c);
        return c;
    }
    // fiber 内: nonblocking accept + netpoller park
    rw_set_nonblocking(listen_fd);   // idempotent (初回のみ effect)
    for (;;) {
        int c = accept(listen_fd, NULL, NULL);
        if (c >= 0) { rw_set_nonblocking(c); return c; }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            rw_net_park_read(listen_fd);
            continue;
        }
        return -1;
    }
}

void rw_tcp_read(rw_str *out, int64_t fd, int64_t max) {
    if (max <= 0) { out->len = 0; out->ptr = NULL; return; }
    char *buf = malloc((size_t)max);
    if (!buf)     { out->len = 0; out->ptr = NULL; return; }
    for (;;) {
        ssize_t n = recv((int)fd, buf, (size_t)max, 0);
        if (n > 0)  { out->len = n; out->ptr = buf; return; }
        if (n == 0) { free(buf); out->len = 0; out->ptr = NULL; return; }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (tls_m) { rw_net_park_read((int)fd); continue; }
            // main thread (blocking でない fd を main が読む通常はないが、
            // 念のため) は EAGAIN を error 扱い
            free(buf); out->len = 0; out->ptr = NULL; return;
        }
        free(buf); out->len = 0; out->ptr = NULL; return;
    }
}

int64_t rw_tcp_write(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;
    for (;;) {
        ssize_t n = send((int)fd, b.ptr, (size_t)b.len, 0);
        if (n >= 0) return n;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (tls_m) { rw_net_park_write((int)fd); continue; }
            return -1;
        }
        return -1;
    }
}

int64_t rw_tcp_close(int64_t fd) {
    return close((int)fd);
}
```

### rw 言語側の組込み

5 つの組込み関数を Sema / irgen に追加。シグネチャは:

```
tcp_listen(int)        -> int
tcp_accept(int)        -> int
tcp_read(int, int)     -> Bytes
tcp_write(int, Bytes)  -> int
tcp_close(int)         -> int
```

全部 `spawn` 禁止 (組込みは spawn できない既存ルール)。

irgen は既存パターン:
- `tcp_read` は **pointer-out** (16 byte 戻り値だが alloca + load パターンに揃える)
- `tcp_write` は Bytes (16 byte) を value 渡し
- 残り 3 つはスカラ

### ファイル構成

```
runtime/net/                          (新規ディレクトリ)
├── netpoller.h                       (共通 API 宣言)
├── netpoller.c                       (init / shutdown / park / 共通ロジック)
├── netpoller_kqueue.c                (macOS 固有: kevent ベース)
├── netpoller_epoll.c                 (Linux 固有: epoll ベース)
├── tcp.h
└── tcp.c

runtime/runtime.h                     (5 つの tcp_* + 2 つの park プロトタイプ追加)
runtime/runtime.c                     (rw_init / rw_shutdown に netpoller 呼び出し追加)
runtime/Makefile                      (net/*.o + uname 分岐)

runtime/fiber/test_netpoller_pipe.c   (新規 C テスト)
runtime/fiber/test_tcp_loopback.c     (新規 C テスト)

rwc/sema.py                           (5 組込み + spawn 禁止)
rwc/irgen.py                          (5 組込み emit)

examples/tcp_echo.rw                  (echo server デモ)
examples/tcp_echo.rw.expected         (今回は使わない、後述)

tests/test_e2e_tcp.py                 (Python から socket で接続して echo を検証)

docs/specs/12-netpoller-tcp.md        (本ファイル)
docs/plans/2026-05-23-netpoller-tcp.md (writing-plans で作成)
```

### e2e テスト戦略

通常の `tests/test_e2e.py` のような「stdout を `.expected` と比較」は echo
server には合わない (echo server は無限ループで stdout も無い)。

新規ファイル `tests/test_e2e_tcp.py`:

```python
import socket, subprocess, time, signal, os

def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port

def _start_server(port):
    # examples/tcp_echo.rw をビルドして起動
    # ただし port は環境変数 RW_ECHO_PORT で渡せるよう example 側も対応
    # (もしくは tcp_listen の引数を埋め込んだ tmp .rw を生成)
    ...

def test_echo_single():
    port = _free_port()
    proc = _start_server(port)
    try:
        time.sleep(0.2)  # サーバ起動待ち
        s = socket.create_connection(('127.0.0.1', port))
        s.sendall(b"hello\n")
        assert s.recv(1024) == b"hello\n"
        s.close()
    finally:
        proc.terminate(); proc.wait()

def test_echo_concurrent_10():
    port = _free_port()
    proc = _start_server(port)
    try:
        time.sleep(0.2)
        socks = [socket.create_connection(('127.0.0.1', port)) for _ in range(10)]
        for i, s in enumerate(socks):
            s.sendall(f"client-{i}\n".encode())
        for i, s in enumerate(socks):
            assert s.recv(1024) == f"client-{i}\n".encode()
        for s in socks:
            s.close()
    finally:
        proc.terminate(); proc.wait()
```

port は `socket.bind(0)` で空きを確保。example の port をハードコードできない
ので、`tcp_echo.rw` の最終形は **port を `argv` か環境変数から読む** 必要が
ある。

ただし rw 言語に argv / env 読み取り API はまだ無い → **e2e 用に
`examples/tcp_echo.rw` を毎回テキスト置換** する形にする (port 数字だけ
書き換える簡易テンプレ)。これが最小コスト。

### コミット構成 (1 PR、6 commits)

1. **runtime: netpoller スケルトン (init/shutdown のみ)**
   - `runtime/net/netpoller.{c,h}`, `netpoller_kqueue.c`, `netpoller_epoll.c`
   - `rw_netpoller_init` で pthread を起動、`rw_netpoller_shutdown` で join
   - Makefile に `net/*.o` 追加、`uname -s` で kqueue/epoll を分岐
   - `runtime/runtime.c` の `rw_init` / `rw_shutdown` に呼び出し追加
   - C テスト `test_netpoller_init.c` (init/shutdown を 10 回回す)

2. **runtime: park/wake と pipe テスト**
   - `rw_set_nonblocking`、`rw_net_park_read/write` の本実装
   - netpoller スレッド本体のループ
   - C テスト `test_netpoller_pipe.c`: pipe 2 つを作り、reader fiber が
     `rw_net_park_read` で park、writer fiber が write、reader が起きるか

3. **runtime: tcp_* helper**
   - `runtime/net/tcp.{c,h}`
   - 5 関数の実装
   - C テスト `test_tcp_loopback.c`: localhost で listen → connect → recv/send

4. **rwc: 5 組込みを sema + irgen**
   - `_check_call` に 5 分岐 + spawn 禁止
   - `_emit_call` に 5 分岐 (`tcp_read` は pointer-out shim)
   - positive 5 + negative 5 (引数型エラー、spawn 禁止)

5. **examples + e2e**
   - `examples/tcp_echo.rw`
   - `tests/test_e2e_tcp.py` (single + concurrent 10)
   - `tests/test_e2e.py` には追加しない (echo server は stdout 比較に
     合わないので別 e2e ファイル)

6. **plan ファイル commit**

### 検証

```sh
# ランタイム単体
make -C runtime clean && make -C runtime
cd runtime
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_netpoller_pipe.c librw.a -o fiber/test_netpoller_pipe && ./fiber/test_netpoller_pipe
cc -O2 -Wall -Wextra -std=c11 -D_GNU_SOURCE -pthread fiber/test_tcp_loopback.c librw.a -o fiber/test_tcp_loopback && ./fiber/test_tcp_loopback

# pytest
cd ..
uv run pytest -q
# 期待: 既存 131 + sema 新規 10 + e2e_tcp 新規 2 = 143 件

# 手動 echo 確認
RW_ECHO_PORT=18080 uv run rwc run examples/tcp_echo.rw &
sleep 0.2
nc 127.0.0.1 18080 <<< 'hello'   # => hello
kill %1
```

## リスクと対処

| リスク | 対処 |
|---|---|
| kqueue と epoll の API 差異が漏れる | netpoller.h で共通 API、`netpoller_kqueue.c` / `netpoller_epoll.c` を `#if defined(__APPLE__)` で完全分離。Linux CI で epoll パスを検証、ローカル macOS で kqueue パス |
| fiber が park 直後 close されると ONESHOT 登録が garbage に | ONESHOT は kernel が自動で外す。close 後に kevent が ready を返してきても fiber pointer は無効でない (fiber handle は join まで生きる)。double-wake のリスクのみで実害なし |
| main thread が tcp_accept で sleep 中に Ctrl-C が来る | OS が SIGINT で main を起こす、accept が EINTR で返る → rw_tcp_accept は -1 を返してユーザコードがループから抜ける。これは spec の現状動作 |
| ephemeral port を CI で確保するときの port 衝突 | Python 側で `socket.bind(0)` で空き port を取り、それを env 経由で rw に渡す |
| 接続失敗時の `tcp_accept` リトライ無限ループ | accept が EAGAIN 以外のエラー (ECONNABORTED など) を返したら -1 を返す。ユーザコードが `if c < 0: break` で抜ける責任 |
| netpoller スレッドが kevent でずっと寝続けて shutdown シグナルに気づかない | shutdown 時に `g_netpoller_shutdown = 1` をセットし、self-pipe または eventfd で netpoller を起こす (kqueue は EVFILT_USER、epoll は eventfd) |
| 既存 e2e (`test_e2e.py`) は無限ループサーバを実行しないので問題なし | parametrize に tcp_echo は追加しない。新規 `test_e2e_tcp.py` で扱う |
| Linux で `epoll_ctl` の MOD/ADD どっち呼ぶか | ADD 失敗 (EEXIST) のときに MOD にフォールバック、または逆。両方試す形で吸収 |
| fd 上限 (`ulimit -n`) を超えた接続で accept が EMFILE | エラー扱い (`-1` 返す)。ユーザコードで対処。spec で「ulimit -n を上げる前提」明示 |
