/*
 * Address-based entry points shared by every handle kind (spec 50):
 * dial / listen / accept / open, plus local_addr / peer_addr.
 *
 *   tcp://host:port      host may be a name, an IPv4 literal, or [v6]
 *   unix:///path         stream Unix domain socket
 *   file:///path         open() only; a bare path also works
 *
 * Handles are plain fds today. Sockets returned here are nonblocking so the
 * generic read/write in io.c take the netpoller path; listening sockets stay
 * blocking until a fiber accepts on them (same rule as tcp.c).
 */

#include "netpoller.h"
#include "../aio.h"
#include "../fiber/sched.h"
#include "../runtime.h"
#include "../timer.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

typedef enum { ADDR_BAD, ADDR_TCP, ADDR_UNIX, ADDR_FILE } addr_kind;

typedef struct {
    addr_kind kind;
    char host[256];   /* tcp: host without brackets ("" = any) */
    char port[16];    /* tcp */
    char path[1024];  /* unix / file */
} parsed_addr;

static int has_prefix(rw_str s, const char *p) {
    size_t n = strlen(p);
    return s.len >= (int64_t)n && memcmp(s.ptr, p, n) == 0;
}

static int copy_cstr(char *dst, size_t cap, const char *src, size_t n) {
    if (n >= cap) return -1;
    memcpy(dst, src, n);
    dst[n] = '\0';
    return 0;
}

/* Parse `addr`; a bare path is accepted only when allow_bare_path. */
static void parse_addr(rw_str addr, int allow_bare_path, parsed_addr *out) {
    memset(out, 0, sizeof *out);
    out->kind = ADDR_BAD;
    if (addr.len <= 0) return;
    if (has_prefix(addr, "unix://") || has_prefix(addr, "file://")) {
        int is_unix = has_prefix(addr, "unix://");
        const char *p = addr.ptr + 7;
        size_t n = (size_t)addr.len - 7;
        if (n == 0 || p[0] != '/' || memchr(p, '\0', n)) return;
        if (copy_cstr(out->path, sizeof out->path, p, n) != 0) return;
        if (is_unix && n >= sizeof(((struct sockaddr_un *)0)->sun_path)) return;
        out->kind = is_unix ? ADDR_UNIX : ADDR_FILE;
        return;
    }
    if (has_prefix(addr, "tcp://")) {
        const char *p = addr.ptr + 6;
        const char *end = addr.ptr + addr.len;
        const char *host, *host_end, *colon;
        if (p < end && *p == '[') {
            host = p + 1;
            host_end = memchr(host, ']', (size_t)(end - host));
            if (!host_end || host_end + 1 >= end || host_end[1] != ':') return;
            colon = host_end + 1;
        } else {
            host = p;
            colon = NULL;
            for (const char *q = p; q < end; q++) if (*q == ':') colon = q;
            if (!colon) return;
            host_end = colon;
            if (memchr(host, ':', (size_t)(host_end - host))) return;  /* v6 needs [] */
        }
        const char *port = colon + 1;
        size_t plen = (size_t)(end - port);
        if (plen == 0 || plen > 5) return;
        for (size_t i = 0; i < plen; i++) if (port[i] < '0' || port[i] > '9') return;
        if (copy_cstr(out->host, sizeof out->host, host, (size_t)(host_end - host)) != 0) return;
        if (copy_cstr(out->port, sizeof out->port, port, plen) != 0) return;
        if (atoi(out->port) > 65535) return;
        out->kind = ADDR_TCP;
        return;
    }
    if (allow_bare_path && !memchr(addr.ptr, ':', (size_t)addr.len) &&
        !memchr(addr.ptr, '\0', (size_t)addr.len)) {
        if (copy_cstr(out->path, sizeof out->path, addr.ptr, (size_t)addr.len) == 0)
            out->kind = ADDR_FILE;
    }
}

/* ---- name resolution, offloaded to the aio pool (getaddrinfo blocks) ---- */

typedef struct {
    const char *host;
    const char *port;
    int passive;
    struct addrinfo *res;
    int rc;
} resolve_req;

static void do_resolve(void *arg) {
    resolve_req *r = (resolve_req *)arg;
    struct addrinfo hints;
    memset(&hints, 0, sizeof hints);
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_family = AF_UNSPEC;
    if (r->passive) hints.ai_flags = AI_PASSIVE;
    hints.ai_flags |= AI_NUMERICSERV;
    r->rc = getaddrinfo(r->host, r->port, &hints, &r->res);
}

static struct addrinfo *resolve(const char *host, const char *port, int passive) {
    resolve_req r = { host, port, passive, NULL, 0 };
    rw_aio_call(do_resolve, &r);
    if (r.rc != 0) { rw_errno = RW_ENOHOST; return NULL; }
    return r.res;
}

/* ---- dial ---- */

/* close() on a failure path must not hide the rw_errno being reported. */
static void close_keep_errno(int fd) {
    int saved = rw_errno;
    close(fd);
    rw_errno = saved;
}

/* Connect `fd` to `sa`, giving up at deadline_ns (0 = never) with
 * ETIMEDOUT. A fiber does a nonblocking connect and parks; the main thread
 * blocks in connect, or in poll when there is a deadline. */
static int connect_fd(int fd, const struct sockaddr *sa, socklen_t len, int64_t deadline_ns) {
    int on_fiber = rw_sched_current_fiber() != NULL;
    if (on_fiber || deadline_ns > 0) rw_set_nonblocking(fd);
    for (;;) {
        if (connect(fd, sa, len) == 0) break;
        if (rw_errno == EINTR) continue;
        if (rw_errno != EINPROGRESS || (!on_fiber && deadline_ns == 0)) return -1;
        /* A wake-up can be spurious (spec 53), so wait until the socket is
         * actually writable. */
        for (;;) {
            if (rw_net_block(fd, RW_NET_WRITE, deadline_ns)) { rw_errno = ETIMEDOUT; return -1; }
            struct pollfd p = { fd, POLLOUT, 0 };
            if (poll(&p, 1, 0) == 1) break;
        }
        int err = 0;
        socklen_t elen = sizeof err;
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &elen) != 0) return -1;
        if (err != 0) { rw_errno = err; return -1; }
        break;
    }
    rw_set_nonblocking(fd);
    return 0;
}

static void no_sigpipe(int fd) {
#ifdef SO_NOSIGPIPE
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#else
    (void)fd;
#endif
}

static int64_t dial_impl(rw_str addr, int64_t deadline_ns) {
    parsed_addr pa;
    parse_addr(addr, 0, &pa);
    if (pa.kind == ADDR_TCP) {
        if (pa.host[0] == '\0') return -1;
        struct addrinfo *res = resolve(pa.host, pa.port, 0);
        if (!res) return -1;
        int fd = -1;
        for (struct addrinfo *ai = res; ai; ai = ai->ai_next) {
            fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
            if (fd < 0) continue;
            if (connect_fd(fd, ai->ai_addr, ai->ai_addrlen, deadline_ns) == 0) break;
            close_keep_errno(fd);
            fd = -1;
        }
        { int saved = rw_errno; freeaddrinfo(res); rw_errno = saved; }
        if (fd >= 0) no_sigpipe(fd);
        return fd;
    }
    if (pa.kind == ADDR_UNIX) {
        struct sockaddr_un sun;
        memset(&sun, 0, sizeof sun);
        sun.sun_family = AF_UNIX;
        strcpy(sun.sun_path, pa.path);
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) return -1;
        if (connect_fd(fd, (struct sockaddr *)&sun, sizeof sun, deadline_ns) != 0) { close_keep_errno(fd); return -1; }
        no_sigpipe(fd);
        return fd;
    }
    return -1;
}

/* ---- listen / accept ---- */

static int bind_listen(int fd, const struct sockaddr *sa, socklen_t len) {
    if (bind(fd, sa, len) != 0 || listen(fd, 128) != 0) return -1;
    return 0;
}

static int64_t listen_impl(rw_str addr) {
    parsed_addr pa;
    parse_addr(addr, 0, &pa);
    if (pa.kind == ADDR_TCP) {
        int one = 1;
        if (pa.host[0] == '\0') {
            /* "tcp://:port" listens on every IPv4 interface, like tcp_listen. */
            struct sockaddr_in sin;
            memset(&sin, 0, sizeof sin);
            sin.sin_family = AF_INET;
            sin.sin_addr.s_addr = htonl(INADDR_ANY);
            sin.sin_port = htons((uint16_t)atoi(pa.port));
            int fd = socket(AF_INET, SOCK_STREAM, 0);
            if (fd < 0) return -1;
            setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
            if (bind_listen(fd, (struct sockaddr *)&sin, sizeof sin) != 0) { close_keep_errno(fd); return -1; }
            return fd;
        }
        struct addrinfo *res = resolve(pa.host, pa.port, 1);
        if (!res) return -1;
        int fd = -1;
        for (struct addrinfo *ai = res; ai; ai = ai->ai_next) {
            fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
            if (fd < 0) continue;
            setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
            if (bind_listen(fd, ai->ai_addr, ai->ai_addrlen) == 0) break;
            close_keep_errno(fd);
            fd = -1;
        }
        { int saved = rw_errno; freeaddrinfo(res); rw_errno = saved; }
        return fd;
    }
    if (pa.kind == ADDR_UNIX) {
        struct sockaddr_un sun;
        memset(&sun, 0, sizeof sun);
        sun.sun_family = AF_UNIX;
        strcpy(sun.sun_path, pa.path);
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) return -1;
        if (bind_listen(fd, (struct sockaddr *)&sun, sizeof sun) != 0) { close_keep_errno(fd); return -1; }
        return fd;
    }
    return -1;
}

int64_t rw_accept(int64_t listen_fd) {
    int64_t c = rw_tcp_accept(listen_fd);   /* records last_error */
    if (c >= 0) no_sigpipe((int)c);
    return c;
}

/* Record the outcome for last_error() (spec 51). A malformed address is
 * EINVAL. */
int64_t rw_dial(rw_str addr) {
    return rw_dial_timeout(addr, 0);
}

/* ms <= 0: no timeout. The deadline covers connecting, not name
 * resolution (spec 53). */
int64_t rw_dial_timeout(rw_str addr, int64_t ms) {
    rw_errno = EINVAL;
    int64_t fd = dial_impl(addr, ms > 0 ? rw_mono_ns() + ms * 1000000 : 0);
    if (fd >= 0) rw_net_reset_fd((int)fd);
    rw_set_last_error(fd >= 0 ? 0 : rw_errno);
    return fd;
}

int64_t rw_listen(rw_str addr) {
    rw_errno = EINVAL;
    int64_t fd = listen_impl(addr);
    if (fd >= 0) rw_net_reset_fd((int)fd);
    rw_set_last_error(fd >= 0 ? 0 : rw_errno);
    return fd;
}

/* ---- open ---- */

int64_t rw_open(rw_str addr, rw_str mode) {
    parsed_addr pa;
    parse_addr(addr, 1, &pa);
    if (pa.kind != ADDR_FILE) { rw_set_last_error(EINVAL); return -1; }
    rw_str path = { (int64_t)strlen(pa.path), pa.path };
    return rw_file_open(path, mode);
}

/* ---- addresses ---- */

static rw_str format_sockaddr(const struct sockaddr_storage *ss, socklen_t len) {
    char buf[1100];
    char host[INET6_ADDRSTRLEN];
    if (ss->ss_family == AF_INET) {
        const struct sockaddr_in *s = (const struct sockaddr_in *)ss;
        inet_ntop(AF_INET, &s->sin_addr, host, sizeof host);
        snprintf(buf, sizeof buf, "tcp://%s:%u", host, ntohs(s->sin_port));
    } else if (ss->ss_family == AF_INET6) {
        const struct sockaddr_in6 *s = (const struct sockaddr_in6 *)ss;
        inet_ntop(AF_INET6, &s->sin6_addr, host, sizeof host);
        snprintf(buf, sizeof buf, "tcp://[%s]:%u", host, ntohs(s->sin6_port));
    } else if (ss->ss_family == AF_UNIX) {
        const struct sockaddr_un *s = (const struct sockaddr_un *)ss;
        size_t off = offsetof(struct sockaddr_un, sun_path);
        size_t n = len > off ? strnlen(s->sun_path, len - off) : 0;
        snprintf(buf, sizeof buf, "unix://%.*s", (int)n, s->sun_path);
    } else {
        buf[0] = '\0';
    }
    return rw_str_copy_cstr(buf);
}

rw_str rw_local_addr(int64_t fd) {
    struct sockaddr_storage ss;
    socklen_t len = sizeof ss;
    memset(&ss, 0, sizeof ss);
    if (getsockname((int)fd, (struct sockaddr *)&ss, &len) != 0) return rw_str_from_cstr("", 0);
    return format_sockaddr(&ss, len);
}

rw_str rw_peer_addr(int64_t fd) {
    struct sockaddr_storage ss;
    socklen_t len = sizeof ss;
    memset(&ss, 0, sizeof ss);
    if (getpeername((int)fd, (struct sockaddr *)&ss, &len) != 0) return rw_str_from_cstr("", 0);
    return format_sockaddr(&ss, len);
}
