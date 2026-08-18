# Benchmarks

**Fill after measuring.** Do not invent RTF, wall time, or device numbers.

This file is a template only. The panel must not read RTF from here until a
real measured row exists for that device. Until then UI ETA stays
“Calibrating…” or the conservative CPU bound in `docs/design.md` §3.5.

## How to measure

Use a fixed 60 s stereo fixture on the dogfood machine. Run:

```
python3 scripts/isolate_cli.py /path/to/fixture_60s.wav /tmp/pv-bench htdemucs
```

Optional `--repo` points at the local Demucs directory. The CLI never
downloads weights; missing models exit 2 with `Model not installed`.

Copy the printed `device`, `model`, `duration`, `wall_time`, and `rtf`
into the table. `RTF = wall_time / duration`. Leave cells empty until
that run exists. Do not copy community estimates from the design doc.

## Measured results

| device | model | duration | wall time | RTF |
| --- | --- | --- | --- | --- |
| | | | | |
