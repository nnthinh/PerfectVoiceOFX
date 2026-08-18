#!/usr/bin/env bash
# Copy the spike WI panel to the *user* plugin dir (no sudo, does not touch /Library).
# Resolve 21 on this machine also lists plugins from:
#   ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
src="$root/hello-wi-panel"
dst="${HOME}/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/PerfectVoiceHelloSpike"
node_src="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node"

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
echo "installed user-space plugin:"
echo "  $dst"
echo "Restart DaVinci Resolve Studio, then Workspace → Workflow Integrations → PerfectVoice Hello Spike."
