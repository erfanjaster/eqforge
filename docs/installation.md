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

```sh
rm -rf ~/.local/lib/eqforge ~/.local/include/eqforge \
       ~/.local/libexec/eqforge ~/.config/eqforge \
       ~/.local/share/eqforge ~/.local/state/eqforge
rm ~/.config/pipewire/pipewire.conf.d/99-eqforge.conf \
   ~/.config/systemd/user/eqforge-*.service
pip uninstall eqforge
```
