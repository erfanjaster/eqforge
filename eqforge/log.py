"""Logging setup: console (concise) + rotating file (verbose)."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from eqforge import paths

_CONFIGURED = False


def setup_logging(verbose: bool = False, quiet: bool = False,
                  log_file: bool = True) -> logging.Logger:
    global _CONFIGURED
    root = logging.getLogger("eqforge")
    if _CONFIGURED:
        return root
    root.setLevel(logging.DEBUG)
    root.propagate = False

    level = logging.DEBUG if verbose else (logging.ERROR if quiet else logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        "%(levelname).1s %(message)s" if not verbose
        else "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(console)

    if log_file:
        try:
            paths.log_dir().mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                paths.log_dir() / "eqforge.log", maxBytes=2_000_000,
                backupCount=3, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
            root.addHandler(fh)
        except OSError:
            pass  # logging must never break the app

    if os.environ.get("EQFORGE_DEBUG"):
        console.setLevel(logging.DEBUG)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"eqforge.{name}")
