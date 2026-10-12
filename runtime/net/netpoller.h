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

/* Directions for rw_net_wait / per-fd timeouts (spec 53). */
#define RW_NET_READ  0
#define RW_NET_WRITE 1

/* Park the current fiber until fd may be ready in direction `dir`, or
 * until the monotonic deadline `deadline_ns` (0 = no deadline). Returns
 * RW_NET_TIMEDOUT, RW_NET_CLOSED (the handle was closed: spec 57), or 0.
 * A 0 return can be spurious: callers retry the syscall and wait again on
 * EAGAIN. Fiber only. */
#define RW_NET_TIMEDOUT 1
#define RW_NET_CLOSED   2
int rw_net_wait(int fd, int dir, int64_t deadline_ns);

/* Wake every fiber parked on fd with RW_NET_CLOSED; rw_close calls it. */
void rw_net_close_fd(int fd);

/* errno for a nonzero rw_net_wait / rw_net_block result. */
#define RW_NET_ERRNO(r) ((r) == RW_NET_CLOSED ? EBADF : ETIMEDOUT)

/* Per-fd timeouts in ms (0 = none), read by the I/O builtins (spec 53).
 * rw_net_reset_fd clears them and the closed mark (spec 57). Every runtime
 * path that creates an fd a fiber may wait on must call it, or the new
 * handle can inherit the state of a closed one with the same number. */
void    rw_net_set_timeout(int fd, int dir, int64_t ms);
int64_t rw_net_get_timeout(int fd, int dir);
void    rw_net_reset_fd(int fd);

/* Deadline for one operation starting now, from fd's timeout (0 = none). */
int64_t rw_net_deadline(int fd, int dir);

/* Wait for fd in direction dir until deadline_ns (0 = none): parks a fiber
 * (rw_net_wait), or blocks the calling thread in poll(2) off-fiber. Same
 * results as rw_net_wait. */
int rw_net_block(int fd, int dir, int64_t deadline_ns);

/* Opaque per-fd poll descriptor; the platform files hand it back. */
typedef struct rw_pollfd rw_pollfd;

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
/* `events` is a mask of (1 << RW_NET_READ) | (1 << RW_NET_WRITE). The
 * platform calls rw_netpoller_ready(pd, mask) for each event. */
int  rw_netpoller_register(int fd, rw_pollfd *pd, int events);
void rw_netpoller_ready(rw_pollfd *pd, int events);

/* Shared shutdown-flag observer. The platform run() loop polls this
 * between events. Implemented in netpoller.c. */
int  rw_netpoller_is_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* RW_NETPOLLER_H */
