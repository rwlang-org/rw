#ifndef RW_IO_H
#define RW_IO_H

#include <stdint.h>

#include "runtime.h"

#ifdef __cplusplus
extern "C" {
#endif

/* See runtime.h for the prototypes; this header exists so io.c can
 * include the netpoller/scheduler internals without polluting
 * runtime.h. */

#ifdef __cplusplus
}
#endif

#endif /* RW_IO_H */
