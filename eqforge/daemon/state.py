"""Daemon-side engine/session state.

Owns:
  - the active profile (A/B slots), bypass flag, post trim
  - the resolved dsp-config written to active-dsp.json (consumed by the SPA
    module and eqforge-rt with hot reload)
  - connections to running real-time hosts (unix socket)
  - the device watcher's current view
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from eqforge import paths
from eqforge.errors import DaemonError
from eqforge.log import get_logger
from eqforge.profiles.store import ProfileStore

log = get_logger("daemon.state")

RT_TIMEOUT = 2.0


class EngineState:
    def __init__(self, store: ProfileStore | None = None):
        self.store = store or ProfileStore()
        cfg = self.store.config()
        self.slot_a: str = cfg.get("profile_a", "flat")
        self.slot_b: str = cfg.get("profile_b", "flat")
        self.active_slot: str = cfg.get("active_slot", "A")
        self.bypass: bool = bool(cfg.get("bypass", False))
        self.auto_switch: bool = bool(cfg.get("auto_switch", False))
        self.last_device: dict | None = None
        self._rt_last: dict = {}
        self._rt_synced: bool = False

    # ---------------- persistence ----------------
    def _persist(self) -> None:
        cfg = self.store.config()
        cfg.update({
            "profile_a": self.slot_a,
            "profile_b": self.slot_b,
            "active_slot": self.active_slot,
            "bypass": self.bypass,
            "auto_switch": self.auto_switch,
        })
        self.store.save_config(cfg)

    # ---------------- profile control ----------------
    @property
    def active_profile(self) -> str:
        return self.slot_a if self.active_slot == "A" else self.slot_b

    def set_profile(self, profile_id: str, slot: str | None = None) -> dict:
        # validate + resolve eagerly so bad profiles never reach the engine
        resolved = self.store.resolve_any(profile_id)
        from eqforge.profiles.resolve import to_dsp_config
        cfg = to_dsp_config(resolved, base_dir=self._base_dir(profile_id))
        slot = slot or self.active_slot
        if slot == "A":
            self.slot_a = profile_id
        else:
            self.slot_b = profile_id
        self._persist()
        self.publish_dsp_config(cfg)
        self._tell_rt({"cmd": "reload", "path": str(paths.active_dsp_file())})
        self._rt_synced = True
        return self.status()

    def _base_dir(self, profile_id: str) -> Path | None:
        for src in (self.store.user_profiles(), self.store.builtin_profiles()):
            if profile_id in src:
                return src[profile_id].parent
        return None

    def set_bypass(self, value: bool) -> dict:
        self.bypass = bool(value)
        self._persist()
        if not self._rt_synced:
            self.sync_rt()          # pushes config *and* current bypass
        else:
            self._tell_rt({"cmd": "bypass", "value": self.bypass})
        return self.status()

    def toggle_bypass(self) -> dict:
        return self.set_bypass(not self.bypass)

    def ab_switch(self, slot: str | None = None) -> dict:
        if slot:
            self.active_slot = "A" if slot.upper().startswith("A") else "B"
        else:
            self.active_slot = "B" if self.active_slot == "A" else "A"
        self._persist()
        resolved = self.store.resolve_any(self.active_profile)
        from eqforge.profiles.resolve import to_dsp_config
        cfg = to_dsp_config(resolved,
                            base_dir=self._base_dir(self.active_profile))
        self.publish_dsp_config(cfg)
        self._tell_rt({"cmd": "reload", "path": str(paths.active_dsp_file())})
        self._rt_synced = True
        return self.status()

    # ---------------- native config publication ----------------
    def publish_dsp_config(self, cfg: dict) -> Path:
        paths.ensure_dirs()
        p = paths.active_dsp_file()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        tmp.replace(p)
        return p

    def ensure_active_config(self) -> Path:
        p = paths.active_dsp_file()
        if p.exists():
            return p
        try:
            resolved = self.store.resolve_any(self.active_profile)
            from eqforge.profiles.resolve import to_dsp_config
            return self.publish_dsp_config(
                to_dsp_config(resolved, self._base_dir(self.active_profile)))
        except Exception as e:  # noqa: BLE001
            log.error("cannot publish active config: %s", e)
            return self.publish_dsp_config({"preamp_db": 0, "eq": {"filters": []}})

    # ---------------- rt host communication ----------------
    def rt_socket_path(self) -> Path:
        return paths.rt_socket()

    def _tell_rt(self, cmd: dict) -> dict | None:
        sock = self.rt_socket_path()
        if not sock.exists():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(RT_TIMEOUT)
                s.connect(str(sock))
                s.sendall((json.dumps(cmd) + "\n").encode())
                data = s.recv(65536)
                resp = json.loads(data.decode()) if data else None
                self._rt_last = resp or {}
                return resp
        except (OSError, json.JSONDecodeError) as e:
            log.debug("rt host not reachable: %s", e)
            return None

    def rt_status(self) -> dict:
        r = self._tell_rt({"cmd": "status"})
        if r is None:
            return {"running": False}
        r["running"] = True
        return r

    def sync_rt(self) -> bool:
        """Push the active dsp-config (and bypass) to a reachable rt host.

        The rt host only reconfigures on command, so after a boot where it
        started before the daemon published active-dsp.json (its log shows
        "initial config rejected: i/o error") it would stay unconfigured.
        Returns True once the host accepted the config.
        """
        p = paths.active_dsp_file()
        if not p.exists():
            p = self.ensure_active_config()
        if not self.rt_socket_path().exists():
            return False
        r = self._tell_rt({"cmd": "reload", "path": str(p)})
        if not (isinstance(r, dict) and r.get("ok")):
            log.debug("rt sync reload failed: %s", r)
            return False
        self._tell_rt({"cmd": "bypass", "value": self.bypass})
        self._rt_synced = True
        return True

    def rt_needs_sync(self) -> bool:
        """True when the running rt host has no config loaded yet."""
        if self._rt_synced:
            return False
        return bool(self.rt_socket_path().exists())

    # ---------------- status ----------------
    def status(self) -> dict:
        st = {
            "active_profile": self.active_profile,
            "slot_a": self.slot_a,
            "slot_b": self.slot_b,
            "active_slot": self.active_slot,
            "bypass": self.bypass,
            "auto_switch": self.auto_switch,
            "rt": self.rt_status(),
            "native_config": str(paths.active_dsp_file()),
            "time": time.time(),
        }
        return st
