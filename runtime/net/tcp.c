/*
 * TCP helpers. See docs/specs/12-netpoller-tcp.md for the design.
 *
 * - Listen fd is created blocking; main-thread tcp_accept uses the
 *   blocking accept() so the OS can kernel-sleep the main thread.
 * - Fiber-thread tcp_accept switches the listen fd to nonblocking
 *   on first use, then loops on accept + netpoller park.
 *
 * Generic fd I/O (read/write/close) has moved to runtime/io.c.
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

static int64_t tcp_listen_impl(int64_t port) {
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

static int64_t tcp_accept_impl(int64_t listen_fd) {
    rw_fiber_handle *me = rw_sched_current_fiber();
    if (me == NULL) {
        /* main thread: blocking accept (kernel sleeps the main thread;
         * worker M / netpoller are separate threads and keep running). */
        int64_t deadline = rw_net_deadline((int)listen_fd, RW_NET_READ);
        if (deadline > 0 && rw_net_block((int)listen_fd, RW_NET_READ, deadline)) {
            rw_errno = ETIMEDOUT;
            return -1;
        }
        int c = accept((int)listen_fd, NULL, NULL);
        if (c < 0) return -1;
        rw_set_nonblocking(c);
        return (int64_t)c;
    }
    /* fiber thread: nonblocking accept + netpoller park */
    rw_set_nonblocking((int)listen_fd);
    int64_t deadline = -1;
    for (;;) {
        int c = accept((int)listen_fd, NULL, NULL);
        if (c >= 0) {
            rw_set_nonblocking(c);
            return (int64_t)c;
        }
        if (rw_errno == EAGAIN || rw_errno == EWOULDBLOCK) {
            if (deadline < 0) deadline = rw_net_deadline((int)listen_fd, RW_NET_READ);
            if (rw_net_wait((int)listen_fd, RW_NET_READ, deadline)) {
                rw_errno = ETIMEDOUT;
                return -1;
            }
            continue;
        }
        return -1;
    }
}


/* Record the outcome for last_error() (spec 51). */
int64_t rw_tcp_listen(int64_t port) {
    rw_errno = 0;
    int64_t fd = tcp_listen_impl(port);
    if (fd >= 0) rw_net_reset_fd((int)fd);
    rw_set_last_error(fd >= 0 ? 0 : (rw_errno ? rw_errno : EINVAL));
    return fd;
}

int64_t rw_tcp_accept(int64_t listen_fd) {
    rw_errno = 0;
    int64_t c = tcp_accept_impl(listen_fd);
    if (c >= 0) rw_net_reset_fd((int)c);
    rw_set_last_error(c >= 0 ? 0 : (rw_errno ? rw_errno : EINVAL));
    return c;
}
