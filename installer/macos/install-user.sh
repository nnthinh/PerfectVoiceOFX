#!/usr/bin/env bash
# User-space install (no sudo). Never defaults to /Library.
# Thin wrapper around build-pkg.sh --install.
set -euo pipefail
if [ "${1:-}" = "--system" ]; then
  echo "refusing: will not install into /Library." >&2
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
  echo "refusing: will not install as root (user-space only; no /Library, no root-owned ~/Library)." >&2
  echo "omit sudo; run this script as the editor account." >&2
  exit 2
fi
exec "$(cd "$(dirname "$0")" && pwd)/build-pkg.sh" --install "$@"
