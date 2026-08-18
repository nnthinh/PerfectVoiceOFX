#!/usr/bin/env bash
# User-space install (no sudo). Never defaults to /Library.
# Thin wrapper around build-pkg.sh --install.
set -euo pipefail
if [ "${1:-}" = "--system" ]; then
  echo "refusing: will not install into /Library." >&2
  exit 2
fi
exec "$(cd "$(dirname "$0")" && pwd)/build-pkg.sh" --install "$@"
