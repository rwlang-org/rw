#ifndef RW_NETPOLLER_H
#define RW_NETPOLLER_H

#include <stdint.h>

#include "../fiber/sched.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Process-level init/shutdown. Called from rw_init / rw_shutdown. */
void rw_netpoller_init(void);
void rw_netpoller_shutdown(void);

/* Set O_NONBLOCK on fd. Idempotent. Returns 0 on success, -1 on
 * failure (errno set). */
int rw_set_nonblocking(int fd);

/* Park the current fiber until fd becomes readable / writable.
 * Caller MUST be a worker thread running a fiber (rw_sched_current_fiber()
 * != NULL). Behavior on main thread or netpoller thread is undefined. */
void rw_net_park_read(int fd);
void rw_net_park_write(int fd);

/* ---- Internals shared between netpoller.c and the platform files ---- */
/* These are intentionally exposed inside the runtime so the platform
 * file can implement init/shutdown/register without exposing kqueue or
 * epoll types in the public header. */

/* Platform-specific init: open kqueue/epoll fd, return 0 on success. */
int  rw_netpoller_platform_init(void);
/* Platform-specific shutdown: close fds, wake the poll loop. */
void rw_netpoller_platform_shutdown(void);
/* The poll loop body. Called from the netpoller pthread. Returns when
 * shutdown has been requested. */
void rw_netpoller_platform_run(void);
/* Register fd for readable/writable readiness, associated with handle.
 * ONESHOT semantics: kernel auto-deregisters after one notification. */
int  rw_netpoller_register_read (int fd, rw_fiber_handle *h);
int  rw_netpoller_register_write(int fd, rw_fiber_handle *h);

/* Shared shutdown-flag observer. The platform run() loop polls this
 * between events. Implemented in netpoller.c. */
int  rw_netpoller_is_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* RW_NETPOLLER_H */
