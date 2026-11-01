"""Terminal output helpers: ANSI when on a tty, clean fallback otherwise."""
from __future__ import annotations

import json as _json
import os
import shutil
import sys

_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _ENABLED else s


def bold(s: str) -> str: return _c("1", s)
def dim(s: str) -> str: return _c("2", s)
def green(s: str) -> str: return _c("32", s)
def yellow(s: str) -> str: return _c("33", s)
def red(s: str) -> str: return _c("31", s)
def cyan(s: str) -> str: return _c("36", s)
def magenta(s: str) -> str: return _c("35", s)


SEV_COLOR = {"ok": green, "info": cyan, "warn": yellow, "action": magenta,
             "error": red}


def severity_tag(sev: str) -> str:
    fn = SEV_COLOR.get(sev, lambda s: s)
    label = {"ok": " OK ", "info": "INFO", "warn": "WARN",
             "action": " DO ", "error": "ERR "}.get(sev, sev.upper()[:4])
    return fn(f"[{label}]")


def kv_table(rows: list[tuple[str, object]], indent: str = "  ") -> str:
    if not rows:
        return ""
    w = max(len(k) for k, _ in rows)
    return "\n".join(f"{indent}{bold(k.ljust(w))}  {v}" for k, v in rows)


def table(headers: list[str], rows: list[list[str]],
          max_widths: list[int] | None = None) -> str:
    if max_widths:
        rows = [[_clip(c, w) for c, w in zip(r, max_widths)] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [bold(line), dim("  ".join("-" * w for w in widths))]
    for r in rows:
        out.append("  ".join(str(c).ljust(widths[i])
                             for i, c in enumerate(r)))
    return "\n".join(out)


def _clip(s: str, w: int) -> str:
    s = str(s)
    return s if len(s) <= w else s[: w - 1] + "…"


def echo(s: str = "") -> None:
    print(s)


def echo_json(obj) -> None:
    print(_json.dumps(obj, indent=2, default=str))


def wrap(text: str, width: int | None = None, indent: str = "  ") -> str:
    width = width or min(shutil.get_terminal_size().columns, 100) - len(indent)
    import textwrap
    return textwrap.fill(text, width=width,
                         initial_indent=indent, subsequent_indent=indent)


def progress(label: str, i: int, n: int, width: int = 30) -> str:
    frac = (i + 1) / max(n, 1)
    filled = int(width * frac)
    bar = "█" * filled + "░" * (width - filled)
    return f"\r  {label} {bar} {i + 1}/{n}"
