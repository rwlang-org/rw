#!/bin/sh
# Print the install prefix of the Boehm GC (bdw-gc): the directory whose
# include/gc/gc.h exists. RW_GC_PREFIX overrides the search (spec 67).
if [ -n "$RW_GC_PREFIX" ]; then echo "$RW_GC_PREFIX"; exit 0; fi
for p in \
    "$(pkg-config --variable=prefix bdw-gc 2>/dev/null)" \
    "$(brew --prefix bdw-gc 2>/dev/null)" \
    /opt/homebrew/opt/bdw-gc /usr/local/opt/bdw-gc /usr/local /usr; do
    if [ -n "$p" ] && [ -f "$p/include/gc/gc.h" ]; then echo "$p"; exit 0; fi
done
echo "rw: Boehm GC not found; install bdw-gc (brew) or libgc-dev (apt)" >&2
exit 1
