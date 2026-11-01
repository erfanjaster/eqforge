"""Profile resolution: inheritance, merging, and dsp-config generation.

resolve_profile() walks the `extends` chain (parents first), deep-merging
sections. EQ filter lists either replace (default) or append, per the child's
`eq.filters_op`. The result is then converted into the *resolved dsp config*
JSON consumed by libeqforge (C core), the PipeWire module and the RT host.

Auto-preamp: when `preamp.mode == "auto"`, the preamp is computed as
-min(0, sum of positive filter gains + margin), i.e. it keeps the worst-case
EQ boost at unity to protect downstream headroom (see smart/headroom.py).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np

from eqforge.errors import ProfileError
from eqforge.log import get_logger

log = get_logger("profiles.resolve")

MAX_CHAIN_DEPTH = 8


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into `base` (returns new dict)."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def resolve_profile(profile_id: str, loader) -> dict:
    """Fully merged profile dict for `profile_id`.

    `loader` is a callable id -> raw profile dict (raises ProfileError when
    missing). Cycle-safe: each id may appear once in the chain.
    """
    chain: list[str] = []
    seen: set[str] = set()

    def walk(pid: str) -> dict:
        if pid in seen:
            raise ProfileError(f"circular profile inheritance at '{pid}'",
                               hint=f"chain so far: {' -> '.join(chain)}")
        if len(chain) > MAX_CHAIN_DEPTH:
            raise ProfileError("profile inheritance chain too deep")
        seen.add(pid)
        chain.append(pid)
        raw = loader(pid)
        merged = dict(raw)
        parents = raw.get("extends") or []
        if parents:
            base: dict = {}
            for p in parents:
                base = deep_merge(base, walk(p))
            filters_op = (raw.get("eq") or {}).get("filters_op", "replace")
            merged = deep_merge(base, raw)
            if filters_op == "append":
                parent_filters: list = []
                for p in parents:
                    # parents are already merged into base
                    pass
                base_filters = (base.get("eq") or {}).get("filters") or []
                own_filters = (raw.get("eq") or {}).get("filters") or []
                merged.setdefault("eq", {})["filters"] = \
                    list(base_filters) + list(own_filters)
                _ = parent_filters
            else:
                if raw.get("eq", {}).get("filters") is not None:
                    merged.setdefault("eq", {})["filters"] = \
                        raw["eq"]["filters"]
        return merged

    resolved = walk(profile_id)
    resolved.pop("extends", None)
    log.debug("resolved %s via chain %s", profile_id, " -> ".join(chain))
    return resolved


# ----------------------------------------------------------------------
# dsp-config generation (the contract with the native core)
# ----------------------------------------------------------------------

def compute_auto_preamp(filters: list[dict]) -> float:
    """Headroom-protecting preamp for a filter list (dB, <= 0).

    Worst-case coherent sum of positive gains; we pull input down by that
    amount so no frequency region can exceed unity output from EQ alone.
    """
    from eqforge.smart.headroom import headroom_db
    return headroom_db(filters)


def load_convolver_ir(convolver: dict, base_dir: Path | None = None
                      ) -> np.ndarray | None:
    """Materialize convolver `ir` (inline or ir_file WAV) to float array."""
    if not convolver.get("enabled"):
        return None
    ir = convolver.get("ir")
    if ir is not None:
        return np.asarray(ir, dtype=np.float64)
    path = convolver.get("ir_file")
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute() and base_dir:
        p = base_dir / p
    from eqforge.audio.io import read
    audio = read(p)
    data = audio.data
    # normalize IR to unit peak to avoid unexpected level shifts
    peak = float(np.max(np.abs(data)))
    if peak > 0:
        data = data / peak
    return data


def to_dsp_config(resolved: dict, base_dir: Path | None = None,
                  max_ir_taps: int = 8192) -> dict:
    """Convert a resolved profile dict into the native dsp-config JSON dict.

    This is the single contract between the Python control plane and all
    native consumers (offline renderer, SPA module, RT host).
    """
    eq = resolved.get("eq") or {}
    filters = []
    for f in eq.get("filters") or []:
        if not f.get("enabled", True):
            continue
        item = {
            "type": f.get("type", "peak"),
            "freq": float(f.get("freq", 1000.0)),
            "gain_db": float(f.get("gain_db", 0.0)),
            "q": float(f.get("q", 1.0)),
            "enabled": True,
        }
        ch = f.get("channel", "all")
        if ch != "all":
            item["channel"] = str(int(ch))
        if f.get("use_q") is False:
            item["use_q"] = False
            item["slope"] = float(f.get("slope", 0.71))
        filters.append(item)

    pre = resolved.get("preamp") or {}
    if pre.get("mode", "auto") == "auto":
        preamp = compute_auto_preamp(filters)
    else:
        preamp = float(pre.get("gain_db", 0.0))
    preamp = max(min(preamp, 60.0), -60.0)

    cfg: dict = {
        "preamp_db": round(preamp, 3),
        "eq": {"filters": filters},
        "post_gain_db": float(resolved.get("post_gain_db", 0.0) or 0.0),
    }

    xf = resolved.get("crossfeed") or {}
    if xf:
        cfg["crossfeed"] = {
            "enabled": bool(xf.get("enabled", False)),
            "level_db": float(xf.get("level_db", -6.0)),
            "fc_hz": float(xf.get("fc_hz", 700.0)),
        }

    comp = resolved.get("compressor") or {}
    if comp:
        cfg["compressor"] = {
            "enabled": bool(comp.get("enabled", False)),
            "threshold_db": float(comp.get("threshold_db", -18.0)),
            "ratio": float(comp.get("ratio", 3.0)),
            "knee_db": float(comp.get("knee_db", 6.0)),
            "attack_ms": float(comp.get("attack_ms", 10.0)),
            "release_ms": float(comp.get("release_ms", 120.0)),
            "makeup_db": float(comp.get("makeup_db", 0.0)),
            "stereo_link": bool(comp.get("stereo_link", True)),
            "rms_detect": bool(comp.get("rms_detect", False)),
        }

    lim = resolved.get("limiter") or {}
    if lim.get("enabled", True):
        cfg["limiter"] = {
            "enabled": True,
            "ceiling_db": float(lim.get("ceiling_db", -1.0)),
            "attack_ms": float(lim.get("attack_ms", 5.0)),
            "release_ms": float(lim.get("release_ms", 60.0)),
            "lookahead_ms": float(lim.get("lookahead_ms", 1.5)),
            "knee_db": float(lim.get("knee_db", 3.0)),
        }

    cv = resolved.get("convolver") or {}
    if cv.get("enabled"):
        ir = load_convolver_ir(cv, base_dir)
        if ir is not None:
            if ir.ndim == 1:
                ir_list = [round(float(v), 8) for v in ir[:max_ir_taps]]
            else:
                ir_list = [[round(float(v), 8) for v in ir[c][:max_ir_taps]]
                           for c in range(ir.shape[0])]
            cfg["convolver"] = {
                "enabled": True,
                "gain_db": float(cv.get("gain_db", 0.0)),
                "ir": ir_list,
            }
        else:
            log.warning("convolver enabled but no usable IR; stage skipped")

    return cfg


def dsp_config_json(resolved: dict, base_dir: Path | None = None,
                    indent: int | None = None) -> str:
    return json.dumps(to_dsp_config(resolved, base_dir), indent=indent)
