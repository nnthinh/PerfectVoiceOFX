"""Target Speaker Extraction (TSE) package."""

from perfectvoice_engine.tse.extractor import extract_target_speaker, gain_envelope, similarity_to_gain
from perfectvoice_engine.tse.store import SpeakerProfile, SpeakerStore, default_store_path
from perfectvoice_engine.tse.weights import (
    ECAPA_MODEL_ID,
    ENCODER_ID,
    SpeakerEncoderNotInstalled,
    is_ecapa_ready,
)

EMBEDDING_DIM = 192

_ENCODER_EXPORTS = ("ECAPAEncoder", "extract_embedding", "get_speaker_encoder")


def __getattr__(name: str):
    # Lazy: serve imports tse.weights and must not pull torch. Without
    # torch the encoder exports resolve to None (callers check).
    if name in _ENCODER_EXPORTS:
        try:
            from perfectvoice_engine.tse import encoder
        except ImportError:
            return None
        return getattr(encoder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ECAPA_MODEL_ID",
    "EMBEDDING_DIM",
    "ENCODER_ID",
    "ECAPAEncoder",
    "SpeakerEncoderNotInstalled",
    "extract_embedding",
    "extract_target_speaker",
    "gain_envelope",
    "get_speaker_encoder",
    "is_ecapa_ready",
    "similarity_to_gain",
    "SpeakerProfile",
    "SpeakerStore",
    "default_store_path",
]
