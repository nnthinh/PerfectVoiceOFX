#!/usr/bin/env bash
# Build a macOS product .pkg (pkgbuild + productbuild).
#
# User-space only:
#   ~/Library/Application Support/PerfectVoice/engine/perfectvoice-engine
#   ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/com.perfectvoice.panel/
# Never defaults to /Library. Refuses --system.
#
# Engine payload is hello-engine (Mach-O spawn contract) unless --engine-dir
# points at a later PyInstaller onedir. No user Python. No Demucs/DFN weights.
#
# Codesign / notary: optional. Missing Developer ID does not fail the build
# (this machine has none). Commands: installer/macos/README.md
set -euo pipefail

macos="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$macos/../.." && pwd)"
version="0.1.0"
identifier="com.perfectvoice.macos"
entitlements="$macos/entitlements-engine.plist"

dry_run=0
do_install=0
sign=0
sign_dev=0
engine_dir=""
out_dir="$macos/dist"

# Relative to $HOME and to the package root (Installer remaps "/" → $HOME).
engine_rel="Library/Application Support/PerfectVoice/engine"
panel_rel="Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/com.perfectvoice.panel"
node_src="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node"

usage() {
  cat <<EOF
usage: $0 [options]

  --dry-run         Stage, pkgbuild, productbuild in a temp dir; delete; exit 0
  --install         Copy staged payload into the current user's home (no sudo)
  --engine-dir DIR  Use a PyInstaller onedir (must contain perfectvoice-engine)
  --sign            Codesign engine with Developer ID Application if present
  --sign-dev        Codesign with Apple Development (local only; not notarize)
  --out-dir DIR     Where to write the product pkg (default: installer/macos/dist)
  -h, --help

Refuses --system / /Library / EUID 0 on --install.
Does not bundle Demucs/DFN weights (including *.onnx).
Does not require a user Python. Does not fail without Developer ID.
Supported installer CLI: -target CurrentUserHomeDirectory (not -target /).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --install) do_install=1 ;;
    --engine-dir)
      engine_dir="$2"
      shift
      ;;
    --sign) sign=1 ;;
    --sign-dev) sign_dev=1 ;;
    --out-dir)
      out_dir="$2"
      shift
      ;;
    --system)
      echo "refusing: will not install into /Library (panel + engine are user-space)." >&2
      echo "omit --system; use --install for ~/Library, or open the .pkg (currentUserHome)." >&2
      exit 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [ ! -f "$entitlements" ]; then
  echo "missing 00c entitlements: $entitlements" >&2
  exit 1
fi

if [ "$do_install" -eq 1 ] && [ "$(id -u)" -eq 0 ]; then
  echo "refusing: will not install as root (user-space only; no /Library, no root-owned ~/Library)." >&2
  echo "omit sudo; run installer/macos/install-user.sh as the editor account." >&2
  exit 2
fi

log() { printf '%s\n' "$*"; }

developer_id_app() {
  security find-identity -v -p codesigning 2>/dev/null \
    | awk -F'"' '/Developer ID Application:/{print $2; exit}'
}

developer_id_installer() {
  security find-identity -v -p codesigning 2>/dev/null \
    | awk -F'"' '/Developer ID Installer:/{print $2; exit}'
}

apple_development_id() {
  security find-identity -v -p codesigning 2>/dev/null \
    | awk -F'"' '/Apple Development:/{print $2; exit}'
}

forbid_weights() {
  local root="$1"
  local hits
  hits="$(find "$root" -type f \( \
      -name '*.th' -o -name '*.bin' -o -name '*.safetensors' \
      -o -name '*.ckpt' -o -name '*.pt' -o -name '*.pth' \
      -o -name '*.onnx' -o -name '*.onnx.data' \
    \) -print 2>/dev/null || true)"
  if [ -n "$hits" ]; then
    echo "refusing: model weights in payload (installer must not bundle Demucs/DFN):" >&2
    printf '%s\n' "$hits" >&2
    exit 1
  fi
}

verify_stage() {
  local root="$1"
  local engine="$root/$engine_rel/perfectvoice-engine"
  local manifest="$root/$panel_rel/manifest.xml"

  if [ ! -f "$engine" ]; then
    echo "stage missing engine binary: $engine" >&2
    exit 1
  fi
  if [ ! -f "$manifest" ]; then
    echo "stage missing panel manifest: $manifest" >&2
    exit 1
  fi
  if [ -f "$root/$panel_rel/WorkflowIntegration.node" ]; then
    echo "refusing: WorkflowIntegration.node must be copied from the host Resolve, not bundled" >&2
    exit 1
  fi
  if find "$root/$panel_rel" -name '*.test.js' -print -quit | grep -q .; then
    echo "refusing: panel tests must not ship in the pkg" >&2
    exit 1
  fi

  forbid_weights "$root"

  if head -c 80 "$engine" | grep -q '^#!.*python'; then
    echo "refusing: engine is a Python script (pkg must not require user Python)" >&2
    exit 1
  fi
  if ! file "$engine" | grep -Eq 'Mach-O'; then
    echo "refusing: engine is not Mach-O (need hello-engine or a PyInstaller onedir):" >&2
    file "$engine" >&2
    exit 1
  fi
  if [ ! -x "$engine" ]; then
    echo "refusing: engine is not executable: $engine" >&2
    exit 1
  fi
}

require_00c_entitlements() {
  local bin="$1"
  local dump
  dump="$(codesign -d --entitlements :- "$bin" 2>/dev/null || true)"
  local missing=0
  local key
  for key in \
    com.apple.security.cs.disable-library-validation \
    com.apple.security.cs.allow-jit \
    com.apple.security.cs.allow-unsigned-executable-memory \
    com.apple.security.network.client
  do
    if ! printf '%s' "$dump" | grep -q "$key"; then
      echo "missing 00c entitlement on $bin: $key" >&2
      missing=1
    fi
  done
  if [ "$missing" -ne 0 ]; then
    echo "codesign -d --entitlements did not show the four 00c keys" >&2
    exit 1
  fi
}

# 00c §8: dylib/.so first, other Mach-Os, then entry with entitlements-engine.plist.
deep_sign_onedir() {
  local onedir="$1"
  local ident="$2"
  local ts=()
  if [ "$sign" -eq 1 ]; then
    ts=(--timestamp)
  else
    ts=(--timestamp=none)
  fi

  local f
  while IFS= read -r -d '' f; do
    codesign --force --options runtime "${ts[@]}" --sign "$ident" "$f"
  done < <(find "$onedir" -type f \( -name '*.dylib' -o -name '*.so' -o -name '*.so.*' \) -print0)

  while IFS= read -r -d '' f; do
    file "$f" | grep -q 'Mach-O' || continue
    codesign --force --options runtime "${ts[@]}" --sign "$ident" "$f"
  done < <(find "$onedir" -type f -perm +111 ! -name 'perfectvoice-engine' \
      ! -name '*.dylib' ! -name '*.so' ! -name '*.so.*' -print0)

  codesign --force --options runtime "${ts[@]}" \
    --entitlements "$entitlements" --sign "$ident" \
    "$onedir/perfectvoice-engine"
  require_00c_entitlements "$onedir/perfectvoice-engine"
}

maybe_sign_engine() {
  local onedir="$1"
  local bin="$onedir/perfectvoice-engine"
  local ident=""
  if [ "$sign" -eq 1 ]; then
    ident="$(developer_id_app || true)"
    if [ -z "$ident" ]; then
      log "no Developer ID Application; leaving engine unsigned (cannot notarize on this machine)"
      return 0
    fi
    if [ -n "$engine_dir" ]; then
      deep_sign_onedir "$onedir" "$ident"
    else
      codesign --force --options runtime --timestamp \
        --entitlements "$entitlements" --sign "$ident" "$bin"
      require_00c_entitlements "$bin"
    fi
    log "signed engine with $ident"
    return 0
  fi
  if [ "$sign_dev" -eq 1 ]; then
    ident="$(apple_development_id || true)"
    if [ -z "$ident" ]; then
      log "no Apple Development identity; leaving engine unsigned"
      return 0
    fi
    if [ -n "$engine_dir" ]; then
      deep_sign_onedir "$onedir" "$ident"
    else
      codesign --force --options runtime --timestamp=none \
        --entitlements "$entitlements" --sign "$ident" "$bin"
      require_00c_entitlements "$bin"
    fi
    log "signed engine with $ident (Apple Development, not Developer ID; not notarized)"
  fi
}

write_engine_stub_note() {
  local dest="$1"
  cat > "$dest" <<'EOF'
PerfectVoice engine (macOS)

enginePath (§3.8 rule 2):
  ~/Library/Application Support/PerfectVoice/engine/perfectvoice-engine

This package ships hello-engine — a small Mach-O that implements the
panel spawn contract (serve --bind 127.0.0.1 --port 0 --token-file;
READY + GET /v1/health). It does not load Demucs or PyTorch.

The production sidecar is a PyInstaller onedir (no user Python / conda),
deep-signed with installer/macos/entitlements-engine.plist (00c). Drop
that onedir here (or rebuild with --engine-dir) when it exists.

Official Demucs weights are NOT in this package. Use Download model
in the panel. Do not install this tree under /Library by default.
EOF
}

stage_payload() {
  local root="$1"
  rm -rf "$root"
  mkdir -p "$root/$engine_rel" "$root/$panel_rel"

  if [ -n "$engine_dir" ]; then
    if [ ! -d "$engine_dir" ]; then
      echo "engine dir not found: $engine_dir" >&2
      exit 1
    fi
    if [ ! -f "$engine_dir/perfectvoice-engine" ]; then
      echo "engine dir missing perfectvoice-engine: $engine_dir" >&2
      exit 1
    fi
    # Fail closed: do not silently strip checkpoints from a provided onedir.
    forbid_weights "$engine_dir"
    rsync -a \
      --exclude '*.th' --exclude '*.bin' --exclude '*.safetensors' \
      --exclude '*.ckpt' --exclude '*.pt' --exclude '*.pth' \
      --exclude '*.onnx' --exclude '*.onnx.data' \
      --exclude '__pycache__' --exclude '.DS_Store' \
      "$engine_dir/" "$root/$engine_rel/"
    log "staged engine from onedir: $engine_dir"
  else
    bash "$repo/scripts/spikes/build-hello-engine.sh" \
      --out "$root/$engine_rel/perfectvoice-engine"
    write_engine_stub_note "$root/$engine_rel/ENGINE-STUB.txt"
    log "staged hello-engine stub (production engine is a PyInstaller onedir later)"
  fi
  chmod 755 "$root/$engine_rel/perfectvoice-engine"
  maybe_sign_engine "$root/$engine_rel"

  rsync -a \
    --exclude 'WorkflowIntegration.node' \
    --exclude '*.test.js' \
    --exclude 'install-user.sh' \
    --exclude '.gitkeep' \
    --exclude '.DS_Store' \
    "$repo/host/com.perfectvoice.panel/" "$root/$panel_rel/"

  verify_stage "$root"
}

write_distribution() {
  local dest="$1"
  local component_name="$2"
  cat > "$dest" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
    <title>PerfectVoice</title>
    <organization>com.perfectvoice</organization>
    <domains enable_anywhere="false" enable_currentUserHome="true" enable_localSystem="false"/>
    <options customize="never" require-scripts="false" hostArchitectures="arm64"/>
    <welcome file="welcome.txt" mime-type="text/plain"/>
    <conclusion file="conclusion.txt" mime-type="text/plain"/>
    <license file="LICENSE.txt" mime-type="text/plain"/>
    <choices-outline>
        <line choice="$identifier"/>
    </choices-outline>
    <choice id="$identifier" visible="false" title="PerfectVoice">
        <pkg-ref id="$identifier"/>
    </choice>
    <pkg-ref id="$identifier" version="$version" auth="none" onConclusion="none">$component_name</pkg-ref>
</installer-gui-script>
EOF
}

patch_auth_none() {
  local pkg="$1"
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/pv-pkginfo.XXXXXX")"
  pkgutil --expand "$pkg" "$tmp/exp"
  if [ -f "$tmp/exp/PackageInfo" ]; then
    # currentUserHome must not demand root / /Library.
    sed -i '' 's/auth="root"/auth="none"/g' "$tmp/exp/PackageInfo"
    if grep -q 'install-location="/Library' "$tmp/exp/PackageInfo"; then
      echo "refusing: component install-location is /Library" >&2
      exit 1
    fi
  fi
  pkgutil --flatten "$tmp/exp" "$pkg"
  rm -rf "$tmp"
}

verify_pkg() {
  local component="$1"
  local listing
  listing="$(pkgutil --payload-files "$component")"
  if printf '%s\n' "$listing" | grep -E '\.(th|bin|safetensors|ckpt|pt|pth|onnx|onnx\.data)$' >/dev/null; then
    echo "refusing: weights inside component pkg" >&2
    exit 1
  fi
  if ! printf '%s\n' "$listing" | grep -q "Library/Application Support/PerfectVoice/engine/perfectvoice-engine"; then
    echo "refusing: component pkg missing user-space enginePath" >&2
    printf '%s\n' "$listing" >&2
    exit 1
  fi
  if ! printf '%s\n' "$listing" | grep -q "com.perfectvoice.panel/manifest.xml"; then
    echo "refusing: component pkg missing user-space panel" >&2
    exit 1
  fi
  if printf '%s\n' "$listing" | grep -q 'WorkflowIntegration.node'; then
    echo "refusing: component pkg bundled WorkflowIntegration.node" >&2
    exit 1
  fi
}

print_notarize_hint() {
  local product="$1"
  cat <<EOF
notarize (only when Developer ID Application + Installer + notary profile exist):
  # do not run on this machine — no Developer ID
  xcrun notarytool submit "$product" --keychain-profile "perfectvoice-notary" --wait
  xcrun stapler staple "$product"
See installer/macos/README.md
EOF
}

build_pkg() {
  local work="$1"
  local out_pkg="$2"
  local root="$work/root"
  local resources="$work/resources"
  local component_name="PerfectVoice-component.pkg"
  local component="$work/$component_name"

  mkdir -p "$resources"
  cp "$macos/resources/welcome.txt" "$resources/"
  cp "$macos/resources/conclusion.txt" "$resources/"
  cp "$repo/LICENSE" "$resources/LICENSE.txt"
  chmod +x "$macos/scripts/preinstall" "$macos/scripts/postinstall"

  pkgbuild \
    --root "$root" \
    --install-location "/" \
    --identifier "$identifier" \
    --version "$version" \
    --scripts "$macos/scripts" \
    "$component"

  patch_auth_none "$component"
  verify_pkg "$component"
  write_distribution "$work/distribution.xml" "$component_name"

  productbuild \
    --distribution "$work/distribution.xml" \
    --package-path "$work" \
    --resources "$resources" \
    "$out_pkg"

  log "product pkg: $out_pkg"
}

install_user() {
  local root="$1"
  local dest_engine="$HOME/$engine_rel"
  local dest_panel="$HOME/$panel_rel"

  if [ "$(id -u)" -eq 0 ]; then
    echo "refusing: will not install as root (user-space only; no /Library, no root-owned ~/Library)." >&2
    exit 2
  fi

  mkdir -p "$dest_engine" "$dest_panel"
  rsync -a "$root/$engine_rel/" "$dest_engine/"
  rsync -a --exclude 'WorkflowIntegration.node' "$root/$panel_rel/" "$dest_panel/"
  chmod 755 "$dest_engine/perfectvoice-engine"
  if [ -f "$node_src" ]; then
    cp "$node_src" "$dest_panel/WorkflowIntegration.node"
    log "copied WorkflowIntegration.node from Resolve Developer examples"
  else
    log "WARNING: WorkflowIntegration.node not found at $node_src"
    log "Install DaVinci Resolve Studio and copy it from Help > Documentation > Developer."
  fi
  log "installed user-space:"
  log "  $dest_engine/perfectvoice-engine"
  log "  $dest_panel"
  log "Restart DaVinci Resolve Studio → Workspace → Workflow Integrations → PerfectVoice."
}

work="$(mktemp -d "${TMPDIR:-/tmp}/pv-pkg.XXXXXX")"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

log "repo: $repo"
log "version: $version"
log "entitlements (00c): $entitlements"
log "engine dest: ~/$engine_rel/perfectvoice-engine"
log "panel dest:  ~/$panel_rel/"
log "domains: currentUserHome only (enable_localSystem=false)"

stage_payload "$work/root"

if [ "$do_install" -eq 1 ]; then
  if [ "$dry_run" -eq 1 ]; then
    log "dry-run --install would rsync to:"
    log "  $HOME/$engine_rel/"
    log "  $HOME/$panel_rel/"
    log "dry-run OK"
    exit 0
  fi
  install_user "$work/root"
fi

product_name="PerfectVoice-${version}-arm64.pkg"
if [ "$dry_run" -eq 1 ]; then
  build_pkg "$work" "$work/$product_name"
  log "dry-run built and discarded: $work/$product_name"
  print_notarize_hint "installer/macos/dist/$product_name"
  log "dry-run OK"
  exit 0
fi

if [ "$do_install" -eq 1 ]; then
  # Stage + user copy is enough; still emit a pkg when --out-dir is the default
  # only if the caller did not ask for install-only convenience.
  exit 0
fi

mkdir -p "$out_dir"
build_pkg "$work" "$out_dir/$product_name"
print_notarize_hint "$out_dir/$product_name"
log "unsigned product pkg written (sign/notarize only with Developer ID — see README)"
