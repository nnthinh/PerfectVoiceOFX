"""Demucs vocals stem — local ``repo=`` only.

Infer constructs ``Separator(model=..., repo=Path(local_repo))`` after the
weight files hash. A pretrained name with no ``repo=`` would Hub/AWS-fetch
in Demucs 4.1.0; that path is not used here.

``vocals_only_bag`` reads the local ft bag YAML, then loads signature
``04573f0d``. The ``.th`` path is never hardcoded; LocalRepo resolves it.
"""

from __future__ import annotations

import importlib
import os
import platform
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import numpy as np

from perfectvoice_engine.constants import (
    MEMORY_CAP_BYTES,
    WINDOW_OVERLAP_SECONDS,
    WINDOW_SECONDS,
    JobCancelled,
    pcm_nbytes,
    raise_if_cancelled,
)
from perfectvoice_engine.models import (
    DEFAULT_MODEL,
    VOCALS_ONLY_SIG,
    ModelNotInstalled,
    require_model,
    require_vocals_only_bag,
    weights_sha256,
)
from perfectvoice_engine.resample import MODEL_SAMPLE_RATE

# Demucs 4.0.1+: on MPS, complex / STFT runs on CPU; the rest is Metal.
# Apple RTF is not "full GPU" and must not be advertised as such.
_MPS_STFT_ON_CPU = True
CLIP_POLICY = "no_demucs_rescale"
DEFAULT_SEGMENT = 7.8
DEFAULT_OVERLAP = 0.25
DEFAULT_SHIFTS = 1


@dataclass(frozen=True)
class SeparateRequest:
    wav_44100_stereo: np.ndarray  # shape (2, T), float32
    model: str = DEFAULT_MODEL
    device: str = "auto"
    segment: float = DEFAULT_SEGMENT
    overlap: float = DEFAULT_OVERLAP
    shifts: int = DEFAULT_SHIFTS
    vocals_only_bag: bool = False
    cancel_event: object | None = None
    on_progress: Callable[[dict[str, Any]], None] | None = None


@dataclass(frozen=True)
class SeparateResult:
    vocals: np.ndarray  # (2, T), float32, 44100
    peak: float
    rtf: float
    device_used: str
    model_sha256: str


def separator_model_name(req: SeparateRequest) -> str:
    if req.vocals_only_bag:
        return VOCALS_ONLY_SIG
    return req.model


def resolve_device(requested: str) -> str:
    if requested and requested != "auto":
        if requested not in {"mps", "cuda", "cpu"}:
            raise ValueError(f"unknown device {requested!r}")
        return requested
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        return "cpu"
    if sys.platform == "darwin" and platform.machine() == "arm64":
        mps = getattr(getattr(torch, "backends", None), "mps", None)
        if mps is not None and getattr(mps, "is_available", lambda: False)():
            return "mps"
    cuda = getattr(torch, "cuda", None)
    if cuda is not None and getattr(cuda, "is_available", lambda: False)():
        return "cuda"
    return "cpu"


@contextmanager
def _offline_hub_env() -> Iterator[None]:
    # Fail closed around Separator only. Restore so a later in-process
    # user-click fetch (PR 15) is not stuck offline for the sidecar life.
    keys = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _separator_cls() -> Any:
    try:
        return importlib.import_module("demucs.api").Separator
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Python package 'demucs' is not installed in the engine interpreter. "
            "Install with: python3 -m pip install 'demucs>=4.0.1'"
        ) from exc


def _to_model_input(arr: np.ndarray) -> Any:
    # Copy: wav[:, start:end] is a view. A future in-place normalize (or a
    # mock that writes arr *= …) must not corrupt the next window's overlap.
    owned = np.array(arr, dtype=np.float32, copy=True)
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        return owned
    return torch.from_numpy(owned)


def _to_numpy(value: Any) -> np.ndarray:
    if isinstance(value, np.ndarray):
        return np.asarray(value, dtype=np.float32)
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        return np.asarray(value.numpy(), dtype=np.float32)
    return np.asarray(value, dtype=np.float32)


def _progress_callback(
    cancel_event: object | None,
    on_progress: Callable[[dict[str, Any]], None] | None,
    *,
    n_samples: int,
    window_start: list[int],
) -> Callable[[dict[str, Any]], None]:
    def callback(info: dict[str, Any]) -> None:
        raise_if_cancelled(cancel_event)
        if on_progress is None:
            return
        data = info if isinstance(info, dict) else {}
        offset = int(data.get("segment_offset") or 0) + int(window_start[0])
        on_progress(
            {
                "segment_offset": offset,
                "audio_length": int(n_samples),
            }
        )

    return callback


def exceeds_memory_cap(
    n_samples: int,
    channels: int,
    *,
    cap: int | None = None,
) -> bool:
    """True when n_samples * ch * 4 would exceed the 2 GiB cap.

    The cap means "do not hand Demucs the whole clip", not a process RSS
    limit. Design §3.4 accepts loading the extract; dest is a second
    full-length buffer. When a later I/O PR memmaps the extract, write
    ``out`` the same way (or in 10-min dest chunks) so the trip bounds RAM.
    """
    limit = MEMORY_CAP_BYTES if cap is None else int(cap)
    return pcm_nbytes(n_samples, channels) > limit


def window_hop_samples(
    sample_rate: int,
    *,
    window_s: float | None = None,
    overlap_s: float | None = None,
) -> tuple[int, int]:
    window_s = WINDOW_SECONDS if window_s is None else float(window_s)
    overlap_s = WINDOW_OVERLAP_SECONDS if overlap_s is None else float(overlap_s)
    window = max(1, int(round(window_s * float(sample_rate))))
    overlap = max(0, int(round(overlap_s * float(sample_rate))))
    if overlap >= window:
        overlap = window - 1
    return window, overlap


def should_window(
    n_samples: int,
    channels: int,
    sample_rate: int,
    *,
    window_s: float | None = None,
    cap: int | None = None,
) -> bool:
    """Window when the clip is longer than 10 min or the 2 GiB cap would trip."""
    if n_samples <= 0:
        return False
    if exceeds_memory_cap(n_samples, channels, cap=cap):
        return True
    window, _overlap = window_hop_samples(sample_rate, window_s=window_s)
    return n_samples > window


def window_slices(
    n_samples: int,
    sample_rate: int,
    *,
    window_s: float | None = None,
    overlap_s: float | None = None,
) -> list[tuple[int, int]]:
    """Half-open ``[start, end)`` slices, 10 min long, 1 s overlap."""
    if n_samples <= 0:
        return []
    window, overlap = window_hop_samples(
        sample_rate, window_s=window_s, overlap_s=overlap_s
    )
    if n_samples <= window:
        return [(0, int(n_samples))]
    hop = window - overlap
    slices: list[tuple[int, int]] = []
    start = 0
    while start < n_samples:
        end = min(start + window, n_samples)
        slices.append((start, end))
        if end >= n_samples:
            break
        start += hop
    return slices


def _fade_gains(n: int, fade_in: int, fade_out: int) -> np.ndarray:
    # Complementary linear ramps: linspace(..., endpoint=False) so an
    # adjacent fade-out + fade-in pair sums to 1 at every overlap sample.
    gain = np.ones(n, dtype=np.float32)
    if fade_in > 0:
        fade_in = min(int(fade_in), n)
        gain[:fade_in] = np.linspace(0.0, 1.0, fade_in, endpoint=False, dtype=np.float32)
    if fade_out > 0:
        fade_out = min(int(fade_out), n)
        gain[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, endpoint=False, dtype=np.float32)
    return gain


def overlap_add(
    chunks: Sequence[np.ndarray],
    slices: Sequence[tuple[int, int]],
    n_samples: int,
    overlap: int,
) -> np.ndarray:
    """Linear OLA of ``(C, T_i)`` windows into ``(C, n_samples)``.

    Fades live here, not inside Demucs. First window has no fade-in; last
    window has no fade-out so the tail is not attenuated.
    """
    if len(chunks) != len(slices):
        raise ValueError("chunks and slices must have the same length")
    if n_samples < 0:
        raise ValueError("n_samples must be >= 0")
    if not chunks:
        return np.zeros((2, max(0, n_samples)), dtype=np.float32)
    channels = int(_as_channels_first(chunks[0]).shape[0])
    out = np.zeros((channels, n_samples), dtype=np.float32)
    last = len(chunks) - 1
    for i, (chunk, (start, end)) in enumerate(zip(chunks, slices)):
        piece = _match_frames(_as_channels_first(chunk), end - start)
        fade_in = int(overlap) if i > 0 else 0
        fade_out = int(overlap) if i < last else 0
        overlap_add_into(out, piece, start, fade_in=fade_in, fade_out=fade_out)
    return out


def overlap_add_into(
    dest: np.ndarray,
    chunk: np.ndarray,
    start: int,
    *,
    fade_in: int,
    fade_out: int,
) -> None:
    """Mix one window into ``dest`` with complementary linear fades."""
    piece = np.asarray(chunk, dtype=np.float32)
    n = int(piece.shape[-1])
    if n == 0:
        return
    gain = _fade_gains(n, fade_in, fade_out)
    dest[:, start : start + n] += piece * gain


def _match_frames(arr: np.ndarray, n: int) -> np.ndarray:
    cur = int(arr.shape[-1])
    if cur == n:
        return arr
    if cur > n:
        return arr[..., :n]
    out = np.zeros(arr.shape[:-1] + (n,), dtype=np.float32)
    out[..., :cur] = arr
    return out


def _as_channels_first(wav: np.ndarray) -> np.ndarray:
    arr = np.asarray(wav, dtype=np.float32)
    if arr.ndim != 2:
        raise ValueError("wav_44100_stereo must be shape (C, T)")
    if arr.shape[0] in (1, 2):
        return arr
    if arr.shape[1] in (1, 2):
        return arr.T
    raise ValueError("wav_44100_stereo must be shape (C, T) with C in {1, 2}")


def _separate_one(separator: Any, wav: np.ndarray, cancel_event: object | None) -> np.ndarray:
    raise_if_cancelled(cancel_event)
    _mix, stems = separator.separate_tensor(
        _to_model_input(wav), sr=MODEL_SAMPLE_RATE
    )
    vocals = _as_channels_first(_to_numpy(stems["vocals"]))
    return _match_frames(vocals, int(wav.shape[-1]))


def _separate_windowed(
    separator: Any,
    wav: np.ndarray,
    cancel_event: object | None,
    *,
    window_s: float | None = None,
    overlap_s: float | None = None,
    window_start: list[int] | None = None,
) -> np.ndarray:
    """Run Demucs per 10-min window and OLA outside the model.

    Incremental mix avoids retaining every *window* tensor. ``out`` is still
    a full-length buffer — the cap is "do not hand Demucs the whole clip",
    not an RSS ceiling (see ``exceeds_memory_cap``). Clip-level cache lives
    in the job worker; finished 10-min windows are not resumed independently.
    Demucs ``segment_offset`` is remapped by ``window_start`` for SSE.
    Cancel discards ``out``.
    """
    n_samples = int(wav.shape[-1])
    channels = int(wav.shape[0])
    slices = window_slices(
        n_samples, MODEL_SAMPLE_RATE, window_s=window_s, overlap_s=overlap_s
    )
    _window, overlap = window_hop_samples(
        MODEL_SAMPLE_RATE, window_s=window_s, overlap_s=overlap_s
    )
    out = np.zeros((channels, n_samples), dtype=np.float32)
    last = len(slices) - 1
    for i, (start, end) in enumerate(slices):
        if window_start is not None:
            window_start[0] = int(start)
        raise_if_cancelled(cancel_event)
        piece = _separate_one(separator, wav[:, start:end], cancel_event)
        fade_in = overlap if i > 0 else 0
        fade_out = overlap if i < last else 0
        overlap_add_into(out, piece, start, fade_in=fade_in, fade_out=fade_out)
    return out


def separate_vocals(req: SeparateRequest, local_repo: Path) -> SeparateResult:
    """Separator(model=..., repo=local_repo) only. Never load a name without repo=."""
    repo = Path(local_repo)
    if req.vocals_only_bag:
        # YAML first, then the signature. Do not pass a .th filename.
        files = require_vocals_only_bag(repo)
        name = VOCALS_ONLY_SIG
    else:
        name = req.model
        files = require_model(name, repo)
    wav = _as_channels_first(req.wav_44100_stereo)
    device = resolve_device(req.device)
    windowed = should_window(int(wav.shape[-1]), int(wav.shape[0]), MODEL_SAMPLE_RATE)
    n_samples = int(wav.shape[-1])
    window_start = [0]
    callback = _progress_callback(
        req.cancel_event,
        req.on_progress,
        n_samples=n_samples,
        window_start=window_start,
    )

    with _offline_hub_env():
        separator = _separator_cls()(
            model=name,
            repo=repo,
            device=device,
            shifts=int(req.shifts),
            overlap=float(req.overlap),
            split=True,
            segment=float(req.segment),
            jobs=0,
            progress=False,
            callback=callback,
        )
        started = time.perf_counter()
        if windowed:
            vocals = _separate_windowed(
                separator, wav, req.cancel_event, window_start=window_start
            )
        else:
            vocals = _separate_one(separator, wav, req.cancel_event)
    vocals = _as_channels_first(vocals)
    # clip_policy=no_demucs_rescale: do not divide by peak or pass clip=rescale.
    # Report sample peak so a later stage can soft-clip if > 0 dBFS.
    peak = float(np.max(np.abs(vocals))) if vocals.size else 0.0
    duration = float(wav.shape[-1]) / float(MODEL_SAMPLE_RATE)
    elapsed = time.perf_counter() - started
    rtf = elapsed / duration if duration > 0 else 0.0
    return SeparateResult(
        vocals=vocals,
        peak=peak,
        rtf=rtf,
        device_used=device,
        model_sha256=weights_sha256(files),
    )


__all__ = [
    "CLIP_POLICY",
    "DEFAULT_OVERLAP",
    "DEFAULT_SEGMENT",
    "DEFAULT_SHIFTS",
    "MEMORY_CAP_BYTES",
    "WINDOW_OVERLAP_SECONDS",
    "WINDOW_SECONDS",
    "JobCancelled",
    "ModelNotInstalled",
    "SeparateRequest",
    "SeparateResult",
    "exceeds_memory_cap",
    "overlap_add",
    "pcm_nbytes",
    "raise_if_cancelled",
    "resolve_device",
    "separate_vocals",
    "separator_model_name",
    "should_window",
    "window_slices",
]
