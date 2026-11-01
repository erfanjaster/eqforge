# Troubleshooting

Start with:

```sh
eqforge doctor
```

It checks the native core, Python deps, codecs, PipeWire, daemon, RT host and
runs an end-to-end DSP self-test. Below are the issues it (and you) may hit.

## Engine / core

**`native core not loaded — numpy fallback`**
The Python side fell back to numpy. Offline processing still works (slower,
equivalent results); real-time hosts require the native core.

```sh
eqforge doctor --build-core       # compile on demand
make -C core && make -C core test # or build manually; run the C test suite
```

If building fails: install `build-essential` (Debian) / `gcc make` (Fedora).
Set `EQFORGE_LIB=/path/to/libeqforge.so` if it lives somewhere unusual.

**`apply` says "numpy engine"** — same cause as above. Check `eqforge version`
for the loaded core path.

**Different results between native and numpy backends?** They should agree to
within float tolerance (parity tests enforce correlation > 0.999 and ±0.3 dB
per band). If you see more, file a bug with `eqforge show <profile> --resolved`
output and both renders — this would be a real parity break.

## System audio

**No sound after `eqforge system start`**
The RT host creates JACK ports but doesn't hijack the graph by itself:

```sh
eqforge system wire                 # connect outputs to default sink
pw-link | grep eqforge              # inspect connections
wpctl status                        # what is the default sink now?
```

**SPA filter node doesn't appear**

```sh
pw-dump | grep -i eqforge           # node present?
journalctl --user -u pipewire | grep eqforge   # load errors?
```

Common causes: module not in the SPA search path (must be under
`/usr/lib/<arch>-linux-gnu/spa-0.2/…` or `$HOME/.local/lib/spa-0.2/…`), or
`config =` path in the fragment points nowhere (the node still loads, but
passes audio through — check `active-dsp.json` exists: run any
`eqforge set-profile` to regenerate it).

**Glitches / xruns under load**
`pw-top` shows per-node xrun counts. Remedies: raise the quantum
(`pw-metadata -n settings 0 default.clock.force-quantum 2048`), ensure the
RT host has RT priority (PipeWire/rtkit normally grants it; the systemd unit
sets `Nice=-11` as a fallback), and check `eqforge system status` latency
numbers are sane.

**Latency feels high with video**
Look-ahead limiting costs 1.5 ms (72 frames) by design; the rest is the graph
quantum. Video players compensate automatically via PipeWire latency
reporting. For low-latency monitoring, lower the limiter look-ahead in the
profile (`limiter.lookahead_ms`) or disable the limiter for that profile.

**Audio plays but EQ has no effect**

```sh
eqforge status                      # bypass ON? RT host running?
cat ~/.config/eqforge/active-dsp.json
eqforge show <active-profile> --resolved
```

If the profile is `flat` with no filters, that *is* the effect (transparent).

## Profiles

**`profile invalid: …`** — the message points at the exact JSON path
(`/eq/filters/3/q`). `eqforge validate <id>` lists all issues including
warnings.

**Import fails: "unrecognized EQ format"** — supported: EQForge JSON, AutoEQ
txt/csv, REW txt (incl. BW-oct filters), EqualizerAPO config (Preamp/Filter/
GraphicEQ), plain `freq,gain[,q[,type]]` CSV. For exotic formats, convert to
CSV — three columns are enough.

**Auto-switch doesn't trigger** — check: `auto_switch` enabled (GUI System
tab / config), the device identity actually matches your rule
(`eqforge devices --json` shows every identity key), and the matched profile
resolves (`eqforge validate <id>`). The daemon logs every decision to
`eqforge.log` (search "auto-switch").

**Corrupt config / weird state**

```sh
eqforge daemon stop
rm ~/.config/eqforge/config.json      # settings only; profiles untouched
eqforge daemon start                  # regenerates defaults
```

A corrupt `config.json` is also auto-quarantined (`config.broken.bak`) on
daemon start.

## Files & formats

**`mp3 decoding needs ffmpeg`** — install `ffmpeg` (or `libavcodec`) for
MP3/M4A/OPUS; WAV/FLAC/OGG work without it via libsndfile.

**Output quieter than expected after `apply`** — auto-preamp is doing its
job: a profile with +6 dB boosts gets −6 dB preamp so nothing clips. The
limiter ceiling then trims further on hot material. If you want loudness
back, normalize: `--normalize -16`, or set the profile preamp to `manual`.

**Metadata missing on output** — tag copying needs ffmpeg for some
containers; pass `--no-metadata` to skip, or transcode tags separately.

## Daemon / GUI

**`cannot reach daemon`** — `eqforge daemon stop && eqforge daemon start`.
Stale sockets in `$XDG_RUNTIME_DIR/eqforge/` are auto-cleaned on start.

**GUI shows no meters** — meters come from a running native host
(`eqforge system start` or the SPA filter). Profile editing works regardless.

**Port already in use** — pass `--port 0` (ephemeral, default) or another
fixed port; the current port is always in `$XDG_RUNTIME_DIR/eqforge/gui.port`.

## Getting help

Include in bug reports: `eqforge doctor` output, `eqforge version`,
`eqforge show <profile> --resolved`, the relevant lines from
`~/.local/state/eqforge/logs/eqforge.log`, and `pw-dump | grep -i eqforge`
for graph issues.
