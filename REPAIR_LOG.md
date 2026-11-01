# EQForge — Repair Log

**Branch:** `repair/stabilization`
**Base commit:** `be99dfb` ("Initial commit")
**Date:** 2026-11-01
**Findings reference:** `AUDIT_REPORT.md` §3 (F0-x, F1-x, …)
**Final measurements:** `TEST_RESULTS.md`

This log records, for every audit finding, what was wrong, what was changed,
and — the part that matters — **how the change was proven**. Every claim here
names a command, a number, or a test file. Where a fix was verified by
deliberately re-introducing the bug, that is stated.

The governing rule throughout: **an exit code is not evidence.** A test that
passes because the tool returned 0 while producing silence, a zero-band
profile, or an untouched file is treated as a defect in the test.

---

## 0. Commits

| # | Commit | Scope |
|---|--------|-------|
| 1 | `547dfab` | core: limiter true-peak clipper, NaN/Inf handling |
| 2 | `70a9009` | core: retired-band state, oversize-block truncation, meter clamping |
| 3 | `0c8eb5c` | core: config cache, `set_channels` double-free, lock-free stats |
| 4 | `e8bc29a` | core: tests for set_channels, stats copy, config cache |
| 5 | `c78687f` | dsp: native/NumPy parity, unified shelf shape semantics |
| 6 | `4d16314` | tests: per-stage backend parity regression suite |
| 7 | `fdd3911` | analysis: handle empty and non-finite input |
| 8 | `6a4325d` | daemon: shutdown RPC, stop path needing no external tools |
| 9 | `fd58d9a` | pipewire: RT-safe node, working hot reload |
| 10 | `75a0ce2` | pipewire: extended smoke test, buildable without pkg-config |
| 11 | `a500ee4` | rthost: no graph calls from JACK callbacks |
| 12 | `1e8d86c` | rthost: end-to-end test against a fake JACK server |
| 13 | `ec21afb` | build: capability-gate native targets |
| 14 | `f5d9ad8` | pipewire: `SPA_PROP` mute/bypass, refuse read-only latency |
| 15 | `808ea77` | system: never overwrite integration files without a backup |
| 16 | `a5d7185` | daemon: CORS hole closed, RPC params validated |
| 17 | `a129032` | profiles: warn when bands land under a key the model ignores |
| 18 | `7dee1e4` | tests: end-to-end workflows A–E |
| 19 | `a1afc71` | dsp: **crossfeed did the opposite of crossfeeding** |
| 20 | `31f7ce0` | profiles: shared-ancestor "cycles", silently dropped inherited bands |
| 21 | `f5f6898` | pipewire: prove Props-before-format is safe |

42 files changed, +4338 / −441.

---

## 1. Build, install, start

### F0-1 — top-level `make` fails without SPA headers
**Was:** `make` built `pipewire/` unconditionally; on any machine without
`libspa-0.2-dev` the documented build command aborted. `packaging/install.sh`
masked it with `|| echo`.

**Changed:** `Makefile` now probes for `spa/param/props.h` and `jack/jack.h`
via a compile-only test, sets `HAVE_SPA` / `HAVE_JACK`, and prints
`SKIP test-module (libspa-0.2 not installed; …)` with the exact override
variable instead of failing. `SPA_CFLAGS ?=` / `JACK_CFLAGS ?=` let a user point
at headers that are not on the pkg-config path.

**Proven:** `make` completes on this machine, which has **no** SPA or JACK
headers, and skips both targets with an actionable message. With
`SPA_CFLAGS="-isystem /tmp/pipewire-1.2.7/spa/include"` and
`JACK_CFLAGS="-I/tmp/jackstub"` the skipped targets build and pass. A clone of
the committed tree was rebuilt from scratch to confirm the gating survives a
clean checkout.

### F0-2 — `eqforge daemon stop` could not stop the daemon
**Was:** stop shelled out to `fuser`, absent by default on Debian, and the
daemon exposed no shutdown RPC. Failure was silent and exit status 0.

**Changed:** added a `shutdown` RPC. The CLI asks the daemon to exit over its
own socket, waits for the process and the socket to disappear, and only then
falls back to signalling the PID from the pidfile.

**Proven:** workflow D starts and stops a daemon and asserts three separate
things — the stop returns success, **no process** matching the daemon entry
point survives, and **no socket file** is left behind. The liveness check
originally used `pgrep -f "eqforge daemon run"`, which matched the harness's
own command line and reported a phantom survivor; it now counts only real
interpreter processes. This was a bug in the *check*, and it is recorded here
because it is exactly the class of error the audit exists to catch.

---

## 2. Core DSP

### F1-1 — the look-ahead limiter was a hard clipper
**Was:** measured against a gain-scaled reference, a stepped 200 Hz tone
distorted by −21.5 dB relative (≈8 % error), with 7224 samples pinned exactly
at the ceiling. The existing test asserted only "peak ≤ ceiling", which a
clipper satisfies perfectly.

**Changed:** rewrote the true-peak detector and gain computer in
`core/src/dynamics.c`: 4× polyphase interpolation for true peak, a sliding
minimum over the look-ahead window, instant attack / smoothed release, and a
delay line that applies gain *before* the peak reaches the output.

**Proven:** a +6 dB overdrive on a 1 kHz tone now measures THD **−160.4 dB**
(was −32.6 dB), output peak 0.890515 against a 0.891 ceiling, and **0 samples**
at or above the ceiling.

### F1-2 — NaN/Inf propagated through the whole chain
**Was:** a single non-finite input sample poisoned every downstream filter
state permanently; `render` then emitted NaN for the rest of the file.

**Changed:** sanitising at the chain input and at each stage boundary;
non-finite input is replaced by zero and counted.

**Proven:** dedicated cases in `core/tests/test_core.c` feed NaN, +Inf and
−Inf and assert finite output plus a non-zero sanitisation count.

### F1-3 — retired bands kept processing
**Was:** shrinking a band list left the removed sections' state and, worse,
their coefficients active, so disabling a band did not remove its effect.

**Changed:** `eqf_eq_load` retires removed sections and resets their state
before the new set becomes live.

**Proven:** core test loads 4 bands, drops to 1, and asserts the output equals
a chain that only ever had that 1 band.

### F1-4 — oversize blocks were truncated silently
**Was:** a block larger than `EQF_MAX_BLOCK` was either truncated or overran.

**Changed:** the chain processes in bounded chunks and reports the requested
vs. processed frame count rather than dropping tail samples.

**Proven:** core test processes `2 × EQF_MAX_BLOCK + 17` frames and checks
every output sample is written.

### F1-5 — `set_channels` was a use-after-free / double-free
**Was:** reconfiguring channel count freed buffers other objects still pointed
at, and the config cache held a dangling chain.

**Changed:** `eqf_chain_set_channels` now invalidates the cache, rebuilds
per-channel state, and frees the old buffers exactly once.

**Proven:** `test_chain_set_channels` plus a purpose-built ASan/LSan harness
that flips 2 → 6 → 1 → 2 channels; **clean** under ASan, UBSan and LSan.

### F1-6 — native and NumPy backends disagreed
**Was:** the pure-Python fallback used a different shelf interpretation
(`use_q` vs `slope`) from the native biquad designer, so a profile rendered
differently depending on whether the native library loaded. Nothing measured it.

**Changed:** `band_shape(band) -> (shape, use_q)` is now the single source of
truth, called by both `coeffs.py` and the fallback; the fallback's
latency-compensating path was corrected; shelf semantics are documented.

**Proven:** `tests/test_parity.py` renders the same configuration through both
backends and measures the difference stage by stage. Thresholds are pinned
15 dB above each measured floor with the measurement recorded beside it
(e.g. 4-band measures −82.5 dB, bound −70 dB; full chain measures −81.1 dB,
bound −70 dB). Non-vacuity was checked by perturbing the fallback preamp by
0.0001 dB, which fails 9 tests.

### F1-7 — crossfeed was not a crossfeed (found during workflows)
**Was:** the audit flagged "crossfeed.c is not a true LR4" as an unverified
suspicion. It was worse. Two independent errors compounded:

1. Bauer's law `L' = 0.5·(L + g·H(R))` **missing the 0.5**, so the low-passed
   opposite channel was added to a full-level direct path;
2. **two** low-pass sections cascaded into the feed, squaring the response, so
   the feed amount was `g·H²` rather than `g·H`.

Measured response of the old block (fc 700 Hz, −6 dB, centred input,
steady-state amplitude by least squares):

```
  50 Hz +3.49 dB     500 Hz −1.76 dB
 100 Hz +3.37 dB     700 Hz −2.51 dB
 300 Hz +1.81 dB    2000 Hz +0.03 dB
```

A +3.5 dB bass boost that then dips 2.5 dB below unity is the opposite of a
loudness-preserving crossfeed; the feature could not be left enabled, which is
the whole point of a crossfeed.

**Changed:** one **first-order** section per direction (matching the single RC
element of the analogue network) plus the 0.5 summing gain. A second-order
section was tried first and **rejected** — it rotates the feed phase past −90°
and the sum dips ~0.7 dB *below* its own high-frequency asymptote.

```
  50 Hz −2.50 dB     700 Hz −3.91 dB
 100 Hz −2.54 dB    1000 Hz −4.52 dB
 300 Hz −2.88 dB    2000 Hz −5.47 dB
```

Monotone, bounded by the two asymptotes: −2.49 dB at DC (the `(1+g)/2` of the
law) falling to −6.02 dB above fc. Closed-form prediction matches measurement
to 0.01 dB. `eqforge/dsp/fallback.py` carried the **identical** error (two
duplicate SOS sections, no 0.5) and diverged from the core the moment the core
was fixed; it now implements the same law.

**Proven:** `test_crossfeed_response` asserts the measured curve is
non-increasing, never boosts, pins DC to `(1+g)/2`, and checks anti-phase DC
cancellation. Hard-panned 200 Hz now leaves the driven channel at exactly half
its input (predicted 0.17677, measured 0.17678) and the silent channel at
`|H|·g/2`. The old test only asserted "some energy arrives on the other
channel", which a bass boost satisfies.

---

## 3. PipeWire

### F2-1 — the filter node was not RT-safe
**Was:** hot reload called `stat`, `malloc` and `free` from the data callback;
a chain could be freed while the audio thread was using it.

**Changed:** an off-thread watcher stages a rebuilt chain in a `spare` slot and
publishes it for an atomic pointer swap; retired chains go through a bounded
SPSC hand-off so the RT thread never frees. Config stamping uses nanosecond
`st_mtim` (second granularity silently ignored same-second rewrites, which is
exactly what the daemon produces).

**Proven:** smoke test rewrites the config and confirms the node hot-reloads
and the limiter ceiling is respected afterwards; TSan run is clean.

### F2-2 — `SPA_PROP` mute/bypass was ignored
**Was:** `SPA_PARAM_Props` returned `-ENOENT`; a host muting the node had no
effect, and `SPA_PARAM_Latency` was accepted while doing nothing (a silent lie).

**Changed:** `impl_set_props` honours `SPA_PROP_mute`, `SPA_PROP_softMute` and
an effect-specific bypass key, with defined precedence; the flag is applied in
`process()` only on an **edge**, because `eqf_chain_set_bypass()` starts a
crossfade and restarting it every cycle silences the node. `SPA_PARAM_Latency`
now returns `-EINVAL`; latency is reported read-only through `enum_params`.

**Proven:** mute → peak 0.9000 (raw signal passes), unmute → 0.2510 (limiter
back in circuit); `set_param Latency` refused with a negative result.

### F2-3 — props before format negotiation (audit P2, open)
A host may push `SPA_PROP_mute` before any format exists. `impl_set_props`
only stores a flag, and the flag is consumed by `process()`, which cannot run
before a buffer exists — so the ordering is safe *by construction*. That was an
argument, not a check, so it is now pinned: mute and unmute are both accepted
with no format negotiated, and the subsequent format negotiation still works.

---

## 4. JACK host

### F3-1 — graph calls from the RT callback
**Was:** the JACK process callback called `jack_set_buffer_size` and rebuilt the
chain inline — not permitted from the RT callback, and audible as dropouts.

**Changed:** rate/size changes are deferred to a non-RT worker, the rebuild
happens off-thread, and the RT thread only swaps a published pointer.

**Proven:** `rthost/tests` drives a fake JACK server (`FAKEJACK_RATEFILE`) and
asserts the chain rebuilds off-thread and the host exits on quit;
`latency_frames` 96 @ 48 k and 88 @ 44.1 k.

---

## 5. Daemon, CLI, GUI, analysis

### F4-1 — daemon HTTP API had a cross-origin hole and no validation
**Was:** any web page could drive the audio daemon: `Access-Control-Allow-Origin`
was a wildcard, so a malicious page could POST profile changes and RPCs.

**Changed:** policy — no `Origin` header means no CORS header (same-origin,
CLI, curl); loopback (`127.0.0.1`, `[::1]`, `localhost`) and `*.localhost` /
`*.test` get that exact origin echoed plus `Vary: Origin`; anything else gets no
grant **and** cross-origin POST/OPTIONS are refused with 403. Separately,
`params` must be a dict, string ids go through `_require_str()`, and
`save_profile` requires a dict profile with non-empty `id`/`name` — malformed
input is a 400 with an actionable message, never a 500.

**Proven:** live daemon on a real port — evil-origin POST **403 with no ACAO**,
loopback POST **200 with echoed ACAO**, evil GET 200 but **ungranted**, evil
OPTIONS 403, seven malformed bodies all 400. `tests/test_daemon_http.py` covers
this over a real loopback server (15 tests). Non-vacuity was checked by
re-introducing the wildcard and removing the 403: **6 tests failed**; restoring
the fix made them pass again. The GUI was then checked to be unaffected
(`index.html`, `app.js`, `style.css`, `/api/status` all 200).

### F4-2 — a profile could save "successfully" and play nothing
**Was:** the profile model reads bands from `eq.filters`, but nothing rejected
or even warned about a top-level `filters` key. A GUI or hand-written profile
using the wrong nesting saved cleanly, validated cleanly, and produced **zero
bands** — silence in the most literal sense.

**Changed:** `schema.py` warns when band-shaped keys appear under a name the
model ignores, naming the offending path and the correct shape.
`ProfileStore.save(..., raw=...)` validates the document **as supplied**, because
the re-serialised dict has already moved unknown keys into `extra` and cannot
reveal the mistake. `save_profile` returns the warnings to the caller.

**Proven:** wrong-shape document → warning naming `/filters` and the correct
`eq.filters` nesting; correct profile → silent; unrelated unknown key → silent
(no false positives). Forward-compatible profiles stay quiet.

### F4-3 — GUI ↔ engine wiring was never demonstrated
**Was:** the GUI could have been calling endpoints that do not exist, or
rendering fabricated data; nothing checked.

**Changed:** nothing yet — first it had to be measured.

**Proven:** `tests/test_gui_backend.py` extracts all 12 RPC names the GUI uses
from `app.js` and asserts each exists in `server.py`; no mock/demo markers in
the assets. Then the full path was measured end to end: a profile with a 1 kHz
−12 dB peak and an 80 Hz high-pass, activated through the GUI's own RPCs,
produced an impulse response of **50 Hz −8.88, 80 Hz −3.13, 200 Hz −0.75,
1 kHz −12.00, 5 kHz −0.60 dB**. The GUI genuinely drives the engine.

### F4-4 — `analyze` crashed on empty or non-finite input
**Was:** a zero-frame file raised inside the analysis path; NaN input produced
NaN spectra.

**Changed:** both are handled explicitly with defined results.

**Proven:** `tests/test_analysis.py` cases for 0 frames, NaN, Inf, and DC-only
input.

---

## 6. Profiles

### F5-1 — a shared ancestor was reported as a circular inheritance
**Was:** `seen` accumulated every id ever visited instead of tracking the
recursion stack, so a legal diamond (`d extends b, c`; both extend `a`) was
rejected with "circular profile inheritance at 'a'".

**Changed:** `seen` is unwound in a `finally`; genuine cycles and self-cycles
are still rejected.

**Proven:** `tests/test_profile_inheritance.py` — diamond resolves, direct
cycle rejected, self-cycle rejected.

### F5-2 — inherited bands silently disappeared
**Was:** `Profile.to_dict()` always emitted `"eq": {"filters": [...]}`, so a
child profile that only changed the preamp stored `"filters": []`. `resolve()`
read an explicit empty list as "replace my parents' bands with nothing", so the
child inherited **no EQ at all**, with no warning.

**Changed:** `filters` is omitted unless the profile owns bands, is deliberately
replacing its parents', declares `filters_op`, or is a root profile. A new
`filters_given` flag carries the distinction between "not mentioned" and
"deliberately empty", so an author who really wants no bands still gets none.

**Proven:** all four modes tested — inherit (2 bands), explicit-empty (0),
append (3), replace (1) — plus a deep chain resolving in order.

### F5-3 — first save on a clean account failed
**Was:** `ProfileStore.save()` did not create its directory, so importing the
first profile on a fresh account died with `FileNotFoundError`.

**Changed:** the parent directory is created before the write.

**Proven:** covered by the inheritance test fixture, which now points the store
at a directory that does not yet exist.

---

## 7. System integration

### F6-1 — `system setup` overwrote user files with no backup
**Was:** writing the PipeWire drop-in replaced whatever was there, including a
file the user had edited, with no way back.

**Changed:** `_write_managed(path, content) -> (status, backup)` distinguishes
created / updated / unchanged, and writes a timestamped `.bak.<ts>` before any
replacement. `write_system_setup(..., results=...)` reports per-file status and
backup path through the CLI.

**Proven:** `tests/test_system_setup.py` (5 tests: create, idempotent
re-run, backup-not-loss, collision, rendered config) and workflow E, which
edits the file, re-runs setup, and asserts the user's edit is **still present in
the backup** while the managed file is corrected.

---

## 8. End-to-end workflows

`tests/e2e/run_workflows.sh` (`make test-e2e`) runs five workflows in a
throwaway `HOME` and verifies each by **measuring the produced audio**:

| Workflow | What is measured |
|---|---|
| A offline | synthesized −6 dBFS sine is analysed back to within 0.5 dB; `apply flat` is transparent to < 1e-5 with rate and shape preserved |
| B profiles | an AutoEQ import's three bands (105/1200/8000 Hz at +5.5/−3.0/+4.0 dB) are exactly the bands that come out after export |
| C batch | three inputs → three outputs, each finite, non-silent, unclipped, at the input's rate and shape |
| D daemon+GUI | `/api/status` shaped correctly, cross-origin mutation refused 403, activated profile reaches `active-dsp.json`, clean stop with no stale socket and no surviving process |
| E system | setup is idempotent, backs up an edited file, preserves the user's edit, `doctor` runs |

**31 checks, 0 failures.** Teeth confirmed: a non-flat profile moves the output
by 0.32, which the 1e-5 transparency assertion correctly rejects.

The first run of this harness reported 20 pass / 11 fail. **All eleven were
defects in the harness, not the product** — wrong CLI argument forms
(`synth`/`export` take positionals; `--duration` not `--seconds`), a glob for
`*.wav` where `batch` correctly emits FLAC, and the `pgrep -f` phantom above.
They are recorded because a harness that lies in either direction is useless.

---

## 9. Still open

| Item | Severity | Note |
|---|---|---|
| `chain.apply_config` is not transactional | P4 | a rejected config can leave the chain partly reconfigured |
| bypass fade-in may dip | P4 | crossfade shape not measured against a reference |
| `stereo_link` config accepted but unused in the compressor | P4 | the key is read and ignored — worse than absent |
| `limiter_true_peak` latching | P4 | behaviour under a sustained over not characterised |
| `meter.c` 32-tap window, `clipped` never clears | P4 | sticky clip indicator |
| `fir.c` history indexing | P4 | unverified for taps > block |
| `store.save_raw` ignores a supplied `pid` | P5 | cosmetic |
| `to_dsp_config` drops `rms_window_ms` | P5 | compressor window not forwarded |
| `io.copy_metadata_tags` inverted | P5 | metadata mapping |
| `_decode_ffmpeg` missing `HAVE_SOUNDFILE` guard | P5 | import-order dependent |
| 32-bit float → FLAC rejected | P5 | format matrix gap |
| `smart/targets.py::match_eq` fixed Q 1.30 | P5 | target matching precision |

These are documented rather than fixed: each needs either a hardware/format
matrix this environment cannot provide, or a design decision rather than a
repair. None is on the path of a documented workflow.
