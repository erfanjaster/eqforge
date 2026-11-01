# EQForge PipeWire SPA filter module
#
# Builds libeqforge-filter.so: an SPA node factory ("eqforge.filter") that
# processes audio through libeqforge. Designed to be hosted by PipeWire via
# libpipewire-module-adapter (see packaging/pipewire/99-eqforge.conf).

CC ?= gcc
PKG_CONFIG ?= pkg-config

CORE_DIR := ../core
CORE_INC := $(CORE_DIR)/include
CORE_LIB := $(CORE_DIR)/build/libeqforge.a

PKGS := libspa-0.2
# SPA_CFLAGS lets you build against headers that pkg-config does not know about
# (unpacked PipeWire source tree, cross toolchain, vendor include dir):
#     make -C pipewire SPA_CFLAGS="-isystem /opt/pipewire/spa/include"
SPA_CFLAGS ?=
ifeq ($(strip $(SPA_CFLAGS)),)
PW_CFLAGS := $(shell $(PKG_CONFIG) --cflags $(PKGS) | sed "s/-I/-isystem /g")
else
PW_CFLAGS := $(SPA_CFLAGS)
endif
CFLAGS ?= -O2
WARNINGS := -Wall -Wextra -Wno-unused-parameter

OUT := build/libeqforge-filter.so

# SPA plugin dir: ask libspa where its libdir is (Debian/Ubuntu multiarch,
# Arch/Fedora plain /usr/lib), fall back to /usr/lib.
SPA_LIBDIR ?= $(shell $(PKG_CONFIG) --variable=libdir libspa-0.2 2>/dev/null)
ifeq ($(SPA_LIBDIR),)
SPA_LIBDIR := /usr/lib
endif
SPA_DIR := $(SPA_LIBDIR)/spa-0.2/eqforge

.PHONY: all clean install

all: $(OUT)

$(CORE_LIB):
	$(MAKE) -C $(CORE_DIR)

$(OUT): module-eqforge.c $(CORE_LIB)
	mkdir -p build
	$(CC) $(CFLAGS) $(WARNINGS) -fPIC -shared \
	  -I$(CORE_INC) $(PW_CFLAGS) \
	  module-eqforge.c $(CORE_LIB) -lm -o $@

install: $(OUT)
	install -d $(DESTDIR)$(SPA_DIR)
	install -m 755 $(OUT) $(DESTDIR)$(SPA_DIR)/

clean:
	rm -rf build
