"""EQForge - a profile-driven, smart audio processing system for Linux.

Native core: libeqforge (C11, RT-safe DSP engine).
Control plane: this package (profiles, analysis, smart advisor, daemon, CLI, GUI).
"""
from eqforge.version import __version__, ABI_VERSION

__all__ = ["__version__", "ABI_VERSION"]
