#!/usr/bin/env python3
"""Import a WAV and AppendToTimeline (mediaType=2).

Spike for PR 00b. Default is a dry run that only prints clipInfo. Pass
``--apply`` to mutate the open project. If Resolve is not running, prints
the planned clipInfo and exits 0.

When connected, ``out_fps`` comes from ``project.GetSetting("timelineFrameRate")``
unless ``--fps-num`` / ``--fps-den`` is set, and ``recordFrame`` comes from
``GetSelectedClips`` (then playhead + linked), not the timeline origin.

    python3 scripts/spikes/place_test_wav.py
    python3 scripts/spikes/place_test_wav.py --apply --wav /tmp/pv.wav
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolve_connect import connect, host_facts, json_safe  # noqa: E402
from shared.perfectvoice_time import (  # noqa: E402
    FPS_24,
    append_clip_info,
    as_fps,
    expected_output_sample_count,
    parse_timeline_frame_rate,
    place_frames,
    round_half_up,
)


SPIKE_BIN = "PerfectVoice Spike"
SPIKE_TRACK = "PV Isolated Voice"
DEFAULT_SR = 48000


def write_silence_wav(
    path: Path,
    *,
    sr: int = DEFAULT_SR,
    nframes: int,
    channels: int = 2,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(b"\x00\x00" * nframes * channels)
    return path


def wav_stats(path: Path) -> dict:
    with wave.open(str(path), "r") as wav:
        sr = wav.getframerate()
        nframes = wav.getnframes()
        channels = wav.getnchannels()
    duration_s = nframes / sr if sr else 0.0
    return {
        "path": str(path),
        "sample_rate": sr,
        "nframes": nframes,
        "channels": channels,
        "duration_s": duration_s,
    }


def planned_clip_info(
    args: argparse.Namespace,
    fps: Fraction,
    record_frame: Any,
) -> dict:
    place = place_frames(
        t0=args.t0,
        t1=args.t1,
        file_dur=args.file_dur,
        out_fps=fps,
        handle_s=args.handle,
    )
    info = append_clip_info(
        media_pool_item="<MediaPoolItem after ImportMedia>",
        place=place,
        record_frame=record_frame,
        track_index=args.track_index,
        media_type=2,
    )
    if args.start_frame is not None:
        info["startFrame"] = args.start_frame
    if args.end_frame is not None:
        info["endFrame"] = args.end_frame
    return {
        "clipInfo": info,
        "place": {
            "handle_start_frame": place.handle_start_frame,
            "handle_end_frame_exclusive": place.handle_end_frame_exclusive,
            "handle_end_frame_inclusive": place.handle_end_frame,
            "out_fps": f"{place.out_fps_num}/{place.out_fps_den}",
        },
        "notes": [
            "startFrame/endFrame are frames on the imported WAV grid, not src_in_sample.",
            "endFrame is inclusive (official 7_add_subclips_to_timeline.py).",
            "recordFrame should be original TimelineItem.GetStart().",
            "mediaType 2 = audio only.",
        ],
    }


def cli_fps_override(args: argparse.Namespace) -> Optional[Fraction]:
    if args.fps_num is None and args.fps_den is None:
        return None
    return as_fps((args.fps_num if args.fps_num is not None else 24, args.fps_den or 1))


def live_timeline_fps(project: Any) -> Tuple[Optional[Fraction], Any]:
    if project is None or not hasattr(project, "GetSetting"):
        return None, None
    try:
        raw = project.GetSetting("timelineFrameRate")
    except Exception as exc:
        return None, f"GetSetting(timelineFrameRate) raised {type(exc).__name__}: {exc}"
    try:
        return parse_timeline_frame_rate(raw), raw
    except ValueError:
        return None, raw


def _track_kind(item: Any) -> Optional[str]:
    getter = getattr(item, "GetTrackTypeAndIndex", None)
    if not callable(getter):
        return None
    try:
        track = getter()
    except Exception:
        return None
    if isinstance(track, (list, tuple)) and track:
        return track[0]
    return None


def pick_record_item(timeline: Any) -> Tuple[Optional[Any], str, List[str]]:
    """Prefer GetSelectedClips, then playhead + linked; GetStartFrame last."""
    warnings: List[str] = []
    if timeline is None:
        return None, "none", ["no current timeline"]

    selected: List[Any] = []
    if callable(getattr(timeline, "GetSelectedClips", None)):
        try:
            raw = timeline.GetSelectedClips() or []
        except Exception as exc:
            warnings.append(f"GetSelectedClips raised {type(exc).__name__}: {exc}")
            raw = []
        if isinstance(raw, list):
            selected = raw
        if selected:
            audio = [it for it in selected if _track_kind(it) == "audio"]
            chosen = (audio or selected)[0]
            return chosen, "GetSelectedClips", warnings
        warnings.append("GetSelectedClips returned empty")
    else:
        warnings.append("timeline.GetSelectedClips is not callable")

    current = None
    if callable(getattr(timeline, "GetCurrentVideoItem", None)):
        try:
            current = timeline.GetCurrentVideoItem()
        except Exception as exc:
            warnings.append(f"GetCurrentVideoItem raised {type(exc).__name__}: {exc}")
    if current:
        pool = [current]
        if callable(getattr(current, "GetLinkedItems", None)):
            try:
                linked = current.GetLinkedItems() or []
            except Exception:
                linked = []
            if isinstance(linked, list):
                pool.extend(linked)
        audio = [it for it in pool if _track_kind(it) == "audio"]
        return (audio or pool)[0], "playhead_GetCurrentVideoItem_plus_GetLinkedItems", warnings

    warnings.append(
        "no selection and no current video item; recordFrame=GetStartFrame() "
        "(timeline origin, often 86400 for 01:00:00:00 — not the clip under test)"
    )
    return None, "GetStartFrame", warnings


def resolve_place_context(resolve: Any, args: argparse.Namespace) -> dict:
    project = None
    timeline = None
    if resolve is not None:
        try:
            project = resolve.GetProjectManager().GetCurrentProject()
            timeline = project.GetCurrentTimeline() if project else None
        except Exception as exc:
            return {
                "fps": as_fps(FPS_24),
                "fps_source": f"default_24/1 (project lookup failed: {exc})",
                "record_frame": args.record_frame if args.record_frame is not None else 0,
                "record_source": "cli" if args.record_frame is not None else "default_0",
                "record_item_name": None,
                "warnings": [str(exc)],
                "project": None,
                "timeline": None,
            }

    override = cli_fps_override(args)
    if override is not None:
        fps = override
        fps_source = f"cli {fps.numerator}/{fps.denominator}"
    else:
        live, raw = live_timeline_fps(project)
        if live is not None:
            fps = live
            fps_source = f"project.GetSetting('timelineFrameRate')={raw!r}"
        else:
            fps = as_fps(FPS_24)
            fps_source = (
                "default_24/1"
                if resolve is None
                else f"default_24/1 (unparsed timelineFrameRate={raw!r})"
            )

    warnings: List[str] = []
    if args.record_frame is not None:
        record_frame = args.record_frame
        record_source = "cli"
        record_item_name = None
    else:
        item, record_source, warnings = pick_record_item(timeline)
        record_item_name = None
        if item is not None and hasattr(item, "GetStart"):
            record_frame = item.GetStart()
            record_item_name = item.GetName() if hasattr(item, "GetName") else None
        elif timeline is not None and hasattr(timeline, "GetStartFrame"):
            record_frame = timeline.GetStartFrame()
            record_source = "GetStartFrame"
        else:
            record_frame = 0
            record_source = "default_0"

    return {
        "fps": fps,
        "fps_source": fps_source,
        "record_frame": record_frame,
        "record_source": record_source,
        "record_item_name": record_item_name,
        "warnings": warnings,
        "project": project,
        "timeline": timeline,
    }


def wav_covers_place(stats: dict, place: dict, fps: Fraction) -> Tuple[bool, str]:
    end_excl = place["handle_end_frame_exclusive"]
    wav_end_excl = round_half_up(stats["duration_s"] * float(fps))
    if end_excl > wav_end_excl:
        return False, (
            f"endFrame inclusive={place['handle_end_frame_inclusive']} "
            f"(exclusive={end_excl}) past WAV grid exclusive={wav_end_excl} "
            f"({stats['duration_s']:.4f}s @ {fps.numerator}/{fps.denominator})"
        )
    return True, (
        f"WAV {stats['nframes']} samples @ {stats['sample_rate']} Hz "
        f"covers exclusive end {end_excl}"
    )


def ensure_stereo_track(timeline, name: str) -> Optional[int]:
    count = timeline.GetTrackCount("audio")
    for idx in range(1, int(count) + 1):
        if timeline.GetTrackName("audio", idx) == name:
            return idx
    ok = timeline.AddTrack("audio", "stereo")
    if not ok:
        return None
    new_idx = int(timeline.GetTrackCount("audio"))
    timeline.SetTrackName("audio", new_idx, name)
    return new_idx


def apply_place(resolve, wav: Path, args: argparse.Namespace, ctx: dict, plan: dict) -> dict:
    project = ctx.get("project")
    timeline = ctx.get("timeline")
    if not project:
        return {"ok": False, "error": "no current project"}
    media_pool = project.GetMediaPool()
    if not media_pool or not timeline:
        return {"ok": False, "error": "need a current timeline and media pool"}

    root = media_pool.GetRootFolder()
    spike_folder = None
    for folder in root.GetSubFolderList() or []:
        if folder.GetName() == SPIKE_BIN:
            spike_folder = folder
            break
    if spike_folder is None:
        spike_folder = media_pool.AddSubFolder(root, SPIKE_BIN)
    if spike_folder:
        media_pool.SetCurrentFolder(spike_folder)

    imported = media_pool.ImportMedia([str(wav)])
    if not imported:
        return {"ok": False, "error": f"ImportMedia failed for {wav}"}
    item = imported[0]

    track_index = args.track_index
    if track_index <= 0:
        created = ensure_stereo_track(timeline, SPIKE_TRACK)
        if created is None:
            return {"ok": False, "error": "AddTrack(audio, stereo) failed"}
        track_index = created

    clip_info = dict(plan["clipInfo"])
    clip_info["mediaPoolItem"] = item
    clip_info["trackIndex"] = track_index
    clip_info["recordFrame"] = ctx["record_frame"]

    placed = media_pool.AppendToTimeline([clip_info])
    return {
        "ok": bool(placed),
        "imported": item.GetName() if hasattr(item, "GetName") else repr(item),
        "track_index": track_index,
        "clipInfo_sent": {
            k: json_safe(v) if k != "mediaPoolItem" else "<MediaPoolItem>"
            for k, v in clip_info.items()
        },
        "placed_count": len(placed) if isinstance(placed, list) else json_safe(placed),
        "place": plan["place"],
        "fps_source": ctx["fps_source"],
        "record_source": ctx["record_source"],
        "record_item_name": ctx["record_item_name"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wav",
        type=Path,
        help="Existing WAV; default writes silence sized to Appendix A N_out @ 48 kHz",
    )
    parser.add_argument("--apply", action="store_true", help="Mutate the open Resolve project")
    parser.add_argument("--t0", type=float, default=0.2)
    parser.add_argument("--t1", type=float, default=1.2)
    parser.add_argument("--file-dur", type=float, default=10.0)
    parser.add_argument("--handle", type=float, default=0.5)
    parser.add_argument(
        "--fps-num",
        type=int,
        default=None,
        help="Override output fps numerator (default: live timelineFrameRate, else 24)",
    )
    parser.add_argument("--fps-den", type=int, default=None, help="Override output fps denominator")
    parser.add_argument(
        "--record-frame",
        type=float,
        default=None,
        help="Timeline recordFrame; default GetSelectedClips().GetStart(), then playhead",
    )
    parser.add_argument("--track-index", type=int, default=0, help="0 = create/reuse PV Isolated Voice")
    parser.add_argument("--start-frame", type=float, default=None)
    parser.add_argument("--end-frame", type=float, default=None)
    args = parser.parse_args()

    resolve, note = connect()
    ctx = resolve_place_context(resolve, args)
    plan = planned_clip_info(args, ctx["fps"], ctx["record_frame"])

    n_out = expected_output_sample_count(
        args.t0, args.t1, args.file_dur, DEFAULT_SR, args.handle
    )
    wav = args.wav
    generated = False
    if wav is None:
        wav = Path("/tmp/perfectvoice_spike_silence.wav")
        write_silence_wav(wav, sr=DEFAULT_SR, nframes=n_out)
        generated = True

    stats = wav_stats(wav)
    covers, cover_note = wav_covers_place(stats, plan["place"], ctx["fps"])

    report = {
        "wav": str(wav),
        "wav_generated": generated,
        "wav_stats": stats,
        "wav_n_out_target": n_out,
        "wav_covers_place": covers,
        "wav_cover_note": cover_note,
        "apply": args.apply,
        "host": host_facts(),
        "fps_source": ctx["fps_source"],
        "record_source": ctx["record_source"],
        "record_item_name": ctx["record_item_name"],
        "warnings": ctx["warnings"],
        **plan,
    }

    if resolve is None:
        report["connected"] = False
        report["reason"] = note
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print(
            "\nResolve is not running — place skipped. Exit 0 (spike / CI-safe).",
            file=sys.stderr,
        )
        return 0

    report["connected"] = True
    report["product"] = json_safe(resolve.GetProductName())
    report["version"] = json_safe(resolve.GetVersionString())
    if not args.apply:
        report["note"] = "connected; pass --apply to ImportMedia + AppendToTimeline"
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    if not covers:
        report["result"] = {"ok": False, "error": cover_note}
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nRefusing --apply: {cover_note}", file=sys.stderr)
        return 1

    result = apply_place(resolve, wav, args, ctx, plan)
    report["result"] = result
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
