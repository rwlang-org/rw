/*
 * TCP loopback smoke test. Spawns a server fiber that listens on
 * 127.0.0.1:<random>, accepts one connection, echoes a single
 * message, then closes. The client side runs in a separate pthread
 * (NOT a fiber) so we can do blocking connect/send/recv from a
 * "user-perspective" caller.
 */

#include "../net/netpoller.h"
#include "sched.h"
#include "../runtime.h"

#include <arpa/inet.h>
#include <assert.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static int g_port;

static int64_t server_fiber(void *arg) {
    int listen_fd = (int)(intptr_t)arg;
    int64_t client = rw_tcp_accept((int64_t)listen_fd);
    if (client < 0) return -1;
    rw_str msg = { .len = 0, .ptr = NULL };
    rw_tcp_read(&msg, client, 64);
    if (msg.len <= 0) { rw_tcp_close(client); return -2; }
    rw_tcp_write(client, msg);
    rw_tcp_close(client);
    return 0;
}

static void *client_thread(void *arg) {
    (void)arg;
    /* Give the server time to start listening. */
    struct timespec ts = { .tv_sec = 0, .tv_nsec = 100 * 1000 * 1000 };
    nanosleep(&ts, NULL);

    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return (void *)1;
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((uint16_t)g_port);
    if (connect(s, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(s); return (void *)2;
    }
    const char *m = "ping\n";
    send(s, m, strlen(m), 0);
    char buf[64];
    ssize_t n = recv(s, buf, sizeof(buf), 0);
    if (n != 5 || memcmp(buf, "ping\n", 5) != 0) {
        close(s); return (void *)3;
    }
    close(s);
    return NULL;
}

int main(void) {
    rw_init();

    int64_t lfd = rw_tcp_listen(0);   /* let kernel pick port */
    if (lfd < 0) { fprintf(stderr, "listen failed\n"); return 1; }
    /* Read the assigned port via getsockname. */
    struct sockaddr_in sa; socklen_t sl = sizeof(sa);
    getsockname((int)lfd, (struct sockaddr *)&sa, &sl);
    g_port = ntohs(sa.sin_port);

    pthread_t client;
    pthread_create(&client, NULL, client_thread, NULL);

    rw_future_t *sfut = rw_spawn_i64(server_fiber, (void *)(intptr_t)lfd);
    int64_t srv_rc = rw_await_i64(sfut);
    assert(srv_rc == 0);

    void *cli_rc;
    pthread_join(client, &cli_rc);
    assert(cli_rc == NULL);

    rw_tcp_close(lfd);
    rw_shutdown();
    printf("tcp loopback test ok\n");
    return 0;
}
