#!/usr/bin/env bash
# Fail if official Demucs remotes leak into infer/load or the panel.
# Allowed: scripts/download_demucs.py, engine/perfectvoice_engine/weight_fetch.py, docs/
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

patterns='huggingface.co/adefossez|dl.fbaipublicfiles.com'
fail=0
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

{
  if [ -f engine/perfectvoice_engine/separate.py ]; then
    echo engine/perfectvoice_engine/separate.py
  fi
  if [ -f engine/perfectvoice_engine/models.py ]; then
    echo engine/perfectvoice_engine/models.py
  fi
  if [ -d engine/perfectvoice_engine ]; then
    find engine/perfectvoice_engine -type f \( -name '*.py' -o -name '*.pyi' \) \
      ! -name weight_fetch.py
  fi
  if [ -d engine/models ]; then
    find engine/models -type f \( -name '*.json' -o -name '*.py' -o -name '*.pyi' \)
  fi
  if [ -d host ]; then
    find host -type f ! -name .gitkeep
  fi
} | LC_ALL=C sort -u > "$tmp"

while IFS= read -r f; do
  [ -n "$f" ] || continue
  if grep -nE "$patterns" "$f" >/dev/null 2>&1; then
    echo "FORBIDDEN: official Demucs URL in load/infer/panel path: $f"
    grep -nE "$patterns" "$f" || true
    fail=1
  fi
done < "$tmp"

if [ "$fail" -ne 0 ]; then
  echo
  echo "huggingface.co/adefossez and dl.fbaipublicfiles.com are allowed only in"
  echo "scripts/download_demucs.py, engine/perfectvoice_engine/weight_fetch.py, and docs/."
  exit 1
fi

echo "URL gate OK (no official Demucs remotes in load/infer/panel paths)."
