"""Event-driven PipeWire graph watching.

Why this exists
---------------
The daemon used to learn about the audio graph only on its polling tick (2 s).
For device auto-switching that is fine - nobody notices a profile change a
second late. For *routing* it is not: PipeWire destroys an application's
stream node the moment it pauses, and creates a brand-new node when playback
starts again, which WirePlumber links straight to the default sink. The
daemon then had to notice, and move that stream through the processor - up to
two seconds later. The audible result is exactly the reported symptom:

    press play -> the first ~1 s is unprocessed, then the EQ "snaps in".

`pw-dump --monitor` (`-m`) streams the same registry events WirePlumber acts
on, as JSON, the instant they happen. Reacting to those turns a 2000 ms
reaction into a ~30-60 ms one (debounce + one `pw-dump` snapshot + a couple
of `pw-link` calls).

Design notes
------------
* One background thread per watcher, one child process (`pw-dump -m`).
* Events are *coalesced*: a single stream start generates a burst of node,
  port and link events. Reacting to each one would run several rewiring
  passes for one playback start; instead the first event starts a short
  debounce window and the callback runs once per burst.
* The callback runs on the watcher thread and must be cheap and must never
  raise. Exceptions are logged and swallowed: a watcher that dies because a
  single pass failed is worse than a slow one.
* If `pw-dump -m` is unavailable (PipeWire < 0.3.56, or a tool that refuses
  the flag) the watcher reports `event_capable == False` after giving up, and
  the daemon keeps its polling tick as the only mechanism. Nothing breaks.
* `EQFORGE_MONITOR_CMD` overrides the monitor command; the test suite uses
  it to run a fake monitor instead of a real PipeWire session.
"""
from __future__ import annotations

import os
import select
import shlex
import shutil
import subprocess
import threading
import time

from eqforge.log import get_logger

log = get_logger("system.watch")

#: Object types worth reacting to. Nodes and links are what routing cares
#: about; clients and metadata do not move audio, and reacting to them would
#: just cost snapshots. The markers are the `type` strings pw-dump emits.
RELEVANT_MARKERS = (
    b"PipeWire:Interface:Node",
    b"PipeWire:Interface:Link",
)

#: How long to wait after the first event of a burst before acting, so that a
#: stream's node + ports + links are all in the graph when we look.
DEFAULT_DEBOUNCE = 0.025

#: Give the monitor this long to prove it works before declaring it dead.
STARTUP_GRACE = 1.5


def monitor_command() -> list[str]:
    """The command that streams graph events (overridable for tests)."""
    override = os.environ.get("EQFORGE_MONITOR_CMD")
    if override:
        return shlex.split(override)
    return ["pw-dump", "-m"]


def available() -> bool:
    """True when a monitor could plausibly run in this session."""
    cmd = monitor_command()
    return bool(cmd) and shutil.which(cmd[0]) is not None


class GraphWatcher:
    """Push-style PipeWire graph notifications.

    >>> w = GraphWatcher(lambda reason: print(reason))
    >>> w.start()
    >>> ...
    >>> w.stop()

    `on_change` receives a short reason string ("graph"). It is called from
    the watcher thread, at most once per debounce window.
    """

    def __init__(self, on_change, *, debounce: float = DEFAULT_DEBOUNCE,
                 cmd: list[str] | None = None, max_restarts: int = 5,
                 restart_backoff: float = 1.0):
        self._on_change = on_change
        self._debounce = max(0.001, float(debounce))
        self._cmd = list(cmd) if cmd else monitor_command()
        self._max_restarts = int(max_restarts)
        self._restart_backoff = float(restart_backoff)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

        self.events = 0            # bursts delivered
        self.raw_events = 0        # individual event chunks seen
        self.restarts = 0
        self.event_capable = available()
        self.last_error: str | None = None

    # ---------------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start watching. False when no monitor tool is installed."""
        if self.running:
            return True
        if not self._cmd or shutil.which(self._cmd[0]) is None:
            self.event_capable = False
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="eqforge-watch",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # --------------------------------------------------------------- worker
    def _run(self) -> None:
        first = True
        while not self._stop.is_set():
            if not self._spawn(first):
                break
            first = False
            if self._stop.is_set():
                break
            if self.restarts >= self._max_restarts:
                log.warning("graph watcher: giving up after %d restarts",
                            self.restarts)
                self.event_capable = False
                break
            self.restarts += 1
            self._stop.wait(self._restart_backoff)
        self._close_proc()

    def _spawn(self, first: bool) -> bool:
        started = time.monotonic()
        try:
            proc = subprocess.Popen(self._cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as e:                      # pragma: no cover - env
            self.last_error = str(e)
            log.warning("graph watcher: cannot run %s (%s)", self._cmd[0], e)
            self.event_capable = False
            return False
        with self._lock:
            self._proc = proc
        seen_output = False
        pending = False            # an event arrived, not yet delivered
        deadline = 0.0

        while not self._stop.is_set():
            fd = proc.stdout.fileno() if proc.stdout else -1
            timeout = 0.2
            if pending:
                timeout = max(0.0, min(timeout, deadline - time.monotonic()))
            try:
                ready, _, _ = select.select([fd], [], [], timeout) if fd >= 0 \
                    else ([], [], [])
            except (OSError, ValueError):         # pragma: no cover - env
                break
            if not ready:
                if proc.poll() is not None:
                    break
                if pending and time.monotonic() >= deadline:
                    self._deliver()
                    pending = False
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:                       # pragma: no cover - env
                break
            if not chunk:
                break
            seen_output = True
            if any(m in chunk for m in RELEVANT_MARKERS):
                self.raw_events += 1
                if not pending:
                    pending = True
                    deadline = time.monotonic() + self._debounce
        rc = proc.poll()
        self._close_proc()
        if not seen_output and (time.monotonic() - started) < STARTUP_GRACE \
                and rc not in (None, 0, -15):
            # The tool exists but refused to monitor (old PipeWire, or a
            # different `pw-dump`): stop trying, keep polling.
            self.last_error = f"{self._cmd[0]} -m exited {rc}"
            log.info("graph watcher: %s; falling back to polling",
                     self.last_error)
            self.event_capable = False
            return False
        return not self._stop.is_set()

    def _deliver(self) -> None:
        self.events += 1
        try:
            self._on_change("graph")
        except Exception:                          # noqa: BLE001 - never die
            log.debug("graph watcher callback failed", exc_info=True)

    def _close_proc(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()
        except OSError:                             # pragma: no cover
            pass
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:                     # pragma: no cover
                    pass

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict:
        return {"running": self.running, "event_capable": self.event_capable,
                "events": self.events, "raw_events": self.raw_events,
                "restarts": self.restarts, "command": self._cmd,
                "error": self.last_error}
