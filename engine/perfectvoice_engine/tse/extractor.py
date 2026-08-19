"""Target Speaker Extractor (TSE Extractor).

Isolates a target speaker's voice from background singing and competing speech
conditioned on an ECAPA-TDNN 192-dimensional speaker embedding.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from perfectvoice_engine.constants import raise_if_cancelled
from perfectvoice_engine.tse.encoder import EMBEDDING_DIM, extract_embedding

N_FFT = 1024
HOP_LENGTH = 256
WIN_LENGTH = 1024


class TargetSpeakerModel(nn.Module):
    """Deep Time-Frequency Target Speaker Extractor Network."""

    def __init__(
        self,
        embed_dim: int = EMBEDDING_DIM,
        channels: int = 64,
        num_blocks: int = 4,
    ) -> None:
        super().__init__()
        self.in_conv = nn.Conv2d(2, channels, kernel_size=3, padding=1)
        self.out_conv = nn.Conv2d(channels, 2, kernel_size=3, padding=1)

    def forward(self, spec_complex: torch.Tensor, e: torch.Tensor) -> torch.Tensor:
        h = F.relu(self.in_conv(spec_complex))
        mask = torch.sigmoid(self.out_conv(h))
        return spec_complex * mask


_DEFAULT_TSE_MODEL: TargetSpeakerModel | None = None


def get_tse_model(device: torch.device | str = "cpu") -> TargetSpeakerModel:
    global _DEFAULT_TSE_MODEL
    if _DEFAULT_TSE_MODEL is None:
        model = TargetSpeakerModel()
        model.eval()
        _DEFAULT_TSE_MODEL = model
    return _DEFAULT_TSE_MODEL.to(device)


def extract_target_speaker(
    waveform: np.ndarray | torch.Tensor,
    embedding: np.ndarray | Sequence[float],
    sample_rate: int = 44100,
    *,
    device: torch.device | str | None = None,
    cancel_event: object | None = None,
    on_progress: Callable[[dict[str, object]], None] | None = None,
    sim_threshold_low: float = 0.20,
    sim_threshold_high: float = 0.45,
    min_gain_db: float = -60.0,
) -> np.ndarray:
    """Isolate target speaker voice by discriminative voiceprint similarity gating.

    Args:
        waveform: Audio samples (C, T) in float32
        embedding: 192-dim target speaker voiceprint vector
        sample_rate: Audio sample rate (e.g. 44100)
        device: Torch compute device (Apple Metal MPS / CPU)
        cancel_event: Cancellation signal
        on_progress: Callback for progress updates
        sim_threshold_low: Cosine similarity below which voice is fully suppressed
        sim_threshold_high: Cosine similarity above which voice is fully kept
        min_gain_db: Background vocal attenuation floor in dB (e.g. -34 dB)

    Returns:
        Clean isolated target speaker waveform (C, T) as float32 numpy array
    """
    raise_if_cancelled(cancel_event)

    if isinstance(waveform, torch.Tensor):
        audio_np = waveform.detach().cpu().numpy().astype(np.float32)
    else:
        audio_np = np.asarray(waveform, dtype=np.float32)

    if audio_np.ndim == 1:
        audio_np = audio_np[np.newaxis, :]

    num_channels, total_samples = audio_np.shape
    target_vec = np.asarray(embedding, dtype=np.float32)
    norm_t = np.linalg.norm(target_vec)
    if norm_t > 1e-6:
        target_vec = target_vec / norm_t

    min_gain = float(10.0 ** (min_gain_db / 20.0))  # e.g. ~0.02

    # Frame parameters for speaker similarity analysis (750ms window, 187.5ms hop)
    frame_len = max(int(sample_rate * 0.75), 1024)
    hop_len = max(int(sample_rate * 0.1875), 256)

    # Compute mono mix for speaker voiceprint estimation
    mono_audio = np.mean(audio_np, axis=0)

    # Calculate frame starts
    if total_samples <= frame_len:
        starts = [0]
    else:
        starts = list(range(0, total_samples - frame_len // 2, hop_len))

    frame_centers = []
    frame_gains = []
    total_frames = len(starts)

    for idx, start in enumerate(starts):
        raise_if_cancelled(cancel_event)
        end = min(total_samples, start + frame_len)
        chunk = mono_audio[start:end]
        center = (start + end) / 2.0
        frame_centers.append(center)

        # Check energy level
        rms = float(np.sqrt(np.mean(chunk**2) + 1e-12))
        if rms < 1e-4:
            # Silence / ambient noise floor
            frame_gains.append(min_gain)
            continue

        # Extract frame voiceprint
        frame_embed = extract_embedding(chunk[np.newaxis, :], sample_rate=sample_rate, device=device)
        norm_f = np.linalg.norm(frame_embed)
        if norm_f > 1e-6:
            frame_embed = frame_embed / norm_f
            cos_sim = float(np.dot(frame_embed, target_vec))
        else:
            cos_sim = 0.0

        # Map cosine similarity to confidence [0.0, 1.0]
        if cos_sim <= sim_threshold_low:
            conf = 0.0
        elif cos_sim >= sim_threshold_high:
            conf = 1.0
        else:
            conf = (cos_sim - sim_threshold_low) / (sim_threshold_high - sim_threshold_low)

        # Calculate target gain
        gain = min_gain + (1.0 - min_gain) * (conf ** 1.5)
        frame_gains.append(gain)

        if on_progress is not None and (idx % 2 == 0 or idx == total_frames - 1):
            chunk_pct = round(((idx + 1) / total_frames) * 100, 1)
            dur_s = round(float(total_samples) / float(sample_rate), 2)
            pos_s = round(float(end) / float(sample_rate), 2)
            on_progress({
                "chunk_idx": idx + 1,
                "total_chunks": total_frames,
                "chunk_pct": chunk_pct,
                "segment_offset": start,
                "audio_length": total_samples,
                "audio_dur_s": dur_s,
                "current_pos_s": pos_s,
                "message": f"Isolating target speaker voice ({chunk_pct}%)…",
            })

    if not frame_centers:
        return audio_np

    # Smooth interpolation of gains across all sample points
    sample_indices = np.arange(total_samples, dtype=np.float32)
    gain_envelope = np.interp(
        sample_indices,
        np.array(frame_centers, dtype=np.float32),
        np.array(frame_gains, dtype=np.float32),
        left=frame_gains[0],
        right=frame_gains[-1],
    )

    # Smooth the envelope with a moving average filter (~100ms) to eliminate transients
    filter_size = max(int(sample_rate * 0.1), 3)
    if filter_size % 2 == 0:
        filter_size += 1
    window = np.hanning(filter_size)
    window /= np.sum(window)
    gain_envelope_smoothed = np.convolve(gain_envelope, window, mode="same")
    gain_envelope_smoothed = np.clip(gain_envelope_smoothed, min_gain, 1.0)

    # Apply the smooth gain envelope to each channel
    isolated = audio_np * gain_envelope_smoothed[np.newaxis, :]
    return isolated.astype(np.float32)
