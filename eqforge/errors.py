"""EQForge error taxonomy.

Every user-facing failure carries an EQForgeError so the CLI/daemon can print
actionable messages and exit codes stay meaningful.
"""
from __future__ import annotations


class EQForgeError(Exception):
    """Base class. `hint` optionally carries remediation advice."""

    exit_code = 1

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class ProfileError(EQForgeError):
    """Invalid, missing or unresolvable profile."""
    exit_code = 2


class ProfileValidationError(ProfileError):
    pass


class ProfileMigrationError(ProfileError):
    pass


class AudioIOError(EQForgeError):
    """File could not be read/written or format unsupported."""
    exit_code = 3


class NativeError(EQForgeError):
    """libeqforge rejected a configuration or failed."""
    exit_code = 4


class ImportError_(EQForgeError):
    """External EQ data could not be imported."""
    exit_code = 5


class SystemError(EQForgeError):
    """PipeWire/system integration problem."""
    exit_code = 6


class DaemonError(EQForgeError):
    """Daemon not running / RPC failure."""
    exit_code = 7
