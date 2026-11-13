#!/usr/bin/env bash
# EQForge installer + updater  (one script, both jobs)
#
#   curl -fsSL https://raw.githubusercontent.com/erfanjaster/eqforge/main/scripts/eqforge-update.sh | bash
#
#   ./scripts/eqforge-update.sh              # install if absent, update if present
#   ./scripts/eqforge-update.sh --check      # only say what would happen
#   ./scripts/eqforge-update.sh --ref v1.2.0 # install a specific tag/commit
#   ./scripts/eqforge-update.sh --rollback   # go back to the previous version
#
# Why one script: there is no state a hand-written installer keeps that this
# cannot rediscover. It looks at what is already on the machine (a managed
# install of ours, a pip --user install, a git checkout), decides between
# "fresh install" and "update", and does the same build in both cases.
#
# Layout it manages (all under $PREFIX, default ~/.local):
#
#   ~/.local/bin/eqforge              -> symlink into the venv
#   ~/.local/bin/eqforge-rt           -> symlink to the native realtime host
#   ~/.local/lib/eqforge/             libeqforge.so + headers
#   ~/.local/libexec/eqforge/         eqforge-rt
#   ~/.local/lib/spa-0.2/eqforge/     SPA filter module (module deployment)
#   ~/.local/share/eqforge/venv/      python environment (system site pkgs on)
#   ~/.local/share/eqforge/src/<ref>/ the source revision this build came from
#   ~/.local/share/eqforge/install.json   what is installed (the state file)
#   ~/.local/share/eqforge/backups/    native binaries of the previous version
#
# Notes
#   * No sudo needed for the default (user) install. Root is only touched with
#     --system, and even then only for the prefix.
#   * Existing installs are never silently destroyed: the previous native
#     binaries are backed up and the previous source revision is kept, which
#     is what `--rollback` restores.
#   * The script is safe to re-run. "Already up to date" is a successful,
#     quiet, change-free outcome.
#
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-EQForge-Commercial
set -euo pipefail

SCRIPT_VERSION="1.1.0"
REPO="${EQFORGE_REPO:-erfanjaster/eqforge}"
GITHUB="https://github.com/${REPO}.git"
CODELOAD="https://codeload.github.com/${REPO}/tar.gz"
CHANNEL="${EQFORGE_CHANNEL:-stable}"        # stable = newest git tag, main = tip

# ------------------------------------------------------------------ output --
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
    C_DIM=$'\033[2m'; C_B=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_OK=; C_WARN=; C_ERR=; C_DIM=; C_B=; C_OFF=
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$C_B" "$C_OFF" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n%serror:%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'EOF'
EQForge installer + updater. Installs when nothing is installed, updates an
existing install otherwise. Both cases build from the same source revision.

  --check               report installed vs. available version; change nothing
  --dry-run             print the plan, change nothing
  --yes, -y             never prompt (needed for `curl | bash`)
  --ref REF             git tag / branch / commit to install
  --channel stable|main newest release tag (default) or tip of main
  --source-dir DIR      build from a local checkout instead of GitHub
  --editable            install the python package in editable mode (dev)
  --prefix DIR          install prefix (default: ~/.local)
  --system              prefix /usr (needs root)
  --no-native           skip the C builds (python package only)
  --no-service          do not stop/restart the systemd user units
  --no-autostart        do not offer to enable login autostart
  --force               reinstall even when the version is unchanged
  --rollback            reinstall the previous version recorded in install.json
  --uninstall           remove the managed install (keeps ~/.config/eqforge)
  --purge               with --uninstall: also remove profiles and config
  --self-test           run the script's own sanity checks (offline, harmless)
  --version             print this script's version
  -h, --help            this text

Environment: EQFORGE_REPO, EQFORGE_CHANNEL, EQFORGE_PREFIX, NO_COLOR
EOF
}

# ------------------------------------------------------------------ state ----
MODE="update"          # update | check | dryrun | rollback | uninstall | selftest
ASSUME_YES=0
REF=""
SOURCE_DIR=""
EDITABLE=0
DO_NATIVE=1
DO_SERVICE=1
DO_AUTOSTART=1
FORCE=0
PURGE=0
PREFIX="${EQFORGE_PREFIX:-$HOME/.local}"
DATA="${EQFORGE_DATA:-}"

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --check) MODE="check" ;;
            --dry-run) MODE="dryrun" ;;
            --yes|-y) ASSUME_YES=1 ;;
            --ref) REF="${2:?--ref needs a value}"; shift ;;
            --ref=*) REF="${1#*=}" ;;
            --channel) CHANNEL="${2:?--channel needs a value}"; shift ;;
            --channel=*) CHANNEL="${1#*=}" ;;
            --source-dir) SOURCE_DIR="${2:?--source-dir needs a value}"; shift ;;
            --source-dir=*) SOURCE_DIR="${1#*=}" ;;
            --editable) EDITABLE=1 ;;
            --prefix) PREFIX="${2:?--prefix needs a value}"; shift ;;
            --prefix=*) PREFIX="${1#*=}" ;;
            --system) PREFIX="/usr"; DATA="/usr/local/lib/eqforge" ;;
            --no-native) DO_NATIVE=0 ;;
            --no-service) DO_SERVICE=0 ;;
            --no-autostart) DO_AUTOSTART=0 ;;
            --force) FORCE=1 ;;
            --rollback) MODE="rollback" ;;
            --uninstall) MODE="uninstall" ;;
            --purge) PURGE=1 ;;
            --self-test) MODE="selftest" ;;
            --version) printf 'eqforge-update %s\n' "$SCRIPT_VERSION"; exit 0 ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option: $1 (try --help)" ;;
        esac
        shift
    done
    PREFIX="${PREFIX/#\~/$HOME}"
    [ -n "$DATA" ] || DATA="${XDG_DATA_HOME:-$HOME/.local/share}/eqforge"
    DATA="${DATA/#\~/$HOME}"
}

# Paths (assigned in setup_paths)
BIN_DIR=""; LIB_DIR=""; LIBEXEC_DIR=""; SPA_DIR=""; VENV=""; SRC_ROOT=""
MARKER=""; BACKUP_DIR=""; LOCK=""

setup_paths() {
    BIN_DIR="$PREFIX/bin"
    LIB_DIR="$PREFIX/lib/eqforge"
    LIBEXEC_DIR="$PREFIX/libexec/eqforge"
    SPA_DIR="$PREFIX/lib/spa-0.2/eqforge"
    VENV="$DATA/venv"
    SRC_ROOT="$DATA/src"
    MARKER="$DATA/install.json"
    BACKUP_DIR="$DATA/backups"
}

# ------------------------------------------------------------------ helpers --
# Small python helper: json field out of the state file, empty when absent.
json_get() {      # json_get FILE KEY
    [ -f "$1" ] || return 0
    python3 - "$1" "$2" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
value = data
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        sys.exit(0)
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
elif value is not None:
    print(value)
PY
}

json_write() {    # json_write FILE key=value ...  (strings, bools, nulls)
    python3 - "$@" <<'PY'
import json, sys
path = sys.argv[1]
fields = {}
for item in sys.argv[2:]:
    key, _, raw = item.partition("=")
    if raw in ("true", "false"):
        fields[key] = raw == "true"
    elif raw == "null" or raw == "":
        fields[key] = None
    else:
        fields[key] = raw
with open(path, "w", encoding="utf-8") as fh:
    json.dump(fields, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
}

# version comparison using GNU sort -V (Linux): is $1 newer than $2?
ver_gt() {
    local a="${1#v}" b="${2#v}"
    [ -z "$a" ] && return 1
    [ -z "$b" ] && return 0
    [ "$a" = "$b" ] && return 1
    [ "$(printf '%s\n%s\n' "$a" "$b" | sort -V | tail -n1)" = "$a" ]
}

first_semver() { grep -oE '[0-9]+\.[0-9]+\.[0-9]+' <<<"$1" | head -n1 || true; }

confirm() {       # confirm "question" -> 0 yes / 1 no
    [ "$ASSUME_YES" = 1 ] && return 0
    [ -t 0 ] || return 1
    local reply
    printf '  %s [Y/n] ' "$1"
    read -r reply || true
    case "${reply:-y}" in [Yy]*|"") return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- detection --
detect_installed() {
    # Sets: INSTALLED_VERSION INSTALLED_KIND INSTALLED_SOURCE INSTALLED_REF
    INSTALLED_VERSION=""; INSTALLED_KIND="none"; INSTALLED_SOURCE=""
    INSTALLED_REF=""; INSTALLED_STAMP=""

    if [ -f "$MARKER" ]; then
        INSTALLED_VERSION="$(json_get "$MARKER" version)"
        INSTALLED_SOURCE="$(json_get "$MARKER" source)"
        INSTALLED_REF="$(json_get "$MARKER" ref)"
        INSTALLED_STAMP="$(json_get "$MARKER" stamp)"
        INSTALLED_KIND="managed"
        return
    fi
    if [ -x "$BIN_DIR/eqforge" ]; then
        INSTALLED_VERSION="$(first_semver "$("$BIN_DIR/eqforge" version 2>&1 || true)")"
        INSTALLED_KIND="external"
        return
    fi
    if have python3; then
        # Probe from / so a checkout in the *current* directory is not mistaken
        # for an installed copy (that is what --source-dir is for), while a
        # `pip install --user` is still found through the user site directory.
        local where version
        # (python3 -c rather than a heredoc: a heredoc inside a command
        # substitution is a bash parsing minefield and buys nothing here.)
        where="$(cd / && python3 -c 'import eqforge, os; print(os.path.dirname(os.path.dirname(eqforge.__file__)))' 2>/dev/null || true)"
        version="$(cd / && python3 -c 'from eqforge.version import __version__; print(__version__)' 2>/dev/null || true)"
        if [ -n "${where:-}" ] || [ -n "${version:-}" ]; then
            INSTALLED_VERSION="${version:-}"
            INSTALLED_KIND="python"
            INSTALLED_SOURCE="${where:-}"
            if [ -n "${where:-}" ] && [ -d "$where/.git" ]; then
                INSTALLED_KIND="checkout"
            fi
        fi
    fi
}

latest_ref() {
    # Sets TARGET_REF (what to fetch), TARGET_VERSION (what to show) and
    # TARGET_STAMP (identity used to decide "already up to date"). The stamp
    # matters on the main channel, where the version number does not move
    # between commits: comparing versions there would report "up to date"
    # forever.
    if [ -n "$REF" ]; then
        TARGET_REF="$REF"
        TARGET_STAMP="$REF"
        TARGET_VERSION="$(first_semver "$REF")"
        [ -n "$TARGET_VERSION" ] || TARGET_VERSION="$REF"
        return
    fi
    if [ "$CHANNEL" = "main" ]; then
        TARGET_REF="main"
        TARGET_VERSION=""
        TARGET_STAMP="main"
        if have git; then
            local sha
            sha="$(git ls-remote "$GITHUB" refs/heads/main 2>/dev/null \
                   | awk '{print $1}' | head -n1)"
            [ -n "$sha" ] && TARGET_STAMP="main@${sha:0:12}"
        fi
        TARGET_VERSION="$TARGET_STAMP"
        return
    fi
    local tag=""
    if have git; then
        tag="$(git ls-remote --tags --refs --sort=-v:refname "$GITHUB" \
               'refs/tags/v*' 2>/dev/null | awk -F/ '{print $NF}' | head -n1)"
    fi
    if [ -z "$tag" ]; then
        # No usable tags (or no git): fall back to the tip of main rather than
        # failing - "can't tell what the latest release is" must not become
        # "you cannot install".
        warn "no release tag found; using main"
        CHANNEL="main"
        latest_ref
        return
    fi
    TARGET_REF="$tag"
    TARGET_VERSION="${tag#v}"
    TARGET_STAMP="$tag"
}

# ------------------------------------------------------------------- source --
fetch_source() {   # fetch_source -> sets SRC_DIR
    local slug
    slug="$(printf '%s' "${TARGET_REF}" | tr '/ ' '__')"
    SRC_DIR="$SRC_ROOT/$slug"

    if [ -n "$SOURCE_DIR" ]; then
        SRC_DIR="$(cd "$SOURCE_DIR" && pwd)"
        [ -f "$SRC_DIR/pyproject.toml" ] \
            || die "$SRC_DIR does not look like an EQForge checkout"
        ok "source: $SRC_DIR (local)"
        return
    fi
    if [ "$MODE" = "dryrun" ]; then
        info "would fetch $TARGET_REF into $SRC_DIR"
        return
    fi
    mkdir -p "$SRC_ROOT"
    if [ -d "$SRC_DIR/.git" ]; then
        info "updating checkout at $SRC_DIR"
        git -C "$SRC_DIR" fetch --depth 1 origin "$TARGET_REF" >/dev/null 2>&1 \
            && git -C "$SRC_DIR" checkout -q FETCH_HEAD \
            || die "cannot fetch $TARGET_REF"
    elif have git; then
        info "cloning $TARGET_REF"
        git clone --depth 1 --branch "$TARGET_REF" "$GITHUB" "$SRC_DIR" \
            >/dev/null 2>&1 || die "git clone failed (is $TARGET_REF a real ref?)"
    else
        info "downloading $TARGET_REF (no git available)"
        mkdir -p "$SRC_DIR"
        curl -fsSL "$CODELOAD/$TARGET_REF" \
            | tar -xz -C "$SRC_DIR" --strip-components=1 \
            || die "download of $TARGET_REF failed"
    fi
    ok "source: $SRC_DIR"
}

# -------------------------------------------------------------------- build --
build_native() {
    [ "$DO_NATIVE" = 1 ] || { warn "skipping native build (--no-native)"; return; }
    [ -d "$SRC_DIR/core" ] || die "no core/ in $SRC_DIR"

    step "native core"
    if [ "$MODE" = "dryrun" ]; then
        info "would: make -C $SRC_DIR/core && make -C core install PREFIX=$PREFIX"
    else
        make -C "$SRC_DIR/core" >/dev/null || die "core build failed (need gcc + make)"
        make -C "$SRC_DIR/core" install PREFIX="$PREFIX" >/dev/null
        ok "libeqforge installed in $LIB_DIR"
    fi

    step "realtime host"
    if [ "$MODE" = "dryrun" ]; then
        info "would: make -C $SRC_DIR/rthost (optional)"
    elif make -C "$SRC_DIR/rthost" >/dev/null 2>&1; then
        mkdir -p "$LIBEXEC_DIR" "$BIN_DIR"
        install -m 755 "$SRC_DIR/rthost/build/eqforge-rt" "$LIBEXEC_DIR/eqforge-rt"
        ln -sf "$LIBEXEC_DIR/eqforge-rt" "$BIN_DIR/eqforge-rt"
        ok "eqforge-rt installed in $LIBEXEC_DIR"
    else
        warn "eqforge-rt not built (libjack / pipewire-jack headers missing)"
        info "install libjack-jackd2-dev (Debian) or pipewire-jack, then re-run"
    fi

    step "SPA filter module"
    if [ "$MODE" = "dryrun" ]; then
        info "would: make -C $SRC_DIR/pipewire (optional)"
    elif make -C "$SRC_DIR/pipewire" >/dev/null 2>&1; then
        mkdir -p "$SPA_DIR"
        install -m 755 "$SRC_DIR/pipewire/build/libeqforge-filter.so" \
            "$SPA_DIR/libeqforge-filter.so"
        ok "module installed in $SPA_DIR"
        if [ "$PREFIX" != "/usr" ]; then
            info "user-prefix modules are only loaded with SPA_PLUGIN_DIR or a"
            info "system install (sudo $0 --system); the rt host needs neither"
        fi
    else
        warn "SPA module not built (libspa-0.2 headers missing) - optional"
    fi
}

capture_version() {
    NEW_VERSION=""
    [ "$MODE" = "dryrun" ] && return 0
    [ -x "$BIN_DIR/eqforge" ] || return 0
    NEW_VERSION="$(first_semver "$("$BIN_DIR/eqforge" version 2>&1 || true)")"
}

install_python() {
    step "python package"
    if [ "$MODE" = "dryrun" ]; then
        info "would: create $VENV and pip install $SRC_DIR"
        return
    fi
    if [ ! -x "$VENV/bin/python" ]; then
        info "creating $VENV"
        python3 -m venv --system-site-packages "$VENV" 2>/dev/null \
            || python3 -m venv "$VENV" \
            || die "cannot create a venv (Debian/Ubuntu: apt install python3-venv)"
    fi
    "$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    local mode_flag=""
    [ "$EDITABLE" = 1 ] && mode_flag="-e"
    info "installing $TARGET_VERSION"
    # --no-deps is wrong here (numpy/scipy/soundfile are real requirements), so
    # a failure to reach the index is reported as such instead of half-hiding.
    if ! "$VENV/bin/python" -m pip install --quiet --upgrade $mode_flag \
            "$SRC_DIR" 2>"$DATA/.pip.err"; then
        warn "pip install failed:"
        sed 's/^/      /' "$DATA/.pip.err" >&2 || true
        die "python install failed (network? see $DATA/.pip.err)"
    fi
    mkdir -p "$BIN_DIR"
    ln -sf "$VENV/bin/eqforge" "$BIN_DIR/eqforge"
    ok "eqforge -> $VENV/bin/eqforge"
}

# ------------------------------------------------------------------ service --
unit_exists() { systemctl --user cat "$1" >/dev/null 2>&1; }

stop_units() {
    [ "$DO_SERVICE" = 1 ] || return 0
    [ "$MODE" = "dryrun" ] && return 0
    have systemctl || return 0
    for unit in eqforge-rt.service eqforge-daemon.service; do
        if unit_exists "$unit"; then
            systemctl --user stop "$unit" >/dev/null 2>&1 || true
        fi
    done
}

start_units() {
    [ "$DO_SERVICE" = 1 ] || return 0
    [ "$MODE" = "dryrun" ] && return 0
    have systemctl || { info "no systemctl: not touching services"; return 0; }
    local units=()
    for unit in eqforge-rt.service eqforge-daemon.service; do
        unit_exists "$unit" && units+=("$unit")
    done
    [ ${#units[@]} -gt 0 ] || return 0
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user restart "${units[@]}" >/dev/null 2>&1 \
        && ok "restarted ${units[*]}" \
        || warn "could not restart ${units[*]} (check: journalctl --user -u eqforge-rt)"
}

backup_native() {
    [ "$MODE" = "dryrun" ] && return 0
    local stamp="$DATA/backups/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$stamp"
    local found=0
    for rel in lib/eqforge libexec/eqforge bin/eqforge-rt lib/spa-0.2/eqforge; do
        if [ -e "$PREFIX/$rel" ]; then
            found=1
            mkdir -p "$stamp/$(dirname "$rel")"
            cp -a "$PREFIX/$rel" "$stamp/$rel" 2>/dev/null || true
        fi
    done
    if [ "$found" = 1 ]; then
        echo "$stamp" > "$DATA/last-backup"
        ok "previous native files backed up in $stamp"
    fi
}

restore_native() {
    local stamp="${1:-}"
    [ -n "$stamp" ] || stamp="$(cat "$DATA/last-backup" 2>/dev/null || true)"
    [ -n "$stamp" ] && [ -d "$stamp" ] || return 1
    for rel in lib/eqforge libexec/eqforge bin/eqforge-rt lib/spa-0.2/eqforge; do
        [ -e "$stamp/$rel" ] || continue
        mkdir -p "$PREFIX/$(dirname "$rel")"
        cp -a "$stamp/$rel" "$PREFIX/$rel" 2>/dev/null || true
    done
    ok "native files restored from $stamp"
}

# ------------------------------------------------------------------- verify --
verify() {
    step "verification"
    if [ "$MODE" = "dryrun" ]; then
        info "would: $BIN_DIR/eqforge version && $BIN_DIR/eqforge doctor"
        return
    fi
    local out version
    if out="$("$BIN_DIR/eqforge" version 2>&1)"; then
        version="$(first_semver "$out")"
        ok "eqforge version ${version:-unknown}"
        # Only meaningful when the target is a release: on the main channel
        # the stamp is a commit, not a version, and the package version only
        # changes when the maintainer bumps it.
        case "$TARGET_VERSION" in
            [0-9]*.[0-9]*.[0-9]*)
                if [ -n "${version:-}" ] && [ "$version" != "$TARGET_VERSION" ]; then
                    warn "installed $version, expected $TARGET_VERSION"
                fi ;;
        esac
    else
        warn "eqforge version failed:"
        sed 's/^/      /' <<<"$out" >&2 || true
        return 1
    fi
    if "$BIN_DIR/eqforge" doctor >/dev/null 2>&1; then
        ok "eqforge doctor: clean"
    else
        warn "eqforge doctor reported problems: run '$BIN_DIR/eqforge doctor'"
    fi
    if have pw-dump; then
        ok "pipewire tools found"
    else
        warn "pw-dump not found: system-wide processing needs PipeWire installed"
    fi
}

write_marker() {
    [ "$MODE" = "dryrun" ] && return 0
    local sha=""
    [ -d "$SRC_DIR/.git" ] && sha="$(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || true)"
    local autostart="false"
    [ "$AUTOSTART_ENABLED" = 1 ] && autostart="true"
    mkdir -p "$DATA"
    local prev_version="${INSTALLED_VERSION:-}" prev_source="${INSTALLED_SOURCE:-}"
    local prev_ref="${INSTALLED_REF:-}"
    json_write "$MARKER" \
        "version=${NEW_VERSION:-$TARGET_VERSION}" "ref=$TARGET_REF" \
        "stamp=$TARGET_STAMP" "sha=$sha" \
        "installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "prefix=$PREFIX" "venv=$VENV" "source=$SRC_DIR" \
        "channel=$CHANNEL" "autostart=$autostart" "editable=$([ "$EDITABLE" = 1 ] && echo true || echo false)" \
        "previous_version=$prev_version" "previous_source=$prev_source" \
        "previous_ref=$prev_ref"
    ok "state written to $MARKER"
}

autostart_step() {
    AUTOSTART_ENABLED=0
    if [ -f "$HOME/.config/systemd/user/eqforge-rt.service" ]; then
        AUTOSTART_ENABLED=1
    fi
    if [ "$DO_AUTOSTART" = 1 ] && [ "$AUTOSTART_ENABLED" = 0 ] && confirm \
            "start EQForge at login and keep it in the audio path (recommended)?"; then
        if [ "$MODE" = "dryrun" ]; then
            info "would: $BIN_DIR/eqforge system autostart on"
        elif "$BIN_DIR/eqforge" system autostart on >/dev/null 2>&1; then
            AUTOSTART_ENABLED=1
            ok "login autostart enabled"
        else
            warn "could not enable autostart (run: eqforge system autostart on)"
        fi
    elif [ "$AUTOSTART_ENABLED" = 1 ] && [ "$MODE" != "dryrun" ]; then
        # Units contain absolute paths to the binary and the venv; regenerate
        # them against this install so an update cannot leave them stale.
        "$BIN_DIR/eqforge" system autostart on >/dev/null 2>&1 \
            && ok "login units refreshed" \
            || warn "could not refresh the login units"
    fi
}

# ------------------------------------------------------------------ actions --
plan_report() {
    local verb="update"
    if [ "$INSTALLED_KIND" = "none" ]; then
        verb="install"
    fi
    [ "$MODE" = "dryrun" ] && verb="(dry run) $verb"
    step "summary"
    info "action        : $verb"
    info "installed     : ${INSTALLED_VERSION:-none} (${INSTALLED_KIND})"
    [ -n "$INSTALLED_SOURCE" ] && info "existing path : $INSTALLED_SOURCE"
    info "available     : ${TARGET_VERSION:-unknown} (${TARGET_STAMP:-auto})"
    info "prefix        : $PREFIX"
    info "python env    : $VENV"
    info "state file    : $MARKER"
    if [ "$INSTALLED_KIND" != "none" ] && [ "$INSTALLED_KIND" != "managed" ]; then
        warn "an unmanaged install was found; this script installs its own copy"
        info "the new binary takes precedence on PATH; remove the old one with"
        info "  python3 -m pip uninstall eqforge     (if it was pip --user)"
    fi
}

perform_install() {
    backup_native
    stop_units
    fetch_source
    build_native
    install_python
    capture_version
    autostart_step
    write_marker
    start_units
    verify

    step "done"
    ok "EQForge ${TARGET_VERSION} is installed in $PREFIX"
    say ""
    info "next steps:"
    info "  $BIN_DIR/eqforge system start --manage    # route audio through it"
    info "  $BIN_DIR/eqforge system always-on on --now # zero-gap routing policy"
    info "  $BIN_DIR/eqforge gui                      # graphical interface"
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "$BIN_DIR is not on PATH: add it, or call eqforge by full path" ;;
    esac
}

do_rollback() {
    [ -f "$MARKER" ] || die "nothing to roll back to ($MARKER is missing)"
    local version source ref
    version="$(json_get "$MARKER" previous_version)"
    source="$(json_get "$MARKER" previous_source)"
    ref="$(json_get "$MARKER" previous_ref)"
    [ -n "${version:-}" ] || die "no previous version recorded in $MARKER"
    step "rollback to $version"
    if [ -n "${source:-}" ] && [ -d "$source" ]; then
        SOURCE_DIR="$source"
        TARGET_REF="${ref:-$version}"
        TARGET_VERSION="$version"
        backup_native
        stop_units
        fetch_source
        build_native
        install_python
        # build_native has already installed the native artifacts from the
        # previous source tree. Do not restore the backup made above: that is
        # the currently-installed (newer) build and would undo the rollback.
        # forget the version we just rolled back from, so a second --rollback
        # does not bounce between the same two builds
        json_write "$MARKER" "version=$version" "ref=$TARGET_REF" \
            "source=$SOURCE_DIR" "prefix=$PREFIX" "venv=$VENV" \
            "installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            "previous_version=" "previous_source=" "previous_ref="
        autostart_step
        start_units
        verify
        return
    fi
    warn "previous source is gone; restoring the backed-up native binaries only"
    restore_native || die "no backup available either - reinstall with --ref $version"
    start_units
}

do_uninstall() {
    step "uninstall"
    stop_units
    if have systemctl; then
        for unit in eqforge-rt.service eqforge-daemon.service; do
            systemctl --user disable "$unit" >/dev/null 2>&1 || true
        done
        rm -f "$HOME/.config/systemd/user/eqforge-rt.service" \
              "$HOME/.config/systemd/user/eqforge-daemon.service" 2>/dev/null || true
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
    rm -f "$BIN_DIR/eqforge" "$BIN_DIR/eqforge-rt"
    rm -rf "$LIB_DIR" "$LIBEXEC_DIR" "$SPA_DIR" "$VENV" "$SRC_ROOT" "$MARKER"
    ok "managed files removed from $PREFIX and $DATA"
    if [ "$PURGE" = 1 ]; then
        rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/eqforge"
        rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/eqforge"
        ok "profiles, config and logs removed"
    else
        info "your profiles and settings are kept in"
        info "  ${XDG_CONFIG_HOME:-$HOME/.config}/eqforge   (use --purge to remove)"
    fi
}

run_self_test() {
    step "self-test"
    local failures=0
    t() { if eval "$2"; then ok "$1"; else warn "FAIL: $1"; failures=$((failures+1)); fi; }

    t "python3 present"           "have python3"
    t "bash version >= 4"         "[ \"${BASH_VERSINFO[0]}\" -ge 4 ]"
    t "ver_gt 1.2.0 1.1.9"        "ver_gt 1.2.0 1.1.9"
    t "! ver_gt 1.0.0 1.0.0"      "! ver_gt 1.0.0 1.0.0"
    t "ver_gt 1.10.0 1.9.0"       "ver_gt 1.10.0 1.9.0"
    t "ver_gt v2.0.0 1.99.99"     "ver_gt v2.0.0 1.99.99"
    t "! ver_gt 0.9.0 1.0.0"      "! ver_gt 0.9.0 1.0.0"
    t "first_semver"              "[ \"\$(first_semver 'eqforge 1.4.2 (git)')\" = 1.4.2 ]"

    local tmp
    tmp="$(mktemp -d)"
    json_write "$tmp/state.json" "version=1.2.3" "autostart=true" "source=/x"
    t "json roundtrip version"    "[ \"\$(json_get "$tmp/state.json" version)\" = 1.2.3 ]"
    t "json roundtrip bool"       "[ \"\$(json_get "$tmp/state.json" autostart)\" = true ]"
    t "json missing key empty"    "[ -z \"\$(json_get "$tmp/state.json" nope)\" ]"
    rm -rf "$tmp"

    if [ "$failures" -eq 0 ]; then
        say ""
        ok "self-test: all checks passed"
        return 0
    fi
    say ""
    warn "self-test: $failures check(s) failed"
    return 1
}

# --------------------------------------------------------------------- main --
main() {
    parse_args "$@"
    setup_paths

    if [ "$MODE" = "selftest" ]; then
        run_self_test
        exit $?
    fi

    step "EQForge installer / updater $SCRIPT_VERSION"
    have python3 || die "python3 is required (install python3 first)"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "python 3.10+ is required (found $(python3 -V 2>&1))"

    if [ "$PREFIX" = "/usr" ] && [ "$EUID" -ne 0 ]; then
        die "a /usr install needs root: use sudo, or drop --system"
    fi
    if [ "$MODE" != "uninstall" ] && [ "$EUID" -eq 0 ] && [ "$PREFIX" != "/usr" ]; then
        warn "running as root; the default install prefix is for the user"
        info "pass --system for a /usr install, or run as your normal user"
    fi

    # Read-only modes must not create the data directory just to inspect state.
    # Besides being surprising, that violated the documented --check/--dry-run
    # promise that they leave the machine unchanged.
    if [ "$MODE" != "check" ] && [ "$MODE" != "dryrun" ]; then
        mkdir -p "$DATA"
    fi
    if have flock && [ "$MODE" != "check" ] && [ "$MODE" != "dryrun" ]; then
        LOCK="$DATA/.update.lock"
        exec 9>"$LOCK"
        flock -n 9 || die "another update is already running"
    fi

    case "$MODE" in
        uninstall) do_uninstall; exit 0 ;;
    esac

    detect_installed
    if [ "$MODE" = "rollback" ]; then
        do_rollback
        exit 0
    fi
    latest_ref

    case "$MODE" in
        check|dryrun)
            plan_report
            if [ "$MODE" = "check" ]; then
                if [ "$INSTALLED_KIND" = "managed" ] && [ -n "${INSTALLED_STAMP:-}" ] \
                        && [ "${INSTALLED_STAMP}" = "${TARGET_STAMP}" ]; then
                    say ""
                    ok "up to date (${INSTALLED_VERSION})"
                    exit 0
                fi
                say ""
                if [ "$INSTALLED_KIND" = "none" ]; then
                    info "not installed: run this script without --check to install"
                elif [ "${INSTALLED_STAMP:-}" = "$TARGET_STAMP" ]; then
                    info "up to date, but not installed by this script: run with"
                    info "--force to take it over"
                else
                    info "update available: ${INSTALLED_VERSION:-unknown} -> $TARGET_VERSION"
                fi
                exit 0
            fi
            perform_install
            exit 0
            ;;
    esac

    # normal install/update path
    if [ "$INSTALLED_KIND" = "managed" ] && [ -n "${INSTALLED_STAMP:-}" ] \
            && [ "$INSTALLED_STAMP" = "$TARGET_STAMP" ] && [ "$FORCE" != 1 ]; then
        plan_report
        say ""
        ok "already up to date (${INSTALLED_VERSION}); nothing to do (--force to reinstall)"
        exit 0
    fi

    plan_report
    if ! confirm "proceed?"; then
        info "nothing changed"
        exit 0
    fi
    perform_install
}

main "$@"
