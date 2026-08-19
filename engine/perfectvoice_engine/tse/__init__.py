"""Target Speaker Extraction (TSE) package."""

from perfectvoice_engine.tse.store import SpeakerProfile, SpeakerStore, default_store_path

try:
    from perfectvoice_engine.tse.encoder import (
        EMBEDDING_DIM,
        ECAPAEncoder,
        extract_embedding,
        get_speaker_encoder,
    )
    from perfectvoice_engine.tse.extractor import (
        TargetSpeakerModel,
        extract_target_speaker,
        get_tse_model,
    )
except ImportError:
    EMBEDDING_DIM = 192
    ECAPAEncoder = None  # type: ignore[assignment, misc]
    extract_embedding = None  # type: ignore[assignment]
    get_speaker_encoder = None  # type: ignore[assignment]
    TargetSpeakerModel = None  # type: ignore[assignment, misc]
    extract_target_speaker = None  # type: ignore[assignment]
    get_tse_model = None  # type: ignore[assignment]

__all__ = [
    "EMBEDDING_DIM",
    "ECAPAEncoder",
    "extract_embedding",
    "get_speaker_encoder",
    "TargetSpeakerModel",
    "extract_target_speaker",
    "get_tse_model",
    "SpeakerProfile",
    "SpeakerStore",
    "default_store_path",
]

