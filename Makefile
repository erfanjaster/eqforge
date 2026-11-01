# EQForge - top-level build orchestration.
#
#   make            build native core + pipewire module + rt host
#   make test       run C tests, module smoke test, python tests
#   make install    install native parts + python package (user prefix)
#   make gui        build nothing, just start daemon+gui (convenience)

PYTHON ?= python3
PREFIX ?= $(HOME)/.local

.PHONY: all core pipewire rthost test test-core test-module test-python \
        install install-core clean gui lint

all: core pipewire rthost

core:
	$(MAKE) -C core

pipewire: core
	$(MAKE) -C pipewire

rthost: core
	$(MAKE) -C rthost

test: test-core test-module test-python

test-core:
	$(MAKE) -C core test

test-module: pipewire
	cd pipewire && cc -O1 -g -Wall -Wextra -isystem /usr/include/spa-0.2 \
	  test_module.c -o build/test_module -ldl -lm && ./build/test_module

test-python:
	$(PYTHON) -m pytest tests/ -q

install: all
	$(MAKE) -C core install PREFIX=$(PREFIX)
	$(MAKE) -C pipewire install
	$(MAKE) -C rthost install
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
