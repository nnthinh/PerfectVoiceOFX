# PerfectVoice

[![CI](https://github.com/nnthinh/PerfectVoiceOFX/actions/workflows/ci.yml/badge.svg)](https://github.com/nnthinh/PerfectVoiceOFX/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-3ee0c5?labelColor=111)](LICENSE)
[![Host](https://img.shields.io/badge/host-DaVinci%20Resolve%20Studio-111?labelColor=111&color=e8e4dc)](https://www.blackmagicdesign.com/products/davinciresolve)
[![macOS arm64](https://img.shields.io/badge/v1.0-macOS%2013%2B%20Apple%20Silicon-111?labelColor=111&color=9aa0ab)](#requirements)

<p align="center">
  <img src="docs/assets/banner.svg" alt="PerfectVoice — strip the band, keep the voice" width="100%">
</p>

**Isolate dialogue from musical accompaniment inside [DaVinci Resolve](https://www.blackmagicdesign.com/products/davinciresolve) Studio.** Select clips on the Edit or Fairlight page, run *Remove musical accompaniment*, and get a new `PV Isolated Voice` track — sample-accurate, non-destructive, no trip through another app.

The repo is named `PerfectVoiceOFX` because that is what the project was called on day one. **The host is not OpenFX.** Resolve’s OFX surface is an image-effect API. There is no official audio buffer. PerfectVoice lives at **Workspace → Workflow Integrations → PerfectVoice**: an Electron panel plus a localhost Python sidecar.

<p align="center"><img src="docs/assets/mark.svg" width="72" height="72" alt="PerfectVoice mark"></p>

## What it is

```
clip on timeline  →  inspect + reject  →  sidecar job
                                          extract (ffmpeg)
                                          resample (soxr)
                                          Demucs vocals  (local weights only)
                                          optional DeepFilterNet 3
                                          wet/dry + BWF
                 ←  place on "PV Isolated Voice"
```

- **Stage 1 (always):** Meta [Demucs](https://github.com/adefossez/demucs) `htdemucs` / `htdemucs_ft`. Music source separation — drums / bass / other / vocals.
- **Stage 2 (off by default):** [DeepFilterNet 3](https://github.com/Rikorose/DeepFilterNet) for leftover environmental noise. If you leave it off, this tool does **not** claim “crystal-clear voice.”
- **Cache** is a full identity hash (48 kHz vs 96 kHz is two keys). Re-run the same clip and you skip infer.
- **Weights are not in the installer.** First use: click *Download model* (~84 MB Fast / ~330 MB Quality). Infer only loads `Separator(..., repo=<local path>)`. Jobs never phone home.

## What it is not

| Not this | Why |
| --- | --- |
| A denoiser | Demucs is trained for *songs*, not HVAC, traffic, or room tone. |
| SOTA 2026 vocal isolation | BS-RoFormer / Mel-RoFormer usually score higher. Demucs is the honest, local, inspectable stack. |
| A replacement for Resolve Voice Isolation | Use Voice Isolation first when the problem is *noise*. Use PerfectVoice when the problem is *a song under the line*. |
| An OpenFX / VST plugin | Offline sidecar. HTDemucs wants seconds of audio, not a 10–43 ms Fairlight buffer. |
| A weight redistributor | Official checkpoints are **not MIT**. See [NOTICE](NOTICE) and [facebookresearch/demucs#327](https://github.com/facebookresearch/demucs/issues/327). |

Full design (Vietnamese): [docs/design.md](docs/design.md).

## Requirements

| | v1.0 |
| --- | --- |
| OS | **macOS 13+ Apple Silicon** |
| Host | **DaVinci Resolve Studio standalone 20.0+** (recommended **21.0.4**). Download from Blackmagic, **not** the Mac App Store. |
| Free Resolve | No Workflow Integration Electron → **not supported** |
| UI | English. Docs in Vietnamese. |
| Windows + NVIDIA CUDA | v1.1 path in [`installer/windows/`](installer/windows/) |
| Intel Mac / Linux | Non-goal |

## Quick start (macOS)

The public tree is a **working dogfood**. The `.pkg` currently stages a spawn-contract **hello-engine** stub. Production infer is the Python sidecar until a PyInstaller onedir lands.

```bash
git clone https://github.com/nnthinh/PerfectVoiceOFX.git
cd PerfectVoiceOFX

# 1. Panel — user-space only, will not touch /Library plugins
bash host/com.perfectvoice.panel/install-user.sh

# 2. Engine stub (spawn + /health). Optional until the onedir exists:
bash installer/macos/install-user.sh

# 3. Weights — you click this. It is not a sublicense.
python3 scripts/download_demucs.py htdemucs

# 4. Restart Resolve Studio → Workspace → Workflow Integrations → PerfectVoice
```

Dump a live selection (Studio must be open, clip selected):

```bash
python3 scripts/spikes/dump_resolve_selection.py --out /tmp/pv-selection-dump.json
```

Dogfood without Resolve:

```bash
python3 scripts/isolate_cli.py /path/to/clip.wav /tmp/pv-out htdemucs
```

## Architecture

```mermaid
flowchart LR
  subgraph Resolve Studio
    TL[Timeline selection]
    WI[Workflow Integration panel]
    TR[PV Isolated Voice]
  end
  subgraph Sidecar["127.0.0.1 sidecar"]
    API["HTTP + token + SSE"]
    PIPE[extract → Demucs → blend]
    CACHE[(identity cache)]
  end
  TL --> WI
  WI -->|POST /v1/jobs| API --> PIPE
  PIPE --> CACHE
  PIPE -->|BWF + handles_*_actual| WI --> TR
```

The sidecar binds **localhost only**. Auth is a token file or stdin — never `--token-fd 3`. Missing weights fail closed.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
bash scripts/ci_forbid_demucs_urls.sh
python3 -m unittest tests.unit.test_schemas tests.unit.test_serve tests.unit.test_weight_fetch \
  tests.unit.test_resample_sync tests.unit.test_blend
python3 -m unittest tests.golden.test_sync tests.golden.test_cache_keys tests.golden.test_appendix_a
node --test host/com.perfectvoice.panel/resolve/*.test.js host/com.perfectvoice.panel/engine.test.js
```

`shared/schema/` is the clip / params / job v1 contract. Clients must not send `wet_dry_sample_rate`. `handles_*_actual` is job-result only.

## Status

Public preview. First open-source release.

| Done | Next |
| --- | --- |
| Panel + sidecar + reject matrix | Live Resolve dump to pin speed / reverse / Elastic Wave keys |
| User-click official weight fetch | PyInstaller onedir (no user Python) |
| macOS user-space installer | Developer ID + notarize |
| Windows installer sketches | CUDA engine SKU |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md). Security: [SECURITY.md](SECURITY.md).

## License

- PerfectVoice source: [MIT](LICENSE), Copyright 2026 PerfectVoice contributors.
- Third-party and **Demucs weight terms**: [NOTICE](NOTICE), [docs/licenses/](docs/licenses/).

---

### Tiếng Việt

Plugin **Workflow Integration** cho DaVinci Resolve **Studio**: chọn clip → *Remove musical accompaniment* → track `PV Isolated Voice` đồng bộ sample. Engine là sidecar Python (Demucs local), **không** phải OpenFX.

- Mạnh với **nhạc nền / beat**. Yếu với ồn phòng, HVAC, đường.
- Không bundle checkpoint. User tự click *Download model*.
- Thiết kế đầy đủ: [docs/design.md](docs/design.md).
