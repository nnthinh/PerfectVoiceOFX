"""Demucs vocals stem — local ``repo=`` only.

Infer constructs ``Separator(model=..., repo=Path(local_repo))`` after the
weight files hash. A pretrained name with no ``repo=`` would Hub/AWS-fetch
in Demucs 4.1.0; that path is not used here.
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
from typing import Any, Callable, Iterator

import numpy as np

from perfectvoice_engine.models import (
    DEFAULT_MODEL,
    VOCALS_ONLY_SIG,
    ModelNotInstalled,
    require_model,
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


class JobCancelled(RuntimeError):
    pass


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
    return importlib.import_module("demucs.api").Separator


def _to_model_input(arr: np.ndarray) -> Any:
    contiguous = np.ascontiguousarray(arr, dtype=np.float32)
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        return contiguous
    return torch.from_numpy(contiguous)


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


def _cancel_callback(cancel_event: object | None) -> Callable[[dict[str, Any]], None] | None:
    if cancel_event is None:
        return None

    def callback(_info: dict[str, Any]) -> None:
        is_set = getattr(cancel_event, "is_set", None)
        if callable(is_set) and is_set():
            raise JobCancelled("job cancelled")

    return callback


def _as_channels_first(wav: np.ndarray) -> np.ndarray:
    arr = np.asarray(wav, dtype=np.float32)
    if arr.ndim != 2:
        raise ValueError("wav_44100_stereo must be shape (C, T)")
    if arr.shape[0] in (1, 2):
        return arr
    if arr.shape[1] in (1, 2):
        return arr.T
    raise ValueError("wav_44100_stereo must be shape (C, T) with C in {1, 2}")


def separate_vocals(req: SeparateRequest, local_repo: Path) -> SeparateResult:
    """Separator(model=..., repo=local_repo) only. Never load a name without repo=."""
    repo = Path(local_repo)
    name = separator_model_name(req)
    files = require_model(name, repo)
    wav = _as_channels_first(req.wav_44100_stereo)
    device = resolve_device(req.device)

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
            callback=_cancel_callback(req.cancel_event),
        )
        started = time.perf_counter()
        _mix, stems = separator.separate_tensor(_to_model_input(wav), sr=MODEL_SAMPLE_RATE)
    vocals = _to_numpy(stems["vocals"])
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
    "JobCancelled",
    "ModelNotInstalled",
    "SeparateRequest",
    "SeparateResult",
    "resolve_device",
    "separate_vocals",
    "separator_model_name",
]
