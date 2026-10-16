CC      ?= cc
AR      ?= ar
CFLAGS  ?= -O2 -Wall -Wextra -fPIC -std=c11 -D_GNU_SOURCE

# Boehm GC (spec 67): headers from the bdw-gc install (see scripts/gc-prefix.sh).
GC_PREFIX ?= $(shell sh ../scripts/gc-prefix.sh)
CFLAGS  += -I$(GC_PREFIX)/include
ASFLAGS ?= -O2 -Wall -Wextra -fPIC

LIB     := librw.a

# Detect architecture for the assembly switch routine.
UNAME_M := $(shell uname -m)

ifeq ($(UNAME_M),arm64)
  FIBER_ASM := fiber/fiber_arm64.o
else ifeq ($(UNAME_M),aarch64)
  FIBER_ASM := fiber/fiber_arm64.o
else ifeq ($(UNAME_M),x86_64)
  FIBER_ASM := fiber/fiber_x86_64.o
else
  $(error unsupported architecture: $(UNAME_M))
endif

# Detect OS for the netpoller backend.
UNAME_S := $(shell uname -s)

ifeq ($(UNAME_S),Darwin)
  NET_PLATFORM_O := net/netpoller_kqueue.o
else ifeq ($(UNAME_S),Linux)
  NET_PLATFORM_O := net/netpoller_epoll.o
else
  $(error unsupported OS: $(UNAME_S))
endif

OBJS := runtime.o io.o aio.o timer.o chan.o signal.o fs.o process.o gc.o \
        fiber/fiber.o fiber/sched.o fiber/runq.o fiber/park.o \
        net/netpoller.o net/tcp.o net/addr.o $(NET_PLATFORM_O) \
        $(FIBER_ASM)

.PHONY: all clean

all: $(LIB)

$(LIB): $(OBJS)
	$(AR) rcs $@ $^

runtime.o: runtime.c runtime.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@

io.o: io.c io.h runtime.h net/netpoller.h fiber/sched.h
	$(CC) $(CFLAGS) -c io.c -o io.o

chan.o: chan.c chan.h fiber/sched.h
	$(CC) $(CFLAGS) -c chan.c -o chan.o

timer.o: timer.c timer.h fiber/sched.h
	$(CC) $(CFLAGS) -c timer.c -o timer.o

aio.o: aio.c aio.h runtime.h fiber/sched.h
	$(CC) $(CFLAGS) -c aio.c -o aio.o

fiber/fiber.o: fiber/fiber.c fiber/fiber.h
	$(CC) $(CFLAGS) -c $< -o $@

fiber/sched.o: fiber/sched.c fiber/sched.h fiber/fiber.h fiber/runq.h fiber/park.h runtime.h
	$(CC) $(CFLAGS) -c $< -o $@

fiber/runq.o: fiber/runq.c fiber/runq.h
	$(CC) $(CFLAGS) -c $< -o $@

fiber/park.o: fiber/park.c fiber/park.h
	$(CC) $(CFLAGS) -c $< -o $@

net/netpoller.o: net/netpoller.c net/netpoller.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@

net/netpoller_kqueue.o: net/netpoller_kqueue.c net/netpoller.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@

net/netpoller_epoll.o: net/netpoller_epoll.c net/netpoller.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@

net/tcp.o: net/tcp.c net/tcp.h net/netpoller.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@

fiber/fiber_arm64.o: fiber/fiber_arm64.S
	$(CC) $(ASFLAGS) -c $< -o $@

fiber/fiber_x86_64.o: fiber/fiber_x86_64.S
	$(CC) $(ASFLAGS) -c $< -o $@

clean:
	rm -f $(OBJS) $(LIB)
