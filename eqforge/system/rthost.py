"""Locating, launching and supervising the native rt host (`eqforge-rt`).

This lives in the system layer rather than the CLI because two callers need
it and they must not diverge:

* `eqforge system start` - a human runs it once;
* the daemon's watcher loop - keeps the host alive across crashes and
  reboots when it was started with `--manage-rt`.

It also owns the two pieces of knowledge that make the difference between
the host starting and the host dying with an opaque error:

1. **Path resolution.** `EQFORGE_RT` unset used to become `Path("")` ==
   `PosixPath(".")`, which passes `exists()` and `os.access(X_OK)` because a
   traversable directory has its x bit set - so Popen tried to exec the cwd
   and raised `PermissionError: [Errno 13] Permission denied: '.'`. Only a
   regular file with the exec bit counts now.
2. **The JACK namespace.** Under pipewire-jack the host must load PipeWire's
   libjack; loading jack2's instead yields "cannot open JACK client (17)"
   even though PipeWire is running fine. `jack.launch_wrapper()` supplies the
   `pw-jack` prefix when - and only when - PipeWire is the JACK provider.
"""
from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from eqforge import paths
from eqforge.errors import SystemError
from eqforge.log import get_logger
from eqforge.system import jack

log = get_logger("system.rthost")


#: packaged install locations, in probe order
SYSTEM_PATHS = (
    "/usr/libexec/eqforge/eqforge-rt",
    "/usr/local/libexec/eqforge/eqforge-rt",
)


def _repo_root() -> Path:
    """Checkout root (…/eqforge), i.e. the parent of the `eqforge` package."""
    return Path(__file__).resolve().parents[2]


def is_executable_file(cand: Path) -> bool:
    """True only for an existing *regular file* this user may execute.

    The previous guard was `cand.exists() and os.access(cand, os.X_OK)`,
    which a directory satisfies: a traversable directory has its x bit set,
    and `Path("")` is `PosixPath(".")` - truthy, existing and "executable".
    So an unset `EQFORGE_RT` resolved to the *current directory* and
    `subprocess.Popen(["."])` died with
    `PermissionError: [Errno 13] Permission denied: '.'`, long after the
    code had decided it had found the rt host.
    """
    try:
        return cand.is_file() and os.access(cand, os.X_OK)
    except OSError:                      # unreadable parent, ELOOP, ...
        return False


def in_tree_rt() -> Path:
    """The rt host built inside this checkout (repo/rthost/build/eqforge-rt)."""
    return _repo_root() / "rthost/build/eqforge-rt"


def candidates(override: str | os.PathLike[str] | None = None) -> list[Path]:
    """Ordered eqforge-rt candidates: explicit override, $EQFORGE_RT, tree, system."""
    cands: list[Path] = []
    for raw in (override, os.environ.get("EQFORGE_RT")):
        text = str(raw).strip() if raw is not None else ""
        if text:                         # an empty override must not become "."
            cands.append(Path(text).expanduser())
    cands.append(in_tree_rt())
    cands += [Path(p) for p in SYSTEM_PATHS]
    return cands


def find_binary(override: str | os.PathLike[str] | None = None) -> Path | None:
    """First usable eqforge-rt candidate, or None if there is none.

    Never returns a directory, and never invents one from an unset
    `EQFORGE_RT`. Callers that must not proceed without a host should use
    `resolve_binary()`, which explains *why* instead of returning None.
    """
    for cand in candidates(override):
        if is_executable_file(cand):
            return cand
    which = shutil.which("eqforge-rt")
    return Path(which) if which else None


def resolve_binary(override: str | os.PathLike[str] | None = None) -> Path:
    """`find_binary()` with actionable errors.

    An explicit `--rt-binary` or a non-empty `$EQFORGE_RT` is a statement of
    user intent, so when it is unusable we report that instead of quietly
    falling back to a packaged binary the user did not ask for.
    """
    explicit = str(override).strip() if override else \
        os.environ.get("EQFORGE_RT", "").strip()
    if explicit:
        cand = Path(explicit).expanduser()
        if is_executable_file(cand):
            return cand
        where = "--rt-binary" if override else "EQFORGE_RT"
        if cand.is_dir():
            raise SystemError(
                f"{where} points at a directory: {cand}",
                hint="give the path to the eqforge-rt executable itself, e.g. "
                     "rthost/build/eqforge-rt")
        if not cand.exists():
            raise SystemError(
                f"{where} points at a missing file: {cand}",
                hint="build it: make -C rthost   "
                     "(installs to /usr/libexec/eqforge with make -C rthost install)")
        raise SystemError(
            f"{where} is not executable: {cand}",
            hint=f"chmod +x {cand}")

    rt = find_binary()
    if rt is None:
        raise SystemError(
            "eqforge-rt binary not found",
            hint="build it: make -C rthost && make -C rthost install, "
                 "or pass --rt-binary PATH")
    return rt


def log_tail(path: Path, lines: int = 4, window: int = 8192) -> list[str]:
    """Last non-empty lines of a log file, or [] when unreadable."""
    try:
        raw = path.read_bytes()[-window:]
    except OSError:
        return []
    text = raw.decode(errors="replace")
    return [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]


def exec_hint(exc: OSError, rt: Path) -> str:
    if exc.errno == errno.EACCES:
        return f"{rt} is not executable: chmod +x {rt}"
    if exc.errno == errno.ENOENT:
        return f"{rt} vanished (stale build?): rebuild with make -C rthost"
    if exc.errno == errno.ENOEXEC:
        return f"{rt} is not a valid executable for this system"
    return f"cannot run {rt}: {exc}"


def most_useful(lines: list[str], keep: int = 2) -> list[str]:
    """Prefer the host's own diagnostics over libjack noise, de-duplicated.

    A failed JACK open prints several lines of library internals
    (`JackShmReadWritePtr::~...`) around the one line that says what
    happened; showing the last N lines verbatim showed the wrong ones.
    """
    deduped = list(dict.fromkeys(lines))
    primary = [ln for ln in deduped if "eqforge-rt:" in ln]
    rest = [ln for ln in deduped if ln not in primary]
    return (primary + rest)[:keep]


def start_failure(rc: int | None, tail: list[str],
                      logfile: Path) -> SystemError:
    """Build an error that carries what eqforge-rt actually said.

    "rt host failed to start, see <log>" forces the user to go hunting for
    the real reason - which for a JACK client is almost always a packaging
    problem, not an EQForge one. Surface the last lines and decode the
    JACK status bitmask inline.
    """
    useful = most_useful(tail)
    message = "rt host failed to start"
    if rc is not None:
        message += f" (exit {rc})"
    if useful:
        message += ": " + " / ".join(useful)

    hint = f"see {logfile}"
    joined = " ".join(tail)
    m = re.search(r"cannot open JACK client \((\d+)\)", joined, re.IGNORECASE)
    if m:
        from eqforge.system import jack
        status = int(m.group(1))
        info = jack.inspect()
        # Only recommend swapping packages when detection actually found the
        # conflict; otherwise say what is missing without prescribing a fix
        # that does not apply to this machine.
        advice = info.fix or ("no JACK-compatible server was reachable; "
                              + jack.PIPEWIRE_JACK_SHORT)
        hint = (f"JACK refused the client: {jack.decode_status(status)} "
                f"({status}). {advice}")
        if info.server_socket:
            hint += f"  [found {info.server_socket}]"
    return SystemError(message, hint=hint)




@dataclass
class LaunchResult:
    """What a successful `start()` produced."""

    pid: int
    rt: Path
    argv: list[str]
    wrapper: list[str]
    socket: Path
    logfile: Path
    proc: subprocess.Popen | None = None


def launch_argv(rt: Path, socket: Path, config: Path, channels: int,
                wrapper: list[str] | None = None,
                name: str = "eqforge") -> list[str]:
    """The exact command line for the rt host, wrapper included.

    Shared by `start()` and the systemd unit generator so the two cannot
    disagree about flags or about needing `pw-jack`.
    """
    wrapper = jack.launch_wrapper() if wrapper is None else wrapper
    return [*wrapper, str(rt), "--name", name, "--socket", str(socket),
            "--config", str(config), "--channels", str(channels)]


def is_running(socket: Path | None = None) -> bool:
    """Is something listening on the rt control socket?

    A successful connect() is the whole test: for AF_UNIX the kernel refuses
    the connection when no process is listening, so a stale socket file left
    by a dead host correctly reports False. Probing further (send/recv)
    would block on a host that is up but busy inside its audio callback -
    and reporting a live host as down is how a supervisor ends up starting a
    second one.
    """
    sock = socket or paths.rt_socket()
    if not sock.exists():
        return False
    import socket as _socket
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(sock))
        return True
    except OSError:
        return False


def start(rt_binary: str | Path | None = None, channels: int = 2,
          wait: float = 5.0, name: str = "eqforge",
          wrapper: list[str] | None = None,
          ensure_config: bool = True) -> LaunchResult:
    """Resolve, launch and confirm the rt host. Raises SystemError on failure.

    `ensure_config` publishes active-dsp.json first; the host rejects a
    missing config at startup and then sits unconfigured until something
    pushes one to it.
    """
    rt = resolve_binary(rt_binary)
    paths.ensure_dirs()
    sock = paths.rt_socket()
    cfg = paths.active_dsp_file()
    if ensure_config and not cfg.exists():
        cfg.write_text('{"preamp_db": 0, "eq": {"filters": []}}',
                       encoding="utf-8")
    logfile = paths.log_dir() / "rt.out"
    argv = launch_argv(rt, sock, cfg, channels, wrapper=wrapper, name=name)

    # The child dups these fds, so the parent's copy must be closed -
    # leaving it open leaked a descriptor on every start.
    with open(logfile, "ab") as logfh:
        try:
            proc = subprocess.Popen(argv, stdout=logfh, stderr=logfh,
                                    start_new_session=True,
                                    stdin=subprocess.DEVNULL)
        except OSError as e:
            raise SystemError(
                f"could not execute {argv[0]}: {e.strerror or e}",
                hint=exec_hint(e, Path(argv[0]))) from e

    deadline = time.time() + wait
    while time.time() < deadline and not sock.exists():
        if proc.poll() is not None:
            break                      # it already died; don't wait it out
        time.sleep(0.1)
    rc = proc.poll()
    if rc is not None or not sock.exists():
        if rc is None:                 # alive but never opened the socket
            proc.terminate()
        raise start_failure(rc, log_tail(logfile), logfile)

    log.info("rt host started: pid %d via %s", proc.pid, " ".join(argv))
    return LaunchResult(pid=proc.pid, rt=rt, argv=argv,
                        wrapper=[argv[0]] if argv[0] != str(rt) else [],
                        socket=sock, logfile=logfile, proc=proc)


def stop(proc: subprocess.Popen | None = None, socket: Path | None = None,
         timeout: float = 3.0) -> bool:
    """Terminate a host this process launched (used by --manage-rt)."""
    sock = socket or paths.rt_socket()
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        sock.unlink(missing_ok=True)
    except OSError:
        pass
    return True
