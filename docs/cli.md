# CLI Manual

All commands support `--json` (machine-readable output), `-v/--verbose`,
`-q/--quiet`. Exit codes: `0` ok · `1` generic · `2` profile error ·
`3` audio I/O · `4` native engine · `5` import · `6` system · `7` daemon.

## Analyze & process files

### `eqforge analyze FILE [-p PROFILE] [--json]`
Full report: format, duration, integrated/momentary LUFS, loudness range,
peak/true-peak, RMS, crest factor, DC offset, clipping events, band-energy
balance, spectral centroid/flatness — plus advisor findings. With `-p`, the
advisor also predicts how the profile interacts with this content
(clipping risk, normalization plan).

### `eqforge apply PROFILE IN OUT [options]`
Render IN through PROFILE with the native engine.

| Option | Effect |
|---|---|
| `--normalize LUFS` | two-pass loudness normalization (true-peak limited) |
| `--format fmt` | wav/flac/ogg/mp3/m4a/opus… (default: OUT suffix) |
| `--bits 16\|24\|32` | PCM depth (TPDF dither applied unless `--no-dither`) |
| `--quality N` | lossy encoder quality (mp3: `-q:a` 0–9) |
| `--no-metadata` | skip tag copying from the source |
| `--dry-run` | run the advisor on (content × profile) without writing |

If the profile defines `loudness.target_lufs`, normalization happens
automatically. Latency introduced by the limiter look-ahead is compensated —
output stays sample-aligned with input.

### `eqforge batch PROFILE DIR [-o OUTDIR] [--format flac] [--normalize -16]`
Recursively processes every audio file under DIR, preserving the directory
structure. Default output: `DIR-eqforge/`.

### `eqforge suggest FILE [--target harman-overear] [--save-as ID]`
Compares the file's smoothed spectrum against a target curve and proposes a
gain-limited, 1/3-octave-quantized corrective filter set. `--save-as` stores
the result as a profile. Targets: `harman-overear`, `flat`, `broadcast`.

### `eqforge synth SIGNAL OUT [--freq F] [--duration S] [--rate SR]`
Test signals: `sine`, `sweep`, `pink`, `white`, `impulse`, `square`.

## Profiles

### `eqforge list-profiles [--json]`
Builtin + user profiles (user shadows builtin by id).

### `eqforge show PROFILE [--ascii] [--svg FILE] [--resolved]`
Renders the EQ curve in the terminal, or to SVG. `--resolved` prints the exact
dsp-config the native engine receives (post-inheritance, auto-preamp applied).

### `eqforge new ID [--from PROFILE] [--name NAME] [--force]`
Create a user profile, optionally cloned from an existing one.

### `eqforge import FILE [--id ID] [--name NAME]`
Auto-detects and imports: **AutoEQ** txt/csv (jaakkopasanen), **Room EQ
Wizard** exports (incl. BW-octave → Q conversion), **EqualizerAPO** config
(Preamp/Filter/GraphicEQ/Device), plain **freq,gain,q,type CSV**, and native
EQForge JSON. Imported preamp values become manual preamp; `Device:` lines
become matching-rule hints.

### `eqforge export PROFILE DEST`
Exports the **fully resolved** profile — self-contained, no `extends` — the
right way to share profiles.

### `eqforge diff A B`
Unified diff of the two profiles' resolved dsp-configs (what actually differs
sonically, not cosmetically).

### `eqforge validate PROFILE|FILE` · `eqforge delete PROFILE`
Schema validation (errors + warnings) with automatic migration when needed.
`delete` only removes user profiles.

## Runtime (needs the daemon; auto-spawned on demand)

### `eqforge status`
Engine backend, daemon/RT-host state, active profile & slots, bypass,
PipeWire default sink.

### `eqforge set-profile PROFILE [--slot A|B]`
Validate → resolve → publish `active-dsp.json` → hot-reload native hosts.

### `eqforge bypass [on|off|toggle]` · `eqforge ab [A|B|toggle]`
Instant bypass; A/B compares two loaded profiles (bound to <kbd>space</kbd>
in the GUI).

### `eqforge devices [--json]` · `eqforge streams [--json]`
PipeWire sinks/sources (with kind classification: bluetooth/usb/hdmi/…) and
running application streams.

### `eqforge daemon start|stop|restart|status|run`
`run` is the foreground instance (used by systemd units and auto-spawn).

### `eqforge gui [--port N] [--no-browser] [--wait]`
Serves the GUI on localhost and opens `xdg-open`.

### `eqforge system setup|start|stop|status|wire`
Manages native integration: writes PipeWire/systemd files (`setup`), runs the
JACK RT host (`start`, `--manage` to auto-connect to the default sink),
reports RT latency and engine telemetry (`status`).

### `eqforge doctor [--build-core]`
Full diagnostics + end-to-end DSP self-test. `--build-core` compiles
libeqforge on demand when missing.

### `eqforge version`
Versions of the Python package and the loaded native core (or fallback).

## Scripting examples

```sh
# loudness-normalize a podcast feed directory to -16 LUFS
eqforge batch normalize-16 ./episodes -o ./publish --format flac

# CI check: refuse files that would clip with the house profile
eqforge apply house-profile track.wav /dev/null --dry-run --json \
  | jq -e '.findings[] | select(.id=="combined.clipping_risk")' && exit 1

# auto-correct a headphone measurement toward Harman
eqforge suggest sweep-recording.wav --target harman-overear --save-as hd650-match
eqforge import AutoEQ/sennheiser/hd650.txt --id hd650-autoeq
eqforge diff hd650-match hd650-autoeq

# react to device changes from a script
eqforge devices --json | jq -r '.devices[] | select(.kind=="bluetooth") | .name'
```
