#!/usr/bin/env python3
"""Pin the sha256 of Hub LFS checkpoints into weight_fetch.HUB_PINS.

Until a digest is pinned, downloads are verified against the Hub's
``X-Linked-Etag`` header. Pinning freezes today's file: a later upstream
swap then fails closed instead of being trusted.

    python3 scripts/pin_model_hashes.py          # print digests
    python3 scripts/pin_model_hashes.py --write  # rewrite HUB_PINS in place
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = REPO_ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine import weight_fetch as wf  # noqa: E402

TARGET = ENGINE_DIR / "perfectvoice_engine" / "weight_fetch.py"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def hub_sha256(url: str) -> str:
    """LFS sha256 from the un-followed redirect of ``url``."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=wf._ssl_context()),
        _NoRedirect,
    )
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": wf._UA})
    try:
        resp = opener.open(req, timeout=60)
        headers = resp.headers
    except urllib.error.HTTPError as exc:  # 302 surfaces here with no redirect handler
        headers = exc.headers
    digest = wf._etag_digest(headers.get("X-Linked-Etag")) or wf._etag_digest(headers.get("ETag"))
    if not digest:
        raise SystemExit(f"no sha256 advertised for {url}")
    return digest


def rewrite_pins(text: str, pins: dict[str, str]) -> str:
    names = {wf.ROFORMER_URL: "ROFORMER_URL", wf.ECAPA_URL: "ECAPA_URL"}
    for url, digest in pins.items():
        name = names[url]
        text, n = re.subn(
            rf"^(\s*){name}: (None|\"[0-9a-f]{{64}}\"),$",
            rf'\1{name}: "{digest}",',
            text,
            flags=re.M,
        )
        if n != 1:
            raise SystemExit(f"could not find HUB_PINS entry for {name}")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite HUB_PINS in weight_fetch.py")
    args = parser.parse_args()
    pins = {url: hub_sha256(url) for url in (wf.ROFORMER_URL, wf.ECAPA_URL)}
    for url, digest in pins.items():
        print(f"{digest}  {url}")
    if args.write:
        TARGET.write_text(rewrite_pins(TARGET.read_text(encoding="utf-8"), pins), encoding="utf-8")
        print(f"updated {TARGET.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
