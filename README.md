# EQForge

**A profile-driven, smart audio processing system for Linux.**

EQForge is a system-wide equalizer and audio processor with a native C DSP
core, first-class PipeWire integration, offline file processing, an honest
smart-advisor, and both a terminal and a graphical interface. It is built the
way audio software should be: the real-time path never allocates, never locks,
and never touches Python — while everything *around* audio (profiles,
analysis, automation, UI) is flexible, scriptable and script-friendly.

```
        ┌────────────────────────────────────────────────────────────┐
        │                        control plane                       │
        │  eqforge CLI · daemon (JSON-RPC + GUI server) · advisor    │
        │  profiles & inheritance · importers · BS.1770 analysis     │
        └──────────────┬─────────────────────────────┬───────────────┘
                       │ resolved dsp-config JSON     │
        ┌──────────────▼──────────────┐   ┌──────────▼───────────────┐
        │   real-time hosts (native)  │   │  offline renderer (ctypes)│
        │  PipeWire SPA filter module │   │  analyze · apply · batch  │
        │  eqforge-rt (JACK client)   │   │  normalize · dither       │
        └──────────────┬──────────────┘   └──────────┬───────────────┘
                       │                             │
        ┌──────────────▼─────────────────────────────▼───────────────┐
        │           libeqforge — C11 DSP core (RT-safe, no deps)     │
        │   biquad EQ · compressor · look-ahead true-peak limiter    │
        │   crossfeed · FIR convolver · K-weight meters · JSON cfg   │
        └─────────────────────────────────────────────────────────────┘
```

## Features

**Equalizer** — up to 64 bands per channel: peaking, low/high shelves
(6 & 12 dB/oct), low/high-pass, band-pass, notch, all-pass. Per-channel
filters, smooth zipper-free parameter ramps, instant bypass and A/B slots.

**Profiles** — human-readable JSON with schema validation, format versioning
and migration, and true inheritance (`flat → headphone-base → my-sonnet-1`).
Import AutoEQ, Room EQ Wizard, EqualizerAPO and CSV correction files. Export
self-contained resolved profiles for sharing. Diff any two profiles.

**Analysis** — ITU-R BS.1770-4 integrated/momentary/short-term LUFS, EBU
Tech 3342 loudness range, 4×-oversampled true peak (ITU BS.1770 Annex 2),
crest factor, clipping and DC detection, 1/3-octave band balance, spectral
flatness/centroid, and narrowband resonance detection.

**Smart advisor** — real signal processing, not marketing:
auto-preamp from worst-case EQ headroom, clipping-risk prediction for a
(profile × content) pair, resonance → notch suggestions, target-curve
matching (Harman) with conservative gain-limited suggestions, loudness
normalization plans, and plain-English warnings with machine-readable
actions.

**Linux-native** — a compiled PipeWire SPA filter node
(`libeqforge-filter.so`) with hot-reloading config, plus a JACK/PipeWire-JACK
real-time host with a control socket. Device and application stream discovery
via the PipeWire graph, rule-based auto-switching per device (Bluetooth
headphones plug in → your headphone profile activates), desktop
notifications, systemd user units, XDG paths.

**File processing** — `eqforge apply` / `batch` render files through the same
native chain used in real time: bit-identical engine behavior, latency
compensation, two-pass LUFS normalization with true-peak ceiling, TPDF dither
on bit-depth reduction, metadata preservation, MP3/M4A/OPUS via ffmpeg.

**Interfaces** — a designed CLI (`eqforge …`, everything scriptable, `--json`
everywhere) and a fast local web GUI (no frameworks, no CDN): draggable EQ
canvas with per-band ghosts and spectrum overlay, live meters over SSE,
profile/device management, A/B and bypass.

**Reliability** — every layer validates: profile schema with errors *and*
warnings, native config re-validation, safe fallbacks (numpy engine when the
core can't load), corrupt-config quarantine, stale-socket recovery, and
`eqforge doctor` end-to-end self-tests.

## Quick start

```sh
# 1. build the native core (needs gcc + make; ~5 seconds)
make -C core

# 2. install the Python control plane (editable, user scope)
python3 -m pip install --user -e .

# 3. check everything
eqforge doctor

# 4. analyze and process a file
eqforge analyze song.flac
eqforge apply bass-boost song.flac song-processed.flac

# 5. see your profiles, draw the curve in your terminal
eqforge list-profiles
eqforge show headphone-crossfeed

# 6. system-wide processing (PipeWire session required)
eqforge system setup && eqforge system start
eqforge set-profile gaming
eqforge gui            # browser UI: EQ canvas, meters, devices
```

## The one-minute tour

```console
$ eqforge suggest vocal-demo.wav --target harman-overear --save-as auto-match
  Target: harman-overear
  rms deviation  4.2 dB
  max deviation  8.9 dB

  Suggested corrective filters:
  type      freq     gain    q
  ----------------------------
  lowshelf  63 Hz    -4.2    0.71
  peak      250 Hz   -2.8    1.3
  peak      6300 Hz  +3.1    1.3
  saved as profile auto-match → ~/.config/eqforge/profiles/auto-match.json

$ eqforge show auto-match
  Auto-match (harman-overear)
  +15.0 |
   ...  |            **
   +0.0 |------*****------***********------------
   ...  |  *****                *****
  -15.0 |***                        ************
        +---------------------------------------
         20   50   100  200  500  1k   2k   5k  10k  20k

$ eqforge apply auto-match --normalize -16 vocal-demo.wav final.flac
  ✓ final.flac  (native engine, 71.3× realtime, normalized +4.2 dB)
```

## Repository layout

```
core/        libeqforge — C11 DSP engine (headers, sources, C test suite)
pipewire/    SPA filter node + standalone smoke test (no daemon needed)
rthost/      eqforge-rt — JACK/PipeWire real-time host with control socket
eqforge/     Python control plane (CLI, daemon, GUI, profiles, analysis…)
tests/       pytest suite (parity, analysis, profiles, engine, CLI, RPC)
docs/        full documentation (start here: docs/architecture.md)
packaging/   desktop file, systemd units, PipeWire config, install.sh
```

## Documentation

| Topic | Document |
|---|---|
| Install / build / package | [docs/installation.md](docs/installation.md) |
| Architecture & decisions | [docs/architecture.md](docs/architecture.md) |
| CLI manual | [docs/cli.md](docs/cli.md) |
| GUI guide | [docs/gui.md](docs/gui.md) |
| Profile format & dsp-config | [docs/profile-format.md](docs/profile-format.md) |
| PipeWire / system audio | [docs/pipewire-integration.md](docs/pipewire-integration.md) |
| Configuration reference | [docs/configuration.md](docs/configuration.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Development guide | [docs/development.md](docs/development.md) |

## Requirements

- Linux with PipeWire (recommended) or JACK; Python ≥ 3.10
- `gcc`, `make` for the native core (or use the numpy fallback engine)
- Python: `numpy`, `scipy`, `soundfile` (pip or distro packages)
- Optional: `ffmpeg` (MP3/M4A), `pipewire-audio` CLI tools (`pw-dump`…)

## License

MIT — see [LICENSE](LICENSE).
