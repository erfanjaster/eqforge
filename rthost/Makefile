# eqforge-rt: native JACK/PipeWire real-time host for libeqforge.

CC ?= gcc
PKG_CONFIG ?= pkg-config

CORE_DIR := ../core
CORE_INC := $(CORE_DIR)/include
CORE_LIB := $(CORE_DIR)/build/libeqforge.a
PREFIX ?= /usr
DESTDIR ?=

JACK_CFLAGS := $(shell $(PKG_CONFIG) --cflags jack 2>/dev/null)
JACK_LIBS := $(shell $(PKG_CONFIG) --libs jack 2>/dev/null || echo "-ljack")

CFLAGS ?= -O2
WARNINGS := -Wall -Wextra -Wno-unused-parameter

OUT := build/eqforge-rt

.PHONY: all clean install test test-asan test-tsan

all: $(OUT)

$(CORE_LIB):
	$(MAKE) -C $(CORE_DIR)

$(OUT): eqforge-rt.c $(CORE_LIB)
	mkdir -p build
	$(CC) $(CFLAGS) $(WARNINGS) -I$(CORE_INC) $(JACK_CFLAGS) \
	  eqforge-rt.c $(CORE_LIB) $(JACK_LIBS) -lm -lpthread -o $@

# --- tests -----------------------------------------------------------------
# JACK needs a running sound server, so the host is exercised against a
# functional fake libjack (tests/fakejack.c): same API, real audio cycles,
# real callbacks. This validates the host's reload / retirement / sample-rate
# logic, which a compile check cannot.

TEST_DIR := build/test
JACK_STUB_INC ?= $(JACK_CFLAGS)

# The test links against the fake server, so it does NOT require a real
# libjack at build time - only the JACK *headers*.
test: $(CORE_LIB)
	mkdir -p $(TEST_DIR)
	$(CC) -std=c11 -O2 -fPIC -shared $(JACK_STUB_INC) \
	  tests/fakejack.c -o $(TEST_DIR)/libjack.so -lpthread -lm
	$(CC) -std=c11 -O2 -I$(CORE_INC) $(JACK_CFLAGS) \
	  eqforge-rt.c $(CORE_LIB) -o $(TEST_DIR)/eqforge-rt \
	  -L$(TEST_DIR) -ljack -lm -lpthread -Wl,-rpath,'$$ORIGIN'
	$(CC) -O1 -g $(JACK_STUB_INC) tests/test_rthost.c -o $(TEST_DIR)/test_rthost \
	  -L$(TEST_DIR) -ljack -lm -lpthread \
	  -Wl,-rpath,'$$ORIGIN'
	cd $(TEST_DIR) && ./test_rthost . ./eqforge-rt

# Sanitized variant: catches use-after-free across the reload swap and leaked
# chains. Rebuilds the host and the core with the sanitizer.
SAN := -fsanitize=address,undefined -fno-omit-frame-pointer

test-asan: $(CORE_LIB)
	mkdir -p $(TEST_DIR)
	$(CC) -std=c11 -O1 -g $(JACK_STUB_INC) $(SAN) \
	  tests/fakejack.c -fPIC -shared -o $(TEST_DIR)/libjack.so -lpthread -lm
	$(CC) -std=c11 -O1 -g $(SAN) -I$(CORE_INC) $(JACK_CFLAGS) \
	  eqforge-rt.c ../core/src/*.c -o $(TEST_DIR)/eqforge-rt \
	  -L$(TEST_DIR) -ljack -lm -lpthread -Wl,-rpath,'$$ORIGIN'
	$(CC) -O1 -g $(JACK_STUB_INC) $(SAN) tests/test_rthost.c \
	  -o $(TEST_DIR)/test_rthost -L$(TEST_DIR) -ljack -lm -lpthread \
	  -Wl,-rpath,'$$ORIGIN'
	cd $(TEST_DIR) && ASAN_OPTIONS=detect_leaks=1 \
	  ./test_rthost . ./eqforge-rt

test-tsan: $(CORE_LIB)
	mkdir -p $(TEST_DIR)
	$(CC) -std=c11 -O1 -g $(JACK_STUB_INC) -fsanitize=thread \
	  tests/fakejack.c -fPIC -shared -o $(TEST_DIR)/libjack.so -lpthread -lm
	$(CC) -std=c11 -O1 -g -fsanitize=thread -I$(CORE_INC) $(JACK_CFLAGS) \
	  eqforge-rt.c ../core/src/*.c -o $(TEST_DIR)/eqforge-rt \
	  -L$(TEST_DIR) -ljack -lm -lpthread -Wl,-rpath,'$$ORIGIN'
	$(CC) -O1 -g $(JACK_STUB_INC) -fsanitize=thread tests/test_rthost.c \
	  -o $(TEST_DIR)/test_rthost -L$(TEST_DIR) -ljack -lm -lpthread \
	  -Wl,-rpath,'$$ORIGIN'
	cd $(TEST_DIR) && ./test_rthost . ./eqforge-rt

install: $(OUT)
	install -d $(DESTDIR)$(PREFIX)/libexec/eqforge
	install -m 755 $(OUT) $(DESTDIR)$(PREFIX)/libexec/eqforge/

clean:
	rm -rf build
