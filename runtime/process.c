/*
 * Child processes (spec 61): posix_spawnp with three pipes, waitpid on the
 * aio pool, and kill by signal name. posix_spawn is safe in a
 * multi-threaded process, unlike fork followed by arbitrary code.
 */

#include "runtime.h"
#include "aio.h"
#include "net/netpoller.h"

#include <errno.h>
#include <pthread.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

/* Serializes pipe creation and spawning, so a child started by one fiber
 * never inherits the pipes another fiber is setting up (there is no
 * portable pipe2(O_CLOEXEC) on macOS). */
static pthread_mutex_t g_spawn_mu = PTHREAD_MUTEX_INITIALIZER;

static char *cstr(rw_str s) {
    char *p = (char *)malloc((size_t)s.len + 1);
    if (!p) return NULL;
    if (s.len > 0) memcpy(p, s.ptr, (size_t)s.len);
    p[s.len] = '\0';
    return p;
}

static void close_pair(int p[2]) {
    if (p[0] >= 0) close(p[0]);
    if (p[1] >= 0) close(p[1]);
}

/* [pid, stdin_w, stdout_r, stderr_r], or an empty list with last_error. */
void rw_proc_start(rw_list_int *out, const rw_list *argv) {
    out->len = 0;
    out->cap = 0;
    out->data = NULL;
    if (argv->len < 1) { rw_set_last_error(EINVAL); return; }
    const rw_str *as = (const rw_str *)argv->data;
    char **args = (char **)calloc((size_t)argv->len + 1, sizeof(char *));
    if (!args) { rw_set_last_error(ENOMEM); return; }
    int err = 0;
    for (int64_t i = 0; i < argv->len; i++) {
        if (memchr(as[i].ptr, '\0', (size_t)as[i].len)) { err = EINVAL; break; }
        if (!(args[i] = cstr(as[i]))) { err = ENOMEM; break; }
    }
    int in[2] = { -1, -1 }, o[2] = { -1, -1 }, e[2] = { -1, -1 };
    pthread_mutex_lock(&g_spawn_mu);
    if (!err && (pipe(in) != 0 || pipe(o) != 0 || pipe(e) != 0)) err = errno;
    pid_t pid = -1;
    if (!err) {
        /* No end may leak into any child: dup2 onto 0/1/2 in the child
         * clears the flag on the copies it needs. */
        int ends[6] = { in[0], in[1], o[0], o[1], e[0], e[1] };
        for (int k = 0; k < 6; k++) fcntl(ends[k], F_SETFD, FD_CLOEXEC);
        posix_spawn_file_actions_t fa;
        posix_spawn_file_actions_init(&fa);
        posix_spawn_file_actions_adddup2(&fa, in[0], 0);
        posix_spawn_file_actions_adddup2(&fa, o[1], 1);
        posix_spawn_file_actions_adddup2(&fa, e[1], 2);
        posix_spawn_file_actions_addclose(&fa, in[0]);
        posix_spawn_file_actions_addclose(&fa, o[1]);
        posix_spawn_file_actions_addclose(&fa, e[1]);
        /* rw ignores SIGPIPE (spec 51) and may have handlers installed
         * (spec 57); the child starts with default dispositions. */
        posix_spawnattr_t attr;
        posix_spawnattr_init(&attr);
        sigset_t def;
        sigemptyset(&def);
        sigaddset(&def, SIGPIPE);
        sigaddset(&def, SIGINT);
        sigaddset(&def, SIGTERM);
        sigaddset(&def, SIGHUP);
        sigaddset(&def, SIGQUIT);
        sigaddset(&def, SIGUSR1);
        sigaddset(&def, SIGUSR2);
        posix_spawnattr_setsigdefault(&attr, &def);
        sigset_t none;
        sigemptyset(&none);
        posix_spawnattr_setsigmask(&attr, &none);
        posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK);
        err = posix_spawnp(&pid, args[0], &fa, &attr, args, environ);
        posix_spawn_file_actions_destroy(&fa);
        posix_spawnattr_destroy(&attr);
    }
    if (err) {
        close_pair(in);
        close_pair(o);
        close_pair(e);
    } else {
        close(in[0]);
        close(o[1]);
        close(e[1]);
    }
    pthread_mutex_unlock(&g_spawn_mu);
    for (int64_t i = 0; i < argv->len; i++) free(args[i]);
    free(args);
    if (err) {
        rw_set_last_error(err);
        return;
    }
    /* The numbers may belong to handles closed earlier: drop their closed
     * mark and timeouts (spec 57). */
    rw_net_reset_fd(in[1]);
    rw_net_reset_fd(o[0]);
    rw_net_reset_fd(e[0]);
    /* Nonblocking parent ends go through the netpoller (spec 61, io.c). */
    rw_set_nonblocking(in[1]);
    rw_set_nonblocking(o[0]);
    rw_set_nonblocking(e[0]);
    int64_t *v = (int64_t *)malloc(4 * sizeof(int64_t));
    if (!v) { rw_set_last_error(ENOMEM); return; }
    v[0] = pid;
    v[1] = in[1];
    v[2] = o[0];
    v[3] = e[0];
    out->len = out->cap = 4;
    out->data = v;
    rw_set_last_error(0);
}

typedef struct {
    pid_t pid;
    int status;
    int rc;
    int err;
} wait_req;

static void do_wait(void *arg) {
    wait_req *r = (wait_req *)arg;
    do {
        r->rc = (int)waitpid(r->pid, &r->status, 0);
    } while (r->rc < 0 && errno == EINTR);
    r->err = r->rc < 0 ? errno : 0;
}

/* Exit status; 128 + signal number if the child was killed; -1 on error. */
int64_t rw_proc_wait(int64_t pid) {
    wait_req r = { (pid_t)pid, 0, 0, 0 };
    rw_aio_call(do_wait, &r);
    rw_set_last_error(r.err);
    if (r.rc < 0) return -1;
    if (WIFEXITED(r.status)) return WEXITSTATUS(r.status);
    if (WIFSIGNALED(r.status)) return 128 + WTERMSIG(r.status);
    return -1;
}

int64_t rw_proc_kill(int64_t pid, rw_str sig) {
    static const struct { int sig; const char *name; } names[] = {
        { SIGTERM, "SIGTERM" }, { SIGKILL, "SIGKILL" }, { SIGINT, "SIGINT" },
        { SIGHUP, "SIGHUP" }, { SIGQUIT, "SIGQUIT" }, { SIGUSR1, "SIGUSR1" },
        { SIGUSR2, "SIGUSR2" }, { SIGSTOP, "SIGSTOP" }, { SIGCONT, "SIGCONT" },
    };
    for (size_t i = 0; i < sizeof names / sizeof names[0]; i++) {
        size_t n = strlen(names[i].name);
        if (sig.len == (int64_t)n && memcmp(sig.ptr, names[i].name, n) == 0) {
            int rc = kill((pid_t)pid, names[i].sig);
            rw_set_last_error(rc == 0 ? 0 : errno);
            return rc == 0 ? 0 : -1;
        }
    }
    rw_set_last_error(EINVAL);
    return -1;
}
