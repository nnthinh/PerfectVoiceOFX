"""Job pipeline: extract → resample → separate → [dfn] → blend → cache → WAV.

Jobs never fetch weights. Missing local repo → ``Model not installed``.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from perfectvoice_engine import __version__ as ENGINE_VERSION
from perfectvoice_engine.blend import blend_to_wav
from perfectvoice_engine.cache import (
    CacheIndex,
    clip_hash12,
    compute_input_hash,
    default_cache_dir,
    default_cache_index_path,
    file_id_from_path,
)
from perfectvoice_engine.constants import raise_if_cancelled
from perfectvoice_engine.enhance import (
    ENHANCER_ID as DFN_ENHANCER_ID,
    EnhancerNotInstalled,
    is_enhancer_installed,
)
from perfectvoice_engine.ffmpeg_io import decode_f32, extract_with_handles
from perfectvoice_engine.models import (
    VOCALS_ONLY_SIG,
    default_local_repo,
    files_for,
    require_model,
    weights_sha256,
)
from perfectvoice_engine.resample import MODEL_SAMPLE_RATE, to_model_rate
from perfectvoice_engine.separate import SeparateRequest, separate_vocals

VOICE_WAV = "voice.wav"
META_JSON = "meta.json"
_UNSAFE_NAME = re.compile(r"[^\w.\- ]+", re.UNICODE)


def dest_wav_path(output_dir: str | Path, clip: Mapping[str, Any]) -> Path:
    """WAV in the job output dir, named from the source clip stem.

    ``…/SHOW/Source/C8629.MP4`` + output ``…/SHOW/PerfectVoice`` → ``C8629.wav``.
    """
    raw = Path(str(clip.get("source_path") or clip.get("display_name") or "voice")).stem
    stem = _UNSAFE_NAME.sub("_", raw).strip(" ._") or "voice"
    return Path(output_dir) / f"{stem}.wav"


ProgressFn = Callable[[dict[str, Any]], None]


def job_model_name(params: Mapping[str, Any]) -> str:
    if params.get("vocals_only_bag"):
        return VOCALS_ONLY_SIG
    return str(params["model"])


def _require_enhancer(params: Mapping[str, Any]) -> None:
    """Fail closed before infer if DFN was requested but is not installed."""
    if str(params.get("enhancer")) != DFN_ENHANCER_ID:
        return
    if not is_enhancer_installed():
        raise EnhancerNotInstalled()


def clip_input_hash(
    clip: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    file_id: Sequence[int],
    weights_digest: str,
    engine_semver: str = ENGINE_VERSION,
) -> str:
    return compute_input_hash(
        file_id=file_id,
        src_in=int(clip["source_in_sample"]),
        src_out=int(clip["source_out_sample"]),
        audio_stream_index=int(clip["audio_stream_index"]),
        channel_map=tuple(int(c) for c in clip["channel_map"]),
        model_name=str(params["model"]),
        weights_sha256=weights_digest,
        vocals_only_bag=bool(params["vocals_only_bag"]),
        wet=float(params["wet"]),
        gain=float(params["output_gain_db"]),
        mono=bool(params["mono"]),
        handles_requested=float(clip["handles_seconds"]),
        file_duration_seconds=float(clip["file_duration_seconds"]),
        segment=float(params["segment"]),
        overlap=float(params["overlap"]),
        shifts=int(params["shifts"]),
        enhancer_id=str(params["enhancer"]),
        project_sample_rate=int(clip["project_sample_rate"]),
        sample_format=str(params["sample_format"]),
        resampler_id=str(params["resampler_id"]),
        clip_policy=str(params["clip_policy"]),
        engine_semver=engine_semver,
    )


def clip_result(
    *,
    clip_id: str,
    input_hash: str,
    output_path: str,
    output_samples: int,
    handles_left_actual: float,
    handles_right_actual: float,
    wet_dry_sample_rate: int,
    peak: float,
    cache_hit: bool,
) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "input_hash": input_hash,
        "output_path": output_path,
        "output_samples": int(output_samples),
        "handles_left_actual": float(handles_left_actual),
        "handles_right_actual": float(handles_right_actual),
        "wet_dry_sample_rate": int(wet_dry_sample_rate),
        "peak": float(peak),
        "cache_hit": bool(cache_hit),
    }


def _apply_channel_map(frames: np.ndarray, channel_map: Sequence[int]) -> np.ndarray:
    idxs = [int(i) for i in channel_map]
    if not idxs:
        raise ValueError("channel_map must not be empty")
    if any(i < 0 or i >= frames.shape[1] for i in idxs):
        raise ValueError(
            f"channel_map {idxs} out of range for {frames.shape[1]} channels"
        )
    return np.ascontiguousarray(frames[:, idxs], dtype=np.float32)


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    required = (
        "input_hash",
        "handles_left_actual",
        "handles_right_actual",
        "wet_dry_sample_rate",
        "output_samples",
        "peak",
    )
    if any(key not in data for key in required):
        return None
    return data


def _write_meta(path: Path, result: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "input_hash": result["input_hash"],
                "handles_left_actual": result["handles_left_actual"],
                "handles_right_actual": result["handles_right_actual"],
                "wet_dry_sample_rate": result["wet_dry_sample_rate"],
                "output_samples": result["output_samples"],
                "peak": result["peak"],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _copy_wav(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)


def _cache_lookup(
    index: CacheIndex | None,
    input_hash: str,
    dest: Path,
) -> dict[str, Any] | None:
    if index is None:
        return None
    entry = index.get(input_hash)
    if entry is None:
        return None
    cached = Path(entry.path)
    meta = _read_meta(cached.with_name(META_JSON))
    if meta is None or not cached.is_file():
        return None
    if str(meta.get("input_hash")) != input_hash:
        return None
    _copy_wav(cached, dest)
    return meta


def _cache_store(
    index: CacheIndex | None,
    input_hash: str,
    dest: Path,
    result: Mapping[str, Any],
) -> None:
    if index is None:
        return
    # Full hash so a 12-hex dest prefix collision cannot clobber another identity.
    cache_wav = default_cache_dir() / input_hash / VOICE_WAV
    _copy_wav(dest, cache_wav)
    _write_meta(cache_wav.with_name(META_JSON), result)
    index.put(input_hash, cache_wav)


def process_clip(
    clip: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    output_dir: str | Path,
    cancel_event: object | None = None,
    cache_index: CacheIndex | None = None,
    local_repo: Path | None = None,
    weights_digest: str | None = None,
    model_checked: bool = False,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Run one clip. Cache hit skips infer. Never opens a weight-download socket."""
    raise_if_cancelled(cancel_event)
    repo = Path(local_repo) if local_repo is not None else default_local_repo()
    name = job_model_name(params)
    digest = (
        weights_digest
        if weights_digest is not None
        else weights_sha256(files_for(name))
    )
    source = Path(str(clip["source_path"]))
    file_id = file_id_from_path(source)
    input_hash = clip_input_hash(clip, params, file_id=file_id, weights_digest=digest)
    dest = dest_wav_path(output_dir, clip)
    dest.parent.mkdir(parents=True, exist_ok=True)
    clip_id = str(clip["clip_id"])
    if on_progress is not None:
        on_progress(
            {"clip_id": clip_id, "segment_offset": 0, "audio_length": 0}
        )

    use_cache = bool(params.get("use_cache", True))
    index = cache_index if use_cache else None
    cached = _cache_lookup(index, input_hash, dest)
    if cached is not None:
        return clip_result(
            clip_id=clip_id,
            input_hash=input_hash,
            output_path=str(dest.resolve()),
            output_samples=int(cached["output_samples"]),
            handles_left_actual=float(cached["handles_left_actual"]),
            handles_right_actual=float(cached["handles_right_actual"]),
            wet_dry_sample_rate=int(cached["wet_dry_sample_rate"]),
            peak=float(cached["peak"]),
            cache_hit=True,
        )

    if not model_checked:
        require_model(name, repo)
    _require_enhancer(params)

    raise_if_cancelled(cancel_event)
    src_sr = int(clip["source_sample_rate"])
    # clip.v1 source_in/out are content t0/t1 in samples — not the extract
    # window. Engine applies Appendix A once; the panel must not pre-add H.
    t0 = float(clip["source_in_sample"]) / float(src_sr)
    t1 = float(clip["source_out_sample"]) / float(src_sr)

    def _fwd_progress(info: Mapping[str, Any]) -> None:
        if on_progress is None:
            return
        payload: dict[str, Any] = {
            "clip_id": clip_id,
            "clip_name": clip.get("name") or clip.get("display_name") or "clip",
            **dict(info),
        }
        on_progress(payload)

    tmp = Path(tempfile.mkdtemp(prefix="pv-clip-"))
    try:
        extract_path = tmp / "extract.wav"
        extracted = extract_with_handles(
            source,
            extract_path,
            t0=t0,
            t1=t1,
            handle_s=float(clip["handles_seconds"]),
            sample_format="float32",
            stream_index=int(clip["audio_stream_index"]),
            file_dur=float(clip["file_duration_seconds"]),
            source_sample_rate=src_sr,
            cancel_event=cancel_event,
        )
        raise_if_cancelled(cancel_event)
        frames, _probe = decode_f32(extract_path, cancel_event=cancel_event)
        frames = _apply_channel_map(frames, clip["channel_map"])
        model_frames = to_model_rate(frames, extracted.sample_rate)
        wav_ct = np.ascontiguousarray(model_frames.T, dtype=np.float32)
        raise_if_cancelled(cancel_event)
        mode = str(params.get("mode") or "music")
        speaker_id = str(params.get("speaker_id") or "")
        if mode == "tse" and speaker_id:
            from perfectvoice_engine.tse import SpeakerStore, extract_target_speaker
            store = SpeakerStore()
            profile = store.get(speaker_id)
            if profile is not None:
                isolated_arr = extract_target_speaker(
                    wav_ct,
                    profile.embedding,
                    sample_rate=MODEL_SAMPLE_RATE,
                    cancel_event=cancel_event,
                    on_progress=_fwd_progress if on_progress is not None else None,
                )
                vocals = np.ascontiguousarray(isolated_arr.T, dtype=np.float32)
            else:
                separated = separate_vocals(
                    SeparateRequest(
                        wav_44100_stereo=wav_ct,
                        model=str(params["model"]),
                        device=str(params["device"]),
                        segment=float(params["segment"]),
                        overlap=float(params["overlap"]),
                        shifts=int(params["shifts"]),
                        vocals_only_bag=bool(params["vocals_only_bag"]),
                        cancel_event=cancel_event,
                        on_progress=_fwd_progress if on_progress is not None else None,
                    ),
                    repo,
                )
                vocals = np.ascontiguousarray(separated.vocals.T, dtype=np.float32)
        else:
            separated = separate_vocals(
                SeparateRequest(
                    wav_44100_stereo=wav_ct,
                    model=str(params["model"]),
                    device=str(params["device"]),
                    segment=float(params["segment"]),
                    overlap=float(params["overlap"]),
                    shifts=int(params["shifts"]),
                    vocals_only_bag=bool(params["vocals_only_bag"]),
                    cancel_event=cancel_event,
                    on_progress=_fwd_progress if on_progress is not None else None,
                ),
                repo,
            )
            vocals = np.ascontiguousarray(separated.vocals.T, dtype=np.float32)
        raise_if_cancelled(cancel_event)
        blended = blend_to_wav(
            dest,
            model_frames,
            vocals,
            in_sample_rate=MODEL_SAMPLE_RATE,
            enhancer=str(params["enhancer"]),
            project_sample_rate=int(clip["project_sample_rate"]),
            wet=float(params["wet"]),
            gain_db=float(params["output_gain_db"]),
            mono=bool(params["mono"]),
            sample_format=str(params["sample_format"]),
            cancel_event=cancel_event,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    result = clip_result(
        clip_id=clip_id,
        input_hash=input_hash,
        output_path=str(dest.resolve()),
        output_samples=int(blended.sample_count),
        handles_left_actual=float(extracted.extract.h_left_actual),
        handles_right_actual=float(extracted.extract.h_right_actual),
        wet_dry_sample_rate=int(blended.wet_dry_sample_rate),
        peak=float(blended.peak),
        cache_hit=False,
    )
    _cache_store(index, input_hash, dest, result)
    return result


def run_job(
    clips: Sequence[Mapping[str, Any]],
    params: Mapping[str, Any],
    output_dir: str | Path,
    *,
    cancel_event: object | None = None,
    on_progress: ProgressFn | None = None,
    local_repo: Path | None = None,
    cache_index: CacheIndex | None = None,
) -> list[dict[str, Any]]:
    """Process clips sequentially. Cache hits skip infer; miss requires local weights."""
    raise_if_cancelled(cancel_event)
    repo = Path(local_repo) if local_repo is not None else default_local_repo()
    name = job_model_name(params)
    digest = weights_sha256(files_for(name))
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    own_index = cache_index is None and bool(params.get("use_cache", True))
    index = cache_index
    if own_index:
        index = CacheIndex(default_cache_index_path())
    model_checked = False
    results: list[dict[str, Any]] = []
    try:
        for clip in clips:
            raise_if_cancelled(cancel_event)
            result = process_clip(
                clip,
                params,
                output_dir=out_dir,
                cancel_event=cancel_event,
                cache_index=index,
                local_repo=repo,
                weights_digest=digest,
                model_checked=model_checked,
                on_progress=on_progress,
            )
            if not result["cache_hit"]:
                model_checked = True
            results.append(result)
    finally:
        if own_index and index is not None:
            index.close()
    return results


__all__ = [
    "clip_input_hash",
    "clip_result",
    "dest_wav_path",
    "job_model_name",
    "process_clip",
    "run_job",
]
