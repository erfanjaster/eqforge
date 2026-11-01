"""Desktop notifications (notify-send / org.freedesktop.Notifications)."""
from __future__ import annotations

import shutil
import subprocess

from eqforge.log import get_logger

log = get_logger("system.notify")

_URGENCIES = {"info": "normal", "warn": "critical", "error": "critical"}


def notify(title: str, body: str = "", urgency: str = "info",
           timeout_ms: int = 5000) -> bool:
    """Send a desktop notification. Returns True when delivered."""
    u = _URGENCIES.get(urgency, "normal")
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", "-u", u, "-t", str(timeout_ms),
                 "-a", "EQForge", "-i", "audio-equalizer", title, body],
                check=False, timeout=5, capture_output=True)
            return True
        except subprocess.SubprocessError:
            pass
    # bus fallback via gdbus (session bus)
    if shutil.which("gdbus"):
        try:
            subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.freedesktop.Notifications",
                 "--object-path", "/org/freedesktop/Notifications",
                 "--method", "org.freedesktop.Notifications.Notify",
                 "eqforge", "0", "audio-equalizer", title, body,
                 "[]", "{}", str(timeout_ms)],
                check=False, timeout=5, capture_output=True)
            return True
        except subprocess.SubprocessError:
            pass
    log.debug("notification not delivered (no notify-send/gdbus): %s", title)
    return False
