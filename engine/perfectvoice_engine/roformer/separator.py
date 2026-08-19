"""Mel-Band RoFormer inference separator for Studio-Quality Vocal Extraction.

Runs Band-Split Rotary Position Embedding Transformer (Mel-Band RoFormer)
on Apple Silicon Metal (MPS), CUDA, or CPU with chunked overlap-add.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

from perfectvoice_engine.constants import raise_if_cancelled

ROFORMER_SR = 44100
_MODEL_CACHE: dict[str, Any] = {}


def _get_roformer_dir() -> Path:
    base = os.getenv("PERFECTVOICE_APP_SUPPORT")
    if base:
        d = Path(base) / "models" / "roformer"
    else:
        d = (
            Path.home()
            / "Library"
            / "Application Support"
            / "PerfectVoice"
            / "models"
            / "roformer"
        )
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_roformer_ready() -> bool:
    d = _get_roformer_dir()
    ckpt = d / "MelBandRoformer.ckpt"
    cfg = d / "config_vocals_mel_band_roformer_kj.yaml"
    return ckpt.exists() and cfg.exists() and ckpt.stat().st_size > 800_000_000


def resolve_roformer_device(requested: str | None = None) -> str:
    if requested and requested != "auto":
        if requested in {"mps", "cuda", "cpu"}:
            return requested
    import torch
    if sys.platform == "darwin" and getattr(getattr(torch, "backends", None), "mps", None) is not None:
        if torch.backends.mps.is_available():
            return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def get_roformer_model(device: str | None = None) -> Any:
    target_device = resolve_roformer_device(device)
    if "model" in _MODEL_CACHE and _MODEL_CACHE.get("device") == target_device:
        return _MODEL_CACHE["model"]

    d = _get_roformer_dir()
    ckpt_file = str(d / "MelBandRoformer.ckpt")
    cfg_file = str(d / "config_vocals_mel_band_roformer_kj.yaml")

    if not (os.path.exists(ckpt_file) and os.path.exists(cfg_file)):
        raise RuntimeError(
            f"Mel-Band RoFormer model files not found in [{d}]. Please ensure weights are installed."
        )

    import torch
    import yaml
    from perfectvoice_engine.roformer.mel_band_roformer import MelBandRoformer

    with open(cfg_file, "r", encoding="utf-8") as f:
        config = yaml.unsafe_load(f)

    model_args = config["model"]
    model_args["flash_attn"] = False
    model = MelBandRoformer(**model_args)

    state_dict = torch.load(ckpt_file, map_location="cpu", weights_only=False)
    if "state" in state_dict:
        state_dict = state_dict["state"]
    elif "model" in state_dict:
        state_dict = state_dict["model"]

    cleaned_sd = {}
    for k, v in state_dict.items():
        cleaned_k = k.replace("_orig_mod.", "").replace("module.", "")
        cleaned_sd[cleaned_k] = v

    model.load_state_dict(cleaned_sd, strict=False)
    model = model.to(target_device).eval()

    _MODEL_CACHE["model"] = model
    _MODEL_CACHE["device"] = target_device
    _MODEL_CACHE["config"] = config
    return model


def separate_roformer(
    wav: Any,
    sample_rate: int = 44100,
    device: str | None = None,
    cancel_event: object | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    """Separate clean studio vocals from background music/instruments using Mel-Band RoFormer."""
    import numpy as np
    import torch
    import torchaudio

    raise_if_cancelled(cancel_event)
    target_device = resolve_roformer_device(device)
    model = get_roformer_model(target_device)

    # Ensure 2D (C, T)
    arr = np.asarray(wav, dtype=np.float32)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=0)
    elif arr.ndim == 2 and arr.shape[0] == 1:
        arr = np.repeat(arr, 2, axis=0)
    elif arr.ndim == 2 and arr.shape[0] > 2:
        arr = arr.T

    channels, total_samples = arr.shape
    dur_s = round(total_samples / sample_rate, 2)

    # Resample to 44.1 kHz if needed
    tensor_in = torch.from_numpy(arr).float()
    if sample_rate != ROFORMER_SR:
        resampled = torchaudio.functional.resample(
            tensor_in, orig_freq=sample_rate, new_freq=ROFORMER_SR
        )
    else:
        resampled = tensor_in

    num_channels, resampled_len = resampled.shape
    chunk_size = 352800  # 8.0 seconds at 44.1 kHz
    step = 176400        # 4.0 seconds (50% overlap)

    if resampled_len <= chunk_size:
        # Pad to at least chunk_size for consistent STFT boundaries
        pad_len = chunk_size - resampled_len
        inp = torch.nn.functional.pad(resampled, (0, pad_len)).unsqueeze(0).to(target_device)
        with torch.no_grad():
            out = model(inp)
        out = out.squeeze(0)[:, :resampled_len].cpu()
    else:
        # Overlap-add across chunks
        out = torch.zeros((num_channels, resampled_len), dtype=torch.float32)
        weight = torch.zeros((1, resampled_len), dtype=torch.float32)
        window = torch.hann_window(chunk_size)

        num_chunks = int(np.ceil((resampled_len - chunk_size) / step)) + 1
        for idx in range(num_chunks):
            raise_if_cancelled(cancel_event)
            start = idx * step
            end = min(start + chunk_size, resampled_len)
            chunk = resampled[:, start:end]
            actual_len = chunk.shape[1]

            if actual_len < chunk_size:
                chunk = torch.nn.functional.pad(chunk, (0, chunk_size - actual_len))

            inp = chunk.unsqueeze(0).to(target_device)
            with torch.no_grad():
                chunk_out = model(inp).squeeze(0)[:, :actual_len].cpu()

            w = window[:actual_len].unsqueeze(0)
            out[:, start:end] += chunk_out * w
            weight[:, start:end] += w

            if on_progress is not None:
                pct = round(((idx + 1) / num_chunks) * 100.0, 1)
                on_progress({
                    "stage_name": f"Pass 1/2: Mel-Band RoFormer ({idx + 1}/{num_chunks})",
                    "overall_pct": round(pct * 0.5, 1),
                    "current_pass": 1,
                    "total_passes": 2,
                    "chunk_idx": idx + 1,
                    "total_chunks": num_chunks,
                    "chunk_pct": pct,
                    "message": f"Mel-Band RoFormer: processing chunk {idx + 1}/{num_chunks}...",
                    "audio_dur_s": dur_s,
                    "current_pos_s": round(end / ROFORMER_SR, 2),
                })

        weight = torch.clamp(weight, min=1e-6)
        out = out / weight

    # Resample back to original sample rate if needed
    if sample_rate != ROFORMER_SR:
        out = torchaudio.functional.resample(
            out, orig_freq=ROFORMER_SR, new_freq=sample_rate
        )

    out_np = out.numpy().astype(np.float32)
    return out_np
