# Development Guide

## Workspace setup

```sh
git clone <repo> eqforge && cd eqforge
python3 -m pip install -r requirements.txt -r <(echo pytest)
make all          # core + pipewire module + rt host
make test         # everything
```

Fast loops:

```sh
make -C core test          # C DSP suite (~1 s)
python3 -m pytest tests/test_engine.py -q      # one area
python3 -m pytest tests/ -q -x --lf            # last failures
make -C core test-cxx      # C++ portability canary
cd pipewire && ./build/test_module             # SPA module without a daemon
```

## Project layout & where to add things

| You want to… | Touch |
|---|---|
| add a DSP stage (e.g. stereo widener) | `core/include/eqforge/<stage>.h` + `core/src/<stage>.c`, wire into `chain.c` (order + JSON keys), extend `dsp/fallback.py`, add parity test |
| add a filter type | `types.h` enum, `biquad.c` design case, `coeffs.py` mirror, schema `FILTER_TYPES`, GUI `FILTER_TYPES` |
| add a profile field | `model.py` (dataclass + to/from dict), `schema.py` validation, `resolve.py` dsp-config emission (if DSP-relevant), docs/profile-format.md, bump `PROFILE_FORMAT_VERSION` + migration if renaming/moving existing fields |
| add an import format | `importers/<fmt>.py` with `looks_like_<fmt>()`/`parse_<fmt>()`/`import_file()`, register in `importers/detect.py`, sample file in tests |
| add an advisor rule | `smart/advisor.py`: pure function returning `Finding`s with stable `id`s, machine-readable `action` where applicable; test in `tests/test_advisor.py` |
| add an RPC method | `daemon/server.py::Daemon.rpc`, docs/configuration.md table; GUI client in `gui/static/app.js` if user-facing |
| add a CLI command | `cli/commands_*.py` function `cmd_x(args)` + parser entry in `cli/main.py`; smoke test in `tests/test_cli.py` |

## Conventions

* **Python**: 3.10+, type hints, dataclasses for models, no heavy deps beyond
  numpy/scipy/soundfile. Errors derive from `EQForgeError` (message + hint +
  exit code) — the CLI prints them uniformly.
* **C**: C11, `-Wall -Wextra -Wpedantic -Wshadow -Wdouble-promotion` clean;
  C++-compatible subset (no VLAs in headers, no reserved identifiers, explicit
  casts for float↔double). RT paths: no alloc/syscall/lock. Public API in
  `include/eqforge/`, prefixed `eqf_`.
* **The contract**: profile JSON → *resolved dsp-config* → native hosts.
  Never pass profile semantics (inheritance, auto-preamp, ir_file paths) to C.
* **Tests**: every DSP behavior gets an anchor (measurable physical
  expectation: dB at fc, ceiling respected, LUFS of known signals), not
  snapshot comparisons.
* **Logging**: `get_logger(__name__)`; user-facing errors via EQForgeError;
  never `print()` outside `cli/output.py`.

## Running pieces manually

```sh
# daemon in foreground, fixed GUI port
python3 -m eqforge daemon run --port 8732

# RT host against a running PipeWire (needs pipewire-jack)
XDG_RUNTIME_DIR=/run/user/$UID rthost/build/eqforge-rt -f ~/.config/eqforge/active-dsp.json

# SPA module in a scratch PipeWire graph (integration machines only)
pw-cli create-node adapter '{ factory.name="eqforge.filter",
  node.name="eqforge_test", config="'"$HOME"'/.config/eqforge/active-dsp.json" }'

# drive the daemon RPC by hand
echo '{"method":"status"}' | socat - UNIX:$XDG_RUNTIME_DIR/eqforge/daemon.sock
```

## Benchmarking

```sh
# offline throughput (native vs fallback)
python3 - <<'EOF'
import time, numpy as np
from eqforge.audio import synth
from eqforge.dsp.engine import render
x = synth.pink_noise(60.0, 48000)
cfg = {"preamp_db":-3,"eq":{"filters":[{"type":"peak","freq":f*1000,
       "gain_db":3,"q":1} for f in (1,2,4,8)]},
       "limiter":{"enabled":True,"ceiling_db":-1}}
t=time.time(); r=render(x,48000,cfg); dt=time.time()-t
print(f"{r.backend}: {60/dt:.1f}× realtime")
EOF

# RT headroom: watch pw-top while playing audio through the filter node
```

## Release checklist

1. `make test` green (C + SPA + pytest), `make -C core test-cxx` green.
2. Bump versions **in sync**: `core/include/eqforge/version.h`,
   `eqforge/version.py`, `pyproject.toml`.
3. `eqforge doctor` passes on a clean container (`podman run -it debian:12`,
   install deps from docs/installation.md).
4. Docs review: CLI table matches `cli/main.py`; profile-format matches
   `schema.py`.
5. Tag; build wheel (`python -m build`); attach `core` sources (they are the
   product's heart and must ship with any binary).
