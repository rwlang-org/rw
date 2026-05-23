/*
 * TCP helpers — stub (Task 2 of the netpoller-tcp plan).
 *
 * The real implementations land in Task 4. These stubs let the build
 * succeed after Task 2 so the netpoller skeleton can be exercised on
 * its own (test_netpoller_pipe in Task 3 does not need any tcp_*).
 */

#include "tcp.h"
#include "netpoller.h"
#include "../runtime.h"

int64_t rw_tcp_listen(int64_t port) {
    (void)port;
    return -1;
}

int64_t rw_tcp_accept(int64_t listen_fd) {
    (void)listen_fd;
    return -1;
}

void rw_tcp_read(rw_str *out, int64_t fd, int64_t max) {
    (void)fd;
    (void)max;
    out->len = 0;
    out->ptr = (const char *)0;
}

int64_t rw_tcp_write(int64_t fd, rw_str b) {
    (void)fd;
    (void)b;
    return -1;
}

int64_t rw_tcp_close(int64_t fd) {
    (void)fd;
    return -1;
}
