#ifndef RW_TIMER_H
#define RW_TIMER_H

#include <stdint.h>

/* Timers and clocks (spec 39). */
void    rw_timer_init(void);
void    rw_timer_shutdown(void);

/* Park the calling fiber for at least `ms` milliseconds (a plain
 * nanosleep on a thread that is not a fiber). */
void    rw_sleep_ms(int64_t ms);

int64_t rw_time_ms(void);   /* wall clock: Unix time in milliseconds */
int64_t rw_mono_ns(void);   /* monotonic clock in nanoseconds */

#endif
