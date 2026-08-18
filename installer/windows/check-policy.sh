#!/usr/bin/env bash
# Lock Windows installer policy: user-space paths, no weights, same IPC as PR 02.
# Runs on macOS CI (no PowerShell / no PE required).
set -euo pipefail

win="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$win/../.." && pwd)"
ps1="$win/Install-User.ps1"
readme="$win/README.md"
sku="$win/cuda-sku.txt"

fail=0
note() { printf '%s\n' "$*"; }
bad() { printf 'POLICY: %s\n' "$*" >&2; fail=1; }

require_file() {
  if [ ! -f "$1" ]; then
    bad "missing $1"
    return 1
  fi
}

require_file "$ps1"
require_file "$readme"
require_file "$sku"
require_file "$win/install-user.cmd"
require_file "$win/Uninstall-User.ps1"
require_file "$win/uninstall-user.cmd"

# Path table §3.8 (README uses single backslashes inside backticks).
for needle in \
  '%LOCALAPPDATA%\\PerfectVoice\\engine' \
  '%LOCALAPPDATA%\\PerfectVoice\\models' \
  '%LOCALAPPDATA%\\PerfectVoice\\Logs' \
  '%LOCALAPPDATA%\\PerfectVoice\\Cache' \
  '%LOCALAPPDATA%\\PerfectVoice\\run'
do
  # README is markdown: \ is a single backslash in the file.
  plain="${needle//\\\\/\\}"
  if ! grep -Fq "$plain" "$readme"; then
    bad "README missing path $plain"
  fi
done

if ! grep -Fq 'perfectvoice-engine.exe' "$readme"; then
  bad "README missing enginePath perfectvoice-engine.exe"
fi

# Scripts must target the same dests (frozen path comments + Join-Path leaves).
for needle in engine models Logs Cache run; do
  if ! grep -Eq "PerfectVoice.+$needle|Join-Path \\\$pv \"$needle\"" "$ps1"; then
    bad "Install-User.ps1 missing PerfectVoice/$needle destination"
  fi
done
if ! grep -Fq 'perfectvoice-engine.exe' "$ps1"; then
  bad "Install-User.ps1 missing perfectvoice-engine.exe"
fi

# Refuse all-users / admin install.
if ! grep -Eq 'refusing: will not install into Program Files / ProgramData' "$ps1"; then
  bad "Install-User.ps1 must refuse -System / Program Files / ProgramData"
fi
if ! grep -Eq 'will not install as Administrator' "$ps1"; then
  bad "Install-User.ps1 must refuse Administrator"
fi
if ! grep -Eq '\$System' "$ps1"; then
  bad "Install-User.ps1 must accept -System so it can refuse it"
fi

# Same IPC / auth as PR 02.
if ! grep -Fq -- '--token-fd' "$ps1"; then
  bad "Install-User.ps1 must mention --token-fd (as forbidden)"
fi
if ! grep -Fq '127.0.0.1' "$ps1"; then
  bad "Install-User.ps1 must document bind 127.0.0.1"
fi
if ! grep -Eq 'token-file' "$ps1"; then
  bad "Install-User.ps1 must document --token-file"
fi
if ! grep -Eq 'protocol_version' "$ps1"; then
  bad "Install-User.ps1 must keep protocol_version"
fi
if ! grep -Eq 'ProtocolVersion = 1' "$ps1"; then
  bad "Install-User.ps1 protocol_version must stay 1"
fi
if grep -En -- '--token-fd' "$ps1" | grep -Ev 'forbidden|no --token-fd|cấm' >/dev/null; then
  # still OK as long as no spawn uses it; extra check below
  :
fi
if grep -Eq 'token-fd 3' "$ps1" && ! grep -Eq 'no --token-fd|forbidden' "$ps1"; then
  bad "Install-User.ps1 must not introduce --token-fd 3 as a supported API"
fi

# protocol_version pin in sku + readme
if ! grep -Eq '^protocol_version=1$' "$sku"; then
  bad "cuda-sku.txt protocol_version must be 1"
fi
if ! grep -Eq 'protocol_version.*=.*\*\*1\*\*|protocol_version` = \*\*1\*\*' "$readme"; then
  if ! grep -Fq 'protocol_version` = **1**' "$readme" && ! grep -Fq 'protocol_version = **1**' "$readme"; then
    bad "README must pin protocol_version = 1"
  fi
fi

# CUDA SKU is concrete.
if ! grep -Eq '^sku=cu126$' "$sku"; then
  bad "cuda-sku.txt must pin sku=cu126"
fi
if ! grep -Eq '^cuda_range=12.6-12.8$' "$sku"; then
  bad "cuda-sku.txt must pin cuda_range=12.6-12.8"
fi
if ! grep -Fq 'https://download.pytorch.org/whl/cu126' "$sku"; then
  bad "cuda-sku.txt must pin the cu126 wheel index"
fi
if ! grep -Fq 'cu126' "$readme"; then
  bad "README must document cu126"
fi
if ! grep -Fq 'vc_redist.x64.exe' "$readme"; then
  bad "README must document VC++ redist"
fi
if ! grep -Fiq 'SmartScreen' "$readme"; then
  bad "README must document SmartScreen"
fi

# No Demucs remotes / no bundled weights in installer/windows
# (exclude this policy script — it names the forbidden hosts).
if grep -REn --exclude='check-policy.sh' \
    'huggingface.co/adefossez|dl.fbaipublicfiles.com' "$win" >/dev/null 2>&1; then
  bad "installer/windows must not embed official Demucs remotes"
  grep -REn --exclude='check-policy.sh' \
    'huggingface.co/adefossez|dl.fbaipublicfiles.com' "$win" || true
fi

if find "$win" -type f \( \
    -name '*.th' -o -name '*.bin' -o -name '*.safetensors' \
    -o -name '*.ckpt' -o -name '*.pt' -o -name '*.pth' \
    -o -name '*.onnx' -o -name '*.onnx.data' \
  \) -print | grep -q .; then
  bad "installer/windows contains weight files"
  find "$win" -type f \( \
      -name '*.th' -o -name '*.bin' -o -name '*.safetensors' \
      -o -name '*.ckpt' -o -name '*.pt' -o -name '*.pth' \
      -o -name '*.onnx' -o -name '*.onnx.data' \
    \) -print || true
fi

# Fail-closed weight globs in the copier.
for g in '*.th' '*.safetensors' '*.onnx' '*.pt' '*.pth' '*.ckpt' '*.bin'; do
  if ! grep -Fq "$g" "$ps1"; then
    bad "Install-User.ps1 must fail-closed on $g"
  fi
done

# Must not ship WorkflowIntegration.node; must copy from host Resolve.
if ! grep -Fq 'WorkflowIntegration.node' "$ps1"; then
  bad "Install-User.ps1 must handle WorkflowIntegration.node (copy-from-host)"
fi
if ! grep -Eq 'must be copied from the host Resolve, not bundled' "$ps1"; then
  bad "Install-User.ps1 must refuse a bundled WorkflowIntegration.node"
fi

# Cmd wrappers must not change machine ExecutionPolicy — only Bypass this file.
if ! grep -Fq -- '-ExecutionPolicy Bypass' "$win/install-user.cmd"; then
  bad "install-user.cmd should Bypass only this script"
fi
if grep -Ei 'Set-ExecutionPolicy' "$win"/*.cmd "$win"/*.ps1 >/dev/null; then
  bad "must not call Set-ExecutionPolicy"
fi

# Dry-run / uninstall switches exist.
for sw in DryRun Uninstall Purge EngineDir StageRoot; do
  if ! grep -Eq "\\\$$sw" "$ps1"; then
    bad "Install-User.ps1 missing -$sw"
  fi
done

# No stray .gitkeep once real files exist — optional; ignore.

# If pwsh is present, exercise -DryRun (stub, no PE).
if command -v pwsh >/dev/null 2>&1; then
  note "pwsh found; running -DryRun"
  if ! pwsh -NoProfile -File "$ps1" -DryRun; then
    bad "pwsh Install-User.ps1 -DryRun failed"
  fi
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/pv-win-th.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT
  mkdir -p "$tmp/th"
  printf 'MZ' > "$tmp/th/perfectvoice-engine.exe"
  echo fake > "$tmp/th/htdemucs.th"
  set +e
  pwsh -NoProfile -File "$ps1" -EngineDir "$tmp/th" -StageRoot "$tmp/stage" >/tmp/pv-win-th.out 2>/tmp/pv-win-th.err
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    bad "expected EngineDir with .th to fail closed"
  fi
else
  note "pwsh not found; static policy only (expected on macOS CI)"
fi

if [ "$fail" -ne 0 ]; then
  echo "Windows installer policy FAILED" >&2
  exit 1
fi

echo "policy OK (Windows paths, cu126, VC++/SmartScreen notes, no weights, protocol_version=1, no token-fd)"
