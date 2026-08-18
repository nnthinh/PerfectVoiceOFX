#!/usr/bin/env python3
"""Dev + user-click official Demucs weight download.

Does not run during infer or ``POST /v1/jobs``. Writes into ``local_repo``
after sha256 verify.

Allowlist (same as ``weight_fetch.py``):
  https://huggingface.co/adefossez/HTDemucs
  https://huggingface.co/adefossez/HTDemucs-ft
  https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ENGINE = _ROOT / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from perfectvoice_engine.models import (  # noqa: E402
    ALLOWED_MODELS,
    DEFAULT_MODEL,
    default_local_repo,
)
from perfectvoice_engine.weight_fetch import (  # noqa: E402
    WeightFetchError,
    download_model,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="download_demucs.py",
        description=(
            "Fetch official Demucs weights into the local repo. "
            "Engineers may run this; the shipped panel uses the same "
            "fetcher only after the user clicks Download model."
        ),
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_MODEL,
        choices=sorted(ALLOWED_MODELS),
        help=f"Model bag to fetch (default {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Destination directory (default: platform local_repo)",
    )
    args = parser.parse_args(argv)
    repo = args.repo if args.repo is not None else default_local_repo()

    def progress(filename: str, done: int, total: int) -> None:
        if total > 0:
            print(f"{filename}: {done}/{total}", file=sys.stderr)
        else:
            print(f"{filename}: {done}", file=sys.stderr)

    try:
        files = download_model(args.name, repo, progress=progress)
    except (WeightFetchError, ValueError, OSError) as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        return 1
    print(f"ok {args.name} -> {repo}")
    for filename, digest in files.items():
        print(f"  {filename}  {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
