/*
 * TCP helpers. See docs/specs/12-netpoller-tcp.md for the design.
 *
 * - Listen fd is created blocking; main-thread tcp_accept uses the
 *   blocking accept() so the OS can kernel-sleep the main thread.
 * - Fiber-thread tcp_accept switches the listen fd to nonblocking
 *   on first use, then loops on accept + netpoller park.
 * - tcp_read / tcp_write always assume the fd is nonblocking
 *   (set by tcp_accept when handing out the client fd).
 */

#include "tcp.h"
#include "netpoller.h"
#include "../fiber/sched.h"
#include "../runtime.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

int64_t rw_tcp_listen(int64_t port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }
    if (listen(fd, 128) < 0) {
        close(fd);
        return -1;
    }
    return (int64_t)fd;
}

int64_t rw_tcp_accept(int64_t listen_fd) {
    rw_fiber_handle *me = rw_sched_current_fiber();
    if (me == NULL) {
        /* main thread: blocking accept (kernel sleeps the main thread;
         * worker M / netpoller are separate threads and keep running). */
        int c = accept((int)listen_fd, NULL, NULL);
        if (c < 0) return -1;
        rw_set_nonblocking(c);
        return (int64_t)c;
    }
    /* fiber thread: nonblocking accept + netpoller park */
    rw_set_nonblocking((int)listen_fd);
    for (;;) {
        int c = accept((int)listen_fd, NULL, NULL);
        if (c >= 0) {
            rw_set_nonblocking(c);
            return (int64_t)c;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            rw_net_park_read((int)listen_fd);
            continue;
        }
        return -1;
    }
}

void rw_tcp_read(rw_str *out, int64_t fd, int64_t max) {
    if (max <= 0) { out->len = 0; out->ptr = NULL; return; }
    char *buf = (char *)malloc((size_t)max);
    if (!buf)     { out->len = 0; out->ptr = NULL; return; }
    for (;;) {
        ssize_t n = recv((int)fd, buf, (size_t)max, 0);
        if (n > 0)  { out->len = n; out->ptr = buf; return; }
        if (n == 0) { free(buf); out->len = 0; out->ptr = NULL; return; }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (rw_sched_current_fiber()) {
                rw_net_park_read((int)fd);
                continue;
            }
            free(buf); out->len = 0; out->ptr = NULL; return;
        }
        free(buf); out->len = 0; out->ptr = NULL; return;
    }
}

int64_t rw_tcp_write(int64_t fd, rw_str b) {
    if (b.len <= 0) return 0;
    for (;;) {
        ssize_t n = send((int)fd, b.ptr, (size_t)b.len, 0);
        if (n >= 0) return (int64_t)n;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (rw_sched_current_fiber()) {
                rw_net_park_write((int)fd);
                continue;
            }
            return -1;
        }
        return -1;
    }
}

int64_t rw_tcp_close(int64_t fd) {
    return (int64_t)close((int)fd);
}
