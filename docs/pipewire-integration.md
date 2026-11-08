# PipeWire / Linux Integration

EQForge offers two native real-time deployment styles. Both run the *same*
`libeqforge` chain and read the *same* `~/.config/eqforge/active-dsp.json`.
Pick one; they can coexist but only one should be in your audio path.

## A. SPA filter node (`libeqforge-filter.so`)

A compiled PipeWire plugin implementing the SPA node factory
`eqforge.filter` (1 input port, 1 output port, F32 planar or interleaved,
1–8 channels, any rate). It is hosted by PipeWire itself via
`libpipewire-module-adapter`, so it appears as a regular graph node with:

* correct latency reporting (limiter look-ahead),
* hot-reload of `active-dsp.json` (mtime poll ≈1 Hz, click-guarded),
* zero Python at runtime.

### Install

```sh
make -C pipewire
sudo make -C pipewire install     # → $(pkg-config --variable=libdir libspa-0.2)/spa-0.2/eqforge/
                                  #   Arch/Fedora: /usr/lib/spa-0.2/eqforge/
                                  #   Debian/Ubuntu: /usr/lib/x86_64-linux-gnu/spa-0.2/eqforge/
eqforge system setup              # writes ~/.config/pipewire/pipewire.conf.d/99-eqforge.conf
systemctl --user restart pipewire pipewire-pulse wireplumber
```

The generated fragment (see `packaging/pipewire/99-eqforge.conf`):

```hocon
# PipeWire >= 0.3.5x resolves SPA factories via this table; without it the
# daemon logs "no library for eqforge.filter".
context.spa-libs = {
    eqforge.* = eqforge/libeqforge-filter
}

context.modules = [
    { name = libpipewire-module-adapter }
]

# Persistent nodes are created through context.objects; module args in
# context.modules only register the adapter factory and create nothing.
context.objects = [
    { factory = adapter
        args = {
            factory.name     = eqforge.filter
            node.name        = "eqforge_filter"
            node.description = "EQForge Audio Processor"
            media.class      = "Audio/Sink"
            audio.channels   = 2
            audio.position   = [ FL FR ]
            config           = "/home/you/.config/eqforge/active-dsp.json"
        }
    }
]
```

> **Known limitation (verified against PipeWire 0.3.65):** when hosted by
> `libpipewire-module-adapter` with `media.class = Audio/Sink`, PipeWire treats
> the node's output ports as *monitor* ports and never hands the SPA node an
> output IO buffer - processed audio does not reach the graph (monitor output
> measures silent). The filter therefore loads, configures and hot-reloads, but
> the SPA path cannot currently carry system audio end-to-end. Use the RT host
> path (B) for real processing until the module grows a dedicated
> source/sink node pair.

### Route audio through it

The node shows up as an `Audio/Sink`. Make it the default so applications
play into it, and connect its output to your real speakers:

```sh
wpctl status                       # find "eqforge_filter" under Audio
wpctl set-default <eqforge-id>     # apps now play into EQForge
pw-dump | grep -A2 '"port.name"'   # confirm the actual port names
```

WirePlumber users can automate both steps with a small policy rule (an
example lives in `packaging/wireplumber/50-eqforge.lua.example`).

Verify: `pw-top` should show `eqforge_filter` in the graph while audio plays.

## B. RT host (`eqforge-rt`, JACK client)

A native JACK client — under PipeWire that means `pipewire-jack`, no jackd
needed. It is the most portable option (also works on plain JACK2/jackd) and
the daemon can wire it automatically.

```sh
make -C rthost && sudo make -C rthost install
eqforge system start --manage       # start + wire rt outputs to default sink
eqforge system wire                 # full chain: app streams -> eqforge -> sink
eqforge system status               # latency, rate, telemetry
```

What it does:

* registers `eqforge:in_N` / `eqforge:out_N` ports,
* processes through `libeqforge` in the JACK callback (RT-safe),
* listens on `$XDG_RUNTIME_DIR/eqforge/rt.sock` for JSON commands from the
  daemon: `reload`, `bypass`, `connect`, `status`, `quit`,
* reports look-ahead latency through the JACK latency callback, so
  PipeWire compensates A/V sync properly.

Routing all system audio through it ( PipeWire):

```sh
# one command: rt outputs -> default sink AND every playback stream that
# currently feeds the default sink -> eqforge:in_N (idempotent):
eqforge system wire
# manual equivalent, per app:
pw-link firefox:out_FL eqforge:in_1
```

Streams started *after* wiring are pulled in automatically: the daemon
watches the graph and re-routes them (see below). The SPA path (A) is still
tidier for full system-wide capture; the RT host shines for per-app routing,
JACK workflows and non-PipeWire setups.

## Zero-gap onset (the "first second has no EQ" problem)

Both deployments sit between an application and the speakers, and the
interesting question is not *whether* audio is processed but *from which
sample*. There are three windows in which unprocessed audio can escape:

| Window | Cause | Fix |
|---|---|---|
| 0 – 2 s | the daemon polled the graph every 2 s, so a stream that had been linked straight to the sink was re-routed up to 2 s late | registry events (`pw-dump -m`) instead of polling: `eqforge.system.watch` |
| ~0 – 60 ms | even with instant reaction, the stream exists on the sink *before* anyone can move it | a WirePlumber rule that creates streams on EQForge's inputs: `eqforge system always-on on` |
| ~0 – 500 ms | a node (or the rt host) that started before `active-dsp.json` existed ran as pass-through until the next config poll | the SPA node loads its config at creation and polls for a missing file 10×/s; `eqforge-rt` retries its config ~5×/s until it appears |

`eqforge status` shows which mechanism is live:

```
  watch         : events via pw-dump -m (17 events, 0 restarts)
  always-on     : eqforge_filter (installed)
```

### Event-driven reaction

The daemon subscribes to PipeWire's registry events and runs its rewiring
pass as soon as a `Node` or `Link` appears or disappears. A burst of events
(one playback start produces a node, several ports and links) is coalesced
into a single pass, so the cost per playback start is one `pw-dump` snapshot
plus a few `pw-link` calls - measured at **~30-60 ms** from "the stream
exists" to "the stream is processed", instead of up to 2 s.

The 2 s tick stays as the safety net: it still catches default-sink metadata
changes, a monitor process that died, and anything the event stream cannot
report. Nothing regresses if `pw-dump -m` is unavailable (PipeWire < 0.3.56):
the watcher reports `event_capable: false` and the daemon keeps polling.

### Always-on routing policy (zero, not "small")

A reaction always happens *after* the fact. The only way to make the first
sample processed is for the stream to be created on EQForge's inputs in the
first place, which is WirePlumber's decision:

```sh
eqforge system always-on on --now     # write the drop-in and restart WirePlumber
eqforge system always-on status       # what is targeted, and is it live?
eqforge system always-on off          # back to reaction-only
```

It writes `~/.config/wireplumber/wireplumber.conf.d/60-eqforge-streams.conf`:

```conf
# EQForge always-on routing (generated by `eqforge system always-on on`).
node.rules = [
    {
        matches = [
            { media.class = "Stream/Output/Audio" }
        ]
        actions = {
            update-props = {
                target.object = "eqforge_filter"
            }
        }
    }
]
```

Node name `eqforge_filter` (SPA module) or `eqforge` (rt host) is detected
from the live graph; `--node NAME` overrides it. Guard rails, because a rule
that points at a node nobody owns is how applications end up silent:

* the target node must exist in the graph *before* the file is written -
  otherwise the command fails and changes nothing;
* only `Stream/Output/Audio` is matched: microphones, MIDI, camera audio and
  other filter nodes are untouched;
* WirePlumber < 0.5 does not read `node.rules` from a `.conf` drop-in, so it
  is refused (`--force` overrides) with a pointer to
  `packaging/wireplumber/50-eqforge.lua.example`;
* `off` deletes the file; the daemon's rewiring keeps working exactly as
  before.

Both mechanisms layer deliberately: **policy for the first sample, events for
whenever reality disagrees with policy** (another drop-in shadows the rule, a
device hot-plugs, a player ignores the target).

## Device awareness & auto-switching

The daemon follows the PipeWire graph - event-driven, with a ~2 s poll as the
safety net (`pw-dump`) - and:

1. detects the current default sink and its stable identity
   (name, ALSA card, bluez address, kind…),
2. evaluates matching rules from `config.json` + all profiles'
   `matching.devices`,
3. when **auto-switch** is enabled and a rule matches, it activates that
   profile (hot-reloading the native host) and sends a desktop notification.

Enable in the GUI (System tab) or:

```sh
eqforge daemon …            # running
curl -X POST localhost:PORT/api/auto_switch -d '{"value": true}'
# or config.json: {"auto_switch": true}
```

Hot-plug behavior: when a Bluetooth headset connects and becomes the default
sink, your `bluez_address`-matched profile activates within tens of
milliseconds (graph events; the poll interval is only the fallback). When it
disconnects, the previous sink becomes default again; rules are re-evaluated
on every graph change. If no rule matches, the active profile is left alone
(no surprising switches).

## Application-aware audio

`eqforge streams` lists running streams with their `application.process.binary`
and `media.name`. Profiles can carry `matching.apps` rules. The daemon exposes
them via RPC (`streams`, `apply_rules_now`); automatic per-app *routing* is
deployment-specific:

* SPA path: create one filter node instance per app group and use WirePlumber
  policy rules to link apps to the right node (fragment provided in
  `packaging/wireplumber/`),
* RT path: `pw-link <app>:out_FL eqforge:in_1` per app — the daemon's `wire`
  command automates the common cases.

Per-app profiles with per-app processing chains (multiple filter instances)
are on the roadmap; the identity/rule machinery is already in place.

## Session behavior

* `packaging/systemd/eqforge-daemon.service` — user unit autostarting the
  daemon (GUI server + watcher). Enable:
  `systemctl --user enable --now eqforge-daemon`
* `packaging/systemd/eqforge-rt.service` — user unit for the RT host (written
  by `eqforge system setup`).
* `packaging/eqforge.desktop` — launches `eqforge gui`.
* Sockets live in `$XDG_RUNTIME_DIR/eqforge/` (mode 0600/0700).
* Stale sockets from crashes are detected and replaced on next start.

## Latency accounting

The limiter's look-ahead (default 1.5 ms @48k = 72 frames) is the only
processing latency. It is reported:

* SPA: `SPA_PARAM_Latency` (+ `min_ns/max_ns`),
* JACK: `jack_set_latency_callback`,
* daemon telemetry: `rt.latency_frames`,
* offline renders: compensated automatically (sample-aligned output).

With a 1024-frame quantum at 48 kHz, total added latency ≈ 21 ms + look-ahead;
lower the quantum (`pw-metadata -n settings 0 default.clock.allowed-rates`,
`force-quantum`) if you need tighter monitoring.
