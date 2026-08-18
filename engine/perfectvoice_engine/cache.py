"""Full-identity cache keys (§3.7).

Every field that changes on-disk output bytes is hashed. The client never
writes wet_dry_sample_rate — the engine derives it from enhancer_id
(none→44100, dfn3/deepfilternet3→48000) and folds the derived rate in so
a future enhancer that shares an id but not a blend rate cannot collide.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

# Normative client/identity fields. Order is the §3.7 concatenation order.
HASH_FIELDS: tuple[str, ...] = (
    "file_id",
    "src_in",
    "src_out",
    "audio_stream_index",
    "channel_map",
    "model_name",
    "weights_sha256",
    "vocals_only_bag",
    "wet",
    "gain",
    "mono",
    "handles_requested",
    "file_duration_seconds",
    "segment",
    "overlap",
    "shifts",
    "enhancer_id",
    "project_sample_rate",
    "sample_format",
    "resampler_id",
    "clip_policy",
    "engine_semver",
)

# Derived after enhancer_id is canonicalized. Not a client-writable field.
DERIVED_HASH_FIELDS: tuple[str, ...] = ("wet_dry_sample_rate",)

DOMAIN = b"perfectvoice.cache.v1"
CLIP_HASH_PREFIX_LEN = 12

ENHANCER_NONE = "none"
ENHANCER_DFN3 = "deepfilternet3"
WET_DRY_RATE_NONE = 44100
WET_DRY_RATE_DFN3 = 48000

_ENHANCER_ALIASES = {
    "none": ENHANCER_NONE,
    "deepfilternet3": ENHANCER_DFN3,
    "dfn3": ENHANCER_DFN3,
}

_SAMPLE_FORMAT_ALIASES = {
    "pcm24": "pcm24",
    "float32": "float32",
    "f32": "float32",
}

_FLOAT_FIELDS = frozenset(
    {
        "wet",
        "gain",
        "handles_requested",
        "file_duration_seconds",
        "segment",
        "overlap",
    }
)
_INT_FIELDS = frozenset(
    {
        "src_in",
        "src_out",
        "audio_stream_index",
        "shifts",
        "project_sample_rate",
        "wet_dry_sample_rate",
    }
)
_BOOL_FIELDS = frozenset({"vocals_only_bag", "mono"})
_STR_FIELDS = frozenset(
    {
        "model_name",
        "weights_sha256",
        "enhancer_id",
        "sample_format",
        "resampler_id",
        "clip_policy",
        "engine_semver",
    }
)


class CacheError(ValueError):
    """Identity field is missing, mistyped, or not allowed as a client input."""


def canonicalize_enhancer_id(enhancer_id: str) -> str:
    key = str(enhancer_id).strip().lower()
    try:
        return _ENHANCER_ALIASES[key]
    except KeyError as exc:
        raise CacheError(f"unknown enhancer_id: {enhancer_id!r}") from exc


def canonicalize_sample_format(sample_format: str) -> str:
    key = str(sample_format).strip().lower()
    try:
        return _SAMPLE_FORMAT_ALIASES[key]
    except KeyError as exc:
        raise CacheError(f"unknown sample_format: {sample_format!r}") from exc


def wet_dry_sample_rate_for(enhancer_id: str) -> int:
    """none→44100, dfn3/deepfilternet3→48000. Client never supplies this."""
    canon = canonicalize_enhancer_id(enhancer_id)
    if canon == ENHANCER_NONE:
        return WET_DRY_RATE_NONE
    return WET_DRY_RATE_DFN3


def file_id_from_path(path: str | os.PathLike[str]) -> tuple[int, int, int, int]:
    """POSIX (dev, ino, size, mtime_ns); Windows (volume serial, file index, size, mtime_ns)."""
    resolved = os.fspath(path)
    if sys.platform == "win32":
        return _win_file_id(resolved)
    st = os.stat(resolved, follow_symlinks=True)
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))


def clip_hash12(input_hash: str) -> str:
    hex_hash = str(input_hash).lower()
    if len(hex_hash) < CLIP_HASH_PREFIX_LEN:
        raise CacheError("input_hash too short for clip directory name")
    return hex_hash[:CLIP_HASH_PREFIX_LEN]


def default_cache_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "PerfectVoice"
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "PerfectVoice" / "Cache"
    return Path.home() / ".cache" / "PerfectVoice"


def default_cache_index_path() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "PerfectVoice"
            / "cache-index.sqlite"
        )
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "PerfectVoice" / "cache-index.sqlite"
    return Path.home() / ".local" / "share" / "PerfectVoice" / "cache-index.sqlite"


def compute_input_hash(
    *,
    file_id: Sequence[int],
    src_in: int,
    src_out: int,
    audio_stream_index: int,
    channel_map: Sequence[int],
    model_name: str,
    weights_sha256: str,
    vocals_only_bag: bool,
    wet: float,
    gain: float,
    mono: bool,
    handles_requested: float,
    file_duration_seconds: float,
    segment: float,
    overlap: float,
    shifts: int,
    enhancer_id: str,
    project_sample_rate: int,
    sample_format: str,
    resampler_id: str,
    clip_policy: str,
    engine_semver: str,
) -> str:
    """sha256 hex of §3.7 identity fields plus derived wet_dry_sample_rate."""
    identity = normalize_identity(
        {
            "file_id": file_id,
            "src_in": src_in,
            "src_out": src_out,
            "audio_stream_index": audio_stream_index,
            "channel_map": channel_map,
            "model_name": model_name,
            "weights_sha256": weights_sha256,
            "vocals_only_bag": vocals_only_bag,
            "wet": wet,
            "gain": gain,
            "mono": mono,
            "handles_requested": handles_requested,
            "file_duration_seconds": file_duration_seconds,
            "segment": segment,
            "overlap": overlap,
            "shifts": shifts,
            "enhancer_id": enhancer_id,
            "project_sample_rate": project_sample_rate,
            "sample_format": sample_format,
            "resampler_id": resampler_id,
            "clip_policy": clip_policy,
            "engine_semver": engine_semver,
        }
    )
    return hashlib.sha256(encode_identity(identity)).hexdigest()


def normalize_identity(fields: Mapping[str, object]) -> dict[str, object]:
    if "wet_dry_sample_rate" in fields:
        raise CacheError(
            "wet_dry_sample_rate is engine-derived; pass enhancer_id only"
        )
    missing = [name for name in HASH_FIELDS if name not in fields]
    if missing:
        raise CacheError(f"missing identity fields: {', '.join(missing)}")

    extra = [name for name in fields if name not in HASH_FIELDS]
    if extra:
        raise CacheError(f"unknown identity fields: {', '.join(sorted(extra))}")

    identity: dict[str, object] = {}
    for name in HASH_FIELDS:
        identity[name] = _normalize_field(name, fields[name])

    identity["enhancer_id"] = canonicalize_enhancer_id(str(identity["enhancer_id"]))
    identity["sample_format"] = canonicalize_sample_format(str(identity["sample_format"]))
    identity["weights_sha256"] = _normalize_weights_sha256(str(identity["weights_sha256"]))
    identity["wet_dry_sample_rate"] = wet_dry_sample_rate_for(str(identity["enhancer_id"]))
    return identity


def encode_identity(identity: Mapping[str, object]) -> bytes:
    """Stable, length-prefixed encoding. Field names in the stream prevent splice collisions."""
    chunks = [DOMAIN, b"\0"]
    for name in HASH_FIELDS + DERIVED_HASH_FIELDS:
        if name not in identity:
            raise CacheError(f"identity missing {name}")
        chunks.append(_encode_named(name, identity[name]))
    return b"".join(chunks)


def _normalize_field(name: str, value: object) -> object:
    if name == "file_id":
        return _normalize_file_id(value)
    if name == "channel_map":
        return _normalize_channel_map(value)
    if name in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise CacheError(f"{name} must be bool, got {type(value).__name__}")
        return value
    if name in _INT_FIELDS:
        return _as_int(name, value)
    if name in _FLOAT_FIELDS:
        return _as_float(name, value)
    if name in _STR_FIELDS:
        if not isinstance(value, str) or not value:
            raise CacheError(f"{name} must be a non-empty str")
        return value
    raise CacheError(f"unhandled identity field: {name}")


def _normalize_file_id(value: object) -> tuple[int, int, int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CacheError("file_id must be a 4-int sequence")
    parts = tuple(int(part) for part in value)
    if len(parts) != 4:
        raise CacheError("file_id must be (dev|volume, ino|index, size, mtime)")
    return parts


def _normalize_channel_map(value: object) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CacheError("channel_map must be a sequence of channel indices")
    channels = tuple(_as_int("channel_map[]", item) for item in value)
    if not 1 <= len(channels) <= 2:
        raise CacheError("channel_map must have 1 or 2 channels")
    if any(ch < 0 for ch in channels):
        raise CacheError("channel_map indices must be >= 0")
    return channels


def _normalize_weights_sha256(value: str) -> str:
    digest = value.strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise CacheError("weights_sha256 must be 64 lowercase hex chars")
    return digest


def _as_int(name: str, value: object) -> int:
    # bool is int; a True vocals flag must not silently become src_in=1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CacheError(f"{name} must be int, got {type(value).__name__}")
    return int(value)


def _as_float(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CacheError(f"{name} must be a number, got {type(value).__name__}")
    return float(value)


def _encode_named(name: str, value: object) -> bytes:
    return name.encode("ascii") + b"\0" + _encode_value(value)


def _encode_value(value: object) -> bytes:
    if isinstance(value, bool):
        return b"B" + (b"\x01" if value else b"\x00")
    if isinstance(value, int):
        return b"I" + struct.pack(">q", value)
    if isinstance(value, float):
        return b"F" + struct.pack(">d", value)
    if isinstance(value, str):
        payload = value.encode("utf-8")
        return b"S" + struct.pack(">I", len(payload)) + payload
    if isinstance(value, tuple):
        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            body = b"".join(struct.pack(">q", item) for item in value)
            return b"T" + struct.pack(">I", len(value)) + body
    raise CacheError(f"cannot encode {type(value).__name__}")


def _win_file_id(path: str) -> tuple[int, int, int, int]:
    import ctypes
    from ctypes import wintypes

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    generic_read = 0x80000000
    share = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
    open_existing = 3
    backup_semantics = 0x02000000
    invalid = wintypes.HANDLE(-1).value

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
    get_info.restype = wintypes.BOOL

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        path,
        generic_read,
        share,
        None,
        open_existing,
        backup_semantics,
        None,
    )
    if handle in (None, invalid):
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for {path!r}")
    try:
        info = BY_HANDLE_FILE_INFORMATION()
        if not get_info(handle, ctypes.byref(info)):
            raise OSError(
                ctypes.get_last_error(),
                f"GetFileInformationByHandle failed for {path!r}",
            )
        file_index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
        mtime_ns = int(os.stat(path, follow_symlinks=True).st_mtime_ns)
        return (int(info.dwVolumeSerialNumber), file_index, size, mtime_ns)
    finally:
        close_handle(handle)


@dataclass(frozen=True)
class CacheEntry:
    input_hash: str
    path: str
    mtime_ns: int
    size: int


class CacheIndex:
    """Tiny SQLite map hash → artifact path + mtime, for lookup and later GC."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                input_hash TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                recorded_at INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CacheIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def put(self, input_hash: str, path: str | os.PathLike[str]) -> CacheEntry:
        resolved = os.fspath(path)
        st = os.stat(resolved)
        entry = CacheEntry(
            input_hash=str(input_hash).lower(),
            path=resolved,
            mtime_ns=int(st.st_mtime_ns),
            size=int(st.st_size),
        )
        self._conn.execute(
            """
            INSERT OR REPLACE INTO cache_entries
                (input_hash, path, mtime_ns, size, recorded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entry.input_hash, entry.path, entry.mtime_ns, entry.size, time.time_ns()),
        )
        self._conn.commit()
        return entry

    def get(self, input_hash: str) -> CacheEntry | None:
        row = self._conn.execute(
            "SELECT input_hash, path, mtime_ns, size FROM cache_entries WHERE input_hash = ?",
            (str(input_hash).lower(),),
        ).fetchone()
        if row is None:
            return None
        entry = CacheEntry(*row)
        try:
            st = os.stat(entry.path)
        except OSError:
            return None
        if int(st.st_mtime_ns) != entry.mtime_ns or int(st.st_size) != entry.size:
            return None
        return entry
