"""ctypes declarations mirroring core/include/eqforge/*.h.

Keep this file mechanically in sync with the C headers; the parity tests in
tests/test_native_parity.py verify the important layouts at runtime.
"""
from __future__ import annotations

import ctypes
from ctypes import (CFUNCTYPE, POINTER, Structure, c_bool, c_char_p, c_double,
                    c_float, c_int, c_size_t, c_uint32, c_void_p)

EQF_MAX_CHANNELS = 16
EQF_MAX_BANDS = 64

# enum eqf_result
EQF_OK = 0

# enum eqf_filter_type (must match types.h ordering)
FILTER_TYPES = {
    "peak": 0,
    "lowshelf": 1,
    "highshelf": 2,
    "lowpass": 3,
    "highpass": 4,
    "bandpass": 5,
    "notch": 6,
    "allpass": 7,
    "lowshelf12": 8,
    "highshelf12": 9,
}
FILTER_TYPE_NAMES = {v: k for k, v in FILTER_TYPES.items()}


class BiquadCoeffs(Structure):
    _fields_ = [("b0", c_float), ("b1", c_float), ("b2", c_float),
                ("a1", c_float), ("a2", c_float)]


class ChainStats(Structure):
    """Mirrors eqf_chain_stats (chain.h)."""
    _fields_ = [
        ("in_peak", c_float * EQF_MAX_CHANNELS),
        ("in_rms", c_float * EQF_MAX_CHANNELS),
        ("out_peak", c_float * EQF_MAX_CHANNELS),
        ("out_rms", c_float * EQF_MAX_CHANNELS),
        ("out_true_peak", c_float * EQF_MAX_CHANNELS),
        ("momentary_lufs", c_float),
        ("limiter_gain_db", c_float),
        ("limiter_true_peak", c_float),
        ("compressor_gr_db", c_float),
        ("in_clipped", c_bool),
        ("out_clipped", c_bool),
        ("latency_frames", c_uint32),
        ("channels", c_uint32),
    ]


def bind(lib: ctypes.CDLL) -> None:
    """Attach argtypes/restypes to every exported function we use."""
    lib.eqf_version_string.restype = c_char_p
    lib.eqf_abi_version.restype = c_uint32

    lib.eqf_chain_new.argtypes = [c_uint32, c_uint32, c_uint32]
    lib.eqf_chain_new.restype = c_void_p
    lib.eqf_chain_free.argtypes = [c_void_p]
    lib.eqf_chain_free.restype = None

    lib.eqf_chain_configure_json.argtypes = [c_void_p, c_char_p, c_size_t]
    lib.eqf_chain_configure_json.restype = c_int
    lib.eqf_chain_configure_file.argtypes = [c_void_p, c_char_p]
    lib.eqf_chain_configure_file.restype = c_int
    lib.eqf_chain_set_sample_rate.argtypes = [c_void_p, c_uint32]
    lib.eqf_chain_set_sample_rate.restype = c_int
    lib.eqf_chain_set_channels.argtypes = [c_void_p, c_uint32]
    lib.eqf_chain_set_channels.restype = c_int

    lib.eqf_chain_set_bypass.argtypes = [c_void_p, c_bool]
    lib.eqf_chain_set_fade.argtypes = [c_void_p, c_bool]
    lib.eqf_chain_set_fade.restype = None
    lib.eqf_chain_set_bypass.restype = None
    lib.eqf_chain_get_bypass.argtypes = [c_void_p]
    lib.eqf_chain_get_bypass.restype = c_bool

    ptr_float = POINTER(c_float)
    lib.eqf_chain_process.argtypes = [c_void_p, POINTER(ptr_float),
                                      POINTER(ptr_float), c_uint32]
    lib.eqf_chain_process.restype = c_int

    lib.eqf_chain_latency.argtypes = [c_void_p]
    lib.eqf_chain_latency.restype = c_uint32
    lib.eqf_chain_snapshot.argtypes = [c_void_p]
    lib.eqf_chain_snapshot.restype = POINTER(ChainStats)
    lib.eqf_chain_last_error.argtypes = [c_void_p]
    lib.eqf_chain_last_error.restype = c_char_p
    lib.eqf_chain_current_config.argtypes = [c_void_p]
    lib.eqf_chain_current_config.restype = c_char_p

    # biquad design/magnitude (used for curve rendering + parity checks)
    lib.eqf_biquad_design.argtypes = [POINTER(BiquadCoeffs), c_int, c_double,
                                      c_double, c_double, c_bool, c_double]
    lib.eqf_biquad_design.restype = c_int
    lib.eqf_biquad_magnitude.argtypes = [POINTER(BiquadCoeffs), c_double,
                                         c_double]
    lib.eqf_biquad_magnitude.restype = c_double

    # offline meter (analysis parity)
    lib.eqf_meter_k_weight_design.argtypes = [c_float, POINTER(c_float * 5 * 2)]
    lib.eqf_meter_k_weight_design.restype = c_int
