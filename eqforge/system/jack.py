"""JACK transport discovery: which JACK implementation is installed, and
whether a JACK-compatible server is actually reachable.

`eqforge-rt` is a JACK client, so starting it needs two things that are easy
to conflate:

1. a *client library* (libjack) that speaks to a server, and
2. a *server* for that library to connect to.

Under PipeWire both come from `pipewire-jack`. A standalone JACK server
(`jack2` / `jackd`) installed next to PipeWire competes for the same
libjack, and the usual symptom is `jack_client_open()` failing with
``JackFailure|JackServerFailed`` - reported by the host as
"cannot open JACK client (17)". That failure looks like an EQForge bug but
is a system packaging conflict.

Where the sockets live (measured, not assumed - jack2 1.9.21 on Debian 12):

* PipeWire:  ``$XDG_RUNTIME_DIR/pipewire-0`` (or ``$PIPEWIRE_REMOTE``);
  pipewire-jack clients connect through it.
* jackd:     *not* in the runtime dir. With ``XDG_RUNTIME_DIR`` set or unset
  the request channel is ``/dev/shm/jack_<server>_<uid>_<n>`` plus the
  registry ``/dev/shm/jack-shm-registry``; older builds used
  ``/tmp/jack-<uid>/jack-0`` (libjack formats that dir as ``%s/jack-%d`` -
  a numeric uid, never a user name).

Detection is dependency-free (PATH lookups, socket/artifact probes - no
libjack, no PipeWire headers) and every probe is injectable, so it can be
unit-tested on a machine with no audio stack at all.
"""
from __future__ import annotations

import glob as _globmod
import os
import shutil
import stat as _statmod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

# jack_status_t (jack/types.h). The host prints the raw bitmask; naming the
# bits turns "(17)" into something a user can act on.
JACK_STATUS_BITS: dict[int, str] = {
    0x0001: "JackFailure",
    0x0002: "JackInvalidOption",
    0x0004: "JackNameNotUnique",
    0x0008: "JackServerStarted",
    0x0010: "JackServerFailed",
    0x0020: "JackServerError",
    0x0040: "JackNoSuchClient",
    0x0080: "JackLoadFailure",
    0x0100: "JackInitFailure",
    0x0200: "JackShmFailure",
    0x0400: "JackVersionError",
    0x0800: "JackBackendError",
    0x1000: "JackClientZombie",
}

#: Remediation text for the jack2-vs-pipewire-jack conflict.
PIPEWIRE_JACK_FIX = (
    "let pipewire-jack own libjack instead of a standalone JACK server: "
    "Arch `pacman -S pipewire-jack` (replaces jack2), "
    "Debian/Ubuntu `apt install pipewire-jack`, "
    "Fedora `dnf install pipewire-jack`; then restart the session "
    "(`systemctl --user restart pipewire`)"
)

#: short form used by `eqforge doctor` output
PIPEWIRE_JACK_SHORT = ("install pipewire-jack (replaces jack2), "
                       "then restart the PipeWire session")

#: where a jackd server leaves its request channel (a UNIX socket)
JACKD_SHM_PATTERNS = ("/dev/shm/jack_*",)
JACKD_REQUEST_CHANNEL = ("/dev/shm/jack_default_0_0",)

#: Stale artifacts that OUTLIVE a jackd and must not be read as "running":
#: measured after `jackd -d dummy` exited - `/dev/shm/jack-shm-registry`
#: (regular file) and `/dev/shm/jack_db-0` (directory) were still there, while
#: the request socket `/dev/shm/jack_default_0_0` had been unlinked.
#: So server detection tests for a socket, never for mere existence.


def decode_status(status: int) -> str:
    """Decode a jack_status_t bitmask into readable flag names.

    >>> decode_status(17)
    'JackFailure|JackServerFailed'
    """
    if not status:
        return "no error"
    names = [name for bit, name in sorted(JACK_STATUS_BITS.items())
             if status & bit]
    known = 0
    for bit in JACK_STATUS_BITS:
        known |= bit
    unknown = status & ~known
    if unknown:
        names.append(f"0x{unknown:x}")
    return "|".join(names)


@dataclass(frozen=True)
class JackInfo:
    """What the JACK side of this machine looks like."""

    pipewire_jack: bool        # pw-jack present -> pipewire-jack client lib
    jackd: bool                # standalone jackd (jack1/jack2) installed
    pipewire_running: bool     # a PipeWire socket is reachable
    jackd_running: bool        # a jackd server left its shm/socket behind
    server_socket: str | None  # what an rt host would connect through
    conflict: bool             # jackd owns libjack while PipeWire runs: breaks
    hazard: bool               # both installed: works, but only under pw-jack
    usable: bool               # eqforge-rt could open a JACK client
    detail: str                # one-line human summary for `eqforge doctor`
    fix: str = ""              # remediation when not usable / conflicting


def _is_socket(path: object) -> bool:
    """True only for an existing UNIX domain socket.

    Existence alone is not evidence of a running server: jackd leaves its shm
    registry behind after it exits, and PipeWire's runtime dir can hold stale
    entries too. A client connects to a socket, so that is what we probe.
    """
    try:
        return _statmod.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _uid() -> int:
    try:
        return os.getuid()
    except AttributeError:                   # non-POSIX
        return 0


def _runtime_dir(environ: Mapping[str, str]) -> Path:
    value = environ.get("XDG_RUNTIME_DIR")
    return Path(value) if value else Path("/tmp")


def _first_present(candidates: list[Path],
                   exists: Callable[[object], bool]) -> Path | None:
    for c in candidates:
        try:
            if exists(c):
                return c
        except OSError:
            continue
    return None


def _user_runtime_dir(exists: Callable[[object], bool]) -> Path | None:
    """`/run/user/<uid>` - the logind runtime dir, when XDG_RUNTIME_DIR is unset.

    Derived from the real uid rather than hardcoding 1000, so another user on
    the box is not mistaken for us. Probed through the injected `exists` so
    tests stay hermetic.
    """
    candidate = Path("/run/user") / str(_uid())
    try:
        return candidate if exists(candidate) else None
    except OSError:
        return None


def launch_wrapper(info: "JackInfo | None" = None, *,
                   which: Callable[[str], str | None] = shutil.which
                   ) -> list[str]:
    """Command prefix that makes a bare JACK client load PipeWire's libjack.

    `pw-jack` does one thing: it adjusts LD_LIBRARY_PATH so the application
    loads pipewire-jack's reimplementation of the JACK client library
    instead of a standalone jack2/jack1 one. Without it, an `eqforge-rt`
    built against jack2's headers resolves `libjack.so.0` to jack2 at run
    time, finds no jackd, and fails with JackFailure|JackServerFailed (17) -
    while `pw-jack eqforge system start` works. Wrapping removes that
    footgun from the manual command line, the daemon and the systemd unit.

    Returns [] when wrapping is unnecessary or would be wrong: with a real
    jackd serving, pipewire-jack must not be forced.
    """
    info = info if info is not None else inspect(which=which)
    if info.pipewire_jack and info.pipewire_running and not info.jackd_running:
        wrapper = which("pw-jack")
        if wrapper:
            return [wrapper]
    return []


def boot_wrapper(info: "JackInfo | None" = None, *,
                 which: Callable[[str], str | None] = shutil.which
                 ) -> list[str]:
    """`launch_wrapper()`, for a systemd unit: what will be true at *login*.

    The runtime probes answer "is PipeWire serving JACK right now", which is
    the wrong question for a unit file. A unit is written in one moment - an
    ssh session with no audio up, or a box where a hand-started jackd happens
    to be running - and then executed at every login, when PipeWire is up and
    jackd is not. Baking in the answer to the *now* question silently yields a
    unit that fails with JackFailure|JackServerFailed (17) on every boot, and
    the failure looks like a broken install rather than a stale decision. So
    for units the prefix depends only on what is installed; `system autostart
    on` prints the command it wrote, which keeps the choice visible.
    """
    info = info if info is not None else inspect(which=which)
    if info.pipewire_jack:
        wrapper = which("pw-jack")
        if wrapper:
            return [wrapper]
    return []


def inspect(*, which: Callable[[str], str | None] = shutil.which,
            exists: Callable[[object], bool] = os.path.exists,
            environ: Mapping[str, str] | None = None,
            is_socket: Callable[[object], bool] = _is_socket,
            glob: Callable[[str], list[str]] = _globmod.glob) -> JackInfo:
    """Probe the JACK transport. Never raises; absence is just data.

    All probes are injectable so tests can describe a machine that does not
    exist here (PipeWire running, jack2 installed, ...).
    """
    env = os.environ if environ is None else environ
    runtime = _runtime_dir(env)
    user_runtime = _user_runtime_dir(exists)
    uid = _uid()

    pipewire_jack = bool(which("pw-jack"))
    jackd = bool(which("jackd"))

    # -- PipeWire socket ---------------------------------------------------
    pw_candidates: list[Path] = []
    remote = env.get("PIPEWIRE_REMOTE", "").strip()
    if remote:
        pw_candidates.append(Path(remote))
    pw_candidates.append(runtime / "pipewire-0")
    if user_runtime is not None and user_runtime != runtime:
        pw_candidates.append(user_runtime / "pipewire-0")
    pw_socket = _first_present(pw_candidates, is_socket)
    pipewire_running = pw_socket is not None

    # -- jackd server ------------------------------------------------------
    jack_candidates: list[Path] = []
    jack_remote = env.get("JACK_REMOTE", "").strip()
    if jack_remote:
        jack_candidates.append(Path(jack_remote))
    jack_candidates += [Path(p) for p in JACKD_REQUEST_CHANNEL]
    jack_candidates += [runtime / "jack-0",                 # some builds/configs
                        Path("/tmp") / f"jack-{uid}" / "jack-0"]  # legacy, uid
    if user_runtime is not None and user_runtime != runtime:
        jack_candidates.append(user_runtime / "jack-0")
    jack_socket = _first_present(jack_candidates, is_socket)
    if jack_socket is None:
        for pattern in JACKD_SHM_PATTERNS:      # any server name / uid
            try:
                hits = sorted(glob(pattern))
            except OSError:
                hits = []
            for hit in hits:
                if is_socket(hit):             # ignore stale shm artifacts
                    jack_socket = Path(hit)
                    break
            if jack_socket is not None:
                break
    jackd_running = jack_socket is not None

    # pipewire-jack routes through the PipeWire socket, so prefer it when the
    # client library is present; a real jackd server wins only otherwise.
    if pipewire_running and pipewire_jack:
        server_socket, server_kind = str(pw_socket), "pipewire-jack"
    elif jackd_running:
        server_socket, server_kind = str(jack_socket), "jackd"
    elif pipewire_running:
        server_socket = str(pw_socket)
        server_kind = "pipewire (no pipewire-jack)"
    else:
        server_socket, server_kind = None, "none"

    usable = bool(pipewire_running and pipewire_jack) or jackd_running
    # jackd installed while PipeWire runs *and* pipewire-jack is absent means
    # libjack belongs to jack2: a bare JACK client never reaches the PipeWire
    # graph, and with jackd not running it fails with status 17.
    conflict = bool(jackd and pipewire_running and not pipewire_jack)
    # Both stacks installed is survivable but fragile: the rt host then only
    # connects reliably inside the PipeWire JACK namespace.
    hazard = bool(jackd and pipewire_jack)

    facts = [f"server: {server_kind}"]
    if server_socket:
        facts.append(server_socket)
    if pipewire_jack:
        facts.append("pipewire-jack present")
    if jackd:
        facts.append("standalone jackd present")
    if not (pipewire_jack or jackd):
        facts.append("no JACK client library found (rt host only)")

    fix = ""
    if conflict:
        fix = PIPEWIRE_JACK_FIX
    elif not usable:
        fix = ("start a JACK-compatible server: "
               + (PIPEWIRE_JACK_FIX if not pipewire_running
                  else "install pipewire-jack"))
    elif hazard:
        fix = ("jack2/jackd is installed next to pipewire-jack; launch the rt "
               "host as `pw-jack eqforge system start` or remove jackd")

    detail = "; ".join(facts)
    if conflict:
        detail += (" - CONFLICT: jack2/jackd owns libjack while PipeWire runs,"
                   " so JACK clients cannot reach the graph")
    elif not usable:
        detail += " - no reachable JACK server"
    elif hazard:
        detail += " - hazard: two JACK stacks installed"

    return JackInfo(pipewire_jack=pipewire_jack, jackd=jackd,
                    pipewire_running=pipewire_running,
                    jackd_running=jackd_running, server_socket=server_socket,
                    conflict=conflict, hazard=hazard, usable=usable,
                    detail=detail, fix=fix)
