"""eqforge CLI - argument parser and dispatch.

Usage overview (see docs/cli.md for the full manual):

  eqforge analyze song.flac [--profile P] [--json]
  eqforge apply P in.wav out.wav [--normalize -16] [--bits 24]
  eqforge batch P dir/ [-o outdir]
  eqforge suggest song.flac [--target harman-overear] [--save-as ID]
  eqforge list-profiles | show P [--ascii|--svg f] | new ID [--from P]
  eqforge import FILE [--id ID] | export P DEST | diff A B | validate P
  eqforge devices | streams | status
  eqforge set-profile P [--slot A|B] | bypass on|off|toggle | ab [A|B|toggle]
  eqforge daemon start|stop|status|run | gui | system setup|start|stop|status
  eqforge doctor [--build-core] | synth SIG OUT | version
"""
from __future__ import annotations

import argparse
import sys

from eqforge.errors import EQForgeError
from eqforge.log import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eqforge",
        description="EQForge - profile-driven smart audio processing for Linux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage overview")[1].strip() if "Usage overview" in (__doc__ or "") else None,
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", "-v", action="store_true")
    common.add_argument("--quiet", "-q", action="store_true")
    common.add_argument("--json", action="store_true",
                        help="machine-readable output where supported")
    for a in common._actions:
        p._add_action(a)
    sub = p.add_subparsers(dest="command", required=True,
                           parser_class=type(p))

    def add_parser(name, **kw):
        kw.setdefault("parents", [common])
        return sub.add_parser(name, **kw)

    # ---- audio ----
    from eqforge.cli import commands_audio as ca

    sp = add_parser("analyze", help="analyze an audio file")
    sp.add_argument("file")
    sp.add_argument("--profile", "-p", help="also advise against this profile")
    sp.set_defaults(fn=ca.cmd_analyze)

    sp = add_parser("apply", help="apply a profile to an audio file")
    sp.add_argument("profile")
    sp.add_argument("input")
    sp.add_argument("output")
    sp.add_argument("--normalize", type=float, metavar="LUFS",
                    help="normalize output loudness (e.g. -16)")
    sp.add_argument("--format", help="output format (wav/flac/mp3/ogg/...)")
    sp.add_argument("--bits", type=int, choices=(16, 24, 32))
    sp.add_argument("--quality", type=int, help="lossy encoder quality")
    sp.add_argument("--no-dither", action="store_true")
    sp.add_argument("--no-metadata", action="store_true")
    sp.add_argument("--dry-run", action="store_true",
                    help="run the advisor on input+profile without writing")
    sp.add_argument("--advise-only", action="store_true",
                    help=argparse.SUPPRESS)
    sp.set_defaults(fn=ca.cmd_apply)

    sp = add_parser("batch", help="process a directory of audio files")
    sp.add_argument("profile")
    sp.add_argument("input_dir")
    sp.add_argument("-o", "--output-dir")
    sp.add_argument("--normalize", type=float, metavar="LUFS")
    sp.add_argument("--format", help="output format (default flac)")
    sp.add_argument("--bits", type=int, choices=(16, 24, 32))
    sp.add_argument("--no-dither", action="store_true")
    sp.set_defaults(fn=ca.cmd_batch)

    sp = add_parser("suggest", help="suggest EQ toward a target curve")
    sp.add_argument("file")
    sp.add_argument("--target", default="harman-overear",
                    help="harman-overear | flat | broadcast")
    sp.add_argument("--max-gain", type=float, default=6.0)
    sp.add_argument("--max-filters", type=int, default=8)
    sp.add_argument("--save-as", metavar="ID", help="save result as profile")
    sp.set_defaults(fn=ca.cmd_suggest)

    sp = add_parser("synth", help="generate test signals")
    sp.add_argument("signal",
                    choices=("sine", "sweep", "pink", "white", "impulse",
                             "square"))
    sp.add_argument("output")
    sp.add_argument("--freq", type=float, default=1000.0)
    sp.add_argument("--freq2", type=float, help="sweep end frequency")
    sp.add_argument("--duration", type=float, default=2.0)
    sp.add_argument("--rate", type=int, default=48000)
    sp.add_argument("--channels", type=int, default=2)
    sp.add_argument("--amplitude", type=float, default=0.9)
    sp.set_defaults(fn=ca.cmd_synth)

    # ---- profiles ----
    from eqforge.cli import commands_profile as cp

    sp = add_parser("list-profiles", help="list builtin + user profiles")
    sp.set_defaults(fn=cp.cmd_list_profiles)

    sp = add_parser("show", help="show a profile (with curve)")
    sp.add_argument("profile")
    sp.add_argument("--ascii", action="store_true",
                    help="force ASCII curve")
    sp.add_argument("--svg", metavar="FILE", help="write SVG curve to FILE")
    sp.add_argument("--resolved", action="store_true",
                    help="dump resolved dsp-config JSON")
    sp.add_argument("--width", type=int, default=76)
    sp.set_defaults(fn=cp.cmd_show)

    sp = add_parser("new", help="create a user profile")
    sp.add_argument("id")
    sp.add_argument("--from", dest="from_profile", metavar="PROFILE",
                    help="base the new profile on an existing one")
    sp.add_argument("--name")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cp.cmd_new)

    sp = add_parser("import",
                        help="import AutoEQ/REW/APO/CSV/EQForge files")
    sp.add_argument("file")
    sp.add_argument("--id", help="profile id (default: derived from filename)")
    sp.add_argument("--name")
    sp.set_defaults(fn=cp.cmd_import)

    sp = add_parser("export", help="export a resolved profile")
    sp.add_argument("profile")
    sp.add_argument("dest")
    sp.set_defaults(fn=cp.cmd_export)

    sp = add_parser("diff", help="diff two profiles' dsp configs")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.set_defaults(fn=cp.cmd_diff)

    sp = add_parser("validate", help="validate a profile id or file")
    sp.add_argument("profile")
    sp.set_defaults(fn=cp.cmd_validate)

    sp = add_parser("delete", help="delete a user profile")
    sp.add_argument("profile")
    sp.set_defaults(fn=cp.cmd_delete)

    # ---- system / runtime ----
    from eqforge.cli import commands_system as cs

    sp = add_parser("devices", help="list PipeWire audio devices")
    sp.set_defaults(fn=cs.cmd_devices)

    sp = add_parser("streams", help="list running application streams")
    sp.set_defaults(fn=cs.cmd_streams)

    sp = add_parser("status", help="engine/daemon/system status")
    sp.set_defaults(fn=cs.cmd_status)

    sp = add_parser("set-profile", help="activate a profile")
    sp.add_argument("profile")
    sp.add_argument("--slot", choices=("A", "B"))
    sp.set_defaults(fn=cs.cmd_set_profile)

    sp = add_parser("bypass", help="bypass processing")
    sp.add_argument("state", nargs="?", choices=("on", "off", "toggle"),
                    default="toggle")
    sp.set_defaults(fn=cs.cmd_bypass)

    sp = add_parser("ab", help="A/B slot switching")
    sp.add_argument("slot", nargs="?", choices=("A", "B", "toggle"),
                    default="toggle")
    sp.set_defaults(fn=cs.cmd_ab)

    sp = add_parser("daemon", help="control the background daemon")
    sp.add_argument("action", choices=("start", "stop", "restart", "status",
                                       "run"))
    sp.add_argument("--foreground", action="store_true",
                    help=argparse.SUPPRESS)
    sp.add_argument("--no-http", action="store_true")
    sp.add_argument("--port", type=int)
    sp.set_defaults(fn=cs.cmd_daemon)

    sp = add_parser("gui", help="open the graphical interface")
    sp.add_argument("--port", type=int)
    sp.add_argument("--no-browser", action="store_true")
    sp.add_argument("--wait", action="store_true",
                    help="keep the terminal attached")
    sp.set_defaults(fn=cs.cmd_gui)

    sp = add_parser("system", help="system-wide processing (rt host)")
    sp.add_argument("action", choices=("setup", "start", "stop", "status",
                                       "wire"))
    sp.add_argument("--rt-binary", help="path to eqforge-rt")
    sp.add_argument("--channels", type=int)
    sp.add_argument("--manage", action="store_true",
                    help="also wire rt outputs to the default sink")
    sp.set_defaults(fn=cs.cmd_system)

    sp = add_parser("doctor", help="diagnose the installation")
    sp.add_argument("--build-core", action="store_true",
                    help="try to build libeqforge if missing")
    sp.set_defaults(fn=cs.cmd_doctor)

    sp = add_parser("version", help="version information")
    sp.set_defaults(fn=cs.cmd_version)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose, quiet=args.quiet,
                  log_file=args.command not in ("version",))
    if args.json:
        # commands honor args.json individually
        pass
    try:
        return args.fn(args) or 0
    except EQForgeError as e:
        print(f"  \033[31m✗\033[0m {e.message}" if sys.stderr.isatty()
              else f"error: {e.message}", file=sys.stderr)
        if e.hint:
            print(f"    hint: {e.hint}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
