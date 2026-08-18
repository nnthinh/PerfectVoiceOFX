"""soxr_hq_v1 resampler.

Pinned id ``soxr_hq_v1`` is implemented by the Python ``soxr`` package at
HQ quality (libsoxr HQ). Typical Homebrew ffmpeg builds are not linked
against libsoxr, so ``aresample=resampler=soxr`` fails with "Requested
resampling engine is unavailable". Do not substitute swr and still
claim this id.

Model target: 44100 Hz, 2 ch, float32, no normalize.
Project output: resample to ``project_sample_rate`` (48 kHz or 96 kHz).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soxr

from perfectvoice_engine.ffmpeg_io import (
    UnsupportedChannelLayout,
    WavInfo,
    decode_f32,
    reject_if_multichannel,
    write_wav,
)

RESAMPLER_ID = "soxr_hq_v1"
MODEL_SAMPLE_RATE = 44100
MODEL_CHANNELS = 2
# soxr.HQ == libsoxr HQ; this is the quality pin behind soxr_hq_v1.
_SOXR_HQ = soxr.HQ


@dataclass(frozen=True)
class ResampleResult:
    path: Path
    in_sample_rate: int
    out_sample_rate: int
    channels: int
    sample_count: int
    sample_format: str
    resampler_id: str = RESAMPLER_ID


def resample_array(
    samples: np.ndarray,
    in_sr: int,
    out_sr: int,
) -> np.ndarray:
    """Resample ``[frames]`` or ``[frames, ch]``. No normalize, no dither."""
    if in_sr <= 0 or out_sr <= 0:
        raise ValueError(f"sample rates must be positive, got {in_sr} → {out_sr}")
    arr = np.asarray(samples)
    squeeze = False
    if arr.ndim == 1:
        arr = arr[:, None]
        squeeze = True
    elif arr.ndim != 2:
        raise ValueError("samples must be [frames] or [frames, ch]")
    reject_if_multichannel(arr.shape[1])
    if in_sr == out_sr:
        out = np.ascontiguousarray(arr)
    else:
        out = soxr.resample(arr, in_sr, out_sr, quality=_SOXR_HQ)
    if squeeze:
        return out[:, 0]
    return out


def to_model_rate(samples: np.ndarray, in_sr: int) -> np.ndarray:
    """44100 Hz stereo float32, no normalize. Mono is duplicated."""
    arr = np.asarray(samples)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    elif arr.ndim == 2 and arr.shape[1] == 1:
        arr = np.repeat(arr, MODEL_CHANNELS, axis=1)
    elif arr.ndim == 2 and arr.shape[1] == MODEL_CHANNELS:
        pass
    else:
        ch = arr.shape[1] if arr.ndim == 2 else arr.ndim
        raise UnsupportedChannelLayout(f"cannot map {ch} channels to model stereo")
    out = resample_array(arr, in_sr, MODEL_SAMPLE_RATE)
    return np.ascontiguousarray(out, dtype=np.float32)


def to_project_rate(samples: np.ndarray, in_sr: int, project_sr: int) -> np.ndarray:
    """Resample wet/dry output to the project sample rate. No normalize."""
    out = resample_array(samples, in_sr, project_sr)
    return np.ascontiguousarray(out, dtype=np.float32)


def resampled_sample_count(
    n_in: int,
    in_sr: int,
    out_sr: int,
    *,
    channels: int = 1,
    chunk_frames: int = 48_000,
) -> int:
    """Count soxr_hq_v1 output frames without keeping audio in RAM."""
    if n_in < 0:
        raise ValueError(f"n_in must be >= 0, got {n_in}")
    if in_sr <= 0 or out_sr <= 0:
        raise ValueError(f"sample rates must be positive, got {in_sr} → {out_sr}")
    reject_if_multichannel(channels)
    if in_sr == out_sr:
        return n_in
    stream = soxr.ResampleStream(
        in_sr, out_sr, channels, dtype="float32", quality=_SOXR_HQ
    )
    remaining = n_in
    n_out = 0
    zeros = np.zeros((chunk_frames, channels), dtype=np.float32)
    while remaining > 0:
        take = min(chunk_frames, remaining)
        remaining -= take
        y = stream.resample_chunk(zeros[:take], last=remaining == 0)
        n_out += int(y.shape[0])
    return n_out


def resample_wav(
    source: str | Path,
    dest: str | Path,
    out_sr: int,
    *,
    sample_format: str = "float32",
) -> ResampleResult:
    """File-to-file soxr_hq_v1. Default float32 (model input)."""
    frames, probe = decode_f32(source)
    out = resample_array(frames, probe.sample_rate, out_sr)
    info: WavInfo = write_wav(dest, out, out_sr, sample_format=sample_format)
    return ResampleResult(
        path=Path(dest),
        in_sample_rate=probe.sample_rate,
        out_sample_rate=out_sr,
        channels=info.channels,
        sample_count=info.sample_count,
        sample_format=info.sample_format,
    )
