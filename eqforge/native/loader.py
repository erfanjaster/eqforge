"""Locate, load and (optionally) build libeqforge.

Search order:
  1. $EQFORGE_LIB environment variable
  2. <repo>/core/build/libeqforge.so (developer tree)
  3. ~/.local/lib/eqforge/libeqforge.so
  4. /usr/lib/eqforge/libeqforge.so, /usr/local/lib/...
  5. on-demand build from bundled sources if a compiler exists (dev fallback)

If no native library can be found, the caller falls back to the numpy
reference engine (eqforge.dsp.fallback) with reduced performance and no
real-time hosts.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from eqforge import paths
from eqforge.log import get_logger
from eqforge.native import capi

log = get_logger("native")

_LIB: ctypes.CDLL | None = None
_LIB_PATH: Path | None = None
_TRIED = False


def _candidate_paths() -> list[Path]:
    cands: list[Path] = []
    env = os.environ.get("EQFORGE_LIB")
    if env:
        cands.append(Path(env))
    pkg_root = Path(__file__).resolve().parent.parent.parent
    cands += [
        pkg_root / "core" / "build" / "libeqforge.so",
        paths.home() / ".local/lib/eqforge/libeqforge.so",
        Path("/usr/lib/eqforge/libeqforge.so"),
        Path("/usr/local/lib/eqforge/libeqforge.so"),
        Path(f"/usr/lib/{platform.machine()}-linux-gnu/eqforge/libeqforge.so"),
    ]
    return cands


def _source_tree() -> Path | None:
    pkg_root = Path(__file__).resolve().parent.parent.parent
    core = pkg_root / "core"
    if (core / "Makefile").exists() and (core / "src").exists():
        return core
    return None


def build_native(core_dir: Path | None = None, quiet: bool = True) -> Path:
    """Compile libeqforge from bundled sources. Returns path to the .so."""
    core_dir = core_dir or _source_tree()
    if core_dir is None:
        raise FileNotFoundError("no bundled core sources found to build")
    cc = os.environ.get("CC", "cc")
    if shutil.which(cc) is None and shutil.which("gcc") is None:
        raise FileNotFoundError("no C compiler available (tried cc/gcc)")
    log.info("building libeqforge from %s", core_dir)
    cmd = ["make", "-C", str(core_dir)]
    if quiet:
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"native build failed:\n{out.stderr[-2000:]}")
    else:
        subprocess.run(cmd, check=True)
    so = core_dir / "build" / "libeqforge.so"
    if not so.exists():
        raise RuntimeError("build did not produce libeqforge.so")
    return so


def load(rebuild: bool = False) -> ctypes.CDLL | None:
    """Load libeqforge; returns None when unavailable."""
    global _LIB, _LIB_PATH, _TRIED
    if _LIB is not None:
        return _LIB
    if _TRIED and not rebuild:
        return None

    for cand in _candidate_paths():
        if cand.exists():
            try:
                lib = ctypes.CDLL(str(cand))
                lib.eqf_abi_version.restype = ctypes.c_uint32
                abi = int(lib.eqf_abi_version())
                if abi != 1:
                    log.warning("%s: ABI %d unsupported (want 1)", cand, abi)
                    continue
                capi.bind(lib)
                _LIB, _LIB_PATH, _TRIED = lib, cand, True
                log.debug("loaded native core %s (version %s)", cand,
                          lib.eqf_version_string().decode())
                return lib
            except (OSError, AttributeError) as e:
                log.warning("failed to load %s: %s", cand, e)
    if rebuild:
        try:
            so = build_native()
            return load(rebuild=False)
        except Exception as e:  # noqa: BLE001 - build issues are non-fatal
            log.warning("on-demand native build failed: %s", e)
    _TRIED = True
    return None


def lib_path() -> Path | None:
    return _LIB_PATH


def info() -> dict:
    lib = load()
    if lib is None:
        return {"available": False, "backend": "numpy-fallback"}
    return {
        "available": True,
        "backend": "native",
        "path": str(_LIB_PATH),
        "version": lib.eqf_version_string().decode(),
        "abi": int(lib.eqf_abi_version()),
    }


class NativeChain:
    """High-level wrapper around an eqf_chain instance."""

    def __init__(self, channels: int, sample_rate: int, max_block: int = 8192):
        lib = load()
        if lib is None:
            raise RuntimeError("libeqforge not available")
        self._lib = lib
        self._handle = lib.eqf_chain_new(channels, sample_rate, max_block)
        if not self._handle:
            raise MemoryError("eqf_chain_new failed")
        self.channels = channels
        self.sample_rate = sample_rate

    def close(self) -> None:
        if self._handle:
            self._lib.eqf_chain_free(self._handle)
            self._handle = None

    def __del__(self) -> None:  # best effort
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "NativeChain":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def configure_json(self, config_json: bytes | str) -> None:
        from eqforge.errors import NativeError
        if isinstance(config_json, str):
            config_json = config_json.encode()
        rc = self._lib.eqf_chain_configure_json(self._handle, config_json,
                                                len(config_json))
        if rc != 0:
            err = self._lib.eqf_chain_last_error(self._handle)
            raise NativeError(
                f"native chain rejected config ({rc})",
                hint=err.decode() if err else None)

    def set_fade(self, enabled: bool) -> None:
        self._lib.eqf_chain_set_fade(self._handle, enabled)

    def set_bypass(self, bypass: bool) -> None:
        self._lib.eqf_chain_set_bypass(self._handle, bypass)

    def set_sample_rate(self, rate: int) -> None:
        rc = self._lib.eqf_chain_set_sample_rate(self._handle, rate)
        if rc != 0:
            from eqforge.errors import NativeError
            raise NativeError(f"set_sample_rate failed ({rc})")
        self.sample_rate = rate

    def latency_frames(self) -> int:
        return int(self._lib.eqf_chain_latency(self._handle))

    def process(self, data) -> None:
        """Process planar float32 numpy array [channels, frames] in place."""
        import numpy as np
        assert data.dtype == np.float32
        n_ch, frames = data.shape
        if n_ch != self.channels:
            raise ValueError(f"expected {self.channels} channels, got {n_ch}")
        PtrFloat = ctypes.POINTER(ctypes.c_float)
        ptrs = (PtrFloat * n_ch)()
        copy_back = None
        if not data.flags["C_CONTIGUOUS"]:
            copy_back = data
            data = np.ascontiguousarray(data)
        base = data.ctypes.data
        stride = frames * 4
        for c in range(n_ch):
            ptrs[c] = ctypes.cast(base + c * stride, PtrFloat)
        rc = self._lib.eqf_chain_process(self._handle, ptrs, ptrs, frames)
        if rc != 0:
            from eqforge.errors import NativeError
            raise NativeError(f"process failed ({rc})")
        if copy_back is not None:
            copy_back[...] = data

    def snapshot(self) -> dict:
        snap = self._lib.eqf_chain_snapshot(self._handle)
        if not snap:
            return {}
        s = snap.contents
        ch = min(int(s.channels), self.channels)
        return {
            "in_peak": [float(s.in_peak[i]) for i in range(ch)],
            "in_rms": [float(s.in_rms[i]) for i in range(ch)],
            "out_peak": [float(s.out_peak[i]) for i in range(ch)],
            "out_rms": [float(s.out_rms[i]) for i in range(ch)],
            "out_true_peak": [float(s.out_true_peak[i]) for i in range(ch)],
            "momentary_lufs": float(s.momentary_lufs),
            "limiter_gain_db": float(s.limiter_gain_db),
            "compressor_gr_db": float(s.compressor_gr_db),
            "in_clipped": bool(s.in_clipped),
            "out_clipped": bool(s.out_clipped),
            "latency_frames": int(s.latency_frames),
        }


def biquad_magnitude_response(filter_type: str, freq: float, gain_db: float,
                              q: float, sample_rate: float,
                              freqs) -> "object":
    """Vectorized magnitude response using the native designer (numpy in/out)."""
    import numpy as np
    lib = load()
    if lib is None:
        from eqforge.dsp.coeffs import design_biquad, magnitude_response
        b, a = design_biquad(filter_type, freq, gain_db, q, sample_rate)
        return magnitude_response(b, a, freqs)
    coeffs = capi.BiquadCoeffs()
    t = capi.FILTER_TYPES.get(filter_type, 0)
    use_q = filter_type not in ("lowshelf", "highshelf")
    rc = lib.eqf_biquad_design(ctypes.byref(coeffs), t, freq, gain_db, q,
                               use_q, sample_rate)
    if rc != 0:
        raise ValueError(f"biquad design failed for {filter_type} {freq} Hz")
    freqs = np.asarray(freqs, dtype=np.float64)
    out = np.empty_like(freqs)
    for i, f in enumerate(freqs.ravel()):
        out.ravel()[i] = lib.eqf_biquad_magnitude(ctypes.byref(coeffs), f,
                                                  sample_rate)
    return out
