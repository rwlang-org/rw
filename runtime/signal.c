/*
 * Signals as values (spec 57). signal_wait(names) installs a handler for
 * each named signal (from then on the signal no longer has its default
 * action) and blocks until one arrives.
 *
 * The handler only writes the signal number to a self-pipe. A dedicated
 * thread reads the pipe, counts the signal as pending, and wakes every
 * waiter; each waiter re-checks the pending counts for its own set and
 * consumes one occurrence, or waits again. Occurrences that arrive while
 * nobody waits stay pending, so none is lost between two waits.
 */

#include "runtime.h"
#include "fiber/sched.h"

#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

static const struct { int sig; const char *name; } k_signals[] = {
    { SIGINT, "SIGINT" }, { SIGTERM, "SIGTERM" }, { SIGHUP, "SIGHUP" },
    { SIGQUIT, "SIGQUIT" }, { SIGUSR1, "SIGUSR1" }, { SIGUSR2, "SIGUSR2" },
};
#define NSIGNALS (sizeof k_signals / sizeof k_signals[0])

typedef struct sig_waiter {
    rw_fiber_handle   *g;
    struct sig_waiter *next;
} sig_waiter;

static pthread_once_t  g_once = PTHREAD_ONCE_INIT;
static int             g_pipe[2] = { -1, -1 };
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_cv = PTHREAD_COND_INITIALIZER;   /* off-fiber waiters */
static int64_t         g_pending[NSIGNALS];
static int             g_installed[NSIGNALS];
static sig_waiter     *g_waiters = NULL;

static void on_signal(int sig) {
    int saved = errno;
    unsigned char b = (unsigned char)sig;
    ssize_t r = write(g_pipe[1], &b, 1);
    (void)r;
    errno = saved;
}

static void *signal_thread(void *arg) {
    (void)arg;
    for (;;) {
        unsigned char b;
        ssize_t n = read(g_pipe[0], &b, 1);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return NULL;
        pthread_mutex_lock(&g_mu);
        for (size_t i = 0; i < NSIGNALS; i++) {
            if (k_signals[i].sig == b) g_pending[i]++;
        }
        sig_waiter *w = g_waiters;
        g_waiters = NULL;
        pthread_cond_broadcast(&g_cv);
        pthread_mutex_unlock(&g_mu);
        while (w) {
            sig_waiter *next = w->next;   /* w lives on its fiber's stack */
            rw_sched_enqueue_ready(w->g);
            w = next;
        }
    }
}

static void start(void) {
    if (pipe(g_pipe) != 0) return;
    pthread_t t;
    pthread_create(&t, NULL, signal_thread, NULL);
    pthread_detach(t);
}

/* Commit for rw_sched_park_commit (spec 24): publish the waiter and drop the
 * lock only after the fiber's context is saved. */
static void commit_wait(rw_fiber_handle *self, void *arg) {
    sig_waiter *w = (sig_waiter *)arg;
    w->g = self;
    w->next = g_waiters;
    g_waiters = w;
    pthread_mutex_unlock(&g_mu);
}

rw_str rw_signal_wait(const rw_list *names) {
    pthread_once(&g_once, start);
    if (g_pipe[0] < 0) return rw_str_from_cstr("", 0);
    const rw_str *ns = (const rw_str *)names->data;
    int want[NSIGNALS] = { 0 };
    int any = 0;
    for (int64_t k = 0; k < names->len; k++) {
        for (size_t i = 0; i < NSIGNALS; i++) {
            size_t len = strlen(k_signals[i].name);
            if (ns[k].len == (int64_t)len && memcmp(ns[k].ptr, k_signals[i].name, len) == 0) {
                want[i] = 1;
                any = 1;
            }
        }
    }
    if (!any) return rw_str_from_cstr("", 0);

    pthread_mutex_lock(&g_mu);
    for (size_t i = 0; i < NSIGNALS; i++) {
        if (want[i] && !g_installed[i]) {
            struct sigaction sa;
            memset(&sa, 0, sizeof sa);
            sa.sa_handler = on_signal;
            sa.sa_flags = SA_RESTART;
            sigemptyset(&sa.sa_mask);
            sigaction(k_signals[i].sig, &sa, NULL);
            g_installed[i] = 1;
        }
    }
    for (;;) {
        for (size_t i = 0; i < NSIGNALS; i++) {
            if (want[i] && g_pending[i] > 0) {
                g_pending[i]--;
                pthread_mutex_unlock(&g_mu);
                return rw_str_from_cstr(k_signals[i].name, (int64_t)strlen(k_signals[i].name));
            }
        }
        if (rw_sched_current_fiber()) {
            sig_waiter w = { NULL, NULL };
            rw_sched_park_commit(commit_wait, &w);   /* unlocks g_mu */
            pthread_mutex_lock(&g_mu);
        } else {
            pthread_cond_wait(&g_cv, &g_mu);
        }
    }
}
