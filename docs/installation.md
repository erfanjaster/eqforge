# Installation & Building

## Requirements

| Component | Needed for | Packages (Debian/Ubuntu) | Packages (Fedora) |
|---|---|---|---|
| Python ≥ 3.10 | control plane | `python3` | `python3` |
| numpy, scipy | analysis, fallback engine | `python3-numpy python3-scipy` | `python3-numpy python3-scipy` |
| soundfile | WAV/FLAC/OGG I/O | `python3-soundfile` | `python3-soundfile` |
| gcc/clang + make | native core | `build-essential` | `gcc make` |
| libpipewire-0.3-dev, libspa | SPA filter module (optional) | `libpipewire-0.3-dev libspa-0.2-dev` | `pipewire-devel` |
| libjack dev headers | RT host (optional) | `libjack-dev` (or pipewire-jack) | `jack-audio-connection-kit-devel` |
| ffmpeg | MP3/M4A/OPUS I/O (optional) | `ffmpeg` | `ffmpeg` |
| pipewire, wireplumber | system audio | `pipewire pipewire-audio wireplumber` | `pipewire-audio wireplumber` |

Arch: `sudo pacman -S base-devel python-numpy python-scipy python-soundfile
pipewire pipewire-alsa wireplumber ffmpeg`

## Build

```sh
git clone <repo> eqforge && cd eqforge

make                # builds core + pipewire module + rt host
make test           # C tests, SPA smoke test, pytest suite
```

Individual pieces:

```sh
make -C core                      # build/libeqforge.{so,a}
make -C core test                 # 900+ DSP checks
make -C core test-cxx             # C++17 compile canary
make -C pipewire                  # build/libeqforge-filter.so
make -C rthost                    # build/eqforge-rt
```

## Install

**User-scope (recommended for development):**

```sh
make install PREFIX=$HOME/.local
python3 -m pip install --user -e .
eqforge doctor        # verify everything
```

**System-wide:**

```sh
sudo make install PREFIX=/usr
sudo pip install .    # or build a wheel: python -m build && pip install dist/*
```

Installs:

* `~/.local/lib/eqforge/libeqforge.so` — DSP core
* `~/.local/include/eqforge/*.h` — C API headers
* `/usr/lib/<arch>-linux-gnu/spa-0.2/eqforge/libeqforge-filter.so` — SPA module
* `~/.local/libexec/eqforge/eqforge-rt` — RT host
* `eqforge` console script

## One-script install and update

`scripts/eqforge-update.sh` is both the installer and the updater. It is the
only file a new user needs:

```sh
# from GitHub (nothing else required)
curl -fsSL https://raw.githubusercontent.com/erfanjaster/eqforge/main/scripts/eqforge-update.sh | bash

# or from a checkout you already have
./scripts/eqforge-update.sh
```

What it decides by itself:

| Machine state | What happens |
|---|---|
| nothing installed | full install: native core, rt host, SPA module (if its headers exist), python package in a private venv, login units offered |
| ours installed, older revision | update in place; previous revision and native binaries kept for `--rollback` |
| ours installed, same revision | "already up to date", no changes (exit 0) |
| a `pip install --user` or a git checkout | installs its own managed copy, says so, and tells you how to remove the old one |

Everything it manages lives in one place:

```
~/.local/bin/eqforge              symlink into the venv
~/.local/bin/eqforge-rt           symlink to the native realtime host
~/.local/lib/eqforge/             libeqforge.so + headers
~/.local/libexec/eqforge/         eqforge-rt
~/.local/lib/spa-0.2/eqforge/     SPA filter module
~/.local/share/eqforge/venv/      python environment
~/.local/share/eqforge/src/<ref>/ the source revision this build came from
~/.local/share/eqforge/install.json   state: version, ref, source, prefix
~/.local/share/eqforge/backups/   native binaries of the previous version
```

The venv is created with `--system-site-packages`, so distribution packages
(`python3-numpy`, `python3-scipy`, `python3-soundfile`) satisfy the
dependencies and an install works on a machine without PyPI access.

Common invocations:

```sh
./scripts/eqforge-update.sh --check                 # installed vs. available
./scripts/eqforge-update.sh --dry-run               # print the plan
./scripts/eqforge-update.sh --ref v1.2.0            # pin a release
./scripts/eqforge-update.sh --channel main          # tip of main
./scripts/eqforge-update.sh --source-dir ~/src/eqforge --editable   # dev
./scripts/eqforge-update.sh --rollback              # previous version
./scripts/eqforge-update.sh --uninstall [--purge]   # remove
```

Existing systemd user units are stopped before binaries are replaced and
restarted afterwards, so an update does not leave a half-linked audio graph
behind. `--no-service`, `--no-autostart` and `--no-native` opt out of the
three side effects an unattended run performs.

## Session integration

```sh
eqforge system setup
```

writes:

* `~/.config/pipewire/pipewire.conf.d/99-eqforge.conf` — hosts the SPA filter
  via `libpipewire-module-adapter`
* `~/.config/systemd/user/eqforge-rt.service` — RT host as a user service

Then either:

```sh
systemctl --user daemon-reload
systemctl --user restart pipewire pipewire-pulse wireplumber   # SPA path
systemctl --user enable --now eqforge-rt                       # RT path
```

Optional desktop extras (see `packaging/`):

* `eqforge.desktop` → `~/.local/share/applications/` (launches the GUI)
* `eqforge-daemon.service` → `~/.config/systemd/user/` (autostart daemon)

## Verifying

```sh
eqforge doctor
```

checks: native core load, python deps, codec support, PipeWire tools, default
sink, daemon, RT host, and runs an end-to-end DSP self-test on synthetic
audio. Everything should PASS (RT host is WARN until started — that's fine).

## Uninstall

With the script (preferred - it also removes the venv and the units):

```sh
./scripts/eqforge-update.sh --uninstall          # keeps your profiles
./scripts/eqforge-update.sh --uninstall --purge   # removes those too
```

By hand:

```sh
rm -rf ~/.local/lib/eqforge ~/.local/include/eqforge \
       ~/.local/libexec/eqforge ~/.config/eqforge \
       ~/.local/share/eqforge ~/.local/state/eqforge
rm ~/.config/pipewire/pipewire.conf.d/99-eqforge.conf \
   ~/.config/systemd/user/eqforge-*.service
pip uninstall eqforge
```
