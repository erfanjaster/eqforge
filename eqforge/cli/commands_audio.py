"""CLI commands: audio file operations (analyze / apply / batch / suggest / synth)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

from eqforge.cli import output as out
from eqforge.errors import EQForgeError


# ---------------------------------------------------------------- analyze --

def cmd_analyze(args) -> int:
    from eqforge.analysis.report import analyze
    from eqforge.audio.io import read
    from eqforge.profiles.store import ProfileStore
    from eqforge.smart.advisor import advise

    audio = read(args.file)
    report = analyze(audio.data, audio.sample_rate)

    resolved = None
    if args.profile:
        store = ProfileStore()
        resolved = store.resolve_any(args.profile)

    advice = advise(report, resolved, audio.sample_rate)

    if args.json:
        payload = report.to_dict()
        payload["advice"] = advice.to_dict()
        out.echo_json(payload)
        return 0

    out.echo(out.bold(f"\n  {Path(args.file).name}"))
    out.echo(out.kv_table([
        ("format", f"{audio.format or '?'} / {audio.subtype or '?'}"),
        ("duration", f"{report.duration_s:.2f} s"),
        ("channels", report.channels),
        ("sample rate", f"{report.sample_rate} Hz"),
    ]))
    out.echo()
    out.echo(out.bold("  Levels"))
    tp = report.true_peak_db
    tp_s = f"{tp:+.2f} dBTP" + (out.red(" ⚠") if tp > 0 else "")
    pk = report.peak_db
    pk_s = f"{pk:+.2f} dBFS" + (out.red(" ⚠") if pk > -0.01 else "")
    out.echo(out.kv_table([
        ("integrated", f"{report.integrated_lufs:.1f} LUFS"),
        ("loudness range", f"{report.loudness_range_lu:.1f} LU"),
        ("peak", pk_s),
        ("true peak", tp_s),
        ("RMS", f"{report.rms_db:.1f} dBFS"),
        ("crest factor", f"{report.crest_factor_db:.1f} dB"),
    ]))
    out.echo()
    out.echo(out.bold("  Frequency balance (dB re total energy)"))
    bands = report.band_energy_db or {}
    if bands:
        cells = []
        for name, v in bands.items():
            vs = "  --  " if v is None or not np.isfinite(v) else f"{v:+5.1f}"
            cells.append(f"{out.dim(name):>{len(name)+8}} {vs}")
        # two per line
        for i in range(0, len(cells), 3):
            out.echo("   " + "   ".join(cells[i:i + 3]))
    out.echo()
    out.echo(out.bold("  Advisor"))
    for f in advice.findings:
        out.echo(f"  {out.severity_tag(f.severity)} {f.message}")
        if f.hint:
            out.echo(out.wrap(f.hint, indent="         "))
    out.echo()
    return 0


# ------------------------------------------------------------------ apply --

def _load_input(path: str) -> tuple[np.ndarray, int, "object"]:
    from eqforge.audio.io import read
    audio = read(path)
    return audio.data, audio.sample_rate, audio


def cmd_apply(args) -> int:
    from eqforge.analysis.loudness import integrated_loudness, true_peak_db
    from eqforge.analysis.report import analyze
    from eqforge.audio.io import copy_metadata_tags, read, write, AudioData
    from eqforge.dsp.engine import normalize_to_lufs, render
    from eqforge.profiles.resolve import to_dsp_config
    from eqforge.profiles.store import ProfileStore
    from eqforge.smart.advisor import advise

    t0 = time.time()
    audio = read(args.input)
    store = ProfileStore()
    resolved = store.resolve_any(args.profile)
    cfg = to_dsp_config(resolved,
                        base_dir=Path(args.profile).parent
                        if Path(args.profile).exists() else None)

    if args.dry_run or args.advise_only:
        report = analyze(audio.data, audio.sample_rate, with_spectrum=False)
        advice = advise(report, resolved, audio.sample_rate)
        if args.json:
            out.echo_json({"input": str(args.input),
                           "profile": args.profile,
                           "report": report.to_dict(),
                           "advice": advice.to_dict()})
            return 0
        for f in advice.findings:
            out.echo(f"  {out.severity_tag(f.severity)} {f.message}")
        if advice.suggested_preamp_db is not None:
            out.echo(out.wrap(
                f"suggested preamp: {advice.suggested_preamp_db:+.1f} dB",
                indent="  "))
        return 0

    data = audio.data
    target = args.normalize
    if target is None:
        target = (resolved.get("loudness") or {}).get("target_lufs")

    result = render(data, audio.sample_rate, cfg)
    outdata = result.data

    # Loudness normalization happens POST-render: the chain's own dynamics
    # (compressor/limiter) change loudness, so the target refers to what
    # comes out. Gain is limited by the true-peak ceiling.
    applied_norm = 0.0
    if target is not None:
        outdata, applied_norm = normalize_to_lufs(
            outdata, audio.sample_rate, float(target),
            float((resolved.get("loudness") or {}).get("true_peak_db", -1.0)))

    # post-render safety analysis
    tp_after = true_peak_db(outdata, audio.sample_rate)
    if tp_after > 0.0:
        out.echo(out.yellow(
            f"  note: output true peak {tp_after:+.2f} dBTP exceeds 0 dB "
            "(source-dependent; limiter ceiling applies downstream)"))

    dest = Path(args.output)
    subtype = None
    if args.bits:
        subtype = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}[args.bits]
    written = write(AudioData(outdata, audio.sample_rate,
                              metadata=audio.metadata),
                    dest, fmt=args.format, subtype=subtype,
                    bit_depth=args.bits, dither=not args.no_dither,
                    quality=args.quality)
    if not args.no_metadata and audio.path:
        try:
            copy_metadata_tags(audio.path, written)
        except EQForgeError:
            pass

    dur = time.time() - t0
    if args.json:
        out.echo_json({
            "ok": True, "input": str(args.input), "output": str(written),
            "backend": result.backend,
            "latency_frames": result.latency_frames,
            "normalize_gain_db": round(applied_norm, 2) if applied_norm else None,
            "true_peak_db_out": round(tp_after, 2),
            "seconds": round(dur, 2),
        })
    else:
        rt_factor = audio.duration / dur if dur > 0 else 0
        out.echo(f"  {out.green('✓')} {written}  "
                 f"({result.backend} engine, {rt_factor:.1f}× realtime"
                 + (f", normalized {applied_norm:+.1f} dB" if applied_norm else "")
                 + ")")
    return 0


# ------------------------------------------------------------------ batch --

def cmd_batch(args) -> int:
    from eqforge.audio.io import AudioData, read, write
    from eqforge.dsp.engine import normalize_to_lufs, render
    from eqforge.profiles.resolve import to_dsp_config
    from eqforge.profiles.store import ProfileStore

    src = Path(args.input_dir)
    if not src.is_dir():
        raise EQForgeError(f"not a directory: {src}")
    exts = {".wav", ".flac", ".ogg", ".mp3", ".m4a", ".aiff", ".opus",
            ".w64", ".aif", ".aac", ".webm", ".mp4"}
    files = sorted(p for p in src.rglob("*")
                   if p.suffix.lower() in exts and p.is_file())
    if not files:
        out.echo("  no audio files found")
        return 0

    store = ProfileStore()
    resolved = store.resolve_any(args.profile)
    cfg = to_dsp_config(resolved, base_dir=None)

    dest_dir = Path(args.output_dir) if args.output_dir else \
        src / f"{src.name}-eqforge"
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_fmt = args.format or "flac"

    ok = failed = 0
    for i, f in enumerate(files):
        rel = f.relative_to(src)
        target_path = (dest_dir / rel).with_suffix(f".{out_fmt}")
        try:
            audio = read(f)
            r = render(audio.data, audio.sample_rate, cfg)
            outdata = r.data
            if args.normalize is not None:
                outdata, _g = normalize_to_lufs(outdata, audio.sample_rate,
                                                args.normalize)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            write(AudioData(outdata, audio.sample_rate), target_path,
                  bit_depth=args.bits, dither=not args.no_dither)
            ok += 1
            status = out.green("✓")
        except (EQForgeError, ValueError, OSError) as e:
            failed += 1
            status = out.red("✗")
            out.echo(f"  {status} {rel}: {e}", file=sys.stderr)
            continue
        sys.stdout.write(out.progress("processing", i, len(files)) + f" {status} {rel.name}   ")
        sys.stdout.flush()
    sys.stdout.write("\n")
    out.echo(f"  {out.green(str(ok))} ok, "
             f"{(out.red(str(failed)) if failed else '0')} failed → {dest_dir}")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- suggest --

def cmd_suggest(args) -> int:
    from eqforge.analysis.report import analyze
    from eqforge.audio.io import read
    from eqforge.smart import targets as T

    audio = read(args.file)
    report = analyze(audio.data, audio.sample_rate)
    spec = report.spectrum
    freqs = np.asarray(spec.get("freqs", []))
    db = np.asarray(spec.get("db", []))
    if len(freqs) == 0:
        out.echo("  not enough signal to suggest EQ")
        return 1

    summary = T.deviation_summary(db, freqs, args.target)
    filters = T.match_eq(db, freqs, args.target,
                         max_gain_db=args.max_gain, max_filters=args.max_filters)

    if args.json:
        out.echo_json({"target": args.target, "deviation": summary,
                       "suggested_filters": filters})
        return 0

    out.echo(out.bold(f"\n  Target: {args.target}"))
    out.echo(out.kv_table([
        ("rms deviation", f"{summary['rms_deviation_db']:.1f} dB"),
        ("max deviation", f"{summary['max_deviation_db']:.1f} dB"),
    ]))
    if not filters:
        out.echo(f"  {out.green('✓')} content already close to target "
                 "(no filters suggested)")
        return 0
    out.echo(out.bold("\n  Suggested corrective filters:"))
    rows = []
    for f in filters:
        rows.append([f["type"], f"{f['freq']:g} Hz",
                     f"{f['gain_db']:+.1f} dB", f"{f.get('q', ''):g}"])
    out.echo(out.table(["type", "freq", "gain", "q"], rows))
    if args.save_as:
        from eqforge.profiles.model import FilterSpec, Profile
        from eqforge.profiles.store import ProfileStore
        prof = Profile(id=args.save_as,
                       name=f"Auto-match ({args.target})",
                       description=f"Generated by `eqforge suggest` toward "
                                   f"{args.target} for {Path(args.file).name}",
                       tags=["auto", "suggest"],
                       filters=[FilterSpec.from_dict(f) for f in filters])
        p = ProfileStore().save(prof)
        out.echo(f"\n  saved as profile {out.cyan(args.save_as)} → {p}")
    else:
        out.echo(out.dim("\n  tip: --save-as my-match to store as a profile"))
    out.echo()
    return 0


# ------------------------------------------------------------------ synth --

def cmd_synth(args) -> int:
    from eqforge.audio import synth
    from eqforge.audio.io import AudioData, write

    kind = args.signal
    sr = args.rate
    dur = args.duration
    ch = args.channels
    if kind == "sine":
        data = synth.sine(args.freq, dur, sr, amplitude=args.amplitude,
                          channels=ch)
    elif kind == "sweep":
        data = synth.sweep(args.freq, args.freq2 or sr / 2 * 0.9, dur, sr,
                           amplitude=args.amplitude, channels=ch)
    elif kind == "pink":
        data = synth.pink_noise(dur, sr, amplitude=args.amplitude, channels=ch)
    elif kind == "white":
        data = synth.white_noise(dur, sr, amplitude=args.amplitude, channels=ch)
    elif kind == "impulse":
        data = synth.impulse(ch, int(dur * sr))
    elif kind == "square":
        data = synth.full_scale_square(dur, sr, args.freq, ch)
    else:
        raise EQForgeError(f"unknown signal {kind!r}")
    p = write(AudioData(data, sr), args.output)
    out.echo(f"  {out.green('✓')} {p} ({kind}, {dur:g}s, {sr} Hz, {ch}ch)")
    return 0
