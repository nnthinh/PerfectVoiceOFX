#!/usr/bin/env bash
# Build the spike Mach-O. Optional --sign-dev uses the local Apple Development
# identity (NOT Developer ID). Never notarizes.
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$root/../.." && pwd)"
out="${HELLO_ENGINE_OUT:-$root/hello-engine}"
sign_dev=0

while [ $# -gt 0 ]; do
  case "$1" in
    --sign-dev) sign_dev=1 ;;
    --out) out="$2"; shift ;;
    -h|--help)
      echo "usage: $0 [--sign-dev] [--out PATH]"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
  shift
done

clang -O2 -Wall -Wextra -o "$out" "$root/hello-engine.c"
echo "built $out"

if [ "$sign_dev" -eq 1 ]; then
  ident="$(security find-identity -v -p codesigning | awk -F'\"' '/Apple Development:/{print $2; exit}')"
  if [ -z "$ident" ]; then
    echo "no Apple Development identity; leaving unsigned" >&2
    exit 1
  fi
  entitlements="$repo/installer/macos/entitlements-engine.plist"
  codesign --force --options runtime --timestamp=none \
    --entitlements "$entitlements" \
    --sign "$ident" \
    "$out"
  echo "signed with $ident (Apple Development, not Developer ID; not notarized)"
  codesign -dv --verbose=2 "$out" 2>&1 | sed -n '1,20p'
fi
