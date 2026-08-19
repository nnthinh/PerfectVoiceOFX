"""Speaker Profile Storage and Management for TSE."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class SpeakerProfile:
    speaker_id: str
    name: str
    embedding: list[float]
    sample_duration_s: float
    created_at: str

    def to_numpy(self) -> np.ndarray:
        return np.array(self.embedding, dtype=np.float32)

    def to_dict(self, include_embedding: bool = False) -> dict[str, Any]:
        d = {
            "speaker_id": self.speaker_id,
            "name": self.name,
            "sample_duration_s": self.sample_duration_s,
            "created_at": self.created_at,
        }
        if include_embedding:
            d["embedding"] = self.embedding
        return d


def default_store_path() -> Path:
    base = os.environ.get("PERFECTVOICE_USER_DIR")
    if base:
        return Path(base) / "speakers.json"
    if os.name == "nt":
        appdata = os.environ.get("LOCALAPPDATA", "~")
        return Path(appdata).expanduser() / "PerfectVoice" / "speakers.json"
    return Path("~/Library/Application Support/PerfectVoice/speakers.json").expanduser()


class SpeakerStore:
    def __init__(self, store_file: Path | None = None) -> None:
        self.file = Path(store_file) if store_file is not None else default_store_path()
        self._profiles: dict[str, SpeakerProfile] = {}
        self._load()

    def _load(self) -> None:
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
            for item in raw.get("speakers", []):
                p = SpeakerProfile(
                    speaker_id=item["speaker_id"],
                    name=item["name"],
                    embedding=item["embedding"],
                    sample_duration_s=float(item.get("sample_duration_s", 0.0)),
                    created_at=item.get("created_at", ""),
                )
                self._profiles[p.speaker_id] = p
        except Exception:
            pass

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "speakers": [asdict(p) for p in self._profiles.values()],
        }
        temp = self.file.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(self.file)

    def enroll(
        self,
        name: str,
        embedding: np.ndarray | list[float],
        sample_duration_s: float = 0.0,
    ) -> SpeakerProfile:
        embed_list = (
            embedding.tolist() if isinstance(embedding, np.ndarray) else list(embedding)
        )
        speaker_id = f"spk_{uuid.uuid4().hex[:12]}"
        profile = SpeakerProfile(
            speaker_id=speaker_id,
            name=name.strip() or f"Speaker {len(self._profiles) + 1}",
            embedding=embed_list,
            sample_duration_s=round(sample_duration_s, 2),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._profiles[speaker_id] = profile
        self._save()
        return profile

    def get(self, speaker_id: str) -> SpeakerProfile | None:
        return self._profiles.get(speaker_id)

    def list_all(self) -> list[SpeakerProfile]:
        return sorted(self._profiles.values(), key=lambda p: p.created_at)

    def delete(self, speaker_id: str) -> bool:
        if speaker_id in self._profiles:
            del self._profiles[speaker_id]
            self._save()
            return True
        return False
