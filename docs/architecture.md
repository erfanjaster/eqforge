# Architecture

This document explains how EQForge is structured, why, and where every
piece lives. Read this before touching the code.

## Design goals

1. **Native where it counts.** All real-time audio runs in C (`libeqforge`).
   Python is a control plane: it configures, analyzes and orchestrates, but
   never processes a live sample.
2. **One engine, three consumers.** The same C chain serves the PipeWire SPA
   filter, the JACK RT host, and offline file rendering (via ctypes). What you
   audition in `eqforge apply` is bit-for-bit the system-wide engine.
3. **Portability to C++.** The core is C11 in a C++-compatible subset; a CI
   canary (`make -C core test-cxx`) compiles every core source as C++17. The
   JSON config format, filter math, and module boundaries are designed so the
   control plane could be reimplemented natively without redesigning anything.
4. **Fail safe.** Invalid profiles are rejected before they reach the engine;
   the engine re-validates; the limiter has a final hard clamp; every layer
   degrades gracefully (no PipeWire → file tools still work; no compiler →
   numpy fallback engine).

## Components

```
┌─────────────────────────── control plane (Python) ─────────────────────────┐
│                                                                            │
│  cli/          argparse command surface (`eqforge …`)                      │
│  daemon/       EngineState (slots, bypass, active-dsp.json) + JSON-RPC     │
│                over a Unix socket + localhost HTTP server (GUI, SSE)       │
│  profiles/     model · schema · versioning/migration · resolve (inherit)   │
│                store (XDG + builtin presets)                               │
│  importers/    AutoEQ · REW · EqualizerAPO · CSV · auto-detect             │
│  analysis/     BS.1770-4 loudness · true peak · spectrum · report          │
│  smart/        advisor rules · headroom · target curves (Harman) · match   │
│  system/       PipeWire graph (pw-dump/pw-metadata) · matching · notify    │
│  render/       SVG + ASCII EQ curves                                      │
│  gui/static/   zero-dependency SPA served by the daemon                    │
│  dsp/engine.py offline renderer (block processing via NativeChain)         │
│  native/       ctypes bindings + library discovery/on-demand build         │
└────────────────────────────────────────────────────────────────────────────┘
        │ resolved dsp-config JSON (the single contract)
        ▼
┌─────────────────────────── audio plane (native C) ─────────────────────────┐
│  core/           libeqforge.so/.a                                          │
│    biquad.c        RBJ designs, DF2T processing, coefficient smoothing     │
│    eq.c            per-channel band banks                                  │
│    dynamics.c      feed-forward compressor · look-ahead true-peak limiter  │
│    crossfeed.c     Bauer-style LP crossfeed network                        │
│    fir.c           time-domain convolver (headphone FIRs)                  │
│    meter.c         peak/RMS · K-weighted momentary LUFS · true peak        │
│    chain.c         stage orchestration, JSON config, fades, snapshots      │
│    json.c          dependency-free JSON parser (arena)                     │
│  pipewire/       libeqforge-filter.so — SPA node factory "eqforge.filter"  │
│  rthost/         eqforge-rt — JACK client + control socket                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

## The dsp-config contract

Everything the native side needs is one JSON document — the *resolved
dsp-config* (full spec in [profile-format.md](profile-format.md#resolved-dsp-config)):

```json
{
  "preamp_db": -6.5,
  "eq": { "filters": [ {"type": "peak", "freq": 105, "gain_db": 5.1, "q": 1.1} ] },
  "crossfeed":  { "enabled": false, "level_db": -6.5, "fc_hz": 700 },
  "compressor": { "enabled": false, "threshold_db": -18, "ratio": 3 },
  "limiter":    { "enabled": true, "ceiling_db": -1.0, "lookahead_ms": 1.5 },
  "convolver":  { "enabled": false }
}
```

* The Python `profiles.resolve.to_dsp_config()` produces it (inheritance,
  auto-preamp, IR file loading all happen in Python).
* `libeqforge` parses and validates it in C (`chain.c`) — the SPA module and
  RT host therefore need **zero** Python at runtime.
* The daemon writes it to `~/.config/eqforge/active-dsp.json`; native hosts
  hot-reload it (mtime check ≈1 Hz, click-guarded reconfigure).

This is also the C++ migration seam: a native control plane would produce the
exact same document.

## Processing chain

Fixed stage order (deliberate, documented decision — see ADR-3):

```
input gain (preamp) → EQ → crossfeed → convolver → compressor → limiter → output gain
```

* EQ before dynamics so compression reacts to the tonal balance you hear.
* Limiter last: it is the safety net; nothing after it can clip.
* Crossfeed before convolution so headphone FIRs see the blended signal.

Chain state machine: `configure*` may be called from any thread; the audio
thread applies a 10 ms fade-in after reconfiguration (offline renders disable
the fade via `eqf_chain_set_fade()` for sample-exact output).

## Real-time discipline

* `process()` paths: no malloc, no syscalls (one documented exception: the
  SPA module's ~1 Hz `stat()` for config hot-reload), no locks on audio data,
  no unbounded work. Denormal protection via state flushing.
* Meters/snapshots are written by the audio thread and read atomically-ish by
  the control plane (single-writer POD struct; readers tolerate staleness).
* The RT host applies control commands on its socket thread; the audio thread
  only reads the chain pointer and bypass flag (C11 atomics).

## Threading model

| Process | Threads | Notes |
|---|---|---|
| daemon | RPC accept loop + per-connection handlers, HTTP server threads, device watcher (2 s poll) | all daemon=True; watcher never dies |
| eqforge-rt | JACK process callback (RT), control socket thread | audio thread never blocks |
| PipeWire SPA | hosted by PW data loop | reload on data thread (bounded) |
| CLI | single-threaded | spawns daemon detached when needed |

## Data flow: system-wide audio

**SPA filter path** (recommended when installed):

```
apps → PipeWire graph → eqforge.filter node → default sink
        (adapter-hosted; reads active-dsp.json)
```

**RT host path** (works wherever JACK API works, incl. pipewire-jack):

```
apps → eqforge-rt in_* → libeqforge → out_* → default sink ports
        (daemon sends connect commands; control socket: reload/bypass/status)
```

The daemon owns the *intent* (which profile, which slot, bypass), publishes
`active-dsp.json`, and pokes whichever host is running.

## Decisions (ADR digest)

1. **C core + Python control plane instead of C++ everywhere.** C11 with a
   C++-compatible subset gives the same portability path with a smaller
   toolchain footprint today; the canary guarantees the door stays open.
2. **JSON config over FFI structs.** A textual contract is inspectable
   (`active-dsp.json`), versionable, and lets native hosts run with zero
   Python. Parse cost is control-thread only.
3. **Fixed chain order, extensible stage set.** A graph engine was considered
   and rejected for v1: fixed order covers the product, and stages are
   isolated behind one interface (`eqf_*_process`) so new stages (e.g. FFT
   convolver, stereo widener) slot in without redesign.
4. **SPA filter + JACK host, not a Pulse shim.** PipeWire is the modern
   stack; the JACK API covers the rest. No PulseAudio-specific code.
5. **Localhost HTTP + SSE for the GUI instead of a native toolkit.** Zero
   runtime deps, works under any DE (and headless/SSH via port-forward), and
   keeps UI code away from the audio path. A pywebview/Tauri shell can wrap
   the same assets later.
6. **Discovery via pw CLI tools, not libpipewire bindings.** `pw-dump` JSON
   is stable, avoids gobject/introspection deps, and degrades cleanly.
7. **Two-pass normalization + TPDF dither** in offline renders; true-peak
   ceiling enforced during normalization gain selection.
8. **Advisor = deterministic rules over measurements.** Every finding cites
   the measurement that produced it and (where applicable) a machine-readable
   action. No opaque scoring.

## Testing strategy

* **C tests** (`core/tests/test_core.c`): DSP math anchors (ITU -23 LUFS
  sine, RBJ magnitude points, limiter ceiling, true-peak accuracy ±2 %).
* **SPA smoke test** (`pipewire/test_module.c`): drives the module exactly
  like a PipeWire host without needing a daemon.
* **Parity tests** (`tests/test_parity.py`): numpy reference vs native core —
  coefficients (≤2e-6), K-weighting, rendered frequency responses, and
  cross-backend correlation >0.999.
* **Behavior tests**: engine stages, loudness anchors, profiles/migration/
  inheritance, importers, advisor rules, daemon RPC, CLI end-to-end in an
  isolated XDG home.

## Future C++ migration path

1. `core/` sources already compile as C++17 (`make test-cxx`); convert to
   classes/RAII incrementally behind the same C API (`extern "C"`).
2. Replace `json.c` with a header-only C++ parser if desired — the contract
   is the dsp-config document, not the parser.
3. Port control-plane pieces in this order (least Python-idiomatic first):
   profile resolution → dsp-config emission → advisor rules → analysis
   (FFT via a native library) → daemon RPC. CLI/GUI can remain Python
   indefinitely; they are thin clients of the documented RPC/JSON contracts.
