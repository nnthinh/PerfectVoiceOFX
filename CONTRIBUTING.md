# Contributing

Thanks for looking. PerfectVoice is early public software — small, careful patches beat large speculative ones.

## Ground rules

- **English UI.** Panel copy, buttons, and errors stay English.
- **Vietnamese docs** are welcome (`docs/`, comments in design). Do not mix languages in the panel.
- **Do not commit weights.** No `*.th`, `*.bin`, `*.safetensors`, `*.onnx`, or official `htdemucs*` checkpoints.
- **Do not auto-fetch on infer.** Jobs and `Separator(..., repo=local)` must never hit Hugging Face or `dl.fbaipublicfiles.com`. User-click download lives only in `scripts/download_demucs.py` and `weight_fetch.py`.
- **No `/Library` installs.** User-space paths only. Do not clobber other Workflow Integration plugins.

## Dev loop

```bash
python3 -m pip install -r requirements-dev.txt
bash scripts/ci_forbid_demucs_urls.sh
python3 -m unittest tests.unit.test_schemas tests.unit.test_serve tests.unit.test_weight_fetch \
  tests.unit.test_resample_sync tests.unit.test_blend
python3 -m unittest tests.golden.test_sync tests.golden.test_cache_keys tests.golden.test_appendix_a
node --test host/com.perfectvoice.panel/resolve/*.test.js host/com.perfectvoice.panel/engine.test.js
```

Need ffmpeg on `PATH` for extract/resample tests.

## What to open a PR for

Good: tests, reject-matrix facts from a live Resolve dump, installer hardening, Windows CUDA, PyInstaller onedir, copy fixes.

Ask first: new model families, in-process Resolve plugins, bundling official Demucs weights.

## License

Contributions land under the MIT license in [`LICENSE`](LICENSE). Official Demucs **weights** stay under upstream terms — see [`NOTICE`](NOTICE).
