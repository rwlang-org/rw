/*
 * macOS / BSD netpoller backend using kqueue.
 */

#if defined(__APPLE__) || defined(__FreeBSD__)

#include "netpoller.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/event.h>
#include <sys/types.h>
#include <unistd.h>

static int g_kq = -1;

int rw_netpoller_platform_init(void) {
    g_kq = kqueue();
    return (g_kq < 0) ? -1 : 0;
}

void rw_netpoller_platform_shutdown(void) {
    /* Wake the kevent() that may be parked. EVFILT_USER with NOTE_TRIGGER
     * is the canonical way; we register it lazily inside run(). */
    struct kevent ev;
    EV_SET(&ev, 1, EVFILT_USER, 0, NOTE_TRIGGER, 0, NULL);
    kevent(g_kq, &ev, 1, NULL, 0, NULL);
}

static int register_user_wakeup(void) {
    struct kevent ev;
    EV_SET(&ev, 1, EVFILT_USER, EV_ADD | EV_CLEAR, 0, 0, NULL);
    return kevent(g_kq, &ev, 1, NULL, 0, NULL);
}

void rw_netpoller_platform_run(void) {
    register_user_wakeup();
    struct kevent events[128];
    while (!rw_netpoller_is_shutdown()) {
        int n = kevent(g_kq, NULL, 0, events, 128, NULL);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("kevent");
            break;
        }
        for (int i = 0; i < n; i++) {
            if (events[i].filter == EVFILT_USER) {
                /* shutdown wake-up; loop top will see the flag */
                continue;
            }
            rw_fiber_handle *h = (rw_fiber_handle *)events[i].udata;
            if (h) rw_sched_enqueue_ready(h);
        }
    }
    if (g_kq >= 0) { close(g_kq); g_kq = -1; }
}

int rw_netpoller_register_read(int fd, rw_fiber_handle *h) {
    struct kevent ev;
    EV_SET(&ev, fd, EVFILT_READ, EV_ADD | EV_ONESHOT, 0, 0, h);
    return kevent(g_kq, &ev, 1, NULL, 0, NULL);
}

int rw_netpoller_register_write(int fd, rw_fiber_handle *h) {
    struct kevent ev;
    EV_SET(&ev, fd, EVFILT_WRITE, EV_ADD | EV_ONESHOT, 0, 0, h);
    return kevent(g_kq, &ev, 1, NULL, 0, NULL);
}

#endif /* __APPLE__ || __FreeBSD__ */
