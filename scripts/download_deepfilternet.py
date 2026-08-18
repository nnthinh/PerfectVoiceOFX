#!/usr/bin/env python3
"""Fetch DeepFilterNet 3 weights (MIT + Apache-2.0).

Official archive only — this is not a Demucs fetch path. Do not add
Demucs Hub or Facebook public-files remotes here.

  URL:    https://github.com/Rikorose/DeepFilterNet/raw/main/models/DeepFilterNet3.zip
  SHA256: <placeholder — pin after a verified fetch>

Infer (perfectvoice_engine.enhance) never opens a socket. Run this
script from a developer machine (or a later user-click endpoint).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = REPO_ROOT / "engine"

# Official DFN3 PyTorch bundle (MIT + Apache-2.0). Not Demucs.
URL = "https://github.com/Rikorose/DeepFilterNet/raw/main/models/DeepFilterNet3.zip"
ARCHIVE_NAME = "DeepFilterNet3.zip"
# All-zero means not pinned yet — refuse to fetch unsigned bytes.
SHA256 = "0000000000000000000000000000000000000000000000000000000000000000"
_PLACEHOLDER_SHA = "0" * 64
_CHUNK = 1024 * 1024


def sha_is_placeholder(digest: str = SHA256) -> bool:
    return digest.lower() == _PLACEHOLDER_SHA


def dest_dir(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))
    from perfectvoice_engine.enhance import default_model_dir

    return default_model_dir()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fetch(dest: Path, *, url: str = URL, expected_sha: str = SHA256) -> Path:
    """Download + verify + extract. Refuses a placeholder checksum."""
    if sha_is_placeholder(expected_sha):
        raise SystemExit(
            "SHA256 is a placeholder. Pin the official DeepFilterNet3.zip "
            "checksum before fetch. Refusing to download unsigned weights.\n"
            f"  url: {url}\n"
            f"  dest: {dest}"
        )
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o700)
    archive = dest / ARCHIVE_NAME
    with urlopen(url) as resp, archive.open("wb") as out:  # noqa: S310 — allowlisted URL
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            out.write(chunk)
    actual = _sha256_file(archive)
    if actual != expected_sha.lower():
        archive.unlink(missing_ok=True)
        raise SystemExit(
            f"checksum mismatch for {ARCHIVE_NAME}: got {actual}, expected {expected_sha}"
        )
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download DeepFilterNet 3 weights (MIT + Apache-2.0). "
            "Separate from Demucs fetch."
        )
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Extract directory (default: platform models/deepfilternet)",
    )
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="Print the official archive URL and exit.",
    )
    args = parser.parse_args(argv)
    if args.print_url:
        print(URL)
        return 0
    dest = dest_dir(args.dest)
    print("DeepFilterNet 3 weights (MIT + Apache-2.0)")
    print(f"  url:    {URL}")
    print(f"  sha256: {SHA256}" + ("  (placeholder)" if sha_is_placeholder() else ""))
    print(f"  dest:   {dest}")
    if sha_is_placeholder():
        print(
            "\nSHA256 is a placeholder. Pin the official archive checksum "
            "before fetch. Refusing to download unsigned weights."
        )
        return 2
    fetch(dest)
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
