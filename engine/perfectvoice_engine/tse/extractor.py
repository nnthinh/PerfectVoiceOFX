"""Target Speaker Extractor (TSE Extractor).

Isolates a target speaker's voice from background singing and competing speech
conditioned on a 192-dimensional speaker embedding.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from perfectvoice_engine.constants import raise_if_cancelled

N_FFT = 1024
HOP_LENGTH = 256
WIN_LENGTH = 1024
EMBEDDING_DIM = 192


class FiLM(nn.Module):
    """Feature-wise Linear Modulation based on target speaker embedding."""

    def __init__(self, embed_dim: int, channels: int) -> None:
        super().__init__()
        self.gamma = nn.Linear(embed_dim, channels)
        self.beta = nn.Linear(embed_dim, channels)

    def forward(self, x: torch.Tensor, e: torch.Tensor) -> torch.Tensor:
        # x: (B, C, F, T), e: (B, embed_dim)
        g = self.gamma(e).unsqueeze(-1).unsqueeze(-1)
        b = self.beta(e).unsqueeze(-1).unsqueeze(-1)
        return (1.0 + g) * x + b


class TSEBlock(nn.Module):
    """Dense convolutional block with dilation and speaker FiLM modulation."""

    def __init__(self, channels: int, dilation: int = 1, embed_dim: int = EMBEDDING_DIM) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(
            channels,
            channels,
            kernel_size=3,
            dilation=dilation,
            padding=dilation,
        )
        self.norm1 = nn.BatchNorm2d(channels)
        self.film = FiLM(embed_dim, channels)
        self.conv2 = nn.Conv2d(channels, channels, kernel_size=1)
        self.norm2 = nn.BatchNorm2d(channels)

    def forward(self, x: torch.Tensor, e: torch.Tensor) -> torch.Tensor:
        res = x
        h = F.relu(self.norm1(self.conv1(x)))
        h = self.film(h, e)
        h = self.norm2(self.conv2(h))
        return F.relu(h + res)


class TargetSpeakerModel(nn.Module):
    """Deep Time-Frequency Target Speaker Extractor Network."""

    def __init__(
        self,
        embed_dim: int = EMBEDDING_DIM,
        channels: int = 64,
        num_blocks: int = 6,
    ) -> None:
        super().__init__()
        self.in_conv = nn.Conv2d(2, channels, kernel_size=3, padding=1)
        self.blocks = nn.ModuleList([
            TSEBlock(channels, dilation=2 ** (i % 3), embed_dim=embed_dim)
            for i in range(num_blocks)
        ])
        # Dual-path complex mask prediction (real and imaginary masks)
        self.out_conv = nn.Conv2d(channels, 2, kernel_size=3, padding=1)

    def forward(self, spec_complex: torch.Tensor, e: torch.Tensor) -> torch.Tensor:
        # spec_complex: (B, 2, F, T) -> [Real, Imag]
        # e: (B, embed_dim)
        h = F.relu(self.in_conv(spec_complex))
        for block in self.blocks:
            h = block(h, e)
        mask = torch.tanh(self.out_conv(h))  # Bounded complex mask
        # Complex multiplication: (R + iI) * (Mr + iMi) = (R*Mr - I*Mi) + i(R*Mi + I*Mr)
        r = spec_complex[:, 0]
        i = spec_complex[:, 1]
        mr = mask[:, 0]
        mi = mask[:, 1]
        target_r = r * mr - i * mi
        target_i = r * mi + i * mr
        return torch.stack([target_r, target_i], dim=1)


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
) -> np.ndarray:
    """Extract target speaker voice using conditioning speaker embedding vector.

    Args:
        waveform: Audio samples (C, T) in float32
        embedding: 192-dim speaker voiceprint vector
        sample_rate: Audio sample rate
        device: Torch compute device (Apple Metal / CUDA / CPU)

    Returns:
        Isolated target speaker waveform (C, T) as float32 numpy array
    """
    raise_if_cancelled(cancel_event)

    if device is None:
        device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

    if isinstance(waveform, np.ndarray):
        audio_t = torch.from_numpy(waveform).float()
    else:
        audio_t = waveform.float()

    if audio_t.ndim == 1:
        audio_t = audio_t.unsqueeze(0)  # (1, T)

    num_channels, total_samples = audio_t.shape
    embed_t = torch.tensor(embedding, dtype=torch.float32).unsqueeze(0).to(device)  # (1, 192)

    model = get_tse_model(device)
    window = torch.hann_window(WIN_LENGTH, device=device)

    # Process channel by channel with chunking for large files
    isolated_channels = []
    chunk_size = 44100 * 10  # 10 second chunks
    hop_size = 44100 * 8    # 2 second overlap

    for ch in range(num_channels):
        ch_audio = audio_t[ch:ch+1].to(device)
        out_ch = torch.zeros_like(ch_audio)
        weight_ch = torch.zeros_like(ch_audio)

        starts = list(range(0, total_samples, hop_size))
        total_chunks = len(starts)

        for chunk_idx, start in enumerate(starts):
            raise_if_cancelled(cancel_event)
            end = min(total_samples, start + chunk_size)
            chunk = ch_audio[:, start:end]

            if chunk.shape[-1] < WIN_LENGTH:
                # Pad small remainder
                pad_amt = WIN_LENGTH - chunk.shape[-1]
                chunk = F.pad(chunk, (0, pad_amt))
            else:
                pad_amt = 0

            # STFT
            stft = torch.stft(
                chunk,
                n_fft=N_FFT,
                hop_length=HOP_LENGTH,
                win_length=WIN_LENGTH,
                window=window,
                return_complex=True,
            )
            spec_in = torch.stack([stft.real, stft.imag], dim=1)  # (1, 2, F, T)

            with torch.no_grad():
                spec_out = model(spec_in, embed_t)

            complex_out = torch.complex(spec_out[:, 0], spec_out[:, 1])
            chunk_reconstructed = torch.istft(
                complex_out,
                n_fft=N_FFT,
                hop_length=HOP_LENGTH,
                win_length=WIN_LENGTH,
                window=window,
                length=chunk.shape[-1],
            )

            if pad_amt > 0:
                chunk_reconstructed = chunk_reconstructed[:, :-pad_amt]

            # Cross-fade trapezoidal window for seamless OLA
            fade_len = min(44100, chunk_reconstructed.shape[-1] // 4)
            fade_w = torch.ones_like(chunk_reconstructed)
            if fade_len > 0:
                ramp = torch.linspace(0, 1, fade_len, device=device)
                fade_w[:, :fade_len] = ramp
                fade_w[:, -fade_len:] = ramp.flip(0)

            out_ch[:, start:end] += chunk_reconstructed * fade_w
            weight_ch[:, start:end] += fade_w

            if on_progress is not None:
                on_progress({
                    "chunk": chunk_idx + 1,
                    "total_chunks": total_chunks,
                    "segment_offset": start,
                    "audio_length": total_samples,
                })

        # Normalize by overlapping weights
        weight_ch = torch.clamp(weight_ch, min=1e-4)
        out_ch = out_ch / weight_ch
        isolated_channels.append(out_ch.squeeze(0).cpu().numpy())

    return np.stack(isolated_channels, axis=0).astype(np.float32)
