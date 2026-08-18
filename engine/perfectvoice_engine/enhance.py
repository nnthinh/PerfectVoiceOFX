"""Optional DeepFilterNet 3 residual-noise stage (default off).

Production never no-ops: missing package or weights raises
``EnhancerNotInstalled``. Infer does not open a socket — weights come
from ``scripts/download_deepfilternet.py`` into ``models/deepfilternet/``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import numpy as np

from perfectvoice_engine.ffmpeg_io import reject_if_multichannel

ENHANCER_ID = "deepfilternet3"
ENHANCER_SAMPLE_RATE = 48000
ENHANCER_NOT_INSTALLED = (
    "enhancer not installed. Run scripts/download_deepfilternet.py "
    "(DeepFilterNet 3, MIT + Apache-2.0)."
)


class EnhancerNotInstalled(RuntimeError):
    def __init__(self, detail: str | None = None) -> None:
        extra = f" ({detail})" if detail else ""
        super().__init__(f"{ENHANCER_NOT_INSTALLED}{extra}")
        self.detail = detail


def default_model_dir() -> Path:
    env = os.environ.get("PERFECTVOICE_DFN_REPO")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "PerfectVoice" / "models" / "deepfilternet"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "PerfectVoice"
            / "models"
            / "deepfilternet"
        )
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "PerfectVoice" / "models" / "deepfilternet"


def _checkpoints_ready(base: Path) -> bool:
    ckpt = base / "checkpoints"
    if not ckpt.is_dir():
        return False
    try:
        return any(ckpt.iterdir())
    except OSError:
        return False


def _model_base(model_dir: Path) -> Path | None:
    """Dir with both config.ini and a non-empty checkpoints/ (not ini alone)."""
    root = Path(model_dir)
    for candidate in (root, root / "DeepFilterNet3"):
        if (candidate / "config.ini").is_file() and _checkpoints_ready(candidate):
            return candidate.resolve()
    return None


def _df_importable() -> bool:
    try:
        return importlib.util.find_spec("df.enhance") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def is_enhancer_installed(model_dir: Path | None = None) -> bool:
    if not _df_importable():
        return False
    root = Path(model_dir) if model_dir is not None else default_model_dir()
    return _model_base(root) is not None


def _as_frames(samples: np.ndarray) -> tuple[np.ndarray, bool]:
    arr = np.asarray(samples, dtype=np.float32)
    squeeze = False
    if arr.ndim == 1:
        arr = arr[:, None]
        squeeze = True
    elif arr.ndim != 2:
        raise ValueError("samples must be [frames] or [frames, ch]")
    reject_if_multichannel(arr.shape[1])
    return np.ascontiguousarray(arr, dtype=np.float32), squeeze


def _import_dfn():
    try:
        import torch
        from df.enhance import enhance as df_enhance
        from df.enhance import init_df
    except ImportError as exc:
        raise EnhancerNotInstalled("package missing") from exc
    return torch, df_enhance, init_df


def _run_dfn(frames: np.ndarray, model_dir: Path) -> np.ndarray:
    """Local-dir only. A pretrained name would make init_df auto-fetch."""
    # config.ini alone is not enough — init_df exit(1)s on a missing ckpt.
    base = _model_base(model_dir)
    if base is None:
        raise EnhancerNotInstalled(f"weights missing under {model_dir}")
    # Absolute existing dir only. The bare name "DeepFilterNet3" makes
    # init_df() auto-download; a filesystem path does not.
    if not (base / "config.ini").is_file() or not _checkpoints_ready(base):
        raise EnhancerNotInstalled(f"incomplete weights under {base}")

    torch, df_enhance, init_df = _import_dfn()
    try:
        model, df_state, _, _ = init_df(
            model_base_dir=str(base),
            log_file=None,
            log_level="ERROR",
        )
        audio = torch.from_numpy(np.ascontiguousarray(frames.T))
        out = df_enhance(model, df_state, audio, pad=True)
    except SystemExit as exc:
        raise EnhancerNotInstalled("checkpoint load failed") from exc
    return np.ascontiguousarray(out.detach().cpu().numpy().T, dtype=np.float32)


def enhance(
    samples: np.ndarray,
    sample_rate: int,
    *,
    model_dir: Path | None = None,
) -> np.ndarray:
    """Enhance vocals at 48 kHz. Raises if DeepFilterNet 3 is not installed."""
    if int(sample_rate) != ENHANCER_SAMPLE_RATE:
        raise ValueError(
            f"DeepFilterNet3 requires {ENHANCER_SAMPLE_RATE} Hz, got {sample_rate}"
        )
    frames, squeeze = _as_frames(samples)
    root = Path(model_dir) if model_dir is not None else default_model_dir()
    if not is_enhancer_installed(root):
        detail = (
            "package missing"
            if not _df_importable()
            else f"weights missing under {root}"
        )
        raise EnhancerNotInstalled(detail)
    out = _run_dfn(frames, root)
    if out.shape[0] != frames.shape[0] and abs(int(out.shape[0]) - int(frames.shape[0])) > 1:
        raise ValueError(
            f"enhancer length mismatch {frames.shape[0]} vs {out.shape[0]} frames"
        )
    return out[:, 0] if squeeze else out
