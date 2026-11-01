# EQForge native core
#
# Builds libeqforge.so (shared) and libeqforge.a (static), plus the test
# binary. No external dependencies beyond libm.
#
# The code is C11 written in a C++-compatible subset; EQF_BUILD_CXX=1
# compiles the exact same sources with g++ as a portability canary for the
# future native C++ migration.

CC ?= gcc
CXX ?= g++
AR ?= ar

CFLAGS ?= -O3 -fno-math-errno
WARNINGS := -Wall -Wextra -Wpedantic -Wshadow -Wdouble-promotion \
            -Wformat=2 -Wundef -Wstrict-prototypes -Wmissing-prototypes
CSTD := -std=c11
CPPFLAGS := -Iinclude -DEQF_BUILDING=1
LDFLAGS := -lm

SRC_DIR := src
OBJ_DIR := build
SRCS := $(wildcard $(SRC_DIR)/*.c)
OBJS := $(patsubst $(SRC_DIR)/%.c,$(OBJ_DIR)/%.o,$(SRCS))

LIB_SHARED := build/libeqforge.so
LIB_STATIC := build/libeqforge.a

.PHONY: all clean test test-cxx objects install

all: $(LIB_SHARED) $(LIB_STATIC)

$(OBJ_DIR):
	mkdir -p $(OBJ_DIR)

$(OBJ_DIR)/%.o: $(SRC_DIR)/%.c | $(OBJ_DIR)
	$(CC) $(CSTD) $(CFLAGS) $(WARNINGS) $(CPPFLAGS) -fPIC -c $< -o $@

$(LIB_SHARED): $(OBJS)
	$(CC) -shared -o $@ $(OBJS) $(LDFLAGS)

$(LIB_STATIC): $(OBJS)
	$(AR) rcs $@ $(OBJS)

# --- C++ compile canary (same sources, g++, stricter) ---
test-cxx: | $(OBJ_DIR)
	@mkdir -p $(OBJ_DIR)/cxx
	@for f in $(SRCS); do \
	  $(CXX) -std=c++17 -O2 -Wall -Wextra $(filter-out -Wstrict-prototypes -Wmissing-prototypes,$(WARNINGS)) \
	    -Iinclude -fPIC -x c++ -c $$f -o $(OBJ_DIR)/cxx/$$(basename $$f .c).o || exit 1; \
	done
	@echo "C++ compile canary: OK (all core sources compile as C++17)"

# --- tests ---
TEST_SRC := tests/test_core.c
TEST_BIN := build/test_core

$(TEST_BIN): $(TEST_SRC) $(LIB_STATIC)
	$(CC) $(CSTD) -O2 -g -Wall -Wextra -Iinclude $(TEST_SRC) $(LIB_STATIC) -o $@ $(LDFLAGS)

test: $(TEST_BIN)
	./$(TEST_BIN)

clean:
	rm -rf $(OBJ_DIR)

install: all
	install -d $(PREFIX)/lib/eqforge $(PREFIX)/include/eqforge
	install -m 755 $(LIB_SHARED) $(PREFIX)/lib/eqforge/
	install -m 644 $(LIB_STATIC) $(PREFIX)/lib/eqforge/
	install -m 644 include/eqforge/*.h $(PREFIX)/include/eqforge/
