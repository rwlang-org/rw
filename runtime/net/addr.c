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
#include "../rwgc.h"
#include "../timer.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
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

typedef enum { ADDR_BAD, ADDR_INET, ADDR_UNIX, ADDR_FILE } addr_kind;

typedef struct {
    addr_kind kind;
    int socktype;     /* inet: SOCK_STREAM (tcp://) or SOCK_DGRAM (udp://) */
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
    if (has_prefix(addr, "tcp://") || has_prefix(addr, "udp://")) {
        out->socktype = has_prefix(addr, "udp://") ? SOCK_DGRAM : SOCK_STREAM;
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
        out->kind = ADDR_INET;
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
    int socktype;
    struct addrinfo *res;
    int rc;
} resolve_req;

static void do_resolve(void *arg) {
    resolve_req *r = (resolve_req *)arg;
    struct addrinfo hints;
    memset(&hints, 0, sizeof hints);
    hints.ai_socktype = r->socktype;
    hints.ai_family = AF_UNSPEC;
    if (r->passive) hints.ai_flags = AI_PASSIVE;
    hints.ai_flags |= AI_NUMERICSERV;
    r->rc = getaddrinfo(r->host, r->port, &hints, &r->res);
}

static struct addrinfo *resolve(const char *host, const char *port, int passive, int socktype) {
    resolve_req r = { host, port, passive, socktype, NULL, 0 };
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
    /* A fresh socket may reuse the number of a handle closed earlier: drop
     * that handle's closed mark and timeouts before waiting on it. */
    rw_net_reset_fd(fd);
    int on_fiber = rw_sched_current_fiber() != NULL;
    if (on_fiber || deadline_ns > 0) rw_set_nonblocking(fd);
    for (;;) {
        if (connect(fd, sa, len) == 0) break;
        /* An interrupted connect keeps going in the background (on Linux
         * the GC's stop-the-world signal can cause this, spec 67): wait for
         * it like a nonblocking one instead of calling connect again. */
        int interrupted = rw_errno == EINTR;
        if (!interrupted && (rw_errno != EINPROGRESS || (!on_fiber && deadline_ns == 0))) return -1;
        /* A wake-up can be spurious (spec 53), so wait until the socket is
         * actually writable. */
        for (;;) {
            int r = rw_net_block(fd, RW_NET_WRITE, deadline_ns);
            if (r) { rw_errno = RW_NET_ERRNO(r); return -1; }
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
    if (pa.kind == ADDR_INET) {
        if (pa.host[0] == '\0') return -1;
        struct addrinfo *res = resolve(pa.host, pa.port, 0, pa.socktype);
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

/* Stream sockets bind and listen; a datagram socket (udp://, spec 69) only
 * binds, and is nonblocking so recv_from / send_to take the netpoller path. */
static int bind_listen(int fd, const struct sockaddr *sa, socklen_t len) {
    int type = SOCK_STREAM;
    socklen_t tl = sizeof type;
    getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &tl);
    if (bind(fd, sa, len) != 0) return -1;
    if (type == SOCK_DGRAM) return rw_set_nonblocking(fd) == 0 ? 0 : -1;
    return listen(fd, 128) != 0 ? -1 : 0;
}

static int64_t listen_impl(rw_str addr) {
    parsed_addr pa;
    parse_addr(addr, 0, &pa);
    if (pa.kind == ADDR_INET) {
        int one = 1;
        if (pa.host[0] == '\0') {
            /* "tcp://:port" listens on every IPv4 interface, like tcp_listen. */
            struct sockaddr_in sin;
            memset(&sin, 0, sizeof sin);
            sin.sin_family = AF_INET;
            sin.sin_addr.s_addr = htonl(INADDR_ANY);
            sin.sin_port = htons((uint16_t)atoi(pa.port));
            int fd = socket(AF_INET, pa.socktype, 0);
            if (fd < 0) return -1;
            setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
            if (bind_listen(fd, (struct sockaddr *)&sin, sizeof sin) != 0) { close_keep_errno(fd); return -1; }
            return fd;
        }
        struct addrinfo *res = resolve(pa.host, pa.port, 1, pa.socktype);
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

/* `scheme` is "tcp" or "udp"; Unix sockets always print as unix://. */
static const char *scheme_of(int fd) {
    int type = SOCK_STREAM;
    socklen_t tl = sizeof type;
    getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &tl);
    return type == SOCK_DGRAM ? "udp" : "tcp";
}

static rw_str format_sockaddr(const struct sockaddr_storage *ss, socklen_t len, const char *scheme) {
    char buf[1100];
    char host[INET6_ADDRSTRLEN];
    if (ss->ss_family == AF_INET) {
        const struct sockaddr_in *s = (const struct sockaddr_in *)ss;
        inet_ntop(AF_INET, &s->sin_addr, host, sizeof host);
        snprintf(buf, sizeof buf, "%s://%s:%u", scheme, host, ntohs(s->sin_port));
    } else if (ss->ss_family == AF_INET6) {
        const struct sockaddr_in6 *s = (const struct sockaddr_in6 *)ss;
        inet_ntop(AF_INET6, &s->sin6_addr, host, sizeof host);
        snprintf(buf, sizeof buf, "%s://[%s]:%u", scheme, host, ntohs(s->sin6_port));
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
    return format_sockaddr(&ss, len, scheme_of((int)fd));
}

rw_str rw_peer_addr(int64_t fd) {
    struct sockaddr_storage ss;
    socklen_t len = sizeof ss;
    memset(&ss, 0, sizeof ss);
    if (getpeername((int)fd, (struct sockaddr *)&ss, &len) != 0) return rw_str_from_cstr("", 0);
    return format_sockaddr(&ss, len, scheme_of((int)fd));
}

/* ---- socket operations (spec 56) ---- */

static int64_t report(int rc) {
    rw_set_last_error(rc == 0 ? 0 : rw_errno);
    return rc == 0 ? 0 : -1;
}

int64_t rw_sock_shutdown(int64_t fd, rw_str how) {
    int h;
    if (how.len == 4 && memcmp(how.ptr, "read", 4) == 0) h = SHUT_RD;
    else if (how.len == 5 && memcmp(how.ptr, "write", 5) == 0) h = SHUT_WR;
    else if (how.len == 4 && memcmp(how.ptr, "both", 4) == 0) h = SHUT_RDWR;
    else { rw_set_last_error(EINVAL); return -1; }
    return report(shutdown((int)fd, h));
}

int64_t rw_set_nodelay(int64_t fd, int8_t on) {
    int v = on ? 1 : 0;
    return report(setsockopt((int)fd, IPPROTO_TCP, TCP_NODELAY, &v, sizeof v));
}

int64_t rw_set_keepalive(int64_t fd, int8_t on) {
    int v = on ? 1 : 0;
    return report(setsockopt((int)fd, SOL_SOCKET, SO_KEEPALIVE, &v, sizeof v));
}

/* listen(2) again on a listening socket only changes its backlog. */
int64_t rw_set_backlog(int64_t fd, int64_t n) {
    if (n < 1) { rw_set_last_error(EINVAL); return -1; }
    return report(listen((int)fd, (int)(n > 65535 ? 65535 : n)));
}

/* ---- datagrams (spec 69) ---- */

void rw_recv_from(rw_datagram *out, int64_t fd, int64_t max) {
    out->data = rw_str_from_cstr("", 0);
    out->addr = rw_str_from_cstr("", 0);
    if (max <= 0) { rw_set_last_error(EINVAL); return; }
    char *buf = (char *)rw_gc_alloc_atomic((size_t)max);
    int64_t deadline = -1;
    for (;;) {
        struct sockaddr_storage ss;
        socklen_t len = sizeof ss;
        ssize_t n = recvfrom((int)fd, buf, (size_t)max, 0, (struct sockaddr *)&ss, &len);
        if (n >= 0) {   /* an empty datagram is a datagram too */
            out->data.ptr = buf;
            out->data.len = n;
            out->addr = format_sockaddr(&ss, len, "udp");
            rw_set_last_error(0);
            return;
        }
        if (rw_errno == EINTR) continue;
        if (rw_errno == EAGAIN || rw_errno == EWOULDBLOCK) {
            if (deadline < 0) deadline = rw_net_deadline((int)fd, RW_NET_READ);
            int r = rw_net_block((int)fd, RW_NET_READ, deadline);
            if (r) { rw_set_last_error(RW_NET_ERRNO(r)); return; }
            continue;
        }
        rw_set_last_error(rw_errno);
        return;
    }
}

int64_t rw_send_to(int64_t fd, rw_str data, rw_str addr) {
    parsed_addr pa;
    parse_addr(addr, 0, &pa);
    if (pa.kind != ADDR_INET || pa.socktype != SOCK_DGRAM || pa.host[0] == '\0') {
        rw_set_last_error(EINVAL);
        return -1;
    }
    struct addrinfo *res = resolve(pa.host, pa.port, 0, SOCK_DGRAM);
    if (!res) { rw_set_last_error(rw_errno); return -1; }
    /* The first address whose family matches the socket. */
    struct sockaddr_storage local;
    socklen_t ll = sizeof local;
    memset(&local, 0, sizeof local);
    getsockname((int)fd, (struct sockaddr *)&local, &ll);
    struct addrinfo *ai = res;
    for (struct addrinfo *a = res; a; a = a->ai_next) {
        if (a->ai_family == local.ss_family) { ai = a; break; }
    }
    int64_t deadline = -1;
    int64_t result = -1;
    for (;;) {
        ssize_t n = sendto((int)fd, data.ptr, (size_t)data.len, 0, ai->ai_addr, ai->ai_addrlen);
        if (n >= 0) { result = n; rw_set_last_error(0); break; }
        if (rw_errno == EINTR) continue;
        if (rw_errno == EAGAIN || rw_errno == EWOULDBLOCK) {
            if (deadline < 0) deadline = rw_net_deadline((int)fd, RW_NET_WRITE);
            int r = rw_net_block((int)fd, RW_NET_WRITE, deadline);
            if (r) { rw_set_last_error(RW_NET_ERRNO(r)); break; }
            continue;
        }
        rw_set_last_error(rw_errno);
        break;
    }
    freeaddrinfo(res);
    return result;
}
