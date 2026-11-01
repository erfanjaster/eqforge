#!/usr/bin/env bash
# EQForge installer (user scope).
#
#   ./packaging/install.sh            build + install to ~/.local
#   ./packaging/install.sh --system   install native parts under /usr (needs root)
#
# Idempotent; safe to re-run after updates.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
MODE="user"
[[ "${1:-}" == "--system" ]] && MODE="system"

echo "==> building native components"
make -C core
make -C pipewire || echo "    (pipewire module skipped - headers missing?)"
make -C rthost   || echo "    (rt host skipped - libjack missing?)"

if [[ "$MODE" == "system" ]]; then
    PREFIX=/usr
    [[ $EUID -ne 0 ]] && { echo "run with sudo for --system"; exit 1; }
else
    PREFIX="$HOME/.local"
    mkdir -p "$PREFIX"
fi

echo "==> installing native parts to $PREFIX"
make -C core install PREFIX="$PREFIX"
[[ -d pipewire/build ]] && make -C pipewire install DESTDIR="${DESTDIR:-}" || true
[[ -x rthost/build/eqforge-rt ]] && make -C rthost install DESTDIR="${DESTDIR:-}" || true

echo "==> installing python package"
if [[ "$MODE" == "system" ]]; then
    python3 -m pip install --break-system-packages "$ROOT" 2>/dev/null || python3 -m pip install "$ROOT"
else
    python3 -m pip install --user -e "$ROOT"
fi

echo "==> installing desktop integration"
if [[ "$MODE" == "user" ]]; then
    mkdir -p "$HOME/.local/share/applications"
    sed "s|^Exec=.*|Exec=$PREFIX/bin/eqforge gui|" \
        packaging/eqforge.desktop > "$HOME/.local/share/applications/eqforge.desktop"
    mkdir -p "$HOME/.config/systemd/user"
    cp packaging/systemd/eqforge-daemon.service "$HOME/.config/systemd/user/" 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
fi

echo
echo "==> done. Verify with:"
echo "      $PREFIX/bin/eqforge doctor"
echo
echo "Next steps:"
echo "  eqforge system setup     # write pipewire/systemd integration files"
echo "  eqforge gui              # open the graphical interface"
