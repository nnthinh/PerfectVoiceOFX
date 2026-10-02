"""Torch-free audio prep for the speaker encoder (16 kHz mono)."""

from __future__ import annotations

from typing import Any

import numpy as np
import soxr

ENCODER_SAMPLE_RATE = 16000


def to_encoder_rate(waveform: Any, sample_rate: int) -> np.ndarray:
    """Mono float32 at 16 kHz. Accepts (T,) or (C, T) numpy / torch."""
    if hasattr(waveform, "detach"):
        waveform = waveform.detach().cpu().numpy()
    audio = np.asarray(waveform, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=0)
    elif audio.ndim != 1:
        raise ValueError("waveform must be (T,) or (C, T)")
    if int(sample_rate) != ENCODER_SAMPLE_RATE:
        audio = soxr.resample(audio, int(sample_rate), ENCODER_SAMPLE_RATE, quality="HQ")
    return np.ascontiguousarray(audio, dtype=np.float32)
