"""Target speaker gating on top of the separated vocal stem.

Pass 2 slides a window over the vocals, embeds each window with the
pretrained ECAPA-TDNN encoder and compares it to the target voiceprint.
Windows that do not sound like the target (backing singers, other talkers)
are attenuated down to ``min_gain_db``; the per-window gains are
interpolated and smoothed into a sample-accurate envelope, so output length
and timing never change.

Pure-numpy helpers (``similarity_to_gain``, ``gain_envelope``) import no
torch; the encoder is imported only when no ``embed_fn`` is injected.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import numpy as np

from perfectvoice_engine.constants import raise_if_cancelled
from perfectvoice_engine.tse.audio import ENCODER_SAMPLE_RATE, to_encoder_rate

FRAME_SECONDS = 0.75
HOP_SECONDS = 0.1875
SILENCE_RMS = 1e-4
EMBED_BATCH = 16

EmbedFn = Callable[[np.ndarray], np.ndarray]


def similarity_to_gain(
    cos_sim: np.ndarray | float,
    *,
    low: float = 0.20,
    high: float = 0.45,
    min_gain: float = 0.001,
) -> np.ndarray:
    """Cosine similarity → linear gain in [min_gain, 1]."""
    if high <= low:
        raise ValueError("sim_threshold_high must exceed sim_threshold_low")
    conf = np.clip((np.asarray(cos_sim, dtype=np.float32) - low) / (high - low), 0.0, 1.0)
    return (min_gain + (1.0 - min_gain) * conf ** 1.5).astype(np.float32)


def gain_envelope(
    centers: np.ndarray,
    gains: np.ndarray,
    total_samples: int,
    sample_rate: int,
    min_gain: float,
) -> np.ndarray:
    """Per-frame gains → smoothed per-sample envelope (Hann, ~100 ms)."""
    idx = np.arange(total_samples, dtype=np.float32)
    env = np.interp(idx, centers, gains, left=gains[0], right=gains[-1])
    size = max(int(sample_rate * 0.1), 3)
    if size % 2 == 0:
        size += 1
    window = np.hanning(size)
    window /= np.sum(window)
    # Edge-pad so the envelope does not dip toward zero at clip boundaries.
    half = size // 2
    padded = np.pad(env, (half, half), mode="edge")
    smoothed = np.convolve(padded, window, mode="valid")
    return np.clip(smoothed, min_gain, 1.0).astype(np.float32)


def _frame_starts(total: int, frame_len: int, hop_len: int) -> list[int]:
    if total <= frame_len:
        return [0]
    return list(range(0, total - frame_len // 2, hop_len))


def extract_target_speaker(
    waveform: Any,
    embedding: np.ndarray | Sequence[float],
    sample_rate: int = 44100,
    *,
    device: Any = None,
    cancel_event: object | None = None,
    on_progress: Callable[[dict[str, object]], None] | None = None,
    sim_threshold_low: float = 0.20,
    sim_threshold_high: float = 0.45,
    min_gain_db: float = -60.0,
    embed_fn: EmbedFn | None = None,
) -> np.ndarray:
    """Attenuate everything in ``waveform`` (C, T) that is not the target voice.

    ``embed_fn`` maps equal-length 16 kHz mono windows (N, L) to unit
    voiceprints (N, 192). Default: the pretrained ECAPA encoder, which
    raises ``SpeakerEncoderNotInstalled`` when its weights are missing.
    """
    raise_if_cancelled(cancel_event)

    if hasattr(waveform, "detach"):
        waveform = waveform.detach().cpu().numpy()
    audio = np.asarray(waveform, dtype=np.float32)
    if audio.ndim == 1:
        audio = audio[np.newaxis, :]
    _channels, total = audio.shape
    if total == 0:
        return audio.copy()

    if embed_fn is None:
        from perfectvoice_engine.tse.encoder import embed_batch_16k, get_speaker_encoder

        get_speaker_encoder(device)  # fail closed before any work

        def embed_fn(segs: np.ndarray) -> np.ndarray:
            return embed_batch_16k(segs, device=device)

    mono16 = to_encoder_rate(audio, sample_rate)

    target = np.asarray(embedding, dtype=np.float32).reshape(-1)
    target = target / max(float(np.linalg.norm(target)), 1e-12)
    min_gain = float(10.0 ** (min_gain_db / 20.0))

    frame_len = max(int(sample_rate * FRAME_SECONDS), 1024)
    hop_len = max(int(sample_rate * HOP_SECONDS), 256)
    starts = _frame_starts(total, frame_len, hop_len)
    n_frames = len(starts)
    mono = audio.mean(axis=0)

    flen16 = max(int(round(frame_len * ENCODER_SAMPLE_RATE / sample_rate)), 400)
    if mono16.shape[0] < flen16:
        mono16 = np.pad(mono16, (0, flen16 - mono16.shape[0]))
    n16 = mono16.shape[0]

    centers = np.empty(n_frames, dtype=np.float32)
    gains = np.full(n_frames, min_gain, dtype=np.float32)
    voiced: list[int] = []
    for i, start in enumerate(starts):
        end = min(total, start + frame_len)
        centers[i] = (start + end) / 2.0
        chunk = mono[start:end]
        if float(np.sqrt(np.mean(chunk ** 2) + 1e-12)) >= SILENCE_RMS:
            voiced.append(i)

    last_milestone = -1
    for b in range(0, max(len(voiced), 1), EMBED_BATCH):
        raise_if_cancelled(cancel_event)
        batch = voiced[b : b + EMBED_BATCH]
        if batch:
            segs = np.empty((len(batch), flen16), dtype=np.float32)
            for row, i in enumerate(batch):
                s16 = min(int(round(starts[i] * ENCODER_SAMPLE_RATE / sample_rate)), n16 - flen16)
                segs[row] = mono16[s16 : s16 + flen16]
            embeds = np.asarray(embed_fn(segs), dtype=np.float32)
            sims = embeds @ target
            gains[batch] = similarity_to_gain(
                sims, low=sim_threshold_low, high=sim_threshold_high, min_gain=min_gain
            )

        done = min(len(voiced), b + EMBED_BATCH)
        frame_idx = (batch[-1] + 1) if batch else n_frames
        if done >= len(voiced):
            frame_idx = n_frames
        if on_progress is not None:
            chunk_pct = round(frame_idx / n_frames * 100, 1)
            pct_int = int(chunk_pct)
            payload: dict[str, object] = {
                "stage_name": "Pass 2/2: Target Speaker Filter (-60dB)",
                "overall_pct": round(50.0 + chunk_pct * 0.5, 1),
                "current_pass": 2,
                "total_passes": 2,
                "chunk_idx": frame_idx,
                "total_chunks": n_frames,
                "chunk_pct": chunk_pct,
                "segment_offset": starts[frame_idx - 1],
                "audio_length": total,
                "audio_dur_s": round(total / float(sample_rate), 2),
                "current_pos_s": round(min(total, starts[frame_idx - 1] + frame_len) / float(sample_rate), 2),
            }
            milestone = pct_int // 10 * 10
            if milestone > last_milestone:
                payload["message"] = f"Isolating target speaker voice ({milestone}%)..."
                last_milestone = milestone
            on_progress(payload)

    env = gain_envelope(centers, gains, total, sample_rate, min_gain)
    return (audio * env[np.newaxis, :]).astype(np.float32)


__all__ = [
    "extract_target_speaker",
    "gain_envelope",
    "similarity_to_gain",
]
