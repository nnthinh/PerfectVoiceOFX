"""Sample-accurate ffmpeg extract and BWF WAV I/O.

Appendix A extract math lives here because ``shared/perfectvoice_time.py``
is a sibling PR and is not on this branch. Engine is source of truth.

5.1 / >2 channels is rejected (no silent downmix). PCM24 and float32
WAV are written with BWF ``Originator=PerfectVoice``. TPDF dither is
intentionally not applied here.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

DEFAULT_HANDLE_S = 0.5
BWF_ORIGINATOR = "PerfectVoice"
SAMPLE_FORMATS = ("pcm24", "float32")
MAX_CHANNELS = 2

# WAVEFORMAT / WAVEFORMATEXTENSIBLE
_WAVE_FORMAT_PCM = 1
_WAVE_FORMAT_IEEE_FLOAT = 3
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE
_KSDATAFORMAT_SUBTYPE_PCM = bytes.fromhex("0100000000001000800000aa00389b71")
_KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = bytes.fromhex("0300000000001000800000aa00389b71")

_FFMPEG_ENV = ("PERFECTVOICE_FFMPEG", "FFMPEG_BINARY", "FFMPEG_PATH")
_FFPROBE_ENV = ("PERFECTVOICE_FFPROBE", "FFPROBE_BINARY", "FFPROBE_PATH")
_FFMPEG_HINTS = (
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)
_FFPROBE_HINTS = (
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
)


class FFmpegError(RuntimeError):
    """ffmpeg / ffprobe missing or failed."""


class UnsupportedChannelLayout(ValueError):
    """5.1 / 7.1 / adaptive >2 ch — reject, do not downmix."""


@dataclass(frozen=True)
class ExtractRange:
    """Source-file sample window. ``src_out_sample`` is exclusive."""

    h_left_actual: float
    h_right_actual: float
    src_in_sample: int
    src_out_sample: int
    src_sr: int

    @property
    def src_sample_count(self) -> int:
        return self.src_out_sample - self.src_in_sample


@dataclass(frozen=True)
class AudioProbe:
    path: Path
    sample_rate: int
    channels: int
    duration: float
    nb_samples: int
    stream_index: int
    codec_name: str
    sample_fmt: str
    channel_layout: str


@dataclass(frozen=True)
class ExtractResult:
    path: Path
    extract: ExtractRange
    channels: int
    sample_rate: int
    sample_format: str
    sample_count: int
    file_duration: float


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_count: int
    sample_format: str
    bits_per_sample: int
    originator: str | None


def round_half_up(value: float) -> int:
    """Round half away from zero (not banker's ``round``)."""
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"round_half_up expects a finite number, got {value!r}")
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def actual_handles(
    t0: float,
    t1: float,
    file_dur: float,
    handle_s: float = DEFAULT_HANDLE_S,
) -> tuple[float, float]:
    """Clamp requested handles into the source file.

    Fixture: t0=0.2, H=0.5 → H_left_actual=0.2 (not 0.5).
    """
    if handle_s < 0:
        raise ValueError(f"handle_s must be >= 0, got {handle_s}")
    h_left = min(handle_s, max(0.0, t0))
    h_right = min(handle_s, max(0.0, file_dur - t1))
    return h_left, h_right


def extract_sample_range(
    t0: float,
    t1: float,
    file_dur: float,
    src_sr: int,
    handle_s: float = DEFAULT_HANDLE_S,
) -> ExtractRange:
    """Sample-accurate extract window on the source file.

    ::

        src_in_sample  = round_half_up((t0 - H_left_actual)  * src_sr)
        src_out_sample = round_half_up((t1 + H_right_actual) * src_sr)  # exclusive
    """
    if src_sr <= 0:
        raise ValueError(f"src_sr must be positive, got {src_sr}")
    h_left, h_right = actual_handles(t0, t1, file_dur, handle_s)
    src_in = round_half_up((t0 - h_left) * src_sr)
    src_out = round_half_up((t1 + h_right) * src_sr)
    if src_out < src_in:
        raise ValueError(
            f"empty extract window: src_in={src_in} src_out={src_out} "
            f"(t0={t0} t1={t1} file_dur={file_dur})"
        )
    return ExtractRange(
        h_left_actual=h_left,
        h_right_actual=h_right,
        src_in_sample=src_in,
        src_out_sample=src_out,
        src_sr=src_sr,
    )


def expected_output_sample_count(
    t0: float,
    t1: float,
    file_dur: float,
    proj_sr: int,
    handle_s: float = DEFAULT_HANDLE_S,
) -> int:
    """Expected WAV length @ project rate (Appendix A ``N_out``)."""
    if proj_sr <= 0:
        raise ValueError(f"proj_sr must be positive, got {proj_sr}")
    h_left, h_right = actual_handles(t0, t1, file_dur, handle_s)
    return round_half_up((t1 - t0 + h_left + h_right) * proj_sr)


def _resolve_binary(name: str, env_keys: Sequence[str], hints: Sequence[str]) -> str:
    for key in env_keys:
        raw = os.environ.get(key)
        if not raw:
            continue
        path = Path(raw)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    found = shutil.which(name)
    if found:
        return found
    for hint in hints:
        if os.path.isfile(hint) and os.access(hint, os.X_OK):
            return hint
    raise FFmpegError(
        f"{name} not found (set {env_keys[0]} or install ffmpeg on PATH)"
    )


def ffmpeg_bin() -> str:
    return _resolve_binary("ffmpeg", _FFMPEG_ENV, _FFMPEG_HINTS)


def ffprobe_bin() -> str:
    return _resolve_binary("ffprobe", _FFPROBE_ENV, _FFPROBE_HINTS)


def _run(cmd: list[str], *, timeout: float | None = 120) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(f"executable missing: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"timed out: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", "replace").strip()
        tail = err[-800:] if err else "no stderr"
        raise FFmpegError(f"{cmd[0]} failed ({exc.returncode}): {tail}") from exc


def _codec_for(sample_format: str) -> str:
    if sample_format == "pcm24":
        return "pcm_s24le"
    if sample_format == "float32":
        return "pcm_f32le"
    raise ValueError(f"sample_format must be pcm24 or float32, got {sample_format!r}")


def probe_audio(path: str | Path, stream_index: int = 0) -> AudioProbe:
    """ffprobe one audio stream. Does not reject channel count."""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    proc = _run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(src),
        ]
    )
    data = json.loads(proc.stdout.decode("utf-8"))
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if stream_index < 0 or stream_index >= len(audio):
        raise FFmpegError(
            f"{src}: audio stream {stream_index} not found ({len(audio)} audio streams)"
        )
    stream = audio[stream_index]
    try:
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FFmpegError(f"{src}: missing sample_rate/channels") from exc
    if sample_rate <= 0 or channels <= 0:
        raise FFmpegError(f"{src}: invalid sample_rate={sample_rate} channels={channels}")

    nb_samples: int | None = None
    duration: float | None = None
    time_base = stream.get("time_base")
    duration_ts = stream.get("duration_ts")
    if duration_ts is not None and time_base:
        try:
            num_s, den_s = str(time_base).split("/", 1)
            num, den = int(num_s), int(den_s)
            ts = int(duration_ts)
            if den == sample_rate and num == 1:
                nb_samples = ts
                duration = ts / sample_rate
            elif den > 0:
                duration = ts * num / den
                nb_samples = round_half_up(duration * sample_rate)
        except (TypeError, ValueError):
            pass
    if duration is None and stream.get("duration") is not None:
        duration = float(stream["duration"])
    if duration is None:
        fmt_dur = (data.get("format") or {}).get("duration")
        if fmt_dur is None:
            raise FFmpegError(f"{src}: cannot determine duration")
        duration = float(fmt_dur)
    if nb_samples is None:
        nb_samples = round_half_up(duration * sample_rate)

    return AudioProbe(
        path=src,
        sample_rate=sample_rate,
        channels=channels,
        duration=duration,
        nb_samples=nb_samples,
        stream_index=stream_index,
        codec_name=str(stream.get("codec_name") or ""),
        sample_fmt=str(stream.get("sample_fmt") or ""),
        channel_layout=str(stream.get("channel_layout") or stream.get("ch_layout") or ""),
    )


def reject_if_multichannel(channels: int, *, path: str | Path | None = None) -> None:
    if channels > MAX_CHANNELS:
        where = f"{path}: " if path is not None else ""
        raise UnsupportedChannelLayout(
            f"{where}{channels} channels (5.1 / >2) rejected; fold to stereo in Fairlight"
        )
    if channels < 1:
        raise UnsupportedChannelLayout(f"invalid channel count {channels}")


def extract_with_handles(
    source: str | Path,
    dest: str | Path,
    t0: float,
    t1: float,
    handle_s: float = DEFAULT_HANDLE_S,
    *,
    sample_format: str = "pcm24",
    stream_index: int = 0,
    file_dur: float | None = None,
    source_sample_rate: int | None = None,
) -> ExtractResult:
    """Decode + ``atrim`` by sample index, write BWF WAV (pcm24 or float32).

    Seek is sample-based after decode (not ``-ss`` before ``-i``) so m4a/mov
    extracts stay sample-accurate.
    """
    codec = _codec_for(sample_format)
    probe = probe_audio(source, stream_index)
    reject_if_multichannel(probe.channels, path=probe.path)
    if source_sample_rate is not None and source_sample_rate != probe.sample_rate:
        raise ValueError(
            f"source sample rate mismatch: claimed {source_sample_rate}, "
            f"file {probe.sample_rate}"
        )
    duration = probe.duration if file_dur is None else file_dur
    rng = extract_sample_range(t0, t1, duration, probe.sample_rate, handle_s)
    src_in = max(0, rng.src_in_sample)
    src_out = min(probe.nb_samples, rng.src_out_sample)
    if src_out <= src_in:
        raise ValueError(
            f"extract window empty after clamp to file: [{src_in}, {src_out})"
        )
    rng = ExtractRange(
        h_left_actual=rng.h_left_actual,
        h_right_actual=rng.h_right_actual,
        src_in_sample=src_in,
        src_out_sample=src_out,
        src_sr=probe.sample_rate,
    )

    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    # atrim end_sample is the first dropped sample (exclusive), matching src_out.
    filt = (
        f"atrim=start_sample={src_in}:end_sample={src_out},"
        "asetpts=PTS-STARTPTS"
    )
    _run(
        [
            ffmpeg_bin(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(probe.path),
            "-map",
            f"0:a:{stream_index}",
            "-af",
            filt,
            "-c:a",
            codec,
            "-write_bext",
            "1",
            "-metadata",
            f"originator={BWF_ORIGINATOR}",
            str(out),
        ]
    )
    info = inspect_wav(out)
    return ExtractResult(
        path=out,
        extract=rng,
        channels=info.channels,
        sample_rate=info.sample_rate,
        sample_format=info.sample_format,
        sample_count=info.sample_count,
        file_duration=duration,
    )


def decode_f32(path: str | Path, *, stream_index: int = 0) -> tuple[np.ndarray, AudioProbe]:
    """Decode one stream to float32 ``[frames, ch]``. No resample, no normalize."""
    probe = probe_audio(path, stream_index)
    reject_if_multichannel(probe.channels, path=probe.path)
    fd, raw_name = tempfile.mkstemp(suffix=".f32le")
    os.close(fd)
    raw = Path(raw_name)
    try:
        _run(
            [
                ffmpeg_bin(),
                "-hide_banner",
                "-nostdin",
                "-y",
                "-i",
                str(probe.path),
                "-map",
                f"0:a:{stream_index}",
                "-f",
                "f32le",
                "-c:a",
                "pcm_f32le",
                str(raw),
            ]
        )
        data = np.fromfile(raw, dtype=np.float32)
    finally:
        try:
            raw.unlink()
        except OSError:
            pass
    if data.size % probe.channels != 0:
        raise FFmpegError(
            f"{path}: decoded {data.size} floats not divisible by {probe.channels} ch"
        )
    frames = data.reshape(-1, probe.channels)
    return frames, probe


def write_wav(
    path: str | Path,
    samples: np.ndarray,
    sample_rate: int,
    *,
    sample_format: str = "pcm24",
    originator: str = BWF_ORIGINATOR,
) -> WavInfo:
    """Write PCM24 or float32 WAV with a BWF bext Originator."""
    codec = _codec_for(sample_format)
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive, got {sample_rate}")
    arr = np.asarray(samples)
    if arr.ndim == 1:
        arr = arr[:, None]
    if arr.ndim != 2:
        raise ValueError("samples must be [frames] or [frames, ch]")
    reject_if_multichannel(arr.shape[1])
    pcm = np.ascontiguousarray(arr, dtype=np.float32)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_name = tempfile.mkstemp(suffix=".f32le")
    os.close(fd)
    raw = Path(raw_name)
    try:
        pcm.tofile(raw)
        _run(
            [
                ffmpeg_bin(),
                "-hide_banner",
                "-nostdin",
                "-y",
                "-f",
                "f32le",
                "-ar",
                str(sample_rate),
                "-ac",
                str(pcm.shape[1]),
                "-i",
                str(raw),
                "-c:a",
                codec,
                "-write_bext",
                "1",
                "-metadata",
                f"originator={originator}",
                str(dest),
            ]
        )
    finally:
        try:
            raw.unlink()
        except OSError:
            pass
    return inspect_wav(dest)


def inspect_wav(path: str | Path) -> WavInfo:
    """Read fmt / data / bext without decoding audio."""
    src = Path(path)
    blob = src.read_bytes()
    if len(blob) < 12 or blob[0:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise ValueError(f"{src}: not a RIFF/WAVE file")
    fmt: dict[str, int] = {}
    originator: str | None = None
    data_size = 0
    off = 12
    while off + 8 <= len(blob):
        cid = blob[off : off + 4]
        size = struct.unpack_from("<I", blob, off + 4)[0]
        payload_off = off + 8
        payload = blob[payload_off : payload_off + size]
        if cid == b"fmt " and size >= 16:
            audio_format, channels, sample_rate, _br, block_align, bits = struct.unpack_from(
                "<HHIIHH", payload, 0
            )
            tag = audio_format
            if audio_format == _WAVE_FORMAT_EXTENSIBLE and size >= 40:
                sub = payload[24:40]
                if sub == _KSDATAFORMAT_SUBTYPE_PCM:
                    tag = _WAVE_FORMAT_PCM
                elif sub == _KSDATAFORMAT_SUBTYPE_IEEE_FLOAT:
                    tag = _WAVE_FORMAT_IEEE_FLOAT
            fmt = {
                "tag": tag,
                "channels": channels,
                "sample_rate": sample_rate,
                "block_align": block_align,
                "bits": bits,
            }
        elif cid == b"bext" and size >= 288:
            raw_orig = payload[256:288].split(b"\x00", 1)[0]
            originator = raw_orig.decode("ascii", "replace")
        elif cid == b"data":
            data_size = size
        off = payload_off + size + (size & 1)
    if not fmt:
        raise ValueError(f"{src}: missing fmt chunk")
    block = fmt["block_align"]
    if block <= 0:
        raise ValueError(f"{src}: invalid block align")
    sample_count = data_size // block
    tag = fmt["tag"]
    bits = fmt["bits"]
    if tag == _WAVE_FORMAT_IEEE_FLOAT and bits == 32:
        sample_format = "float32"
    elif tag == _WAVE_FORMAT_PCM and bits == 24:
        sample_format = "pcm24"
    elif tag == _WAVE_FORMAT_PCM and bits == 16:
        sample_format = "pcm16"
    else:
        sample_format = f"tag{tag}_{bits}"
    return WavInfo(
        sample_rate=fmt["sample_rate"],
        channels=fmt["channels"],
        sample_count=sample_count,
        sample_format=sample_format,
        bits_per_sample=bits,
        originator=originator,
    )
