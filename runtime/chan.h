#ifndef RW_CHAN_H
#define RW_CHAN_H

#include <stdint.h>

/* Channel[T] (spec 44): a FIFO queue between fibers (and the main thread)
 * with capacity 0 (rendezvous) or N. Values are elem_size bytes, passed by
 * pointer. */
typedef struct rw_chan rw_chan;

rw_chan *rw_chan_new  (int64_t capacity, int64_t elem_size);
void     rw_chan_send (rw_chan *c, const void *value);   /* aborts if closed */
int8_t   rw_chan_recv (rw_chan *c, void *value_out);     /* 0 once closed and empty */
void     rw_chan_close(rw_chan *c);
int64_t  rw_chan_len  (rw_chan *c);                      /* buffered values */

#endif
