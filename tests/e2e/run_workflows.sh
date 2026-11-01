#!/usr/bin/env bash
# End-to-end validation of the five primary workflows.
#
# Everything here measures the OUTPUT SIGNAL, not exit codes. A workflow that
# returns 0 while producing silence, double gain, or the wrong number of
# samples must fail.
#
#   A  Offline: generate -> analyze -> suggest -> apply -> verify the file
#   B  Profiles: import (AutoEQ) -> validate -> resolve -> export -> diff
#   C  Batch: process a directory, verify every output
#   D  Daemon/GUI: start -> RPC over HTTP -> activate -> verify engine config
#   E  Integration: system setup -> idempotency -> backup -> doctor
#
# Usage: tests/e2e/run_workflows.sh [work-dir]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${1:-$(mktemp -d)}"
PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

export HOME="$WORK/home"
export XDG_CONFIG_HOME="$WORK/home/.config"
export XDG_DATA_HOME="$WORK/home/.local/share"
export XDG_RUNTIME_DIR="$WORK/run"
export NO_COLOR=1
mkdir -p "$XDG_CONFIG_HOME" "$XDG_RUNTIME_DIR" "$WORK/out"

PASS=0; FAIL=0
ok()   { printf '  ok: %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL: %s\n' "$1"; FAIL=$((FAIL+1)); }
check(){ if [ "$1" = "0" ]; then ok "$2"; else bad "$2"; fi; }

eqf() { "$PY" -m eqforge.cli.main "$@"; }

echo "== A: offline analyze -> suggest -> apply =="
eqf synth sine "$WORK/out/a_src.wav" --freq 1000 --duration 2 --amplitude 0.5 \
    --rate 48000 --channels 2 >/dev/null 2>&1
check $? "generate test signal"
[ -s "$WORK/out/a_src.wav" ] && ok "source file exists" || bad "source file exists"

eqf analyze "$WORK/out/a_src.wav" --json >"$WORK/out/a_analysis.json" 2>/dev/null
check $? "analyze exits 0"
"$PY" - "$WORK/out/a_analysis.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
tp=d.get("true_peak_db", d.get("peak_db"))
assert tp is not None, "no peak reported"
assert -6.5 < tp < -5.5, f"1 kHz at 0.5 amplitude should be about -6 dBFS, got {tp}"
PY
check $? "analyze reports the correct level for a -6 dBFS sine"

eqf apply flat "$WORK/out/a_src.wav" "$WORK/out/a_flat.wav" >/dev/null 2>&1
check $? "apply flat profile exits 0"
"$PY" - "$WORK/out/a_src.wav" "$WORK/out/a_flat.wav" <<'PY'
import sys, numpy as np, soundfile as sf
a,sa=sf.read(sys.argv[1], always_2d=True)
b,sb=sf.read(sys.argv[2], always_2d=True)
assert sa==sb, f"sample rate changed: {sa} -> {sb}"
assert a.shape==b.shape, f"shape changed: {a.shape} -> {b.shape}"
d=float(np.abs(a-b).max())
assert d < 1e-5, f"flat profile altered the signal by {d:.3e}"
PY
check $? "flat profile is transparent (bit-identical within 1e-5)"

echo "== B: profile import -> validate -> resolve -> export -> diff =="
cat > "$WORK/out/autoeq.csv" <<'CSV'
Preamp: -6.0 dB
Filter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 0.70
Filter 2: ON PK Fc 1200 Hz Gain -3.0 dB Q 1.40
Filter 3: ON PK Fc 8000 Hz Gain 4.0 dB Q 2.00
CSV
eqf import "$WORK/out/autoeq.csv" --id e2e_import >/dev/null 2>&1
check $? "import AutoEQ file"
eqf validate e2e_import >/dev/null 2>&1
check $? "validate imported profile"
eqf export e2e_import "$WORK/out/e2e_import.json" >/dev/null 2>&1
check $? "export imported profile"
"$PY" - "$WORK/out/e2e_import.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
bands=d["eq"]["filters"]
assert len(bands)==3, f"expected 3 bands, got {len(bands)}"
freqs=[float(b["freq"]) for b in bands]
for want in (105,1200,8000):
    assert any(abs(f-want)<1.0 for f in freqs), f"missing {want} Hz: {freqs}"
gains={round(float(b["freq"])):float(b["gain_db"]) for b in bands}
assert abs(gains[105]-5.5)<0.05, gains
assert abs(gains[1200]-(-3.0))<0.05, gains
assert abs(gains[8000]-4.0)<0.05, gains
PY
check $? "imported bands are exactly the ones in the file"

eqf diff flat e2e_import >"$WORK/out/b_diff.txt" 2>&1
check $? "diff flat vs imported"
grep -qiE "band|filter|differ|gain" "$WORK/out/b_diff.txt" \
    && ok "diff reports the difference" || bad "diff reports nothing"

echo "== C: batch a directory =="
mkdir -p "$WORK/in_batch"
for f in 1 2 3; do
  eqf synth sine "$WORK/in_batch/t$f.wav" --freq $((200*f)) --duration 0.5 \
      --amplitude 0.3 --rate 44100 --channels 2 >/dev/null 2>&1
done
eqf batch e2e_import "$WORK/in_batch" -o "$WORK/out/batch" >/dev/null 2>&1
check $? "batch exits 0"
n=$(ls "$WORK/out/batch"/* 2>/dev/null | wc -l)
[ "$n" = "3" ] && ok "batch produced 3 outputs" || bad "batch produced $n outputs, expected 3"
"$PY" - "$WORK/in_batch" "$WORK/out/batch" <<'PY'
import sys, glob, os, numpy as np, soundfile as sf
# Match by stem: batch may choose a different (lossless) container than the
# input, so globbing *.wav in the output directory was my bug, not the tool's.
src={os.path.splitext(os.path.basename(p))[0]: p
     for p in glob.glob(os.path.join(sys.argv[1], "*"))}
dst={os.path.splitext(os.path.basename(p))[0]: p
     for p in glob.glob(os.path.join(sys.argv[2], "*"))}
assert set(src)==set(dst), f"stems differ: {sorted(src)} vs {sorted(dst)}"
for stem in sorted(src):
    s,d = src[stem], dst[stem]
    a,sa=sf.read(s, always_2d=True); b,sb=sf.read(d, always_2d=True)
    assert sa==sb, f"{s}: rate {sa}->{sb}"
    assert a.shape==b.shape, f"{s}: shape {a.shape}->{b.shape}"
    assert np.isfinite(b).all(), f"{d}: non-finite output"
    assert np.abs(b).max() > 1e-4, f"{d}: output is silent"
    assert np.abs(b).max() <= 1.0, f"{d}: output clips ({np.abs(b).max():.3f})"
PY
check $? "every batch output is finite, non-silent, unclipped, same rate/shape"

echo "== D: daemon + GUI backend =="
eqf daemon start >/dev/null 2>&1
check $? "daemon start"
DAEMON_PID=$("$PY" - <<'PYPID'
import json, os
from eqforge import paths
try:
    print(json.loads(paths.rt_socket().parent.joinpath("daemon.pid").read_text()))
except Exception:
    print("")
PYPID
)
for _ in $(seq 1 50); do [ -f "$XDG_RUNTIME_DIR/eqforge/gui.port" ] && break; sleep 0.1; done
PORT=$(cat "$XDG_RUNTIME_DIR/eqforge/gui.port" 2>/dev/null)
[ -n "${PORT:-}" ] && ok "GUI port published" || bad "GUI port published"

"$PY" - "$PORT" <<'PY'
import json, sys, urllib.request
port=sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as r:
    d=json.loads(r.read())
assert d["ok"], d
st=d["result"]
assert st.get("native_config"), st
PY
check $? "GUI /api/status reachable and shaped correctly"

"$PY" - "$PORT" <<'PY'
import json, sys, urllib.request, urllib.error
port=sys.argv[1]
req=urllib.request.Request(f"http://127.0.0.1:{port}/api/save_profile",
    data=json.dumps({"profile":{"id":"e2e_evil","name":"x"}}).encode(),
    headers={"Content-Type":"application/json","Origin":"https://evil.example"})
try:
    urllib.request.urlopen(req, timeout=5); print("NOT REFUSED"); sys.exit(1)
except urllib.error.HTTPError as e:
    assert e.code==403, e.code
PY
check $? "cross-origin mutation refused with 403"

eqf set-profile e2e_import >/dev/null 2>&1
check $? "activate imported profile via CLI"
"$PY" - "$XDG_CONFIG_HOME/eqforge/active-dsp.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
b=d["eq"]["filters"]
assert len(b)==3, f"active config has {len(b)} bands, expected 3"
PY
check $? "active-dsp.json carries the activated profile"

eqf daemon stop >/dev/null 2>&1
check $? "daemon stop"
sleep 0.4
# `pgrep -f` matches this script's own command line too, so count only real
# interpreter processes running the daemon entry point.
left=$(ps -eo args | grep -c "^[^ ]*python[0-9.]* -m eqforge daemon run")
[ "$left" = "0" ] && ok "daemon stopped cleanly" \
    || bad "daemon still running after stop ($left processes)"
[ -S "$XDG_RUNTIME_DIR/eqforge/daemon.sock" ] \
    && bad "stale socket left behind" || ok "no stale socket"

echo "== E: system integration =="
eqf system setup >/dev/null 2>&1
check $? "system setup"
CONF="$XDG_CONFIG_HOME/pipewire/pipewire.conf.d/99-eqforge.conf"
[ -f "$CONF" ] && ok "pipewire drop-in written" || bad "pipewire drop-in written"
echo "# user edit" >> "$CONF"
eqf system setup >/dev/null 2>&1
check $? "system setup is re-runnable"
grep -q "user edit" "$CONF" 2>/dev/null \
    && bad "user edit survived in place (should be replaced)" \
    || ok "user edit was replaced"
ls "$XDG_CONFIG_HOME"/pipewire/pipewire.conf.d/*.bak.* >/dev/null 2>&1 \
    && ok "backup of the edited file kept" || bad "backup of the edited file kept"
grep -rq "user edit" "$XDG_CONFIG_HOME"/pipewire/pipewire.conf.d/*.bak.* 2>/dev/null \
    && ok "backup contains the user's edit" || bad "backup contains the user's edit"

eqf doctor >/dev/null 2>&1
rc=$?
[ "$rc" -le 1 ] && ok "doctor runs (rc=$rc)" || bad "doctor returned rc=$rc"

echo
echo "======================================"
printf 'workflows: %d passed, %d failed\n' "$PASS" "$FAIL"
echo "======================================"
[ "$FAIL" -eq 0 ] || exit 1
