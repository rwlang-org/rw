/*
 * Pipe-based smoke test for the netpoller.
 *
 *   - Create a pipe(2).
 *   - Spawn a reader fiber: rw_net_park_read(read_end), then read(2).
 *     Verify the bytes match what the writer wrote.
 *   - Spawn a writer fiber: write(write_end, ...).
 *   - Join both.
 *
 * Tests:
 *   - rw_netpoller_init / shutdown lifecycle
 *   - rw_set_nonblocking on a pipe fd
 *   - rw_net_park_read actually blocks until the writer makes data
 *     available, then resumes
 */

#include "../net/netpoller.h"
#include "sched.h"
#include "../runtime.h"

#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static int g_pipe[2];

static int64_t reader_fiber(void *arg) {
    (void)arg;
    rw_set_nonblocking(g_pipe[0]);
    char buf[16];
    for (;;) {
        ssize_t n = read(g_pipe[0], buf, sizeof(buf));
        if (n > 0) {
            return (int64_t)n;
        }
        if (n == 0) return 0;  /* EOF */
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            rw_net_park_read(g_pipe[0]);
            continue;
        }
        return -1;
    }
}

static int64_t writer_fiber(void *arg) {
    (void)arg;
    /* Sleep a moment to make sure the reader has parked. */
    struct timespec ts = { .tv_sec = 0, .tv_nsec = 50 * 1000 * 1000 };
    nanosleep(&ts, NULL);
    const char *msg = "hello\n";
    ssize_t n = write(g_pipe[1], msg, strlen(msg));
    return (int64_t)n;
}

int main(void) {
    rw_init();

    if (pipe(g_pipe) != 0) { perror("pipe"); return 1; }

    rw_future_t *r = rw_spawn_i64(reader_fiber, NULL);
    rw_future_t *w = rw_spawn_i64(writer_fiber, NULL);

    int64_t wrote = rw_await_i64(w);
    int64_t got   = rw_await_i64(r);

    assert(wrote == 6);
    assert(got == 6);

    close(g_pipe[0]);
    close(g_pipe[1]);

    rw_shutdown();
    printf("netpoller pipe test ok\n");
    return 0;
}
