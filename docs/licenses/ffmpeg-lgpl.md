# ffmpeg (LGPL) — not bundled in this commit

A later macOS installer may ship a **LGPL** ffmpeg build for decode and
sample-accurate extract (m4a / mov / mxf). This repository does not
contain ffmpeg source or binaries yet.

When binaries are added they must:

- be an LGPL (not GPL) build;
- include the corresponding ffmpeg license texts and source offer as
  required by LGPL;
- stay limited to decode / resample use described in docs/design.md.
