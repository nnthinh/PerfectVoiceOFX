# Demucs pretrained weights — not MIT

Official Demucs checkpoints (`htdemucs`, `htdemucs_ft`, and related bags)
are **not** licensed under MIT.

## Primary statement

[facebookresearch/demucs#327](https://github.com/facebookresearch/demucs/issues/327)
(Alexandre Défossez, 2022-05-23):

> The model weights are not covered by the MIT license, and are provided
> only for scientific purposes.

The facebookresearch/demucs repository was archived on 2025-01-01. The
maintained fork is [adefossez/demucs](https://github.com/adefossez/demucs).
Neither tree publishes a later grant that puts official weights under MIT
or another commercial license.

Training data cited by the authors includes MUSDB HQ (research-oriented
terms) and approximately 800 internal Meta tracks. Third-party mirrors
that stamp the same files MIT or CC-BY-NC do not relicense them.

## PerfectVoice policy (2026-08-18)

Recorded in the root [NOTICE](../../NOTICE) and [docs/design.md](../design.md):

| Stage | Behavior |
| --- | --- |
| Development | May download official weights into a local directory (checksum). |
| Shipped product | Does **not** bundle official weights. |
| End user | Clicks **Download model**. Click ≠ sublicense. |
| Inference | Local `Separator(..., repo=Path)` only. No silent Hub/AWS fetch. |

Official download hosts (allowlisted only in fetch scripts and docs, never
in engine load-path or the panel):

- `https://huggingface.co/adefossez/HTDemucs`
- `https://huggingface.co/adefossez/HTDemucs-ft`
- fallback `https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/`

`engine/models/manifest.json` (later PR) maps `name → filename → sha256`
only — no URLs.

## Residual risk

A user click does not change #327. PerfectVoice documents that residual
risk; it does not claim a sublicense.
