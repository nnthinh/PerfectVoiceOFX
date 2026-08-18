"""Wet/dry blend, output gain, optional mono mid, project-rate WAV.

``wet_dry_sample_rate`` is derived from ``enhancer`` here. The client
must not send that field. DeepFilterNet inference is PR 05d — the
48 kHz node is identity so graph lengths stay testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from perfectvoice_engine.ffmpeg_io import (
    BWF_ORIGINATOR,
    WavInfo,
    reject_if_multichannel,
    write_wav,
)
from perfectvoice_engine.resample import resample_array, to_project_rate

DEFAULT_WET = 0.85
DEFAULT_GAIN_DB = 0.0
GAIN_DB_MIN = -12.0
GAIN_DB_MAX = 12.0

ENHANCER_NONE = "none"
ENHANCER_DEEPFILTERNET3 = "deepfilternet3"

# Source of truth — no client-supplied override.
_WET_DRY_SAMPLE_RATE = {
    ENHANCER_NONE: 44100,
    ENHANCER_DEEPFILTERNET3: 48000,
}


def derive_wet_dry_sample_rate(enhancer: str) -> int:
    """44100 if enhancer=none, 48000 if deepfilternet3."""
    try:
        return _WET_DRY_SAMPLE_RATE[enhancer]
    except KeyError:
        known = ", ".join(sorted(_WET_DRY_SAMPLE_RATE))
        raise ValueError(
            f"unknown enhancer {enhancer!r}; expected one of: {known}"
        ) from None


def _as_frames(samples: np.ndarray) -> tuple[np.ndarray, bool]:
    arr = np.asarray(samples, dtype=np.float32)
    squeeze = False
    if arr.ndim == 1:
        arr = arr[:, None]
        squeeze = True
    elif arr.ndim != 2:
        raise ValueError("samples must be [frames] or [frames, ch]")
    reject_if_multichannel(arr.shape[1])
    return np.ascontiguousarray(arr, dtype=np.float32), squeeze


def _match_channels(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if a.shape[1] == b.shape[1]:
        return a, b
    if a.shape[1] == 1 and b.shape[1] == 2:
        return np.repeat(a, 2, axis=1), b
    if a.shape[1] == 2 and b.shape[1] == 1:
        return a, np.repeat(b, 2, axis=1)
    raise ValueError(f"cannot mix {a.shape[1]} ch with {b.shape[1]} ch")


def _match_length(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    # Independent soxr passes can differ by 1 frame; do not pad silence into the mix.
    n = min(a.shape[0], b.shape[0])
    return a[:n], b[:n]


def _validate_wet(wet: float) -> float:
    w = float(wet)
    if not 0.0 <= w <= 1.0:
        raise ValueError(f"wet must be in [0, 1], got {wet}")
    return w


def _validate_gain_db(gain_db: float) -> float:
    g = float(gain_db)
    if not GAIN_DB_MIN <= g <= GAIN_DB_MAX:
        raise ValueError(
            f"output_gain_db must be in [{GAIN_DB_MIN}, {GAIN_DB_MAX}], got {gain_db}"
        )
    return g


def wet_dry_mix(
    dry: np.ndarray,
    vocals: np.ndarray,
    wet: float = DEFAULT_WET,
) -> np.ndarray:
    """``y = (1-w)*x + w*v`` at a shared rate. No resample."""
    w = _validate_wet(wet)
    x, sx = _as_frames(dry)
    v, sv = _as_frames(vocals)
    x, v = _match_channels(x, v)
    x, v = _match_length(x, v)
    if w == 0.0:
        y = x
    elif w == 1.0:
        y = v
    else:
        y = (np.float32(1.0) - np.float32(w)) * x + np.float32(w) * v
    y = np.ascontiguousarray(y, dtype=np.float32)
    if sx and sv and y.shape[1] == 1:
        return y[:, 0]
    return y


def apply_output_gain(
    samples: np.ndarray,
    gain_db: float = DEFAULT_GAIN_DB,
) -> np.ndarray:
    g = _validate_gain_db(gain_db)
    arr, squeeze = _as_frames(samples)
    if g == 0.0:
        out = arr
    else:
        out = arr * np.float32(10.0 ** (g / 20.0))
    out = np.ascontiguousarray(out, dtype=np.float32)
    return out[:, 0] if squeeze else out


def fold_mono_mid(samples: np.ndarray) -> np.ndarray:
    """Mid = mean of channels. Always ``[frames, 1]``."""
    arr, _ = _as_frames(samples)
    if arr.shape[1] == 1:
        return np.ascontiguousarray(arr, dtype=np.float32)
    mid = arr.mean(axis=1, keepdims=True, dtype=np.float64).astype(np.float32)
    return np.ascontiguousarray(mid, dtype=np.float32)


def to_wet_dry_rate(
    samples: np.ndarray,
    in_sr: int,
    enhancer: str,
) -> np.ndarray:
    out_sr = derive_wet_dry_sample_rate(enhancer)
    out = resample_array(samples, in_sr, out_sr)
    return np.ascontiguousarray(out, dtype=np.float32)


@dataclass(frozen=True)
class BlendResult:
    samples: np.ndarray
    wet_dry_sample_rate: int
    wet_dry_sample_count: int
    project_sample_rate: int
    sample_count: int
    channels: int
    wet: float
    gain_db: float
    mono: bool
    peak: float
    sample_format: str | None = None
    path: Path | None = None
    originator: str | None = None


def blend(
    dry: np.ndarray,
    vocals: np.ndarray,
    *,
    in_sample_rate: int,
    enhancer: str,
    project_sample_rate: int,
    wet: float = DEFAULT_WET,
    gain_db: float = DEFAULT_GAIN_DB,
    mono: bool = False,
) -> BlendResult:
    """Resample to wet/dry rate, mix, gain, optional mono, then project rate.

    Does not take ``wet_dry_sample_rate`` — :func:`derive_wet_dry_sample_rate`
    is the only source of that value.
    """
    if in_sample_rate <= 0:
        raise ValueError(f"in_sample_rate must be positive, got {in_sample_rate}")
    if project_sample_rate <= 0:
        raise ValueError(
            f"project_sample_rate must be positive, got {project_sample_rate}"
        )
    w = _validate_wet(wet)
    g = _validate_gain_db(gain_db)
    wd_sr = derive_wet_dry_sample_rate(enhancer)

    x = to_wet_dry_rate(dry, in_sample_rate, enhancer)
    v = to_wet_dry_rate(vocals, in_sample_rate, enhancer)
    # DFN3 runs on v only after this resample (PR 05d). Identity here.

    y = wet_dry_mix(x, v, wet=w)
    y, _ = _as_frames(y)
    y = apply_output_gain(y, g)
    y, _ = _as_frames(y)
    if mono:
        y = fold_mono_mid(y)
    wet_dry_n = int(y.shape[0])
    y = to_project_rate(y, wd_sr, project_sample_rate)
    y, _ = _as_frames(y)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    return BlendResult(
        samples=y,
        wet_dry_sample_rate=wd_sr,
        wet_dry_sample_count=wet_dry_n,
        project_sample_rate=project_sample_rate,
        sample_count=int(y.shape[0]),
        channels=int(y.shape[1]),
        wet=w,
        gain_db=g,
        mono=mono,
        peak=peak,
    )


def blend_to_wav(
    dest: str | Path,
    dry: np.ndarray,
    vocals: np.ndarray,
    *,
    in_sample_rate: int,
    enhancer: str,
    project_sample_rate: int,
    wet: float = DEFAULT_WET,
    gain_db: float = DEFAULT_GAIN_DB,
    mono: bool = False,
    sample_format: str = "pcm24",
) -> BlendResult:
    """Blend then write pcm24/float32 WAV via ``ffmpeg_io.write_wav`` (BWF)."""
    result = blend(
        dry,
        vocals,
        in_sample_rate=in_sample_rate,
        enhancer=enhancer,
        project_sample_rate=project_sample_rate,
        wet=wet,
        gain_db=gain_db,
        mono=mono,
    )
    info: WavInfo = write_wav(
        dest,
        result.samples,
        result.project_sample_rate,
        sample_format=sample_format,
        originator=BWF_ORIGINATOR,
    )
    return BlendResult(
        samples=result.samples,
        wet_dry_sample_rate=result.wet_dry_sample_rate,
        wet_dry_sample_count=result.wet_dry_sample_count,
        project_sample_rate=info.sample_rate,
        sample_count=info.sample_count,
        channels=info.channels,
        wet=result.wet,
        gain_db=result.gain_db,
        mono=result.mono,
        peak=result.peak,
        sample_format=info.sample_format,
        path=Path(dest),
        originator=info.originator,
    )
