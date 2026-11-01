# GUI Guide

Start it with:

```sh
eqforge gui
```

This launches the daemon (if needed), serves the interface on
`http://127.0.0.1:<port>` (localhost only — nothing is exposed to the
network) and opens your browser. Because it is a local web app it also works
over SSH port-forwarding for headless machines:

```sh
ssh -L 8732:127.0.0.1:8732 host   # then browse to the URL eqforge prints
```

## Layout

```
┌─────────────────────────────────────────────────────────────────────────┐
│ top bar: [A|B slots] [profile ▾] [Bypass]            ● daemon ● rt ● pw │
├───────────────┬─────────────────────────────────────────────────────────┤
│ sidebar       │  EQ canvas                                              │
│  · Profiles   │   · green = total response (incl. preamp)               │
│  · Analysis   │   · blue ghosts = individual bands (selected = bright)  │
│  · System     │   · gray = measured spectrum of the analyzed file       │
│               │  drag band: freq/gain · wheel: Q · dbl-click: add       │
│               │  Del: remove selected                                   │
│               ├─────────────────────────────────────────────────────────┤
│               │  band table (numeric editing, enable toggles)           │
│               │  chain strip: Preamp(auto/manual) · Crossfeed ·         │
│               │               Compressor · Limiter  (click = on/off)    │
├───────────────┴─────────────────────────────────────────────────────────┤
│ meters: momentary LUFS · output peak · limiter gain · latency · preamp  │
└─────────────────────────────────────────────────────────────────────────┘
```

## Common tasks

**Switch profile** — top-bar dropdown, or click any profile in the sidebar.
Activating a profile pushes it to the running engine immediately (hot reload,
click-guarded).

**A/B compare** — load two profiles into slots A and B (dropdown while the
slot is selected, or `eqforge set-profile X --slot B`), then click A/B (or
`eqforge ab`). Switching is instant and sample-smooth.

**Bypass** — top-bar button or <kbd>space</kbd>. Passthrough is click-free.

**Edit a curve** — drag bands on the canvas or type exact values in the band
table. The green curve and the auto-preamp readout update live. When you edit
a *builtin* profile, **Save** offers **Save as…** (builtins are read-only);
the copy becomes a user profile that shadows the builtin.

**Analysis** — Analysis tab → enter a path to an audio file on the same
machine → **Analyze**. You get the full report (LUFS, LRA, true peak, crest,
bands), the spectrum drawn behind the EQ curve, and the advisor findings with
their hints.

**Devices & auto-switch** — System tab shows the PipeWire sink list (with
kind classification), the running application streams, and the RT engine
state. Enable **Auto-switch profile per device** to let matching rules in
your profiles pick the right EQ when the default output changes (e.g.
Bluetooth headphones connecting).

**Live meters** — when a native host is running (`eqforge system start` or
the SPA filter), the footer shows momentary LUFS, output peak, limiter gain
reduction and current latency, streamed over SSE at ~10 Hz.

## Notes

* The GUI never touches audio: it edits profiles and talks JSON-RPC to the
  daemon; the native engine does the sound.
* Unsaved edits are marked with the amber dot; switching profiles asks for
  confirmation.
* Everything the GUI does is also available via CLI/RPC — the GUI is a client,
  not a special case.
