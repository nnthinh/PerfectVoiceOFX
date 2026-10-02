"""Torch-free location and readiness of the pretrained speaker encoder.

The encoder is SpeechBrain's ECAPA-TDNN trained on VoxCeleb 1+2
(``speechbrain/spkrec-ecapa-voxceleb``, Apache-2.0). Only the
``embedding_model.ckpt`` state dict is used; ``tse/encoder.py`` rebuilds
the network so no ``speechbrain`` dependency is needed.

Serve and the job gate import this module, so it must not import torch.
"""

from __future__ import annotations

import os
from pathlib import Path

ECAPA_MODEL_ID = "ecapa_voxceleb"
ECAPA_FILENAME = "embedding_model.ckpt"
# The checkpoint is ~83 MB; anything far smaller is a truncated download.
ECAPA_MIN_BYTES = 60_000_000
# Bump when the encoder, its features or its weights change: cached TSE
# output and enrolled voiceprints from another encoder are not comparable.
ENCODER_ID = "speechbrain-ecapa-voxceleb-v1"


class SpeakerEncoderNotInstalled(RuntimeError):
    def __init__(self, detail: str | None = None) -> None:
        extra = f" ({detail})" if detail else ""
        super().__init__(
            "Speaker encoder not installed. [Download model] (~83 MB)." + extra
        )
        self.name = ECAPA_MODEL_ID
        self.detail = detail


def app_support_models_dir() -> Path:
    base = os.getenv("PERFECTVOICE_APP_SUPPORT")
    if base:
        return Path(base) / "models"
    return Path.home() / "Library" / "Application Support" / "PerfectVoice" / "models"


def ecapa_dir() -> Path:
    return app_support_models_dir() / "ecapa"


def ecapa_checkpoint_path() -> Path:
    return ecapa_dir() / ECAPA_FILENAME


def is_ecapa_ready() -> bool:
    path = ecapa_checkpoint_path()
    try:
        return path.is_file() and path.stat().st_size >= ECAPA_MIN_BYTES
    except OSError:
        return False


def require_ecapa() -> Path:
    path = ecapa_checkpoint_path()
    if not is_ecapa_ready():
        raise SpeakerEncoderNotInstalled(f"{ECAPA_FILENAME} missing in {path.parent}")
    return path


__all__ = [
    "ECAPA_FILENAME",
    "ECAPA_MIN_BYTES",
    "ECAPA_MODEL_ID",
    "ENCODER_ID",
    "SpeakerEncoderNotInstalled",
    "app_support_models_dir",
    "ecapa_checkpoint_path",
    "ecapa_dir",
    "is_ecapa_ready",
    "require_ecapa",
]
