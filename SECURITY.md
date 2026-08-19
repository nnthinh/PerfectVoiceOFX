# Security

PerfectVoice runs a localhost sidecar next to DaVinci Resolve and reads source media from paths the editor already has open. Treat that as a privileged helper, not a public service.

## Please report privately

Use [GitHub security advisories](https://github.com/nnthinh/PerfectVoiceOFX/security/advisories/new). Do not open a public issue for a live exploit.

## In scope

- Binding the sidecar anywhere other than `127.0.0.1`
- Auth bypass on the job API (token file / stdin)
- Path escape past `allowed_roots`
- Auto-download of model weights from a job or `Separator()`
- Shipping or executing unexpected binaries from the installer

## Out of scope

- Demucs / DeepFilterNet model quality
- Resolve crashes caused by official Blackmagic APIs
- Unsigned local builds on a machine without Developer ID

## Practice

The engine must fail closed when weights are missing. It must not fetch on infer. CI greps the load path for official Demucs remotes (`scripts/ci_forbid_demucs_urls.sh`).
