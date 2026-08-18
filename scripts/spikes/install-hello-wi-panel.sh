#!/usr/bin/env bash
# Copy the spike WI panel to the *user* plugin dir (no sudo, does not touch /Library).
# Resolve 21 on this machine also lists plugins from:
#   ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/
# Also places hello-engine at the §3.8 user enginePath so the installed panel can spawn it.
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
src="$root/hello-wi-panel"
dst="${HOME}/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/PerfectVoiceHelloSpike"
node_src="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node"
engine_dst="${HOME}/Library/Application Support/PerfectVoice/engine/perfectvoice-engine"
engine_dir="$(dirname "$engine_dst")"

if [ "${1:-}" = "--system" ]; then
  echo "refusing: will not install into /Library (existing plugins: Audiio, GrokDavinci, SamplePlugin)." >&2
  echo "omit --system to install user-space, or copy by hand if you accept sudo." >&2
  exit 2
fi

mkdir -p "$dst"
rsync -a --delete --exclude 'WorkflowIntegration.node' "$src/" "$dst/"
if [ -f "$node_src" ]; then
  cp "$node_src" "$dst/WorkflowIntegration.node"
  echo "copied WorkflowIntegration.node from Developer examples"
else
  echo "WARNING: WorkflowIntegration.node not found at $node_src" >&2
fi

mkdir -p "$engine_dir"
bin=""
if [ -f "$root/hello-engine" ]; then
  bin="$root/hello-engine"
elif [ -f "$root/hello-engine.c" ]; then
  echo "hello-engine Mach-O missing; building…"
  if bash "$root/build-hello-engine.sh"; then
    bin="$root/hello-engine"
  fi
fi
if [ -n "$bin" ] && [ -f "$bin" ]; then
  cp "$bin" "$engine_dst"
  chmod 755 "$engine_dst"
  echo "installed engine (enginePath §3.8):"
  echo "  $engine_dst"
else
  echo "WARNING: hello-engine Mach-O not found; *Spawn hello-engine* will fail until you:" >&2
  echo "  bash scripts/spikes/build-hello-engine.sh" >&2
  echo "  mkdir -p \"$engine_dir\"" >&2
  echo "  cp scripts/spikes/hello-engine \"$engine_dst\"" >&2
  echo "  # or: export PERFECTVOICE_ENGINE=/absolute/path/to/hello-engine" >&2
fi

echo "installed user-space plugin:"
echo "  $dst"
echo "Restart DaVinci Resolve Studio, then Workspace → Workflow Integrations → PerfectVoice Hello Spike."
