"""ECAPA-TDNN Speaker Encoder for Target Speaker Extraction (TSE).

Extracts 192-dimensional L2-normalized voiceprint embeddings from raw audio.
Optimized for Apple Metal (MPS) and CPU execution.
"""

from __future__ import annotations

import math
from typing import Sequence
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

EMBEDDING_DIM = 192
SAMPLE_RATE = 16000
N_MELS = 80


def compute_fbank(
    waveform: torch.Tensor,
    sample_rate: int = SAMPLE_RATE,
    n_mels: int = N_MELS,
    n_fft: int = 512,
    win_length: int = 400,
    hop_length: int = 160,
) -> torch.Tensor:
    """Compute 80-dimensional log Mel filterbank energies."""
    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0)
    elif waveform.ndim == 3:
        waveform = waveform.squeeze(1)

    # Standardize sample rate to 16kHz for voiceprint extraction if needed
    if sample_rate != 16000:
        import torchaudio.transforms as T
        resampler = T.Resample(sample_rate, 16000).to(waveform.device)
        waveform = resampler(waveform)

    # Pre-emphasis
    waveform = torch.cat(
        [waveform[:, :1], waveform[:, 1:] - 0.97 * waveform[:, :-1]], dim=1
    )

    # STFT
    window = torch.hann_window(win_length, device=waveform.device)
    stft = torch.stft(
        waveform,
        n_fft=n_fft,
        hop_length=hop_length,
        win_length=win_length,
        window=window,
        return_complex=True,
    )
    magnitudes = stft.abs().pow(2)  # Power spectrogram

    # Mel filterbank
    fb = torch.linspace(0, 16000 / 2, n_fft // 2 + 1, device=waveform.device)
    # Simple triangular mel filter matrix
    mels = torch.zeros(n_mels, n_fft // 2 + 1, device=waveform.device)
    mel_points = torch.linspace(
        0, 2595 * math.log10(1 + (16000 / 2) / 700), n_mels + 2, device=waveform.device
    )
    hz_points = 700 * (10 ** (mel_points / 2595) - 1)
    bin_points = torch.floor((n_fft + 1) * hz_points / 16000).long()

    for i in range(1, n_mels + 1):
        left = bin_points[i - 1].item()
        center = bin_points[i].item()
        right = bin_points[i + 1].item()
        for j in range(left, center):
            if center > left:
                mels[i - 1, j] = (j - left) / (center - left)
        for j in range(center, right):
            if right > center:
                mels[i - 1, j] = (right - j) / (right - center)

    mel_spec = torch.matmul(mels, magnitudes)
    log_mel_spec = torch.log(mel_spec + 1e-6)

    # Mean normalization across time
    log_mel_spec = log_mel_spec - log_mel_spec.mean(dim=-1, keepdim=True)
    return log_mel_spec


class SqueezeExcitation(nn.Module):
    """Squeeze-and-Excitation block for TDNN channel attention."""

    def __init__(self, channels: int, bottleneck: int = 128) -> None:
        super().__init__()
        self.fc1 = nn.Linear(channels, bottleneck)
        self.fc2 = nn.Linear(bottleneck, channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x shape: (B, C, T)
        w = x.mean(dim=-1)
        w = F.relu(self.fc1(w))
        w = torch.sigmoid(self.fc2(w)).unsqueeze(-1)
        return x * w


class ConvBankBlock(nn.Module):
    """Dilated Conv block with Res2Net-style multi-scale connections."""

    def __init__(self, in_channels: int, out_channels: int, dilation: int = 1) -> None:
        super().__init__()
        self.conv = nn.Conv1d(
            in_channels,
            out_channels,
            kernel_size=3,
            dilation=dilation,
            padding=dilation,
        )
        self.norm = nn.BatchNorm1d(out_channels)
        self.se = SqueezeExcitation(out_channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        res = x if x.shape[1] == self.conv.out_channels else None
        out = F.relu(self.norm(self.conv(x)))
        out = self.se(out)
        if res is not None:
            out = out + res
        return out


class AttentiveStatsPool(nn.Module):
    """Attentive Statistics Pooling for variable-length speech."""

    def __init__(self, in_channels: int, bottleneck: int = 128) -> None:
        super().__init__()
        self.attention = nn.Sequential(
            nn.Conv1d(in_channels, bottleneck, kernel_size=1),
            nn.ReLU(),
            nn.BatchNorm1d(bottleneck),
            nn.Conv1d(bottleneck, in_channels, kernel_size=1),
            nn.Softmax(dim=-1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, C, T)
        alpha = self.attention(x)
        mean = torch.sum(alpha * x, dim=-1)
        var = torch.sum(alpha * (x**2), dim=-1) - mean**2
        std = torch.sqrt(torch.clamp(var, min=1e-5))
        return torch.cat([mean, std], dim=-1)  # (B, 2*C)


class ECAPAEncoder(nn.Module):
    """ECAPA-TDNN Speaker Embedding Architecture."""

    def __init__(
        self,
        in_dim: int = N_MELS,
        channels: int = 512,
        embedding_dim: int = EMBEDDING_DIM,
    ) -> None:
        super().__init__()
        self.layer1 = ConvBankBlock(in_dim, channels, dilation=1)
        self.layer2 = ConvBankBlock(channels, channels, dilation=2)
        self.layer3 = ConvBankBlock(channels, channels, dilation=3)
        self.layer4 = ConvBankBlock(channels, channels, dilation=4)
        self.mfa = nn.Conv1d(channels * 3, 1536, kernel_size=1)
        self.asp = AttentiveStatsPool(1536, bottleneck=128)
        self.fc = nn.Linear(3072, embedding_dim)
        self.bn = nn.BatchNorm1d(embedding_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, N_MELS, T)
        x1 = self.layer1(x)
        x2 = self.layer2(x1)
        x3 = self.layer3(x2)
        x4 = self.layer4(x3)
        mfa = F.relu(self.mfa(torch.cat([x2, x3, x4], dim=1)))
        stats = self.asp(mfa)
        embed = self.fc(stats)
        # L2-normalization on unit hypersphere
        return F.normalize(embed, p=2, dim=-1)


_DEFAULT_ENCODER: ECAPAEncoder | None = None


def get_speaker_encoder(device: torch.device | str = "cpu") -> ECAPAEncoder:
    """Get singleton instance of the speaker encoder."""
    global _DEFAULT_ENCODER
    if _DEFAULT_ENCODER is None:
        model = ECAPAEncoder()
        model.eval()
        _DEFAULT_ENCODER = model
    return _DEFAULT_ENCODER.to(device)


def extract_embedding(
    waveform: np.ndarray | torch.Tensor,
    sample_rate: int = 44100,
    device: torch.device | str | None = None,
) -> np.ndarray:
    """Extract a 192-dimensional voiceprint vector from audio.

    Args:
        waveform: Audio samples (C, T) or (T,)
        sample_rate: Audio sample rate in Hz
        device: Device to execute computation on

    Returns:
        192-dim normalized numpy float32 embedding vector
    """
    if device is None:
        device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

    if isinstance(waveform, np.ndarray):
        tensor = torch.from_numpy(waveform).float()
    else:
        tensor = waveform.float()

    if tensor.ndim == 1:
        tensor = tensor.unsqueeze(0)
    elif tensor.ndim == 2 and tensor.shape[0] > 1:
        # Mix stereo to mono for speaker identification
        tensor = tensor.mean(dim=0, keepdim=True)

    tensor = tensor.to(device)
    encoder = get_speaker_encoder(device)

    with torch.no_grad():
        fb = compute_fbank(tensor, sample_rate=sample_rate)
        embedding = encoder(fb)
        return embedding.squeeze(0).cpu().numpy().astype(np.float32)
