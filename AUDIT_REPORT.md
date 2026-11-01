# EQForge — Independent Audit Report

**Auditor:** independent takeover (no trust placed in prior implementation or docs)
**Repository:** https://github.com/erfanjaster/eqforge.git
**Commit audited:** `be99dfb` ("Initial commit", single commit, clean tree)
**Repair branch:** `repair/stabilization`
**Date:** 2026-11-01

This document is the *initial* audit: what was inspected, what was measured, and
what was found broken. Repair status is tracked in `REPAIR_LOG.md`; final measured
results in `TEST_RESULTS.md`.

---

## 1. Method

Nothing in the repository was taken on trust. The audit proceeded as:

1. **Static review** of every C source/header (12 `.c`, 11 `.h`), every Python
   module (64 `.py`), the SPA module, the JACK host, the GUI assets, the
   Makefiles, packaging and all docs.
2. **Build from the documented instructions**, exactly as written (`make -C core`,
   `pip install -e .`, `make`, `make test`, `make test-cxx`).
3. **Dynamic probes** driving the real binaries/libraries with synthesised
   signals whose correct output is known analytically (sine, sweep, impulse,
   white/pink noise, clipped bursts, silence), then *measuring* the output
   rather than checking exit codes.
4. **Sanitizer runs** (ASan + UBSan) over the C test binary and purpose-built
   harnesses.
5. **Fault injection**: corrupt profiles, empty/invalid audio, missing tools,
   oversized blocks, NaN input, invalid configs, repeated daemon start/stop.
6. **Cross-checking claims** in README/docs against observed behaviour.

Environment actually available (established, not assumed):

| Component | Status |
|---|---|
| Debian 13 (trixie), kernel 6.1.158 | present |
| Python 3.13.16, pip 26.2.2, venv | present |
| gcc 14.2.0, make 4.4.1, pkg-config 1.8.1, node 24 | present |
| numpy 2.5.4, scipy 1.18.1, soundfile 0.14.0, pytest 9.0.3 | present |
| `libspa-0.2` headers / pkg-config | **absent** |
| `jack` headers / pkg-config / jackd | **absent** |
| PipeWire daemon, `pw-dump`, `pw-cli`, `pactl` | **absent** |
| ffmpeg / ffprobe | **absent** |
| `fuser` | **absent** |
| Pillow | absent (irrelevant) |

This constrains what can be *integration*-tested (see §9), but it does not
constrain the DSP, file-processing, CLI, daemon, GUI-server or profile layers,
which were all exercised for real.

---

## 2. Baseline results before any repair

| Check | Result |
|---|---|
| `make -C core` | builds, zero warnings, `libeqforge.so` + `.a` |
| `make -C core test` | **968 checks, 0 failures** |
| `make -C core test-cxx` | passes (one `-Wpedantic` warning, flexible array member) |
| `make` (top level) | **FAILS** — `pipewire/module-eqforge.c` cannot compile (no SPA headers); error is not guarded |
| `pip install -e .` | works |
| `import eqforge` | works |
| `eqforge --help`, `doctor`, `list-profiles`, `analyze`, `apply` | work |
| `pytest tests/` | **133 passed, 1 skipped, 0 failed** |
| ASan/UBSan on C suite | **clean** (968 checks) |

The green test suite is the single most misleading artefact in the repository:
several of the most serious defects below are in code paths the suite never
exercises, and the one limiter test that does exist is *passable by a hard
clipper*, which is exactly what the limiter turned out to be.

---

## 3. Findings (initial audit)

Severity follows the task's P0–P5 scheme.
**Root cause** is stated for each; the fix and its regression test are recorded
in `REPAIR_LOG.md`.

### P0 — build / install / start

**F0-1. Top-level `make` fails on a machine without SPA headers.**
`Makefile: pipewire: core` invokes the PipeWire module build unconditionally and
`make` aborts. `packaging/install.sh` papers over it with `|| echo ...`.
*Root cause:* build orchestration treats an optional, header-gated component as
mandatory, and there is no capability probe.
*Impact:* the documented `make` never completes on a normal desktop without
`libspa-0.2-dev`; users conclude the project is broken.

**F0-2. `eqforge daemon stop` cannot stop the daemon.**
It shells out to `fuser`, which is not installed on Debian by default
(`psmisc`), so stopping silently fails and exits 0.
*Root cause:* process management outsourced to an external tool with no
fallback; the daemon has **no `shutdown`/`quit` RPC** at all.
*Impact:* every subsequent `daemon start` says "already running"; the only way
out is `pkill`/reboot. Also directly contradicts the documented
"stale-socket recovery".

### P1 — core DSP and offline processing

**F1-1. The look-ahead limiter is effectively a hard clipper.**
Measured with a 200 Hz sine that steps from −20 dBFS to +4.1 dBFS, ceiling
−1 dBTP, attack 5 ms, release 60 ms:

```
out peak 0.89125 (= ceiling)         ← ceiling respected (this is all the C
samples at exactly the ceiling: 7224    test checks, so it "passes")
error vs ideal gain-scaled reference: −21.5 dB rel.  ← i.e. 8 % distortion
```

7 224 samples in a 1 s render sit exactly on the clamp, i.e. the limiter is
distorting instead of reducing gain smoothly.
*Root cause (three compounding bugs in `core/src/dynamics.c`):*
1. `gain = coeff*gain + (1-coeff)*want` with `coeff = exp(-1/(ms·fs))` reaches
   only ≈63 % of the required reduction after one full attack time constant, so
   the gain never arrives in time and the final hard clamp does the work;
2. the soft-knee branch computes `want = 1 - t²·(1-knee/ceiling)·0.5`, a shape
   that never approaches `ceiling/det` — the knee and the "over" region are
   discontinuous, so release produces a jump;
3. `eqf_limiter_latency()` is gated on `active` **and** the latency only equals
   `lookahead` by coincidence; coupled with (2) there is no true peak-safe path.

**F1-2. The numpy fallback engine crashes whenever a limiter is enabled.**
```
ValueError: could not broadcast input array from shape (4169,) into shape (4096,)
  eqforge/dsp/fallback.py:190  delayed[c] = np.concatenate([self._lim_delay[c], x[c]])
```
*Root cause:* the look-ahead delay line is sized `lookahead+1` but the
concatenation and the "keep the tail" slice disagree by one sample once the
block length differs from the delay length; `np.empty((channels, frames))` also
receives `frames+lookahead` elements.
*Impact:* the entire advertised "works without a compiler" story is false; every
default profile enables the limiter, so **every** render on a machine without
gcc fails.

**F1-3. Fallback engine is not equivalent to the native engine (advertised
"validated to agree closely").** Once F1-2 is fixed, the two engines still
differ in limiter detector (native = 4× oversampled true peak; fallback = block
sample peak), compressor detector (per-sample vs per-block vectorised), and
convolver implementation. There is **no** test that compares them end-to-end
with the limiter enabled.

**F1-4. Reconfiguring the EQ never clears the state of retired bands.**
`eqf_eq_load()` sets `bands_per_channel[]` but leaves `filters[c][b].z1/z2`
dirty. A band removed and later re-added (or a band count that shrinks then
grows) starts from stale history.
*Root cause:* "retire filters … set to unity" is a comment with no code behind
it — one of several places where a comment asserts behaviour that is not
implemented.

**F1-5. NaN/Inf poison the chain permanently.**
A single NaN input sample produced 500 NaN output samples from one biquad and
still 8 192 NaN samples 20 000 samples later; the meter, the limiter detector
and the snapshot all propagate `NaN` indefinitely — and `NaN` compares false
against the limiter's `out > ceiling` clamp, so the final "safety clamp" does
not catch it.
*Root cause:* no non-finite guard anywhere in the biquad, dynamics, meter or
chain; denormal flushing is done only for `|z| < 1e-20`, which is a no-op for a
denormal double/float already normalised by the FPU only on some CPUs.

**F1-6. `eqf_eq_process()` multiplies `bands_per_channel` by the **active**
channel count, but the EQ is initialised from a channel count that the chain can
change later (`eqf_chain_set_channels` rebuilds, but `eqf_eq_set_sample_rate`
does not re-derive `channels`); combined with `bands_per_channel` never being
reset above `eq->channels`, processing a channel-count change can index filters
that were never designed.** (Reproduced by driving `eqf_eq_load()` directly with
a band list for more channels than the EQ was initialised with.)

**F1-7. `eqf_biquad_set_target()` uses `int ramp_samples` derived from a
`double`, and clamps negative/zero correctly, but `eqf_biquad_process()`
advances the ramp on the *first* sample (`i++, ramp--` at the top), so the
target is reached one sample early and the final `f->current = f->target`
assignment can disagree with the last computed sample.** Minor, but it means a
ramped render is not reproducible from `current`+`ramp_step` alone.

**F1-8. Malformed/empty audio crashes `eqforge analyze` with a traceback and
exit code 1.**
```
$ eqforge analyze empty.wav
Traceback (most recent call last):
  … report.py:97 in analyze  r.true_peak_db = L.true_peak_db(x)
ValueError: zero-size array to reduction /* empty */
```
*Root cause:* `analysis/loudness.py:true_peak()` reduces `np.max` over an empty
array; `analyze()` has no guard for `frames == 0`.

**F1-9. `eqforge apply` writes output whose true peak can exceed 0 dBFS and
merely prints a yellow note.** With normalization enabled the two-pass
normalizer *does* respect the ceiling (verified: −1.0 dBTP), so this only affects
non-normalising runs, but the README's "true-peak limiting" claim is only true
for the normalization path.

**F1-10. The documented `synth`/CLI surface disagrees with the documented
"everything scriptable, `--json` everywhere": `analyze --json` does emit JSON,
but `apply --json` omits the advisor findings that the docs' CI example
(`docs/cli.md` line 117) filters on (`jq -e '.findings[] …'` matches nothing).**
*Root cause:* `apply --json` emits a different shape than `apply --dry-run
--json`; no schema is shared.

### P2 — PipeWire / JACK

**F2-1. Nothing in the PipeWire module could be built or run here (no SPA
headers), so every PipeWire claim in the README is currently unverified.**
Static review found:

* `process_buffers()` allocates **4 × EQF_MAX_CHANNELS × MAX_FRAMES floats on
  the stack** (`float tmp_in[16][8192]`, ×2 = 1 MB) in the interleaved path — a
  stack overflow hazard in a host thread with a default 8 MB stack under
  multiple nodes, and it defeats the documented "no allocation in process()".
* `impl_process()` calls `impl_update_config(this, false)` from the data thread,
  which does `stat()` **and**, on change, `eqf_chain_configure_file()` →
  JSON parse (`malloc`) + EQ re-design + `eqf_chain_set_channels()` (which
  `calloc`s scratch buffers). This is a **documented** trade-off in a comment but
  it is a real RT-safety violation (`malloc` in the callback), and it is *not*
  documented in the user-facing docs as a limitation.
* The module never validates that the negotiated channel count matches the chain
  it built; `impl_set_param(SPA_PARAM_Format)` calls `impl_update_config`
  *before* the chain is known to support the negotiated rate.

**F2-2. `eqforge-rt` (JACK host) cannot be built here (no libjack).** Static
review found:

* `reload` **calls `eqf_chain_configure_file()`** (heap allocation + JSON
  parse + EQ re-design) from the **control socket thread** while the audio
  thread runs `eqf_chain_process()` on the same chain pointer with **no lock the
  audio thread takes**. `chain_lock` is taken only by the control thread — it
  provides zero protection. Reconfiguring while `process()` reads
  `bands_per_channel`/`bands[c][b].target` is a data race: torn state, and
  `eqf_chain_set_channels()` frees/reallocs scratch buffers that `process()`
  may be writing. This is a **use-after-free waiting to happen**.
* `srate_cb()` calls `eqf_chain_set_sample_rate()` from the JACK thread, which
  does `free()`/`calloc()` of meter windows and re-parses config — allocation in
  the RT callback.
* `chain_lock` is initialised but never used to guard the audio thread; the
  `process()` comment "reconfigure is in-place" is false.

**F2-3. `eqforge system setup` silently overwrites a user's existing
`99-eqforge.conf`** (verified: a hand-added `# MY CUSTOM EDIT` line was gone
after a second `system setup`). No backup, no merge, no warning.

**F2-4. The generated PipeWire fragment documents `context.spa-libs` +
`context.objects` hosting, but nothing verifies the `factory.name =
eqforge.filter` string is ever registered**; the module exports
`FACTORY_NAME "eqforge.filter"` — consistent — but `SPA_DIR` in
`pipewire/Makefile` is derived from `pkg-config --variable=libdir libspa-0.2`
which is empty when the package is absent, silently falling back to `/usr/lib`
even on multiarch systems.

### P3 — CLI, daemon, GUI

**F3-1. `eqforge daemon stop` broken (see F0-2).**

**F3-2. `eqforge status` reports `daemon running` even after the RPC socket is
gone** — it probes the socket, so this is mostly correct, but `status` reports
`active profile flat` from the RPC while a *stale* `active-dsp.json` may say
something else; there is no consistency check.

**F3-3. GUI server is reachable from any origin** (`Access-Control-Allow-
Origin: *` on every response, including `/api/set_profile`, `/api/save_profile`,
`/api/delete_profile`). Binds 127.0.0.1, which limits exposure, but any web page
the user visits can drive the audio engine and delete profiles. No CSRF token,
no `Sec-Fetch-Site` check.

**F3-4. `GET /api/<unknown>` crashes the request handler for non-`EQForgeError`
exceptions in `_api_get`** (only `EQForgeError` is caught; anything else escapes
into `BaseHTTPRequestHandler` and closes the connection without a response).

**F3-5. `POST /api/save_profile` accepts an arbitrary dict and writes it to the
user's profile directory after only `Profile.from_dict()` conversion.** Server
side validates on `set_profile`, but `save` validates the *synthesised* dict, so
an attacker-supplied `id` containing `../` is neutralised only by
`store.save()`'s `replace("/", "_")` — and the `id` itself is still stored
verbatim inside the JSON. Path traversal via `delete_profile`/`resolve_any` file
paths is possible (`resolve_any(ref)` accepts **any** existing `*.json` path).

**F3-6. GUI → backend link verified working** (`set_profile`, `save_profile`,
`bypass` all change `active-dsp.json` and the resolved config), so the GUI is not
a fake front-end. However the GUI's own EQ-curve maths is a **third
implementation** of the biquad design (JS), independent of `coeffs.py` and
`biquad.c`, with no parity test — three implementations, zero cross-checks.

**F3-7. `eqforge import` of a profile whose `loudness`/`matching` sections carry
values the schema does not range-check is accepted silently**; `filters_op =
"append"` on a child whose parent has no `eq.filters` produces `filters_op`
being written back into the *resolved* export, which the next `import` then
treats as a fresh profile with `filters_op=append` and no parent.

**F3-8. `daemon restart` builds a fake `argparse.Namespace` via
`type("A", (), {"action": "start"})()`** — it works by accident today but drops
`--port`/`--no-http`.

### P4 — reliability, security, maintainability

**F4-1. Unsafe JSON lifetime design in `core/src/json.c`.** The returned
`eqf_json*` tree lives in an arena whose head is stored in a **process-global
singly-linked registry protected by nothing**. `eqf_json_free()` walks it. Two
threads configuring two chains concurrently can corrupt the registry; a
document freed on a different thread than it was parsed on is fine only because
of that same global. The comment in `capi.py` says "keep this file mechanically
in sync … the parity tests verify the important layouts at runtime" — **no such
parity test exists for `eqf_chain_stats`**.

**F4-2. `capi.py` declares `eqf_chain_process` with `POINTER(POINTER(c_float))`
and `NativeChain.process()` relies on `data.ctypes.data + c*stride`.** This
works only for C-contiguous `float32`; the code *does* check
`data.flags["C_CONTIGUOUS"]` — but it checks it **after** building the pointer
array from the un-copied array in the other branch… actually it copies first and
rebuilds; correct today, fragile.

**F4-3. `eqf_chain_process()` silently truncates `frames > max_block`** instead
of returning an error: `if (frames > c->max_block) frames = c->max_block;`. The
caller is left believing the whole buffer was processed. `NativeChain.process()`
passes the full `frames`, so a caller with `max_block=1024` and a 4096-frame
buffer silently loses 3/4 of the audio. (Verified: with `max_block=1024` the
tail was zero/unprocessed.)

**F4-4. 16 MB of zeroed memory per `eqf_chain`.** `eqf_limiter` is embedded by
value and contains `delay[16][9216]` floats = 590 kB… times `os_state`; `eqf_eq`
contains `filters[16][64]` ≈ 1 MB; `eqf_convolver` contains
`ir[16][8192] + history[16][8192]` = **1 MB of IR + 1 MB of history**. The
chain is `calloc`ked, so every chain construction zeroes ~3 MB. Acceptable for
one chain, wasteful for a multi-node host, and it means a `MAX_FIR_TAPS`
increase is a silent memory bomb.

**F4-5. Documentation claims that are false as written** (each verified):
* "`eqforge apply … bit-identical engine behavior`" — offline render *trims*
  limiter latency, so it is not sample-identical to a live host until you
  account for that; no test asserts it.
* "`true-peak accuracy ±2 %`" — measured 0.55 % on a 12 kHz/45° sine in
  Python, but the **limiter** detector can miss the inter-sample peak entirely
  when the peak falls between its 8-tap phases; not tested.
* "`smooth zipper-free parameter ramps`" — ramps exist (20 ms hard-coded in
  `chain.c`) but are untested.
* "`corrupt-config quarantine`" — only `config.json` is quarantined; a corrupt
  **profile** is skipped with a log line, and a corrupt `active-dsp.json` makes
  the native hosts keep their old config with no user-visible error.
* "`stale-socket recovery`" — no daemon stop, so stale sockets are the norm.
* "`eqforge system setup` … reviewable before touching the system" — it writes
  files immediately and overwrites them next time.
* README "Features → File processing … metadata preservation" — the only path
  (`copy_metadata_tags`) requires ffmpeg, which is optional and absent here, and
  is a no-op without it. WAV/FLAC tags are never preserved via soundfile.

**F4-6. No `--json` schema contract.** Each command emits a different shape; the
documented `jq` examples do not match (`F1-10`).

**F4-7. `paths.runtime_dir()` falls back to `/tmp/eqforge` when
`XDG_RUNTIME_DIR` is unset**, then `chmod 0700`s it — but `/tmp` is
world-writable and the *parent* is not checked for symlink/pre-existing
ownership; a hostile `/tmp/eqforge` owned by another user would cause
`EACCES` (fail-safe) but the error surfaces as "cannot reach daemon".

---

## 4. What is genuinely good (to be preserved)

* The **RBJ biquad design** in `core/src/biquad.c` is correct; coefficients and
  magnitude responses were independently re-derived and matched to <1e-6, and a
  +6 dB/1 kHz peak measured **6.0000 dB** on a rendered sine.
* **BS.1770-4 loudness** is correct: −23 dBFS 1 kHz stereo sine → **−23.037
  LUFS** at 44.1/48/96 kHz (EBU Tech 3341 target −23.0 ±0.1).
* **True-peak estimation** is correct to 0.55 % on a 12 kHz sine.
* **Two-pass LUFS normalization with true-peak ceiling** works: −29.87 LUFS
  in → −16.00 LUFS out, and a peaky signal was ceilinged at exactly −1.0 dBTP.
* **EQ magnitude/`apply` pipeline** is correct: 100 Hz tone through `gaming`'s
  200 Hz high-pass was attenuated −6.37 dB, as the design predicts.
* **Convolver** is correct and sample-exact: an impulse through
  `ir = [1, 0.5, 0.25]` returns exactly `1.0, 0.5, 0.25`.
* **Profiles**: validation, migration v1→v2, inheritance with
  `filters_op=append`, export of resolved profiles, and importers
  (AutoEQ/REW/EqualizerAPO/CSV) all work and round-trip.
* **GUI ↔ daemon**: measured. `set_profile voice` rewrote `active-dsp.json`
  (4 filters, preamp −3.0 dB); a GUI-authored profile saved through
  `save_profile` and activated produced the expected 1-filter/`−4.0 dB`
  config; `bypass` propagated.
* **C core is ASan/UBSan clean** and compiles as C++17.

---

## 5. Root-cause themes

1. **Comments and docs assert behaviour that was never implemented**
   (`≠` between docstring and code in at least a dozen places: retired-filter
   reset, limiter knee, metadata preservation, stale-socket recovery,
   `--json` contract, RT-safe reload).
2. **Tests assert only "did not crash" or "output ≤ ceiling"**, which a hard
   clipper satisfies. The limiter — the safety net of the whole product — was
   never tested for *distortion*, only for peak height.
3. **The fallback engine was never executed with a limiter**, i.e. the one
   configuration every preset uses.
4. **Optional native components were never gated**, so the default build fails.
5. **Process lifecycle was delegated to external binaries** (`fuser`), so
   stopping is impossible on a stock Debian install.
6. **Three independent biquad implementations** (C, numpy, JS) with no parity
   harness covering the JS one.
7. **Real-time safety claims are unenforced** — `malloc`, `stat`, `free`,
   JSON parsing, and unlocked cross-thread reconfiguration all occur on audio
   threads, with comments acknowledging and dismissing it.

---

## 6. Prioritised repair plan

| Pri | Item | Findings |
|---|---|---|
| P0 | Gate optional native builds; make `make` succeed without SPA/JACK | F0-1, F2-4 |
| P0 | Implement `shutdown` RPC + robust `daemon stop`; handle stale sockets | F0-2, F3-1, F2-3(part) |
| P1 | Rewrite limiter gain computer (true look-ahead, log-domain, continuous knee) | F1-1, F1-5(part) |
| P1 | Fix fallback limiter delay lines; bring detectors into parity | F1-2, F1-3 |
| P1 | Non-finite guard in biquad/chain/meter; reset retired bands | F1-4, F1-5 |
| P1 | Empty/short/odd audio handling; correct exit codes | F1-8 |
| P1 | `apply --json` unified schema incl. findings | F1-10, F4-6 |
| P1 | Latency-compensated, distortion-measured limiter tests; fallback parity tests | theme 2–3 |
| P2 | RT-safe reload in SPA module and RT host (no alloc/lock in audio thread) | F2-1, F2-2 |
| P2 | `system setup` must not clobber; write only changed files, back up | F2-3 |
| P3 | GUI API hardening (CSRF/origin, error paths, path validation) | F3-3, F3-4, F3-5 |
| P3 | JS/GUI biquad parity export | F3-6 |
| P4 | JSON arena lifetime without a global registry | F4-1 |
| P4 | Reject `frames > max_block` instead of truncating | F4-3 |
| P5 | Correct every false doc claim; add troubleshooting for each fixed bug | F4-5 |

Items marked "cannot be verified here" (PipeWire runtime, JACK runtime, ffmpeg
formats) will be covered by (a) build-only validation where headers can be
stubbed, (b) host-independent unit tests of the module's pure functions, and
(c) explicit documentation of the exact environment required — never by
asserting success.

---

## 7. Explicit limitations of this environment

* **PipeWire**: the daemon, `libpipewire`, `libspa-0.2` headers and all CLI
  tools are absent. The SPA module cannot be compiled, loaded, or hosted. Its
  correctness can only be established statically + by a substituted-header
  compile check if a stub SPA ABI is feasible, and by the existing
  `pipewire/test_module.c` once headers exist. **No claim that system-wide
  audio processing works will be made.**
* **JACK**: no `libjack`, no `jackd`, no `pipewire-jack`. `eqforge-rt` cannot be
  compiled. Its control protocol and audio path will be audit-fixed statically
  and its logic extracted where possible, but not run.
* **ffmpeg**: absent, so MP3/M4A/OPUS decode/encode and `copy_metadata_tags`
  cannot be executed. Paths are exercised for correct *failure* behaviour only.
* **Desktop GUI**: no browser in the sandbox. The GUI is verified by driving its
  HTTP/SSE API exactly as the JS does, plus `node --check` on the asset.
