# EQForge - top-level build orchestration.
#
#   make            build every component whose dependencies are installed
#   make core       native DSP core (needs only a C compiler + make)
#   make test       run C tests, module smoke test (if buildable), python tests
#   make install    install native parts + python package (user prefix)
#   make gui        start daemon + GUI (convenience)
#
# Components are capability-gated: the PipeWire SPA filter needs libspa-0.2
# headers and the JACK host needs libjack, and neither is required for EQForge
# to work. A missing optional dependency now produces a clear "skipped" line
# instead of aborting the whole build (previously `make` failed outright on any
# machine without libspa-0.2-dev).

PYTHON ?= python3
PREFIX ?= $(HOME)/.local
PKG_CONFIG ?= pkg-config

# --- dependency probes (empty result == not installed) ---
HAVE_SPA      := $(shell $(PKG_CONFIG) --exists libspa-0.2 && echo 1)
HAVE_JACK     := $(shell ($(PKG_CONFIG) --exists jack || \
                          $(PKG_CONFIG) --exists jack2) && echo 1)

.PHONY: all core pipewire rthost test test-core test-module test-module-asan \
        test-rthost test-python test-e2e \
        install install-core clean gui lint check-deps

all: core
	@if [ -n "$(HAVE_SPA)" ]; then \
	  $(MAKE) pipewire || exit 1; \
	else \
	  echo "  SKIP pipewire  (libspa-0.2 headers not found:" \
	       "install libpipewire-0.3-dev / pipewire-devel)"; \
	fi
	@if [ -n "$(HAVE_JACK)" ]; then \
	  $(MAKE) rthost || exit 1; \
	else \
	  echo "  SKIP rthost    (libjack not found:" \
	       "install libjack-jackd2-dev / pipewire-jack)"; \
	fi
	@echo "  eqforge native build complete"

check-deps:
	@echo "libspa-0.2 headers : $(if $(HAVE_SPA),yes,no)"
	@echo "libjack headers    : $(if $(HAVE_JACK),yes,no)"

core:
	$(MAKE) -C core

pipewire: core
	@if [ -n "$(strip $(SPA_CFLAGS))" ]; then \
	  $(MAKE) -C pipewire SPA_CFLAGS="$(SPA_CFLAGS)"; \
	else \
	  $(MAKE) -C pipewire; \
	fi

rthost: core
	$(MAKE) -C rthost

test: test-core test-module test-rthost test-python

test-core:
	$(MAKE) -C core test

# The SPA smoke test drives the filter node exactly like a PipeWire host does,
# without needing a running daemon. It can only be built where SPA exists.
#
# SPA_CFLAGS overrides header discovery when the headers are available but not
# registered with pkg-config (unpacked source tree, cross build, ...):
#     make test-module SPA_CFLAGS=-isystem/opt/pipewire/spa/include
SPA_CFLAGS ?=
ifeq ($(strip $(SPA_CFLAGS)),)
SPA_CFLAGS := $(shell $(PKG_CONFIG) --cflags libspa-0.2 2>/dev/null)
endif

test-module:
	@if [ -n "$(HAVE_SPA)" ] || [ -n "$(strip $(SPA_CFLAGS))" ]; then \
	  $(MAKE) pipewire || exit 1; \
	  mkdir -p pipewire/build; \
	  ( cd pipewire && cc -O1 -g -Wall -Wextra -Wno-unused-parameter \
	      $(SPA_CFLAGS) test_module.c -o build/test_module -ldl -lm ) || exit 1; \
	  ( cd pipewire && ./build/test_module ); \
	else \
	  echo "  SKIP test-module (libspa-0.2 not installed;"; \
	  echo "                   or pass SPA_CFLAGS=-I/path/to/spa/include)"; \
	fi

# Same smoke test under ASan + UBSan + LeakSanitizer. Catches the failures that
# a passing functional test hides: use-after-free across the hot-reload swap,
# misaligned pod construction, leaked chains.
test-module-asan:
	@SPA_CF="$(strip $(SPA_CFLAGS))"; \
	if [ -z "$$SPA_CF" ]; then \
	  SPA_CF=$$($(PKG_CONFIG) --cflags libspa-0.2 2>/dev/null); \
	fi; \
	if [ -z "$$SPA_CF" ]; then \
	  echo "  SKIP test-module-asan (libspa-0.2 not installed;"; \
	  echo "                       or pass SPA_CFLAGS=-isystem/path/to/spa/include)"; \
	  exit 0; \
	fi; \
	mkdir -p pipewire/build; \
	set -e; \
	cd pipewire; \
	cc -O1 -g -std=c11 -Wall -Wextra -Wno-unused-parameter $$SPA_CF \
	   -fsanitize=address,undefined -fno-omit-frame-pointer -fPIC -shared \
	   -I../core/include module-eqforge.c ../core/src/*.c -lm -lpthread \
	   -o build/libeqforge-filter-asan.so; \
	cc -O1 -g $$SPA_CF -fsanitize=address,undefined \
	   test_module.c -o build/test_module_asan -ldl -lm; \
	ASAN_OPTIONS=detect_leaks=1 ./build/test_module_asan \
	   build/libeqforge-filter-asan.so /tmp/eqforge-asan-dsp.json

# The RT host is tested against a functional fake libjack, so this needs only
# the JACK headers. Point JACK_CFLAGS at them when they are not pkg-config'd:
#     make test-rthost JACK_CFLAGS=-I/opt/jack/include
JACK_CFLAGS ?= $(shell $(PKG_CONFIG) --cflags jack 2>/dev/null)

test-rthost: test-core
	@if [ -n "$(strip $(JACK_CFLAGS))" ]; then \
	  $(MAKE) -C rthost test JACK_CFLAGS="$(JACK_CFLAGS)" \
	    JACK_STUB_INC="$(JACK_CFLAGS)" || exit 1; \
	else \
	  echo "  SKIP test-rthost (jack headers not found;)"; \
	  echo "                   pass JACK_CFLAGS=-I/path/to/jack/include)"; \
	fi

# End-to-end workflows A-E. Everything is verified by measuring the produced
# audio, not by trusting exit codes, and it runs in a throwaway HOME.
test-e2e:
	@PY=$(PYTHON) tests/e2e/run_workflows.sh

test-python:
	$(PYTHON) -m pytest tests/ -q

install: all
	$(MAKE) -C core install PREFIX=$(PREFIX)
	@if [ -n "$(HAVE_SPA)" ]; then $(MAKE) -C pipewire install; \
	 else echo "  SKIP pipewire install (not built)"; fi
	@if [ -n "$(HAVE_JACK)" ]; then $(MAKE) -C rthost install; \
	 else echo "  SKIP rthost install (not built)"; fi
	$(PYTHON) -m pip install --user -e .

gui:
	$(PYTHON) -m eqforge gui

lint:
	$(PYTHON) -m pyflakes eqforge/ tests/ || true

clean:
	$(MAKE) -C core clean
	$(MAKE) -C pipewire clean
	$(MAKE) -C rthost clean
	rm -rf .pytest_cache build *.egg-info
