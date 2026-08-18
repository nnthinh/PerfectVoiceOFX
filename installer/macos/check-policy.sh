#!/usr/bin/env bash
# Lock installer policy: no /Library, no weights (Demucs + DFN), no user Python.
# Invoked from CI after the happy-path dry-run.
set -euo pipefail

macos="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$macos/../.." && pwd)"
build="$macos/build-pkg.sh"

expect_rc() {
  local want="$1"
  shift
  local rc=0
  set +e
  "$@" >/tmp/pv-policy.out 2>/tmp/pv-policy.err
  rc=$?
  set -e
  if [ "$rc" -ne "$want" ]; then
    echo "expected exit $want, got $rc: $*" >&2
    cat /tmp/pv-policy.err >&2 || true
    exit 1
  fi
}

expect_rc 2 bash "$build" --system

tmp="$(mktemp -d "${TMPDIR:-/tmp}/pv-policy.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/py"
printf '#!/usr/bin/env python3\nprint("no")\n' > "$tmp/py/perfectvoice-engine"
chmod +x "$tmp/py/perfectvoice-engine"
expect_rc 1 bash "$build" --engine-dir "$tmp/py" --dry-run

bash "$repo/scripts/spikes/build-hello-engine.sh" --out "$tmp/hello"
mkdir -p "$tmp/th" "$tmp/onnx"
cp "$tmp/hello" "$tmp/th/perfectvoice-engine"
cp "$tmp/hello" "$tmp/onnx/perfectvoice-engine"
echo fake > "$tmp/th/htdemucs.th"
echo fake > "$tmp/onnx/deepfilternet3.onnx"
expect_rc 1 bash "$build" --engine-dir "$tmp/th" --dry-run
expect_rc 1 bash "$build" --engine-dir "$tmp/onnx" --dry-run

expect_rc 2 bash "$macos/scripts/preinstall" pkg /
expect_rc 2 bash "$macos/scripts/preinstall" pkg /Library
expect_rc 2 bash "$macos/scripts/postinstall" pkg /
expect_rc 2 bash "$macos/scripts/postinstall" pkg \
  "/Library/Application Support/PerfectVoice"

echo "policy OK (--system, python, .th, .onnx, pre/postinstall dest=/)"
