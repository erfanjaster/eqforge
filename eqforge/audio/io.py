"""Audio file I/O.

Primary backend: libsndfile via `soundfile` (WAV/FLAC/OGG/AIFF...).
Secondary backend: ffmpeg/avconv subprocess (MP3/M4A/OPUS-in-mkv/etc.),
used for decoding when libsndfile cannot open the file, and optionally for
encoding to formats libsndfile lacks.

All audio in EQForge flows as float64 numpy arrays shaped [channels, frames]
with samples in [-1, 1]. Metadata (title/artist/album/ReplayGain...) is
carried alongside and re-attached on export where the format allows.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from eqforge.errors import AudioIOError
from eqforge.log import get_logger

log = get_logger("audio.io")

try:
    import soundfile as sf
    HAVE_SOUNDFILE = True
except ImportError:  # pragma: no cover
    sf = None
    HAVE_SOUNDFILE = False

DECODABLE_ALWAYS = {".wav", ".flac", ".ogg", ".aiff", ".aif", ".w64", ".caf",
                    ".mat", ".raw"}
NEEDS_FFMPEG = {".mp3", ".m4a", ".aac", ".wma", ".opus", ".mp4", ".m4b",
                ".webm", ".ape", ".wv"}


@dataclass
class AudioData:
    data: np.ndarray                     # float64 [channels, frames]
    sample_rate: int
    path: Path | None = None
    format: str | None = None            # e.g. "WAV", "MP3"
    subtype: str | None = None           # e.g. "PCM_16", "FLOAT"
    metadata: dict = field(default_factory=dict)

    @property
    def channels(self) -> int:
        return int(self.data.shape[0])

    @property
    def frames(self) -> int:
        return int(self.data.shape[1])

    @property
    def duration(self) -> float:
        return self.frames / float(self.sample_rate)


def _ffmpeg() -> str | None:
    for tool in ("ffmpeg", "avconv"):
        if shutil.which(tool):
            return tool
    return None


def probe(path: Path) -> dict:
    """Format metadata via ffprobe/ffmpeg or soundfile."""
    ff = _ffmpeg()
    if ff:
        probe_bin = "ffprobe" if shutil.which("ffprobe") else ff
        args = [probe_bin, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", str(path)]
        try:
            out = subprocess.run(args, capture_output=True, text=True,
                                 timeout=30).stdout
            info = json.loads(out)
            for st in info.get("streams", []):
                if st.get("codec_type") == "audio":
                    tags = info.get("format", {}).get("tags", {})
                    tags.update(st.get("tags", {}))
                    return {
                        "codec": st.get("codec_name"),
                        "sample_rate": int(st.get("sample_rate", 0)),
                        "channels": int(st.get("channels", 0)),
                        "duration": float(info.get("format", {}).get(
                            "duration", 0) or 0),
                        "bitrate": int(st.get("bit_rate", 0) or 0),
                        "tags": tags,
                    }
        except (subprocess.SubprocessError, json.JSONDecodeError,
                ValueError) as e:
            log.debug("probe failed for %s: %s", path, e)
    if HAVE_SOUNDFILE:
        try:
            info = sf.info(str(path))
            return {
                "codec": info.format,
                "sample_rate": info.samplerate,
                "channels": info.channels,
                "duration": info.duration,
                "bitrate": 0,
                "tags": {},
            }
        except Exception:  # noqa: BLE001
            pass
    raise AudioIOError(f"cannot probe audio file: {path}")


def _decode_ffmpeg(path: Path) -> AudioData:
    ff = _ffmpeg()
    if not ff:
        raise AudioIOError(
            f"{path.suffix} decoding needs ffmpeg (not installed)",
            hint="install ffmpeg, or convert the file first")
    with tempfile.TemporaryDirectory(prefix="eqforge-") as td:
        tmp = Path(td) / "audio.wav"
        cmd = [ff, "-v", "error", "-y", "-i", str(path),
               "-map", "a:0", "-c:a", "pcm_f32le", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not tmp.exists():
            raise AudioIOError(f"ffmpeg failed to decode {path}: "
                               f"{r.stderr.strip()[:400]}")
        data, sr = sf.read(str(tmp), dtype="float64", always_2d=True)
    meta = probe(path).get("tags", {}) if _ffmpeg() else {}
    return AudioData(data.T.copy(), sr, path=path, format=path.suffix[1:].upper(),
                     subtype="FFMPEG", metadata=meta)


def read(path: str | Path) -> AudioData:
    """Decode an audio file to float64 [channels, frames]."""
    path = Path(path)
    if not path.exists():
        raise AudioIOError(f"file not found: {path}")
    ext = path.suffix.lower()

    if HAVE_SOUNDFILE and (ext in DECODABLE_ALWAYS or ext not in NEEDS_FFMPEG):
        try:
            data, sr = sf.read(str(path), dtype="float64", always_2d=True)
            info = sf.info(str(path))
            return AudioData(data.T.copy(), sr, path=path,
                             format=info.format, subtype=info.subtype,
                             metadata={})
        except Exception as e:  # noqa: BLE001 - fall through to ffmpeg
            log.debug("soundfile could not read %s: %s", path, e)

    return _decode_ffmpeg(path)


_SUBTYPE_FOR_BITS = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT", 8: "PCM_U8"}


def write(audio: AudioData, path: str | Path, fmt: str | None = None,
          subtype: str | None = None, bit_depth: int | None = None,
          dither: bool = True, quality: int | None = None) -> Path:
    """Encode audio to disk.

    fmt: "wav", "flac", "ogg", "mp3", ... (None -> from path suffix)
    subtype/bit_depth: for PCM formats (default: keep source depth or 24-bit)
    quality: for lossy ffmpeg encoders (e.g. mp3 -q:a 0..9 -> we map to -b:a)
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or path.suffix.lstrip(".") or "wav").lower()

    data = audio.data
    if data.dtype != np.float64:
        data = data.astype(np.float64)

    if fmt in NEEDS_FFMPEG:
        return _encode_ffmpeg(audio, path, fmt, quality)

    if not HAVE_SOUNDFILE:
        raise AudioIOError("soundfile not available to write "
                           f"{fmt}; install python3-soundfile")

    if bit_depth and not subtype:
        subtype = _SUBTYPE_FOR_BITS.get(bit_depth)
    if subtype is None:
        subtype = {"wav": "PCM_24", "flac": "PCM_24", "ogg": "VORBIS",
                   "aiff": "PCM_24", "w64": "PCM_24"}.get(fmt)
    if fmt in ("wav", "aiff", "w64") and bit_depth and bit_depth < 32 and dither:
        from eqforge.dsp.engine import quantize
        data = quantize(data, bit_depth, dither=True)

    sf.write(str(path), data.T, audio.sample_rate, subtype=subtype,
             format=fmt.upper() if fmt != "ogg" else "OGG")
    return path


def _encode_ffmpeg(audio: AudioData, path: Path, fmt: str,
                   quality: int | None) -> Path:
    ff = _ffmpeg()
    if not ff:
        raise AudioIOError(f"encoding {fmt} needs ffmpeg (not installed)")
    with tempfile.TemporaryDirectory(prefix="eqforge-") as td:
        tmp = Path(td) / "in.wav"
        sf.write(str(tmp), audio.data.T, audio.sample_rate, subtype="FLOAT")
        cmd = [ff, "-v", "error", "-y", "-i", str(tmp)]
        if fmt == "mp3":
            cmd += ["-c:a", "libmp3lame"]
            if quality is not None:
                cmd += ["-q:a", str(max(0, min(9, quality)))]
            else:
                cmd += ["-b:a", "320k"]
        elif fmt in ("m4a", "aac", "mp4"):
            cmd += ["-c:a", "aac", "-b:a",
                    f"{quality or 256}k"] if quality else ["-c:a", "aac"]
        elif fmt == "opus":
            cmd += ["-c:a", "libopus", "-b:a", f"{quality or 192}k"]
        cmd += [str(path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise AudioIOError(f"ffmpeg encode failed: {r.stderr.strip()[:400]}")
    return path


def copy_metadata_tags(src: Path, dst: Path) -> None:
    """Best-effort tag transfer (ffmpeg present, same-ish container)."""
    ff = _ffmpeg()
    if not ff or dst.suffix.lower() in NEEDS_FFMPEG.union({".mp3"}):
        if ff:
            try:
                with tempfile.TemporaryDirectory(prefix="eqforge-") as td:
                    tmp = Path(td) / dst.name
                    r = subprocess.run(
                        [ff, "-v", "error", "-y", "-i", str(dst), "-i",
                         str(src), "-map", "0:a", "-map_metadata", "1",
                         "-c", "copy", str(tmp)],
                        capture_output=True, text=True, timeout=300)
                    if r.returncode == 0:
                        shutil.move(str(tmp), str(dst))
            except subprocess.SubprocessError as e:
                log.debug("metadata copy failed: %s", e)


def formats_available() -> dict:
    """Which formats this installation can decode/encode."""
    ff = _ffmpeg()
    dec = set(DECODABLE_ALWAYS)
    enc = {"wav", "flac", "ogg", "aiff", "w64"} if HAVE_SOUNDFILE else set()
    if ff:
        dec |= NEEDS_FFMPEG
        enc |= {"mp3", "m4a", "opus", "aac"}
    return {
        "soundfile": HAVE_SOUNDFILE,
        "ffmpeg": ff or None,
        "decode": sorted(x.lstrip(".") for x in dec),
        "encode": sorted(enc),
    }
