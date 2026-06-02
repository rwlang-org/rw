#ifndef RW_AIO_H
#define RW_AIO_H

#include <stdint.h>

#include "runtime.h"   /* rw_str */

#ifdef __cplusplus
extern "C" {
#endif

/* Async file I/O backend. Lifecycle driven by rw_init / rw_shutdown. */
void rw_aio_init(void);
void rw_aio_shutdown(void);

/* Offload a blocking read(2)/write(2) on `fd` to a worker thread, parking
 * the calling fiber until it completes. Off-fiber (main thread) callers
 * fall back to synchronous I/O. Semantics mirror rw_read/rw_write in
 * io.c: read fills *out (len=0 on EOF/error, ptr owns a malloc'd buffer
 * when len>0); write returns bytes written, negative on error. */
void    rw_aio_read (rw_str *out, int64_t fd, int64_t max);
int64_t rw_aio_write(int64_t fd, rw_str b);

#ifdef __cplusplus
}
#endif

#endif /* RW_AIO_H */
