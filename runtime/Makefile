CC      ?= cc
AR      ?= ar
CFLAGS  ?= -O2 -Wall -Wextra -fPIC -std=c11 -D_GNU_SOURCE
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

OBJS := runtime.o fiber/fiber.o fiber/sched.o fiber/runq.o $(FIBER_ASM)

.PHONY: all clean

all: $(LIB)

$(LIB): $(OBJS)
	$(AR) rcs $@ $^

runtime.o: runtime.c runtime.h fiber/sched.h
	$(CC) $(CFLAGS) -c $< -o $@

fiber/fiber.o: fiber/fiber.c fiber/fiber.h
	$(CC) $(CFLAGS) -c $< -o $@

fiber/sched.o: fiber/sched.c fiber/sched.h fiber/fiber.h fiber/runq.h runtime.h
	$(CC) $(CFLAGS) -c $< -o $@

fiber/runq.o: fiber/runq.c fiber/runq.h
	$(CC) $(CFLAGS) -c $< -o $@

fiber/fiber_arm64.o: fiber/fiber_arm64.S
	$(CC) $(ASFLAGS) -c $< -o $@

fiber/fiber_x86_64.o: fiber/fiber_x86_64.S
	$(CC) $(ASFLAGS) -c $< -o $@

clean:
	rm -f $(OBJS) $(LIB)
