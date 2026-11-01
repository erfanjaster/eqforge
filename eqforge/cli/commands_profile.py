"""CLI commands: profile management."""
from __future__ import annotations

import json
import time
from pathlib import Path

from eqforge.cli import output as out
from eqforge.errors import EQForgeError


def _store():
    from eqforge.profiles.store import ProfileStore
    return ProfileStore()


# ------------------------------------------------------------ list-profiles --

def cmd_list_profiles(args) -> int:
    store = _store()
    entries = store.list_ids()
    if args.json:
        out.echo_json(entries)
        return 0
    rows = []
    for e in entries:
        tags = ",".join(e["tags"][:3]) if e["tags"] else ""
        origin = out.dim("builtin") if e["builtin"] else "user"
        rows.append([e["id"], e["name"], origin, tags])
    out.echo(out.table(["id", "name", "origin", "tags"], rows,
                       max_widths=[38, 34, 8, 30]))
    out.echo(out.dim(f"\n  {len(entries)} profiles. "
                     "Details: eqforge show <id>"))
    return 0


# -------------------------------------------------------------------- show --

def cmd_show(args) -> int:
    from eqforge.profiles.resolve import to_dsp_config
    store = _store()
    resolved = store.resolve_any(args.profile)
    filters = (resolved.get("eq") or {}).get("filters", [])
    pre_mode = (resolved.get("preamp") or {}).get("mode", "auto")
    dsp = to_dsp_config(resolved)

    if args.json:
        out.echo_json({"resolved": resolved, "dsp_config": dsp})
        return 0

    out.echo(out.bold(f"\n  {resolved.get('name', resolved.get('id'))}"))
    if resolved.get("description"):
        out.echo(out.wrap(resolved["description"], indent="  "))
    meta = [("id", resolved.get("id")),
            ("tags", ", ".join(resolved.get("tags", [])) or "-"),
            ("preamp", (f"{dsp['preamp_db']:+.1f} dB (auto)"
                        if pre_mode == "auto"
                        else f"{dsp['preamp_db']:+.1f} dB (manual)")),
            ("filters", str(len(dsp["eq"]["filters"])))]
    stages = []
    for stage in ("crossfeed", "convolver", "compressor", "limiter"):
        s = resolved.get(stage) or {}
        stages.append(f"{stage}={'on' if s.get('enabled') else 'off'}")
    meta.append(("chain", "gain → eq → " + " → ".join(stages)))
    out.echo(out.kv_table(meta))

    if args.ascii or not args.svg:
        from eqforge.render.curve import render_ascii
        out.echo()
        out.echo(render_ascii(dsp["eq"]["filters"], dsp["preamp_db"],
                              width=args.width))

    if args.svg:
        from eqforge.render.curve import render_svg
        svg = render_svg(dsp["eq"]["filters"], dsp["preamp_db"],
                         title=resolved.get("name", ""))
        Path(args.svg).write_text(svg, encoding="utf-8")
        out.echo(f"\n  {out.green('✓')} curve written to {args.svg}")

    if args.resolved:
        out.echo(out.dim("\n  resolved dsp-config (what the native engine sees):"))
        out.echo(json.dumps(dsp, indent=2))
    out.echo()
    return 0


# --------------------------------------------------------------------- new --

def cmd_new(args) -> int:
    from eqforge.profiles.model import Profile
    store = _store()
    if args.id in [e["id"] for e in store.list_ids() if not e["builtin"]] \
            and not args.force:
        raise EQForgeError(f"user profile {args.id!r} already exists",
                           hint="use --force to overwrite")
    if args.from_profile:
        src = store.resolve_any(args.from_profile)
        prof = Profile.from_dict(src)
        prof.id = args.id
        prof.name = args.name or f"{prof.name} (custom)"
        prof.builtin = False
        prof.extends = []
    else:
        prof = Profile(id=args.id, name=args.name or args.id,
                       created=time.time())
    p = store.save(prof)
    out.echo(f"  {out.green('✓')} created {args.id} → {p}")
    out.echo(out.dim("  edit the JSON directly, or use the GUI "
                     "(eqforge gui) to shape the curve"))
    return 0


# ------------------------------------------------------------------ import --

def cmd_import(args) -> int:
    from eqforge.importers.detect import detect_and_import
    store = _store()
    prof = detect_and_import(Path(args.file), store=store, new_id=args.id)
    if args.name:
        prof.name = args.name
    p = store.save(prof)
    out.echo(f"  {out.green('✓')} imported {len(prof.filters)} filters → "
             f"profile {out.cyan(prof.id)}")
    out.echo(f"    {p}")
    if prof.preamp_mode == "manual":
        out.echo(f"    preamp: {prof.preamp_db:+.1f} dB (from source file)")
    return 0


# ------------------------------------------------------------------ export --

def cmd_export(args) -> int:
    store = _store()
    p = store.export(args.profile, Path(args.dest))
    out.echo(f"  {out.green('✓')} exported resolved profile → {p}")
    return 0


# -------------------------------------------------------------------- diff --

def cmd_diff(args) -> int:
    import difflib
    from eqforge.profiles.resolve import to_dsp_config
    store = _store()
    a = to_dsp_config(store.resolve_any(args.a))
    b = to_dsp_config(store.resolve_any(args.b))
    ta = json.dumps(a, indent=2, sort_keys=True).splitlines()
    tb = json.dumps(b, indent=2, sort_keys=True).splitlines()
    diff = list(difflib.unified_diff(ta, tb, fromfile=args.a, tofile=args.b,
                                     lineterm=""))
    if not diff:
        out.echo("  profiles are DSP-identical")
        return 0
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            out.echo(out.green(line))
        elif line.startswith("-") and not line.startswith("---"):
            out.echo(out.red(line))
        elif line.startswith("@@"):
            out.echo(out.cyan(line))
        else:
            out.echo(out.dim(line))
    return 0


# ---------------------------------------------------------------- validate --

def cmd_validate(args) -> int:
    from eqforge.profiles.schema import validate_profile
    from eqforge.profiles.versioning import migrate, needs_migration
    store = _store()
    raw = store.load_raw(args.profile) if not Path(args.profile).exists() \
        else json.loads(Path(args.profile).read_text())
    migrated = False
    if needs_migration(raw):
        raw = migrate(raw)
        migrated = True
    issues = validate_profile(raw)
    errors = [i for i in issues if i.severity == "error"]
    warns = [i for i in issues if i.severity == "warning"]
    if migrated:
        out.echo(out.cyan("  format migrated to current version"))
    for w in warns:
        out.echo(f"  {out.severity_tag('warn')} {w}")
    for e in errors:
        out.echo(f"  {out.severity_tag('error')} {e}")
    if errors:
        out.echo(out.red("\n  INVALID"))
        return 1
    out.echo(out.green("\n  valid ✓"))
    return 0


# ------------------------------------------------------------------ delete --

def cmd_delete(args) -> int:
    store = _store()
    store.delete(args.profile)
    out.echo(f"  {out.green('✓')} deleted {args.profile}")
    return 0
