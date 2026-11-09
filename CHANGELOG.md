# Changelog

All notable changes to EQForge. Versions follow the package version in
`eqforge/version.py` and `core/include/eqforge/version.h` (kept in sync).

## Unreleased — terminal-free Audio Studio

### Added

* A redesigned, responsive GUI with a searchable profile library, clearer EQ
  workspace, signal-chain cards, live status/meters, dialogs and shortcuts.
* Real browser workflows for profile import/export/duplicate/delete, audio
  upload/analysis, advisor quick fixes and target-matched corrective EQ.
* GUI controls for output selection, RT start/stop, stream wiring, auto-wire,
  login autostart, always-on routing, integration repair, Doctor and logs.

### Tests

* `tests/test_gui_actions.py` covers the new profile, upload, target matching,
  safe-delete, RT-launch and diagnostics paths.

## 1.1.0 — processed from the first sample

### Fixed

* **The first moment of playback bypassed the processor.** Three separate
  windows could let unprocessed audio reach the speakers, and users heard them
  as one symptom: "the first second has no EQ, then the effects snap in".
  * The daemon learned about the audio graph only on a 2 s poll, so a stream
    that WirePlumber had linked straight to the default sink was re-routed up
    to two seconds late. It now follows PipeWire registry events
    (`pw-dump -m`, coalesced) and reacts in ~30–60 ms. New module:
    `eqforge/system/watch.py`; the poll remains as the safety net.
  * A stream still exists on the sink before anyone can move it. `eqforge
    system always-on on` writes a WirePlumber `node.rules` drop-in that creates
    new playback streams on EQForge's inputs, which removes the window
    completely. New module: `eqforge/system/always_on.py`.
  * The SPA filter node and `eqforge-rt` both ran as pass-through when they
    started before `active-dsp.json` existed (a normal boot ordering). The
    filter node now builds its chain at node creation and polls for a missing
    config 10×/s; the rt host retries its config ~5×/s and says so in its log.
* The daemon's shutdown no longer waits out one poll interval: `stop()` wakes
  the watcher loop.

### Added

* `eqforge system always-on on|off|status` (`--node`, `--now`, `--force`,
  `--json`) — routing policy with guard rails: the target node must be live
  before the rule is written, only `Stream/Output/Audio` is matched, and a
  WirePlumber that cannot read `node.rules` is refused instead of obeyed.
* `eqforge status` reports how the graph is followed (`graph events: events …
  | polling only`) and whether always-on routing is active.
* `scripts/eqforge-update.sh` — one script that installs **and** updates from
  GitHub: private venv (`--system-site-packages`), per-revision source
  directories, native backup of the previous build, `--check`, `--dry-run`,
  `--ref`, `--channel`, `--source-dir`, `--editable`, `--rollback`,
  `--uninstall [--purge]`, `--self-test`, and automatic stop/restart of the
  systemd user units around the swap.
* `eqforge_chain_channels()` in the C API, so a host can refuse to run a chain
  built for a different channel layout.
* `docs/quickstart-fa.md` (Persian quick start), zero-gap section in
  `docs/pipewire-integration.md`, events/always-on notes in `docs/cli.md`,
  `docs/installation.md` and `docs/troubleshooting.md`.

### Changed

* `core/Makefile` tracks header dependencies (`-MMD -MP`): bumping a version
  header no longer leaves stale objects behind, which used to make
  `make -C core test` fail with a version mismatch until `make clean`.
* The install order in `packaging/install.sh` is unchanged, but its header now
  points at `scripts/eqforge-update.sh` for anything outside a checkout.

### Tests

* `tests/test_watch.py` — prompt delivery, burst coalescing, irrelevant events
  ignored, callback exceptions survived, missing/dead monitors degradable.
* `tests/test_event_reaction.py` — an event wakes the daemon and produces a
  rewiring pass in well under the poll interval; status exposes the channel.
* `tests/test_always_on.py` — rule content, idempotency, backup on edit,
  refusal without a live node, refusal on old WirePlumber, `off` semantics,
  CLI wiring.
* `tests/test_updater.py` — `--self-test`, `--dry-run` writes nothing, `--check`
  on an empty home, state roundtrip, error paths.
