#!/usr/bin/env bash
# Deprecated compatibility launcher. Use scripts/eqforge.sh instead.
# Kept so existing links and checkouts continue to work after the rename.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for candidate in "$SCRIPT_DIR/eqforge.sh" "$(pwd)/scripts/eqforge.sh" "$(pwd)/eqforge.sh"; do
    if [ -f "$candidate" ]; then
        exec bash "$candidate" "$@"
    fi
done

command -v curl >/dev/null 2>&1 || {
    printf 'error: curl is required to fetch the renamed updater.\n' >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    printf 'error: python3 is required to validate the downloaded updater.\n' >&2
    exit 1
}
REPO="${EQFORGE_REPO:-erfanjaster/eqforge}"
REF="${EQFORGE_REF:-main}"
TMP="$(mktemp "${TMPDIR:-/tmp}/eqforge.sh.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
URL="https://raw.githubusercontent.com/${REPO}/${REF}/scripts/eqforge.sh"

if ! curl --fail --silent --show-error --location --proto '=https' \
        --connect-timeout 15 --max-time 120 --output "$TMP" "$URL"; then
    printf 'error: could not download the renamed updater: %s\n' "$URL" >&2
    exit 1
fi
python3 - "$TMP" <<'PY'
from pathlib import Path
import re
import sys

content = Path(sys.argv[1]).read_bytes()
if not content.startswith(b"#!/usr/bin/env bash\n"):
    sys.exit("error: GitHub did not return the EQForge Bash script; refusing to run it")
if re.search(rb"<!doctype\s+html|<html(?:\s|>)|<head(?:\s|>)|<body(?:\s|>)", content, re.I):
    sys.exit("error: GitHub returned HTML instead of Bash; refusing to run it")
content.decode("utf-8")
PY
bash -n "$TMP" || {
    printf 'error: downloaded updater has invalid Bash syntax.\n' >&2
    exit 1
}
bash "$TMP" "$@"
