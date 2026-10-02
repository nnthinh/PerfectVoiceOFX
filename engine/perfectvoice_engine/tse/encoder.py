"""Pretrained ECAPA-TDNN speaker encoder (SpeechBrain VoxCeleb weights).

Rebuilds ``speechbrain.lobes.models.ECAPA_TDNN`` with the hyperparameters of
``speechbrain/spkrec-ecapa-voxceleb`` so its ``embedding_model.ckpt`` loads
with ``strict=True`` — module names mirror SpeechBrain's (``conv.conv``,
``norm.norm``) on purpose. Features match ``speechbrain.lobes.features.Fbank``
(80 mels, 25 ms Hamming / 10 ms hop at 16 kHz, 80 dB top) followed by
per-utterance mean normalization, as in ``EncoderClassifier.encode_batch``.

Architecture and features derived from SpeechBrain (Apache-2.0); see
NOTICE and docs/licenses/speechbrain-ecapa.md.

Weights are local-only: a missing checkpoint raises
``SpeakerEncoderNotInstalled``. Download is user-click (``weight_fetch``).
"""

from __future__ import annotations

import math
import threading
from typing import Sequence

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from perfectvoice_engine.tse.audio import ENCODER_SAMPLE_RATE, to_encoder_rate
from perfectvoice_engine.tse.weights import require_ecapa

EMBEDDING_DIM = 192
SAMPLE_RATE = ENCODER_SAMPLE_RATE
N_MELS = 80
N_FFT = 400
WIN_LENGTH = 400
HOP_LENGTH = 160
TOP_DB = 80.0
AMIN = 1e-10

CHANNELS = (1024, 1024, 1024, 1024, 3072)
KERNEL_SIZES = (5, 3, 3, 3, 1)
DILATIONS = (1, 2, 3, 4, 1)
ATTENTION_CHANNELS = 128
RES2NET_SCALE = 8
SE_CHANNELS = 128


def _to_mel(hz: float) -> float:
    return 2595.0 * math.log10(1.0 + hz / 700.0)


def mel_filterbank(
    n_mels: int = N_MELS,
    n_fft: int = N_FFT,
    sample_rate: int = SAMPLE_RATE,
) -> torch.Tensor:
    """Triangular filters exactly as ``speechbrain.processing.features.Filterbank``."""
    mel = torch.linspace(_to_mel(0.0), _to_mel(sample_rate / 2), n_mels + 2)
    hz = 700.0 * (10.0 ** (mel / 2595.0) - 1.0)
    band = (hz[1:] - hz[:-1])[:-1]
    f_central = hz[1:-1]
    all_freqs = torch.linspace(0, sample_rate // 2, n_fft // 2 + 1)
    slope = (all_freqs[None, :] - f_central[:, None]) / band[:, None]
    tri = torch.clamp(torch.minimum(slope + 1.0, -slope + 1.0), min=0.0)
    return tri.transpose(0, 1)  # (n_stft, n_mels)


class Fbank(nn.Module):
    """Log-mel filterbank energies + sentence mean norm. Input (B, T) at 16 kHz."""

    def __init__(self) -> None:
        super().__init__()
        self.register_buffer("window", torch.hamming_window(WIN_LENGTH), persistent=False)
        self.register_buffer("fbank_matrix", mel_filterbank(), persistent=False)

    def forward(self, wav: torch.Tensor) -> torch.Tensor:
        spec = torch.stft(
            wav,
            N_FFT,
            HOP_LENGTH,
            WIN_LENGTH,
            self.window,
            center=True,
            pad_mode="constant",
            normalized=False,
            onesided=True,
            return_complex=True,
        )
        power = torch.view_as_real(spec).pow(2).sum(-1).transpose(1, 2)  # (B, frames, n_stft)
        fbanks = torch.matmul(power, self.fbank_matrix)
        x_db = 10.0 * torch.log10(torch.clamp(fbanks, min=AMIN))
        floor = x_db.amax(dim=(-2, -1)) - TOP_DB
        x_db = torch.maximum(x_db, floor.view(-1, 1, 1))
        return x_db - x_db.mean(dim=1, keepdim=True)  # (B, frames, n_mels)


class _Conv1d(nn.Module):
    """SpeechBrain ``Conv1d(skip_transpose=True)``: 'same' reflect padding."""

    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, dilation: int = 1) -> None:
        super().__init__()
        self.pad = dilation * (kernel_size - 1) // 2
        self.conv = nn.Conv1d(in_channels, out_channels, kernel_size, dilation=dilation)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.pad:
            x = F.pad(x, (self.pad, self.pad), mode="reflect")
        return self.conv(x)


class _BatchNorm1d(nn.Module):
    def __init__(self, channels: int) -> None:
        super().__init__()
        self.norm = nn.BatchNorm1d(channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.norm(x)


class TDNNBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, dilation: int) -> None:
        super().__init__()
        self.conv = _Conv1d(in_channels, out_channels, kernel_size, dilation)
        self.norm = _BatchNorm1d(out_channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.norm(F.relu(self.conv(x)))


class Res2NetBlock(nn.Module):
    def __init__(self, channels: int, scale: int, kernel_size: int, dilation: int) -> None:
        super().__init__()
        hidden = channels // scale
        self.scale = scale
        self.blocks = nn.ModuleList(
            [TDNNBlock(hidden, hidden, kernel_size, dilation) for _ in range(scale - 1)]
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        ys = []
        y = x
        for i, x_i in enumerate(torch.chunk(x, self.scale, dim=1)):
            if i == 0:
                y = x_i
            elif i == 1:
                y = self.blocks[0](x_i)
            else:
                y = self.blocks[i - 1](x_i + y)
            ys.append(y)
        return torch.cat(ys, dim=1)


class SEBlock(nn.Module):
    def __init__(self, channels: int, se_channels: int) -> None:
        super().__init__()
        self.conv1 = _Conv1d(channels, se_channels, 1)
        self.conv2 = _Conv1d(se_channels, channels, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        s = x.mean(dim=2, keepdim=True)
        s = torch.sigmoid(self.conv2(F.relu(self.conv1(s))))
        return s * x


class SERes2NetBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, dilation: int) -> None:
        super().__init__()
        self.tdnn1 = TDNNBlock(in_channels, out_channels, 1, 1)
        self.res2net_block = Res2NetBlock(out_channels, RES2NET_SCALE, kernel_size, dilation)
        self.tdnn2 = TDNNBlock(out_channels, out_channels, 1, 1)
        self.se_block = SEBlock(out_channels, SE_CHANNELS)
        self.shortcut = _Conv1d(in_channels, out_channels, 1) if in_channels != out_channels else None

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        residual = self.shortcut(x) if self.shortcut is not None else x
        x = self.tdnn2(self.res2net_block(self.tdnn1(x)))
        return self.se_block(x) + residual


class AttentiveStatisticsPooling(nn.Module):
    def __init__(self, channels: int, attention_channels: int) -> None:
        super().__init__()
        self.eps = 1e-12
        self.tdnn = TDNNBlock(channels * 3, attention_channels, 1, 1)
        self.conv = _Conv1d(attention_channels, channels, 1)

    def _stats(self, x: torch.Tensor, w: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        mean = (w * x).sum(dim=2)
        std = torch.sqrt((w * (x - mean.unsqueeze(2)).pow(2)).sum(dim=2).clamp(self.eps))
        return mean, std

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        length = x.shape[-1]
        uniform = torch.full_like(x[:, :1, :], 1.0 / length)
        mean, std = self._stats(x, uniform)
        context = torch.cat(
            [x, mean.unsqueeze(2).expand(-1, -1, length), std.unsqueeze(2).expand(-1, -1, length)],
            dim=1,
        )
        attn = F.softmax(self.conv(torch.tanh(self.tdnn(context))), dim=2)
        mean, std = self._stats(x, attn)
        return torch.cat((mean, std), dim=1).unsqueeze(2)


class ECAPAEncoder(nn.Module):
    """ECAPA-TDNN (Desplanques et al. 2020), SpeechBrain VoxCeleb layout."""

    def __init__(self, input_size: int = N_MELS, lin_neurons: int = EMBEDDING_DIM) -> None:
        super().__init__()
        self.blocks = nn.ModuleList(
            [TDNNBlock(input_size, CHANNELS[0], KERNEL_SIZES[0], DILATIONS[0])]
        )
        for i in range(1, len(CHANNELS) - 1):
            self.blocks.append(
                SERes2NetBlock(CHANNELS[i - 1], CHANNELS[i], KERNEL_SIZES[i], DILATIONS[i])
            )
        self.mfa = TDNNBlock(CHANNELS[-2] * (len(CHANNELS) - 2), CHANNELS[-1], KERNEL_SIZES[-1], DILATIONS[-1])
        self.asp = AttentiveStatisticsPooling(CHANNELS[-1], ATTENTION_CHANNELS)
        self.asp_bn = _BatchNorm1d(CHANNELS[-1] * 2)
        self.fc = _Conv1d(CHANNELS[-1] * 2, lin_neurons, 1)

    def forward(self, feats: torch.Tensor) -> torch.Tensor:
        """``feats`` (B, frames, n_mels) → raw embeddings (B, lin_neurons)."""
        x = feats.transpose(1, 2)
        outs = []
        for layer in self.blocks:
            x = layer(x)
            outs.append(x)
        x = self.mfa(torch.cat(outs[1:], dim=1))
        x = self.fc(self.asp_bn(self.asp(x)))
        return x.squeeze(2)


_LOCK = threading.Lock()
_ENCODERS: dict[str, tuple[Fbank, ECAPAEncoder]] = {}


def resolve_device(device: torch.device | str | None) -> str:
    if device is not None and str(device) != "auto":
        return str(device)
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def get_speaker_encoder(device: torch.device | str | None = None) -> tuple[Fbank, ECAPAEncoder]:
    """Fbank + pretrained encoder on ``device``. Fails closed without weights."""
    dev = resolve_device(device)
    with _LOCK:
        cached = _ENCODERS.get(dev)
        if cached is not None:
            return cached
        ckpt = require_ecapa()
        state = torch.load(str(ckpt), map_location="cpu", weights_only=True)
        model = ECAPAEncoder()
        model.load_state_dict(state, strict=True)
        model.eval().to(dev)
        fbank = Fbank().eval().to(dev)
        _ENCODERS[dev] = (fbank, model)
        return fbank, model


def _normalize_rows(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=-1, keepdims=True)
    return (x / np.maximum(norms, 1e-12)).astype(np.float32)


def embed_batch_16k(
    segments: np.ndarray,
    *,
    device: torch.device | str | None = None,
    batch_size: int = 32,
) -> np.ndarray:
    """Equal-length 16 kHz mono segments (N, L) → L2-normalized (N, 192)."""
    segments = np.asarray(segments, dtype=np.float32)
    if segments.ndim != 2:
        raise ValueError("segments must be (N, L)")
    if segments.shape[0] == 0:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    fbank, model = get_speaker_encoder(device)
    dev = resolve_device(device)
    out = []
    with torch.inference_mode():
        for i in range(0, segments.shape[0], batch_size):
            wav = torch.from_numpy(segments[i : i + batch_size]).to(dev)
            out.append(model(fbank(wav)).float().cpu().numpy())
    return _normalize_rows(np.concatenate(out, axis=0))


def extract_embedding(
    waveform: np.ndarray | torch.Tensor | Sequence[float],
    sample_rate: int = 44100,
    device: torch.device | str | None = None,
) -> np.ndarray:
    """One L2-normalized 192-d voiceprint for a whole (C, T) or (T,) signal."""
    mono = to_encoder_rate(waveform, sample_rate)
    if mono.shape[0] < WIN_LENGTH:
        mono = np.pad(mono, (0, WIN_LENGTH - mono.shape[0]))
    return embed_batch_16k(mono[np.newaxis, :], device=device)[0]


__all__ = [
    "ECAPAEncoder",
    "EMBEDDING_DIM",
    "Fbank",
    "SAMPLE_RATE",
    "embed_batch_16k",
    "extract_embedding",
    "get_speaker_encoder",
    "mel_filterbank",
    "resolve_device",
    "to_encoder_rate",
]
