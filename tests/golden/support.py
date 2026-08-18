"""Shared helpers for golden tests. Synthetic WAVs only — never commit audio."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
SCHEMA_DIR = REPO_ROOT / "tests" / "fixtures" / "schemas"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine.ffmpeg_io import (  # noqa: E402
    FFmpegError,
    ffmpeg_bin,
    write_wav,
)
from perfectvoice_engine.models import DEFAULT_MODEL  # noqa: E402
from perfectvoice_engine.pipeline import run_job  # noqa: E402

# Appendix A required fixture (seconds @ 48 kHz).
T0 = 0.2
T1 = 1.2
HANDLE_S = 0.5
FILE_DUR = 2.0
SRC_SR = 48000
SOURCE_IN_SAMPLE = 9600  # 0.2 * 48000
SOURCE_OUT_SAMPLE = 57600  # 1.2 * 48000
IMPULSE_AT = SRC_SR  # t = 1.0 s


def have_ffmpeg() -> bool:
    try:
        ffmpeg_bin()
        return True
    except FFmpegError:
        return False


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_htdemucs_fixture(root: Path) -> dict[str, dict[str, str]]:
    """Tiny local-repo stand-in. Not real Demucs weights."""
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "htdemucs.yaml": b"models: ['955717e8']\n",
        "955717e8-8726e21a.th": b"golden-fixture-not-a-weight",
    }
    mapping: dict[str, str] = {}
    for name, payload in files.items():
        (root / name).write_bytes(payload)
        mapping[name] = digest(payload)
    return {DEFAULT_MODEL: mapping}


def _stereo(frames: np.ndarray) -> np.ndarray:
    arr = np.asarray(frames, dtype=np.float32)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    return np.ascontiguousarray(arr, dtype=np.float32)


def sine_frames(
    seconds: float,
    sample_rate: int,
    freqs: tuple[float, float] = (440.0, 660.0),
    amp: float = 0.25,
) -> np.ndarray:
    n = int(round(seconds * sample_rate))
    t = np.arange(n, dtype=np.float32) / np.float32(sample_rate)
    return _stereo(
        np.stack(
            [amp * np.sin(2 * np.pi * freqs[0] * t), amp * np.sin(2 * np.pi * freqs[1] * t)],
            axis=1,
        )
    )


def impulse_frames(seconds: float, sample_rate: int, at_sample: int, amp: float = 0.9) -> np.ndarray:
    n = int(round(seconds * sample_rate))
    frames = np.zeros((n, 2), dtype=np.float32)
    idx = int(at_sample)
    if not 0 <= idx < n:
        raise ValueError(f"impulse index {idx} out of range 0..{n}")
    frames[idx] = amp
    return frames


def click_train_frames(seconds: float, sample_rate: int, period_s: float = 0.25) -> np.ndarray:
    n = int(round(seconds * sample_rate))
    frames = np.zeros((n, 2), dtype=np.float32)
    step = max(1, int(round(period_s * sample_rate)))
    frames[0:n:step] = 0.8
    return frames


def speech_plus_bed_frames(seconds: float, sample_rate: int) -> np.ndarray:
    """Short synthetic 'voice + bed' — not licensed speech."""
    n = int(round(seconds * sample_rate))
    t = np.arange(n, dtype=np.float32) / np.float32(sample_rate)
    formant = 0.18 * np.sin(2 * np.pi * 180.0 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3.0 * t))
    bed = 0.08 * np.sin(2 * np.pi * 440.0 * t) + 0.06 * np.sin(2 * np.pi * 880.0 * t)
    left = formant + bed
    right = formant * 0.85 + bed * 1.1
    return _stereo(np.stack([left, right], axis=1))


def generate_suite(root: Path) -> dict[str, Path]:
    """Five short synthetic clips. Caller owns ``root`` (temp dir)."""
    root.mkdir(parents=True, exist_ok=True)
    specs: list[tuple[str, np.ndarray, int]] = [
        ("sine_48k_stereo.wav", sine_frames(FILE_DUR, SRC_SR), SRC_SR),
        ("impulse_48k_stereo.wav", impulse_frames(FILE_DUR, SRC_SR, IMPULSE_AT), SRC_SR),
        ("click_train_48k.wav", click_train_frames(FILE_DUR, SRC_SR), SRC_SR),
        ("speech_plus_bed_48k.wav", speech_plus_bed_frames(FILE_DUR, SRC_SR), SRC_SR),
        ("sine_96k_stereo.wav", sine_frames(1.0, 96000), 96000),
    ]
    paths: dict[str, Path] = {}
    for name, frames, sr in specs:
        dest = root / name
        write_wav(dest, frames, sr, sample_format="pcm24")
        paths[name] = dest
    return paths


class IdentitySeparator:
    """Separator stand-in: vocals == mix. No Demucs, no Hub."""

    instances: list["IdentitySeparator"] = []
    separate_calls = 0

    def __init__(self, model: str = DEFAULT_MODEL, repo: Path | None = None, **kwargs: Any) -> None:
        if repo is None:
            raise AssertionError("Separator must be constructed with repo=")
        self.model = model
        self.repo = Path(repo)
        self.kwargs = kwargs
        IdentitySeparator.instances.append(self)

    def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
        IdentitySeparator.separate_calls += 1
        arr = wav
        if hasattr(arr, "detach"):
            arr = arr.detach()
        if hasattr(arr, "cpu"):
            arr = arr.cpu()
        if hasattr(arr, "numpy"):
            arr = arr.numpy()
        arr = np.asarray(arr, dtype=np.float32)
        callback = self.kwargs.get("callback")
        if callable(callback):
            callback({"segment_offset": 0, "audio_length": int(arr.shape[-1])})
        vocals = np.array(arr, dtype=np.float32, copy=True)
        zeros = np.zeros_like(arr)
        return arr, {"vocals": vocals, "drums": zeros, "bass": zeros, "other": zeros}

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.separate_calls = 0


def job_body(
    source_path: str | Path,
    output_dir: str | Path,
    allowed_roots: list[str],
    *,
    source_in_sample: int = SOURCE_IN_SAMPLE,
    source_out_sample: int = SOURCE_OUT_SAMPLE,
    source_sample_rate: int = SRC_SR,
    project_sample_rate: int = SRC_SR,
    file_duration_seconds: float = FILE_DUR,
    handles_seconds: float = HANDLE_S,
    sample_format: str = "pcm24",
    wet: float = 1.0,
    enhancer: str = "none",
    use_cache: bool = True,
) -> dict[str, Any]:
    clip = json.loads((SCHEMA_DIR / "clip.valid.json").read_text(encoding="utf-8"))
    params = json.loads((SCHEMA_DIR / "params.valid.json").read_text(encoding="utf-8"))
    clip["source_path"] = str(source_path)
    clip["source_in_sample"] = source_in_sample
    clip["source_out_sample"] = source_out_sample
    clip["source_sample_rate"] = source_sample_rate
    clip["project_sample_rate"] = project_sample_rate
    clip["file_duration_seconds"] = file_duration_seconds
    clip["handles_seconds"] = handles_seconds
    params["output_dir"] = str(output_dir)
    params["allowed_roots"] = list(allowed_roots)
    params["use_cache"] = use_cache
    params["sample_format"] = sample_format
    params["wet"] = wet
    params["enhancer"] = enhancer
    params["device"] = "cpu"
    return {"clips": [clip], "params": params, "output_dir": str(output_dir)}


@contextmanager
def identity_pipeline(tmp: Path) -> Iterator[dict[str, Path]]:
    """Local dummy repo + identity Separator. Zero Demucs / Hub."""
    repo = tmp / "repo"
    manifest = write_htdemucs_fixture(repo)
    manifest_path = tmp / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    cache_dir = tmp / "cache"
    cache_index = tmp / "cache-index.sqlite"
    IdentitySeparator.reset()
    env = {
        "PERFECTVOICE_DEMUCS_REPO": str(repo),
        "PERFECTVOICE_MANIFEST": str(manifest_path),
        "PERFECTVOICE_CACHE_DIR": str(cache_dir),
        "PERFECTVOICE_CACHE_INDEX": str(cache_index),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    with (
        patch.dict(os.environ, env, clear=False),
        patch("perfectvoice_engine.separate._separator_cls", return_value=IdentitySeparator),
        patch(
            "perfectvoice_engine.separate._to_model_input",
            side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
        ),
        patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
    ):
        yield {
            "repo": repo,
            "manifest": manifest_path,
            "cache_dir": cache_dir,
            "cache_index": cache_index,
        }


def run_clip(body: dict[str, Any]) -> dict[str, Any]:
    # run_job owns CacheIndex so identical identities can hit.
    return run_job(body["clips"], body["params"], body["output_dir"])[0]


def lag_samples(a: np.ndarray, b: np.ndarray) -> int:
    """Integer lag of ``a`` vs ``b`` (positive = ``a`` late). Uses mono mix."""

    def _mono(x: np.ndarray) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim == 2:
            arr = arr.mean(axis=1)
        return np.ascontiguousarray(arr)

    xa, xb = _mono(a), _mono(b)
    n = max(xa.size, xb.size)
    if xa.size < n:
        xa = np.pad(xa, (0, n - xa.size))
    if xb.size < n:
        xb = np.pad(xb, (0, n - xb.size))
    corr = np.correlate(xa, xb, mode="full")
    return int(np.argmax(corr) - (n - 1))
