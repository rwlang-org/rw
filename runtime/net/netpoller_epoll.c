/*
 * Linux netpoller backend using epoll + eventfd.
 */

#if defined(__linux__)

#include "netpoller.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <unistd.h>

static int g_ep = -1;
static int g_wake_fd = -1;     /* eventfd for shutdown wake-up */

int rw_netpoller_platform_init(void) {
    g_ep = epoll_create1(EPOLL_CLOEXEC);
    if (g_ep < 0) return -1;
    g_wake_fd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    if (g_wake_fd < 0) { close(g_ep); g_ep = -1; return -1; }
    struct epoll_event ev = {
        .events = EPOLLIN,
        .data.ptr = NULL,   /* NULL marks the wake-up fd */
    };
    if (epoll_ctl(g_ep, EPOLL_CTL_ADD, g_wake_fd, &ev) != 0) {
        close(g_wake_fd); close(g_ep); g_wake_fd = -1; g_ep = -1;
        return -1;
    }
    return 0;
}

void rw_netpoller_platform_shutdown(void) {
    if (g_wake_fd >= 0) {
        uint64_t one = 1;
        ssize_t r = write(g_wake_fd, &one, sizeof(one));
        (void)r;
    }
}

void rw_netpoller_platform_run(void) {
    struct epoll_event events[128];
    while (!rw_netpoller_is_shutdown()) {
        int n = epoll_wait(g_ep, events, 128, -1);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("epoll_wait");
            break;
        }
        for (int i = 0; i < n; i++) {
            void *p = events[i].data.ptr;
            if (p == NULL) {
                /* shutdown wake-up; drain the eventfd */
                uint64_t v;
                ssize_t r = read(g_wake_fd, &v, sizeof(v));
                (void)r;
                continue;
            }
            uint32_t ev = events[i].events;
            int mask = 0;
            if (ev & (EPOLLIN | EPOLLERR | EPOLLHUP | EPOLLRDHUP)) mask |= 1 << RW_NET_READ;
            if (ev & (EPOLLOUT | EPOLLERR | EPOLLHUP)) mask |= 1 << RW_NET_WRITE;
            rw_netpoller_ready((rw_pollfd *)p, mask);
        }
    }
    if (g_wake_fd >= 0) { close(g_wake_fd); g_wake_fd = -1; }
    if (g_ep >= 0)      { close(g_ep);      g_ep = -1; }
}

/* epoll has one registration per fd, so `events` must name every
 * direction that still has waiters (rw_netpoller_ready re-arms the rest
 * after a one-shot event). */
int rw_netpoller_register(int fd, rw_pollfd *pd, int events) {
    uint32_t mask = EPOLLONESHOT | EPOLLRDHUP;
    if (events & (1 << RW_NET_READ))  mask |= EPOLLIN;
    if (events & (1 << RW_NET_WRITE)) mask |= EPOLLOUT;
    struct epoll_event ev = { .events = mask, .data.ptr = pd };
    /* Try MOD first (already registered), fall back to ADD. */
    if (epoll_ctl(g_ep, EPOLL_CTL_MOD, fd, &ev) == 0) return 0;
    if (errno != ENOENT) return -1;
    return epoll_ctl(g_ep, EPOLL_CTL_ADD, fd, &ev);
}

#endif /* __linux__ */
