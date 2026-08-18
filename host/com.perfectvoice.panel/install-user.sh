#!/usr/bin/env bash
# Copy the production WI panel to the *user* plugin dir (no sudo, does not touch /Library).
# Resolve 21 on this machine also lists plugins from:
#   ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/
# Copies WorkflowIntegration.node from the user's Developer examples (not in git).
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
dst="${HOME}/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/com.perfectvoice.panel"
node_src="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node"

if [ "${1:-}" = "--system" ]; then
  echo "refusing: will not install into /Library (existing plugins: Audiio, GrokDavinci, SamplePlugin)." >&2
  echo "omit --system to install user-space, or copy by hand if you accept sudo." >&2
  exit 2
fi

mkdir -p "$dst"
rsync -a --delete \
  --exclude 'WorkflowIntegration.node' \
  --exclude '.gitkeep' \
  "$root/" "$dst/"

if [ -f "$node_src" ]; then
  cp "$node_src" "$root/WorkflowIntegration.node"
  cp "$node_src" "$dst/WorkflowIntegration.node"
  echo "copied WorkflowIntegration.node from Developer examples (gitignored; not committed)"
else
  echo "WARNING: WorkflowIntegration.node not found at $node_src" >&2
  echo "Install DaVinci Resolve Studio and copy it from Help > Documentation > Developer." >&2
fi

echo "installed user-space plugin:"
echo "  $dst"
echo "Restart DaVinci Resolve Studio, then Workspace → Workflow Integrations → PerfectVoice."
echo "Engine path (§3.8): set PERFECTVOICE_ENGINE or place the binary at"
echo "  ${HOME}/Library/Application Support/PerfectVoice/engine/perfectvoice-engine"
