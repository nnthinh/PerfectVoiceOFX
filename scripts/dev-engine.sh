#!/bin/bash
# DEV sidecar: same spawn contract as hello-engine / onedir (argv: serve --bind …).
# Installer rewrites PYTHON + REPO below. Absolute shebang so WI spawn (empty PATH) works.
set -euo pipefail
PYTHON="${PERFECTVOICE_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.14/bin/python3}"
REPO="${PERFECTVOICE_REPO:-/Users/nnthinh/DEV/PerfectVoiceOFX}"
export PYTHONPATH="${REPO}/engine${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"
exec "$PYTHON" -u -m perfectvoice_engine.serve "$@"
