#!/usr/bin/env python3
"""Import a WAV and AppendToTimeline (mediaType=2).

Spike for PR 00b. Default is a dry run that only prints clipInfo. Pass
``--apply`` to mutate the open project. If Resolve is not running, prints
the planned clipInfo and exits 0.

    python3 scripts/spikes/place_test_wav.py
    python3 scripts/spikes/place_test_wav.py --apply --wav /tmp/pv.wav
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path
from typing import Optional

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
    place_frames,
)


SPIKE_BIN = "PerfectVoice Spike"
SPIKE_TRACK = "PV Isolated Voice"


def write_silence_wav(path: Path, sr: int = 48000, seconds: float = 2.0, channels: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    nframes = int(sr * seconds)
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(b"\x00\x00" * nframes * channels)
    return path


def planned_clip_info(args: argparse.Namespace) -> dict:
    fps = as_fps((args.fps_num, args.fps_den)) if args.fps_num else as_fps(FPS_24)
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
        record_frame=args.record_frame if args.record_frame is not None else 0,
        track_index=args.track_index,
        media_type=2,
    )
    info["startFrame"] = args.start_frame if args.start_frame is not None else info["startFrame"]
    info["endFrame"] = args.end_frame if args.end_frame is not None else info["endFrame"]
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


def apply_place(resolve, wav: Path, args: argparse.Namespace) -> dict:
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        return {"ok": False, "error": "no current project"}
    media_pool = project.GetMediaPool()
    timeline = project.GetCurrentTimeline()
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

    plan = planned_clip_info(args)
    clip_info = dict(plan["clipInfo"])
    clip_info["mediaPoolItem"] = item
    clip_info["trackIndex"] = track_index
    if args.record_frame is None:
        current = timeline.GetCurrentVideoItem()
        clip_info["recordFrame"] = current.GetStart() if current else timeline.GetStartFrame()

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
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path, help="Existing WAV; default writes a 2 s silence file")
    parser.add_argument("--apply", action="store_true", help="Mutate the open Resolve project")
    parser.add_argument("--t0", type=float, default=0.2)
    parser.add_argument("--t1", type=float, default=1.2)
    parser.add_argument("--file-dur", type=float, default=10.0)
    parser.add_argument("--handle", type=float, default=0.5)
    parser.add_argument("--fps-num", type=int, default=24)
    parser.add_argument("--fps-den", type=int, default=1)
    parser.add_argument(
        "--record-frame",
        type=float,
        default=None,
        help="Timeline recordFrame; default 0 (dry-run) or GetStart() of playhead clip (--apply)",
    )
    parser.add_argument("--track-index", type=int, default=0, help="0 = create/reuse PV Isolated Voice")
    parser.add_argument("--start-frame", type=float, default=None)
    parser.add_argument("--end-frame", type=float, default=None)
    args = parser.parse_args()

    wav = args.wav
    if wav is None:
        wav = Path("/tmp/perfectvoice_spike_silence.wav")
        write_silence_wav(wav)
        generated = True
    else:
        generated = False

    plan = planned_clip_info(args)
    report = {
        "wav": str(wav),
        "wav_generated": generated,
        "apply": args.apply,
        "host": host_facts(),
        **plan,
    }

    resolve, note = connect()
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

    result = apply_place(resolve, wav, args)
    report["result"] = result
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
