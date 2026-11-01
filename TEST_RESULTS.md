# EQForge — Final Test Results

**Branch:** `repair/stabilization` @ `f5f6898`
**Date:** 2026-11-01
**Repair narrative:** `REPAIR_LOG.md` · **Findings:** `AUDIT_REPORT.md`

Every number below was produced by a command in this document, on this
machine, from the committed tree. Nothing is copied from a previous run.

---

## 1. Summary

| Suite | Command | Result |
|---|---|---|
| C core | `make -C core test` | **1181 checks, 0 failures** |
| C++ portability | `make -C core test-cxx` | **passes** |
| Python | `python -m pytest tests/` | **185 passed, 1 skipped** |
| End-to-end workflows | `make test-e2e` | **31 checks, 0 failures** |
| PipeWire module | `make test-module SPA_CFLAGS=…` | **ALL OK (0 failures)** |
| JACK host | `make test-rthost JACK_CFLAGS=…` | **ALL OK (0 failures)** |
| Sanitizers | ASan + UBSan + LSan | **clean** |
| Thread sanitizer | TSan on the SPA module | **clean** |
| Top-level `make` | on a host with **no** SPA/JACK headers | **completes**, optional targets skipped |

Baseline for comparison (`AUDIT_REPORT.md` §2): 968 core checks, 133 pytest,
and `make` **failing outright**.

---

## 2. C core

```
$ make -C core test
test_version … test_chain_stats_copy
1181 checks, 0 failures
```

Grown from 968 checks at the audit baseline. New coverage in this branch:
`test_crossfeed_response` (monotonicity, no boost, DC normalisation, anti-phase
cancellation, true bypass), `test_convolver`, meter clamping, NULL handling,
`eqf_json_obj_has`, per-band retirement, oversize blocks, `set_channels`,
lock-free stats snapshot.

```
$ make -C core test-cxx
passes
```

The C++ build is the reason `core/include/eqforge/atomics.h` exists: `<stdatomic.h>`
is not available from C++, so the atomic surface is a header with compiler
intrinsics behind it. This target has failed before; it is in the gate.

---

## 3. Python

```
$ python -m pytest tests/
185 passed, 1 skipped in 17.08s
```

| File | Tests | Focus |
|---|---|---|
| `test_parity.py` | 34 | native vs NumPy, per stage + full chain |
| `test_profiles.py` | 18 | model, migration, validation |
| `test_cli.py` | 17 | commands and argument handling |
| `test_daemon_http.py` | 15 | real loopback server: CORS, malformed input |
| `test_analysis.py` | 14 | empty, NaN, Inf, DC-only input |
| `test_advisor.py` | 13 | suggestions |
| `test_engine.py` | 13 | chain semantics, crossfeed law |
| `test_daemon.py` | 12 | lifecycle, RPC, socket handling |
| `test_importers.py` | 11 | AutoEQ / CSV / REW |
| `test_audio_io.py` | 9 | formats, dither, metadata |
| `test_profile_inheritance.py` | 8 | diamonds, cycles, four merge modes |
| `test_matching.py` | 6 | target matching |
| `test_gui_backend.py` | 6 | GUI RPC ↔ daemon ↔ engine |
| `test_system_setup.py` | 5 | create / idempotent / backup / collision |
| `test_render.py` | 5 | offline render path |

The single skip is an environment-gated test (no `libspa-0.2` on this host).

---

## 4. Backend parity (native vs numpy fallback)

48 kHz, 1 s pink noise at 0.25, first 2000 samples excluded, error relative to
signal peak. Thresholds are pinned ~15 dB above each measured floor rather
than being round numbers set far below it.

| Stage | Measured | Bound |
|---|---|---|
| no-op | −150.5 dB | −135 |
| preamp | −140.2 dB | −125 |
| EQ 1 peak | −105.8 dB | −90 |
| EQ 4 band | −82.5 dB | −70 |
| crossfeed | −134.9 dB | −120 |
| compressor | −114.8 dB | −100 |
| limiter | −150.5 dB | −135 |
| **full chain** | **−81.1 dB** | **−70** |

At the audit baseline the full chain diverged by **−27.1 dB**, which the old
−55 dB bound would have called a failure but the *stage* bounds (−60) would
have tolerated. The old bounds allowed a 26 dB regression in the full chain
without a murmur.

**Non-vacuity:** perturbing the fallback preamp by 0.0001 dB fails 9 tests.

---

## 5. Crossfeed

Centred (correlated) input, fc 700 Hz, level −6 dB, steady-state amplitude by
least squares over the second half of a 2 s render.

| f | before | after |
|---|---|---|
| 50 Hz | **+3.49 dB** | −2.50 dB |
| 100 Hz | +3.37 dB | −2.54 dB |
| 300 Hz | +1.81 dB | −2.88 dB |
| 500 Hz | −1.76 dB | −3.40 dB |
| 700 Hz | −2.51 dB | −3.91 dB |
| 1 kHz | −0.47 dB | −4.52 dB |
| 2 kHz | +0.03 dB | −5.47 dB |

Closed-form: `0.5·(1+g·H(f))`, `g = 10^(−6/20) = 0.5012`, so DC is
`20·log10(0.751) = −2.49 dB` and the high-frequency asymptote is
`20·log10(0.5) = −6.02 dB`. Measured DC: **−2.50 dB** (0.01 dB agreement).

Hard-panned 200 Hz: driven channel 0.17678 RMS (predicted `0.5 × 0.5/√2 =
0.17677`), silent channel 0.08519 RMS (predicted `0.5·|H|·g·0.5/√2`, exact).

Response is now **monotone** between the two asymptotes; before, it rose 3.5 dB
then dove 2.5 dB below unity — a loudness-preserving crossfeed that made centred
bass louder and the corner quieter.

---

## 6. Limiter

+6 dB overdrive on 1 kHz, ceiling −1 dBTP, attack 1 ms, release 40 ms,
look-ahead 2 ms:

| Metric | Before | After |
|---|---|---|
| THD | −32.6 dB | **−160.4 dB** |
| output peak | 0.89125 (= ceiling, clipping) | 0.890515 |
| samples at/over ceiling | 7224 | **0** |

---

## 7. GUI ↔ engine (measured, not asserted)

Profile `eq.filters = [peak 1 kHz −12 dB Q 1, highpass 80 Hz Q 0.707]`,
activated through the GUI's own RPCs, then the impulse response of the
resulting `active-dsp.json` was rendered and measured:

| Frequency | Gain |
|---|---|
| 50 Hz | −8.88 dB |
| **80 Hz** | **−3.13 dB** |
| 200 Hz | −0.75 dB |
| **1 kHz** | **−12.00 dB** |
| 5 kHz | −0.60 dB |

The 80 Hz corner sits at −3 dB and the 1 kHz peak at −12 dB: the GUI's profile
reaches the DSP engine intact.

All 12 RPC names used by `app.js` exist in `server.py`; no mock or demo data in
the assets.

---

## 8. Daemon HTTP security

Live daemon on a real loopback port:

| Request | Result |
|---|---|
| POST, `Origin: https://evil.example` | **403**, no `Access-Control-Allow-Origin` |
| POST, `Origin: http://127.0.0.1:PORT` | 200, origin echoed, `Vary: Origin` |
| GET, evil origin | 200 but **ungranted** (no ACAO) |
| OPTIONS, evil origin | **403** |
| 7 malformed bodies | **400** with actionable messages, never 500 |

**Non-vacuity:** re-introducing the wildcard ACAO and removing the 403 made
**6 tests fail**; restoring the fix made them pass. The GUI was verified
unaffected afterwards.

---

## 9. PipeWire module

```
$ make test-module SPA_CFLAGS="-isystem /tmp/pipewire-1.2.7/spa/include"
ALL OK (0 failures)
```

| Check | Result |
|---|---|
| Props(mute) accepted before any format | ok |
| Props(mute=false) before format | ok |
| mute → peak | 0.9000 (signal passes, bypass) |
| unmute → peak | 0.2510 (limiter back in circuit) |
| `SPA_PARAM_Latency` set_param | refused |
| latency reported | follows look-ahead, 96 frames @ 48 k |
| hot reload after config rewrite | new chain staged, ceiling respected |
| TSan | clean |

---

## 10. JACK host (fake server)

```
$ make test-rthost JACK_CFLAGS="-I/tmp/jackstub"
ALL OK (0 failures)
```

`latency_frames` 96 @ 48 kHz, 88 @ 44.1 kHz. Rate change deferred off the RT
thread; host exits on quit.

---

## 11. System setup

| Step | Result |
|---|---|
| first run | created |
| second run | unchanged (idempotent) |
| user edit then run | updated, `.bak.<ts>` written |
| backup contents | **contains the user's edit** |

---

## 12. End-to-end workflows

```
$ make test-e2e
workflows: 31 passed, 0 failed
```

| Workflow | Evidence |
|---|---|
| A offline | −6 dBFS sine analysed back within 0.5 dB; flat apply transparent < 1e-5 |
| B profiles | imported 3 bands are exported 3 bands |
| C batch | 3 in → 3 out, finite, non-silent, unclipped, rate and shape preserved |
| D daemon + GUI | status shaped, cross-origin 403, `active-dsp.json` carries the profile, clean stop, no stale socket, no surviving process |
| E system | idempotent, backup preserved, `doctor` rc=0 |

**Teeth:** a non-flat profile moves the output by **0.32**, which the 1e-5
transparency assertion rejects. The harness uses an isolated `HOME`/XDG tree
under its work directory, so it cannot touch a real user's configuration.

---

## 13. Build gating

On this host (no `libspa-0.2`, no JACK):

```
$ make
  SKIP test-module (libspa-0.2 not installed; …)
  SKIP test-rthost (jack headers not found; …)
```

`make` completes. Both targets build and pass when pointed at headers via
`SPA_CFLAGS` / `JACK_CFLAGS`. A clean clone of the committed tree was rebuilt to
confirm this is a property of the commit, not of this working copy.

---

## 14. What is not covered

This environment has no PipeWire daemon, no JACK server, no `ffmpeg`/`sox`, no
`fuser`. Therefore:

- the SPA module is exercised through a **stub** SPA build and a smoke harness,
  not inside a live PipeWire graph;
- the JACK host is exercised against a **fake** JACK server, not `jackd`;
- codec coverage stops at what `libsndfile` provides.

These are stated as limits, not papered over. The DSP, file-processing, CLI,
daemon, GUI-server, profile and system-integration layers were all exercised
for real and are covered above.
