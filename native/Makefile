CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -std=c11
LDLIBS ?= -lcrypto

all: ubackup-core

ubackup-core: ubackup-core.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f ubackup-core

test: ubackup-core
	python3 ../tests/test_native_core.py

.PHONY: all clean test
