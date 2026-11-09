# GUI Guide

Start the local Audio Studio with:

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

The interface has no JavaScript dependencies, CDN assets, accounts, or cloud
services. Profiles and audio stay on your computer.

## Audio Studio

The dark, responsive studio is organized into three workspaces:

- **Profiles** — searchable preset/custom library, profile activation, A/B
  comparison, new, duplicate, import, export and delete.
- **Analyze** — browser file picker *or* a server-side path, drag and drop,
  loudness/spectrum measurements, advisor quick fixes and target-matched EQ.
- **System** — output selection, device-based profiles, real-time engine
  controls, stream wiring, login autostart, always-on routing, integration
  repair, Doctor and recent logs.

The main area always keeps the important controls within reach: interactive
EQ curve, exact numeric band editing, preamp and dynamics chain, save/revert,
A/B, bypass and live output meters. On narrow screens the workspace becomes a
slide-out drawer and the studio panels stack vertically.

## Profiles

**Activate** — choose a profile in the top bar or click it in the library. The
resolved profile is sent to the running engine immediately with click-guarded
hot reload.

**Edit** — drag band handles, scroll over a handle to change Q, double-click
empty space to add a band, or use **Add band**. The table provides exact
frequency, gain, Q, type and enable controls. The response preview and auto
preamp update as you edit. **Band curves**, **Spectrum**, and the vertical
range can be changed above the graph.

**Save & activate** — persists the edit and hot-reloads the engine. Factory
presets are read-only, so saving one opens a Save-as flow. **Duplicate** makes
an independent, resolved copy; **Save as…** keeps the current document's
inheritance/metadata where applicable. Unsaved changes are marked in amber
and `Revert` restores the activated version.

**Import** — select an EQForge JSON, AutoEQ, REW, EqualizerAPO, or plain CSV
file directly in the browser. Repeating an import creates a numbered copy
instead of silently overwriting your edits.

**Export** — downloads a fully resolved, self-contained `.eqforge.json` file.
**Delete** asks for confirmation and only permits custom profiles. Deleting
an active profile safely falls back to `flat`.

**A/B compare** — load profiles into A and B, then use the slot buttons to
switch instantly. **Bypass** passes audio through unchanged.

## Analysis and automatic EQ

Drop an audio file onto **Analyze**, use **Choose audio**, or expand the path
option for a file already on the machine. Browser uploads are limited to
128 MB, decoded in a temporary directory, and removed after analysis.

The report includes integrated LUFS, loudness range, sample/true peak, crest
factor, duration, channel count and sample rate. The measured spectrum can be
shown behind the main EQ curve. Smart Advisor findings explain headroom,
clipping, resonances and profile/content interactions; available quick fixes
can be applied directly to the editable preview.

Choose **Harman over-ear**, **Flat**, or **Broadcast**, then **Generate &
preview EQ** to add conservative, gain-limited corrective bands. Analysis and
quick fixes never overwrite a profile by themselves: inspect the curve and
use **Save & activate** when satisfied.

## System audio without a terminal

The **System** workspace exposes the operations that previously required
shell commands:

- Change the PipeWire default output.
- Enable per-device profile switching and apply matching rules.
- **Start** / **Stop** the native real-time host.
- **Connect audio streams** once, or enable **Auto-wire streams** to keep new
  and restarted application streams connected.
- Enable or disable **login autostart** for the supervised user services.
- Enable or disable **always-on routing**, which directs new streams through
  EQForge from their first sample when WirePlumber supports it.
- **Install / repair integration files** without overwriting edits silently;
  existing managed-file backups are retained.
- **Run doctor** for native DSP, Python, formats, PipeWire, JACK, real-time
  host and end-to-end rendering checks.
- **View recent engine logs** in a scrollable dialog for startup diagnostics.

Unavailable optional components are reported as readable warnings rather
than fake success states. An error from the real-time host includes its
actionable diagnostic hint in the GUI.

## Meters and shortcuts

When a native host is running, the footer streams momentary LUFS, output
peak, limiter gain reduction and latency over SSE at approximately 10 Hz.
Control, engine and PipeWire indicators show connection state.

| Shortcut | Action |
|---|---|
| <kbd>Ctrl</kbd> + <kbd>S</kbd> | Save and activate |
| <kbd>Space</kbd> | Toggle bypass |
| <kbd>Delete</kbd> | Remove selected band |
| <kbd>?</kbd> | Open the quick guide |
| Double-click curve | Add a band at that position |
| Scroll over a handle | Adjust Q |

## Architecture and privacy

The GUI never touches audio samples in the real-time path: it edits profiles
and talks JSON-RPC to the daemon; the native engine does the sound. The HTTP
face is loopback-only and refuses cross-origin mutations. Everything the GUI
does uses the same profile, analysis and system primitives as the CLI/RPC —
the GUI is a complete client, not a separate audio engine.
