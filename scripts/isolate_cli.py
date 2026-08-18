#!/usr/bin/env python3
"""Dogfood vocal isolation without DaVinci Resolve.

Local weights only. Missing repo → exit 2, no network, no Separator.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _engine_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "engine"


def _ensure_engine_path() -> None:
    engine = str(_engine_dir())
    if engine not in sys.path:
        sys.path.insert(0, engine)


_ensure_engine_path()

from perfectvoice_engine.models import (  # noqa: E402
    DEFAULT_MODEL,
    ModelNotInstalled,
    default_local_repo,
    require_model,
)

EXIT_USAGE = 1
EXIT_MODEL_NOT_INSTALLED = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="isolate_cli",
        description=(
            "Isolate vocals from a WAV without Resolve. "
            "Uses local Demucs weights only; never downloads."
        ),
    )
    parser.add_argument("input", type=Path, help="Input WAV")
    parser.add_argument("output_dir", type=Path, help="Directory for isolated WAV")
    parser.add_argument(
        "model",
        nargs="?",
        default=DEFAULT_MODEL,
        help=f"Demucs model name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Local Demucs weight directory (default: platform models dir)",
    )
    return parser.parse_args(argv)


def _separate_and_write(src: Path, dest_dir: Path, model: str, repo: Path) -> int:
    # I/O + infer stay lazy so the missing-weights path needs neither ffmpeg nor soxr.
    import numpy as np

    from perfectvoice_engine.ffmpeg_io import decode_f32, write_wav
    from perfectvoice_engine.resample import MODEL_SAMPLE_RATE, to_model_rate
    from perfectvoice_engine.separate import SeparateRequest, separate_vocals

    frames, probe = decode_f32(src)
    model_frames = to_model_rate(frames, probe.sample_rate)
    wav_ct = np.ascontiguousarray(model_frames.T, dtype=np.float32)
    result = separate_vocals(
        SeparateRequest(wav_44100_stereo=wav_ct, model=model, device="auto"),
        repo,
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / f"{src.stem}_vocals.wav"
    vocals = np.ascontiguousarray(result.vocals.T, dtype=np.float32)
    write_wav(out_path, vocals, MODEL_SAMPLE_RATE, sample_format="float32")
    duration = float(wav_ct.shape[-1]) / float(MODEL_SAMPLE_RATE)
    wall = float(result.rtf) * duration
    print(
        f"device={result.device_used} model={model} "
        f"duration={duration:.3f}s wall_time={wall:.3f}s rtf={result.rtf:.4f}",
        flush=True,
    )
    print(f"wrote {out_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    src = Path(args.input)
    if not src.is_file():
        print(f"input not found: {src}", file=sys.stderr)
        return EXIT_USAGE
    repo = Path(args.repo) if args.repo is not None else default_local_repo()
    try:
        require_model(str(args.model), repo)
    except ModelNotInstalled as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_MODEL_NOT_INSTALLED
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    return _separate_and_write(src, Path(args.output_dir), str(args.model), repo)


if __name__ == "__main__":
    raise SystemExit(main())
