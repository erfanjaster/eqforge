# eqforge-rt: native JACK/PipeWire real-time host for libeqforge.

CC ?= gcc
PKG_CONFIG ?= pkg-config

CORE_DIR := ../core
CORE_INC := $(CORE_DIR)/include
CORE_LIB := $(CORE_DIR)/build/libeqforge.a

JACK_CFLAGS := $(shell $(PKG_CONFIG) --cflags jack 2>/dev/null)
JACK_LIBS := $(shell $(PKG_CONFIG) --libs jack 2>/dev/null || echo "-ljack")

CFLAGS ?= -O2
WARNINGS := -Wall -Wextra -Wno-unused-parameter

OUT := build/eqforge-rt

.PHONY: all clean install

all: $(OUT)

$(CORE_LIB):
	$(MAKE) -C $(CORE_DIR)

$(OUT): eqforge-rt.c $(CORE_LIB)
	mkdir -p build
	$(CC) $(CFLAGS) $(WARNINGS) -I$(CORE_INC) $(JACK_CFLAGS) \
	  eqforge-rt.c $(CORE_LIB) $(JACK_LIBS) -lm -lpthread -o $@

install: $(OUT)
	install -d $(DESTDIR)/usr/libexec/eqforge
	install -m 755 $(OUT) $(DESTDIR)/usr/libexec/eqforge/

clean:
	rm -rf build
