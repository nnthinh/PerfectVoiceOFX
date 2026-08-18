"""Local Demucs weight inventory.

Fetch lives in a later PR / dev CLI. This module only reads a URL-free
manifest and hashes files already on disk so infer cannot open a socket.

Demucs 4.1.0 ``Separator(repo=)`` uses ``LocalRepo`` (``*.th``) plus
``BagOnlyRepo`` (``*.yaml``). It never opens ``*.safetensors``. The
manifest therefore lists bag YAML and ``{sig}-{checksum}.th`` — every
path the constructor will actually read — or ``is_model_ready`` is a
false positive.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping

DEFAULT_MODEL = "htdemucs"
QUALITY_MODEL = "htdemucs_ft"
# Vocals specialist inside the htdemucs_ft bag (flag off until A/B).
# Load reads the local bag YAML, then Separator(sig, repo=). Never a .th path.
VOCALS_ONLY_SIG = "04573f0d"
_BAG_SIG = re.compile(r"[0-9a-fA-F]{8}")
_BAG_MODELS_KEY = re.compile(r"^models\s*:\s*(.*)$")
ALLOWED_MODELS = frozenset({DEFAULT_MODEL, QUALITY_MODEL})
MODEL_NOT_INSTALLED = (
    "Model not installed. [Download model] "
    "(~84 MB for Fast / ~330 MB for Quality)."
)
_CHUNK = 1024 * 1024


class ModelNotInstalled(RuntimeError):
    def __init__(self, name: str, detail: str | None = None) -> None:
        extra = f" ({detail})" if detail else ""
        super().__init__(f"{MODEL_NOT_INSTALLED}{extra}")
        self.name = name
        self.detail = detail


def default_local_repo() -> Path:
    env = os.environ.get("PERFECTVOICE_DEMUCS_REPO")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "PerfectVoice" / "models" / "demucs"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "PerfectVoice"
            / "models"
            / "demucs"
        )
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "PerfectVoice" / "models" / "demucs"


def manifest_path() -> Path:
    env = os.environ.get("PERFECTVOICE_MANIFEST")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[1] / "models" / "manifest.json"


def _looks_like_url(value: object) -> bool:
    text = str(value).strip().lower()
    return "://" in text or text.startswith("http")


def _normalize_files(entry: object, name: str) -> dict[str, str]:
    if not isinstance(entry, dict) or not entry:
        raise ValueError(f"manifest[{name!r}] must be filename → sha256")
    # Allow the singleton {filename, sha256} spelling as well as a map.
    if "filename" in entry and "sha256" in entry:
        filename = entry["filename"]
        digest = entry["sha256"]
        if _looks_like_url(filename) or _looks_like_url(digest):
            raise ValueError("manifest must not contain URLs")
        return {str(filename): str(digest).lower()}
    out: dict[str, str] = {}
    for filename, digest in entry.items():
        if _looks_like_url(filename) or _looks_like_url(digest):
            raise ValueError("manifest must not contain URLs")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"manifest[{name!r}][{filename!r}] is not a sha256")
        out[str(filename)] = digest.lower()
    return out


def load_manifest(path: Path | None = None) -> dict[str, dict[str, str]]:
    src = path if path is not None else manifest_path()
    data = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest must be an object")
    out: dict[str, dict[str, str]] = {}
    for name, entry in data.items():
        out[str(name)] = _normalize_files(entry, str(name))
    return out


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def local_repo_signature(filename: str) -> str | None:
    """Signature ``LocalRepo`` would bind to ``filename`` (``*.th`` only)."""
    path = Path(filename)
    if path.suffix != ".th":
        return None
    stem = path.stem
    if "-" in stem:
        return stem.rsplit("-", 1)[0]
    return stem


def parse_bag_models(text: str) -> list[str]:
    """Signatures from a Demucs bag YAML. Stdlib only — no PyYAML, no Hub."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.split("#", 1)[0].rstrip()
        if stripped != stripped.lstrip():
            continue
        match = _BAG_MODELS_KEY.match(stripped)
        if match is None:
            continue
        rest = match.group(1).strip()
        if rest.startswith("["):
            return [hit.group(0).lower() for hit in _BAG_SIG.finditer(rest)]
        sigs: list[str] = []
        for nxt in lines[i + 1 :]:
            body = nxt.split("#", 1)[0]
            if not body.strip():
                continue
            if not body[:1].isspace() and not body.lstrip().startswith("-"):
                break
            if not body.lstrip().startswith("-"):
                continue
            found = _BAG_SIG.search(body)
            if found is not None:
                sigs.append(found.group(0).lower())
        return sigs
    return []


def _quality_bag_yaml(
    manifest: Mapping[str, Mapping[str, str]] | None,
) -> tuple[str, str]:
    try:
        files = files_for(QUALITY_MODEL, manifest)
    except ValueError as exc:
        raise ModelNotInstalled(VOCALS_ONLY_SIG, "bag YAML not in manifest") from exc
    yamls = [(name, digest) for name, digest in files.items() if name.endswith(".yaml")]
    if not yamls:
        raise ModelNotInstalled(VOCALS_ONLY_SIG, "bag YAML not in manifest")
    preferred = f"{QUALITY_MODEL}.yaml"
    yamls.sort(key=lambda item: (item[0] != preferred, item[0]))
    return yamls[0]


def require_listed_in_local_bag(
    local_repo: Path,
    sig: str = VOCALS_ONLY_SIG,
    *,
    manifest: Mapping[str, Mapping[str, str]] | None = None,
) -> list[str]:
    """Read the ft bag YAML already on disk. Missing / wrong list → not installed."""
    repo = Path(local_repo)
    if not repo.is_dir():
        raise ModelNotInstalled(sig, "local repo missing")
    yaml_name, expected = _quality_bag_yaml(manifest)
    path = repo / yaml_name
    if not path.is_file():
        raise ModelNotInstalled(sig, f"{yaml_name} missing")
    if sha256_file(path) != expected:
        raise ModelNotInstalled(sig, f"{yaml_name} checksum mismatch")
    listed = parse_bag_models(path.read_text(encoding="utf-8"))
    if sig not in listed:
        raise ModelNotInstalled(sig, f"{sig} not listed in {yaml_name}")
    return listed


def require_vocals_only_bag(
    local_repo: Path,
    *,
    manifest: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, str]:
    """YAML first, then the specialist .th LocalRepo would open. No hardcoded path."""
    return require_model(VOCALS_ONLY_SIG, local_repo, manifest=manifest)


def files_for(name: str, manifest: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, str]:
    table = dict(manifest) if manifest is not None else load_manifest()
    if name in table:
        return dict(table[name])
    # Signature lookup (vocals-only) — the .th LocalRepo opens, never a remote.
    for files in table.values():
        matched = {
            filename: digest
            for filename, digest in files.items()
            if local_repo_signature(filename) == name
        }
        if matched:
            return matched
    raise ValueError(f"unknown model {name!r}")


def weights_sha256(files: Mapping[str, str]) -> str:
    if len(files) == 1:
        return next(iter(files.values()))
    digest = hashlib.sha256()
    for filename in sorted(files):
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[filename].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def require_model(
    name: str,
    local_repo: Path,
    *,
    manifest: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, str]:
    """Verify ``name`` is present under ``local_repo``. Never opens a socket."""
    if name == VOCALS_ONLY_SIG:
        # Specialist is a bag member, not a bag name. YAML must list it
        # locally before we accept the .th LocalRepo will open.
        require_listed_in_local_bag(local_repo, name, manifest=manifest)
    files = files_for(name, manifest)
    repo = Path(local_repo)
    if not repo.is_dir():
        raise ModelNotInstalled(name, "local repo missing")
    for filename, expected in files.items():
        path = repo / filename
        if not path.is_file():
            raise ModelNotInstalled(name, f"{filename} missing")
        actual = sha256_file(path)
        if actual != expected:
            raise ModelNotInstalled(name, f"{filename} checksum mismatch")
    return files


def is_model_ready(
    name: str,
    local_repo: Path,
    *,
    manifest: Mapping[str, Mapping[str, str]] | None = None,
) -> bool:
    try:
        require_model(name, local_repo, manifest=manifest)
    except (ModelNotInstalled, ValueError, OSError):
        return False
    return True


def models_ready(
    local_repo: Path,
    *,
    manifest: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, bool]:
    table = dict(manifest) if manifest is not None else load_manifest()
    names = [n for n in (DEFAULT_MODEL, QUALITY_MODEL) if n in table] or list(table)
    return {name: is_model_ready(name, local_repo, manifest=table) for name in names}
