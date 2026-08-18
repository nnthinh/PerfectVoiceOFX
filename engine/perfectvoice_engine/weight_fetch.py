"""User-click / CLI official Demucs weight fetch.

Infer and ``POST /v1/jobs`` must never import or call this module.
URLs live here (and ``scripts/download_demucs.py``) so CI can keep
them out of ``separate.py`` / ``models.py``.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from perfectvoice_engine.models import (
    ALLOWED_MODELS,
    files_for,
    require_model,
    sha256_file,
)

# Official hosts only. Path must stay under these prefixes (no user URL).
HF_HTDEMUCS = "https://huggingface.co/adefossez/HTDemucs"
HF_HTDEMUCS_FT = "https://huggingface.co/adefossez/HTDemucs-ft"
FB_HYBRID = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/"
ALLOWED_URL_PREFIXES = (HF_HTDEMUCS, HF_HTDEMUCS_FT, FB_HYBRID)
_HF_REPO = {
    "htdemucs": HF_HTDEMUCS,
    "htdemucs_ft": HF_HTDEMUCS_FT,
}
_CHUNK = 1024 * 1024
_UA = "PerfectVoice/0.1 (user-initiated weight download)"

ProgressFn = Callable[[str, int, int], None]


class WeightFetchError(RuntimeError):
    pass


class HostNotAllowed(WeightFetchError):
    def __init__(self, url: str) -> None:
        super().__init__(f"host not on Demucs allowlist: {url}")
        self.url = url


class ChecksumMismatch(WeightFetchError):
    def __init__(self, filename: str, expected: str, actual: str) -> None:
        super().__init__(
            f"checksum mismatch for {filename}: expected {expected}, got {actual}"
        )
        self.filename = filename
        self.expected = expected
        self.actual = actual


def assert_url_allowed(url: str) -> str:
    """Reject anything that is not an https official prefix (SSRF)."""
    text = str(url).strip()
    parsed = urlparse(text)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise HostNotAllowed(text)
    if ".." in parsed.path.split("/"):
        raise HostNotAllowed(text)
    for prefix in ALLOWED_URL_PREFIXES:
        root = prefix.rstrip("/")
        if text == root or text.startswith(root + "/"):
            return text
    raise HostNotAllowed(text)


def candidate_urls(name: str, filename: str) -> list[str]:
    if name not in _HF_REPO:
        raise ValueError(f"unknown model {name!r}")
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ValueError(f"illegal filename {filename!r}")
    hf = _HF_REPO[name]
    urls = [f"{hf}/resolve/main/{filename}"]
    # LocalRepo opens ``*.th``; Hub only ships safetensors. FB is the .th source.
    if filename.endswith(".th"):
        urls.append(f"{FB_HYBRID}{filename}")
    return [assert_url_allowed(url) for url in urls]


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        assert_url_allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(url: str, timeout: float | None = 120.0):
    assert_url_allowed(url)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": _UA},
    )
    opener = urllib.request.build_opener(_AllowlistRedirectHandler)
    return opener.open(req, timeout=timeout)


def _ensure_repo_dir(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(repo, 0o700)
    except OSError:
        pass


def _download_url(
    url: str,
    dest: Path,
    expected: str,
    *,
    filename: str,
    progress: ProgressFn | None,
) -> None:
    assert_url_allowed(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
    tmp_path = Path(tmp_name)
    digest = hashlib.sha256()
    written = 0
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            with _urlopen(url) as resp:
                total_header = None
                headers = getattr(resp, "headers", None)
                if headers is not None:
                    total_header = headers.get("Content-Length")
                try:
                    total = int(total_header) if total_header else 0
                except (TypeError, ValueError):
                    total = 0
                if progress is not None:
                    progress(filename, 0, total)
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(filename, written, total)
            out.flush()
            os.fsync(out.fileno())
        actual = digest.hexdigest()
        if actual != expected:
            raise ChecksumMismatch(filename, expected, actual)
        os.replace(tmp_path, dest)
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def download_model(
    name: str,
    local_repo: Path,
    *,
    manifest: Mapping[str, Mapping[str, str]] | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, str]:
    """Fetch ``name`` into ``local_repo``. Skip files that already hash-match."""
    if name not in ALLOWED_MODELS:
        raise ValueError(f"unknown model {name!r}")
    files = files_for(name, manifest)
    repo = Path(local_repo)
    _ensure_repo_dir(repo)
    for filename, expected in files.items():
        dest = repo / filename
        if dest.is_file():
            try:
                if sha256_file(dest) == expected:
                    continue
            except OSError:
                pass
        last_error: BaseException | None = None
        for url in candidate_urls(name, filename):
            try:
                _download_url(
                    url,
                    dest,
                    expected,
                    filename=filename,
                    progress=progress,
                )
                last_error = None
                break
            except ChecksumMismatch as exc:
                last_error = exc
                try:
                    dest.unlink()
                except OSError:
                    pass
            except (HostNotAllowed, urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
        if last_error is not None:
            if isinstance(last_error, WeightFetchError):
                raise last_error
            raise WeightFetchError(f"failed to fetch {filename}: {last_error}") from last_error
    return require_model(name, repo, manifest=manifest)


__all__ = [
    "ALLOWED_URL_PREFIXES",
    "ChecksumMismatch",
    "HostNotAllowed",
    "WeightFetchError",
    "assert_url_allowed",
    "candidate_urls",
    "download_model",
]
