#!/usr/bin/env python3
"""Dump Resolve version, GetSetting keys, and selected-clip properties.

Spike script for PR 00b. If Resolve is not running, prints README-derived
facts and exits 0 so CI / unattended runs do not fail.

    python3 scripts/spikes/dump_resolve_selection.py
    python3 scripts/spikes/dump_resolve_selection.py --out /tmp/pv-dump.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolve_connect import connect, host_facts, json_safe  # noqa: E402

# Keys cited by official BMD docs / examples — not guessed.
README_PROJECT_SETTING_KEYS = (
    "superScale",
    "timelineFrameRate",
    "timelineSampleRate",
)
EXAMPLE_PROJECT_SETTING_KEYS = (
    "timelineResolutionWidth",
    "timelineResolutionHeight",
)
README_CLIP_PROPERTY_KEYS = (
    "Super Scale",
    "Cloud Sync",
)
EXAMPLE_CLIP_PROPERTY_KEYS = (
    "File Path",
    "File Name",
    "Frames",
    "Video Codec",
    "Date Added",
)
# Design / README prose claim this exact string; live dump must confirm.
DESIGN_CLAIMED_CLIP_KEYS = ("Sample Rate",)
README_TIMELINEITEM_GETPROPERTY_KEYS = (
    "Pan",
    "Tilt",
    "ZoomX",
    "ZoomY",
    "ZoomGang",
    "RotationAngle",
    "AnchorPointX",
    "AnchorPointY",
    "Pitch",
    "Yaw",
    "FlipX",
    "FlipY",
    "CropLeft",
    "CropRight",
    "CropTop",
    "CropBottom",
    "CropSoftness",
    "CropRetain",
    "DynamicZoomEase",
    "CompositeMode",
    "Opacity",
    "Distortion",
    "RetimeProcess",
    "MotionEstimation",
    "Scaling",
    "ResizeFilter",
)
APPEND_CLIPINFO_KEYS = (
    "mediaPoolItem",
    "startFrame",
    "endFrame",
    "mediaType",
    "trackIndex",
    "recordFrame",
)


def _call(fn: Callable, *args, default=None):
    try:
        return fn(*args)
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}", "_default": default}


def parse_version_fields(fields: Any) -> Optional[Tuple[int, int, int]]:
    if not fields:
        return None
    seq = list(fields) if not isinstance(fields, dict) else list(fields.values())
    if len(seq) < 3:
        return None
    try:
        return int(seq[0]), int(seq[1]), int(seq[2])
    except (TypeError, ValueError):
        return None


def selection_strategy(version: Optional[Tuple[int, int, int]]) -> str:
    if version is None:
        return "unknown"
    if version >= (21, 0, 4):
        return "get_selected_clips_then_dedupe"
    if version >= (20, 0, 0):
        return "playhead_plus_linked"
    return "unsupported"


def has_method(obj: Any, name: str) -> bool:
    return callable(getattr(obj, name, None))


def dump_settings(obj: Any, known_keys: Sequence[str]) -> dict:
    snapshot = _call(obj.GetSetting)
    keyed = {}
    for key in known_keys:
        keyed[key] = _call(obj.GetSetting, key)
    return {"snapshot": json_safe(snapshot), "known_keys": json_safe(keyed)}


def dump_mediapool_item(item: Any) -> dict:
    if item is None:
        return {"_error": "no MediaPoolItem"}
    props = _call(item.GetClipProperty)
    audio_map = _call(item.GetAudioMapping) if has_method(item, "GetAudioMapping") else None
    return {
        "name": _call(item.GetName),
        "unique_id": _call(item.GetUniqueId) if has_method(item, "GetUniqueId") else None,
        "media_id": _call(item.GetMediaId) if has_method(item, "GetMediaId") else None,
        "clip_property_snapshot": json_safe(props),
        "audio_mapping": json_safe(audio_map),
        "probed_keys": {
            key: json_safe(_call(item.GetClipProperty, key))
            for key in (*EXAMPLE_CLIP_PROPERTY_KEYS, *README_CLIP_PROPERTY_KEYS, *DESIGN_CLAIMED_CLIP_KEYS)
        },
    }


def dump_timeline_item(item: Any) -> dict:
    track = _call(item.GetTrackTypeAndIndex) if has_method(item, "GetTrackTypeAndIndex") else None
    linked = []
    if has_method(item, "GetLinkedItems"):
        raw = _call(item.GetLinkedItems, default=[])
        if isinstance(raw, list):
            for sib in raw:
                linked.append(
                    {
                        "name": _call(sib.GetName),
                        "unique_id": _call(sib.GetUniqueId) if has_method(sib, "GetUniqueId") else None,
                        "track": json_safe(
                            _call(sib.GetTrackTypeAndIndex)
                            if has_method(sib, "GetTrackTypeAndIndex")
                            else None
                        ),
                    }
                )
        else:
            linked = json_safe(raw)
    mp = _call(item.GetMediaPoolItem) if has_method(item, "GetMediaPoolItem") else None
    mapping = (
        _call(item.GetSourceAudioChannelMapping)
        if has_method(item, "GetSourceAudioChannelMapping")
        else None
    )
    voice = (
        _call(item.GetVoiceIsolationState)
        if has_method(item, "GetVoiceIsolationState")
        else None
    )
    props = _call(item.GetProperty) if has_method(item, "GetProperty") else None
    return {
        "name": _call(item.GetName),
        "unique_id": _call(item.GetUniqueId) if has_method(item, "GetUniqueId") else None,
        "track": json_safe(track),
        "start": _call(item.GetStart),
        "start_subframe": _call(item.GetStart, True),
        "end": _call(item.GetEnd) if has_method(item, "GetEnd") else None,
        "duration": _call(item.GetDuration) if has_method(item, "GetDuration") else None,
        "source_start_time": _call(item.GetSourceStartTime),
        "source_end_time": _call(item.GetSourceEndTime),
        "source_start_frame": _call(item.GetSourceStartFrame),
        "source_end_frame": _call(item.GetSourceEndFrame),
        "left_offset": _call(item.GetLeftOffset) if has_method(item, "GetLeftOffset") else None,
        "right_offset": _call(item.GetRightOffset) if has_method(item, "GetRightOffset") else None,
        "clip_enabled": _call(item.GetClipEnabled) if has_method(item, "GetClipEnabled") else None,
        "fusion_comp_count": _call(item.GetFusionCompCount)
        if has_method(item, "GetFusionCompCount")
        else None,
        "voice_isolation": json_safe(voice),
        "source_audio_channel_mapping": json_safe(mapping),
        "property_snapshot": json_safe(props),
        "linked_items": linked,
        "media_pool_item": dump_mediapool_item(mp) if mp and not isinstance(mp, dict) else json_safe(mp),
    }


def item_key(item: Any) -> str:
    if has_method(item, "GetUniqueId"):
        uid = _call(item.GetUniqueId)
        if isinstance(uid, str) and uid:
            return uid
    return repr(item)


def _member_file_path(item: Any) -> Any:
    if not has_method(item, "GetMediaPoolItem"):
        return None
    mp = _call(item.GetMediaPoolItem)
    if not mp or isinstance(mp, dict) or not has_method(mp, "GetClipProperty"):
        return None
    return _call(mp.GetClipProperty, "File Path")


def _member_row(item: Any) -> dict:
    track = (
        _call(item.GetTrackTypeAndIndex)
        if has_method(item, "GetTrackTypeAndIndex")
        else None
    )
    kind = track[0] if isinstance(track, (list, tuple)) and track else None
    index = track[1] if isinstance(track, (list, tuple)) and len(track) > 1 else None
    return {
        "name": _call(item.GetName),
        "unique_id": _call(item.GetUniqueId) if has_method(item, "GetUniqueId") else None,
        "track_type": kind,
        "track_index": index,
        "file_path": _member_file_path(item),
    }


def dedupe_linked_group(members: Sequence[dict]) -> List[dict]:
    """One row per distinct audio File Path (design §3.3 rule 4).

    Same path twice stays in the dump with ``suppressed_duplicate=True``.
    Distinct audio paths (dual-system + mixdown) are both unsuppressed.
    Video is used only when the group has no audio sibling.
    """
    audio = [m for m in members if m.get("track_type") == "audio"]
    video = [m for m in members if m.get("track_type") == "video"]
    other = [m for m in members if m.get("track_type") not in ("audio", "video")]
    group_size = len(members)

    def row(member: dict, *, preferred_audio: bool, suppressed: bool, duplicate_of: Any) -> dict:
        path = member.get("file_path")
        if isinstance(path, dict) and "_error" in path:
            path = None
        return {
            "chosen_name": member.get("name"),
            "chosen_track": [member.get("track_type"), member.get("track_index")],
            "file_path": path if path not in ("", None) else None,
            "preferred_audio_sibling": preferred_audio,
            "group_size": group_size,
            "suppressed_duplicate": suppressed,
            "duplicate_of": duplicate_of,
        }

    if audio:
        seen_paths: dict[str, Any] = {}
        rows: List[dict] = []
        for member in audio:
            path = member.get("file_path")
            if isinstance(path, dict) and "_error" in path:
                path = None
            path_key = path.strip() if isinstance(path, str) else ""
            first_id = seen_paths.get(path_key) if path_key else None
            suppressed = bool(path_key and first_id is not None)
            rows.append(
                row(
                    member,
                    preferred_audio=True,
                    suppressed=suppressed,
                    duplicate_of=first_id if suppressed else None,
                )
            )
            if path_key and not suppressed:
                seen_paths[path_key] = member.get("unique_id") or member.get("name")
        return rows

    chosen = (video or other or list(members) or [{}])[0]
    return [row(chosen, preferred_audio=False, suppressed=False, duplicate_of=None)]


def group_and_dedupe(items: Sequence[Any]) -> List[dict]:
    """Prefer audio File Path(s) inside each GetLinkedItems group."""
    seen_groups = set()
    out: List[dict] = []
    for item in items:
        members = [item]
        if has_method(item, "GetLinkedItems"):
            linked = _call(item.GetLinkedItems, default=[])
            if isinstance(linked, list):
                members.extend(linked)
        group_ids = tuple(sorted({item_key(m) for m in members}))
        if group_ids in seen_groups:
            continue
        seen_groups.add(group_ids)
        out.extend(dedupe_linked_group([_member_row(m) for m in members]))
    return out


def collect_playhead_fallback(timeline: Any) -> List[Any]:
    items: List[Any] = []
    current = _call(timeline.GetCurrentVideoItem) if has_method(timeline, "GetCurrentVideoItem") else None
    if current and not isinstance(current, dict):
        items.append(current)
        if has_method(current, "GetLinkedItems"):
            linked = _call(current.GetLinkedItems, default=[])
            if isinstance(linked, list):
                items.extend(linked)
    return items


def dump_live(resolve: Any) -> dict:
    version_fields = _call(resolve.GetVersion)
    version = parse_version_fields(version_fields)
    project = _call(resolve.GetProjectManager().GetCurrentProject)
    if not project or isinstance(project, dict):
        return {
            "connected": True,
            "product": json_safe(_call(resolve.GetProductName)),
            "version_string": json_safe(_call(resolve.GetVersionString)),
            "version_fields": json_safe(version_fields),
            "error": "no current project",
        }

    timeline = _call(project.GetCurrentTimeline)
    media_pool = _call(project.GetMediaPool)
    page = _call(resolve.GetCurrentPage) if has_method(resolve, "GetCurrentPage") else None

    selected = None
    has_tl_get_selected = False
    if timeline and not isinstance(timeline, dict):
        has_tl_get_selected = has_method(timeline, "GetSelectedClips")
        if has_tl_get_selected:
            selected = _call(timeline.GetSelectedClips, default=[])

    selected_list = selected if isinstance(selected, list) else []
    fallback_list = (
        collect_playhead_fallback(timeline)
        if timeline and not isinstance(timeline, dict)
        else []
    )

    timeline_dump = None
    if timeline and not isinstance(timeline, dict):
        audio_n = _call(timeline.GetTrackCount, "audio")
        video_n = _call(timeline.GetTrackCount, "video")
        voice_tracks = {}
        if has_method(timeline, "GetVoiceIsolationState") and isinstance(audio_n, int):
            for idx in range(1, audio_n + 1):
                voice_tracks[str(idx)] = json_safe(_call(timeline.GetVoiceIsolationState, idx))
        timeline_dump = {
            "name": _call(timeline.GetName),
            "start_frame": _call(timeline.GetStartFrame),
            "end_frame": _call(timeline.GetEndFrame),
            "start_timecode": _call(timeline.GetStartTimecode),
            "current_timecode": _call(timeline.GetCurrentTimecode)
            if has_method(timeline, "GetCurrentTimecode")
            else None,
            "audio_tracks": audio_n,
            "video_tracks": video_n,
            "has_GetSelectedClips": has_tl_get_selected,
            "settings": dump_settings(timeline, README_PROJECT_SETTING_KEYS),
            "voice_isolation_by_track": voice_tracks,
        }

    return {
        "connected": True,
        "product": json_safe(_call(resolve.GetProductName)),
        "version_string": json_safe(_call(resolve.GetVersionString)),
        "version_fields": json_safe(version_fields),
        "page": json_safe(page),
        "selection_strategy": selection_strategy(version),
        "has_timeline_GetSelectedClips": has_tl_get_selected,
        "project": {
            "name": _call(project.GetName),
            "settings": dump_settings(
                project, README_PROJECT_SETTING_KEYS + EXAMPLE_PROJECT_SETTING_KEYS
            ),
        },
        "timeline": timeline_dump,
        "selected_clips": [dump_timeline_item(it) for it in selected_list],
        "playhead_fallback": [dump_timeline_item(it) for it in fallback_list],
        "deduped_from_selected": group_and_dedupe(selected_list),
        "deduped_from_playhead": group_and_dedupe(fallback_list),
        "media_pool_has_GetSelectedClips": bool(
            media_pool
            and not isinstance(media_pool, dict)
            and has_method(media_pool, "GetSelectedClips")
        ),
    }


def readme_offline_dump() -> dict:
    """Property names found in the installed Developer Scripting README / examples."""
    return {
        "readme_last_updated": "24 Jul 2026",
        "changelog_last_updated": "5 May 2026 (through 21.0 Beta; no GetSelectedClips entry)",
        "project_GetSetting_documented_keys": list(README_PROJECT_SETTING_KEYS),
        "project_GetSetting_example_keys": list(EXAMPLE_PROJECT_SETTING_KEYS),
        "note_GetSetting": (
            "README: call Project.GetSetting() / Timeline.GetSetting() with no "
            "args (or None / blank) to snapshot every queryable key. Do not guess "
            "undocumented names. timelineSampleRate is documented as Fairlight "
            "'Audio sample rate'."
        ),
        "mediaPoolItem_GetClipProperty_documented_keys": list(README_CLIP_PROPERTY_KEYS),
        "mediaPoolItem_GetClipProperty_example_keys": list(EXAMPLE_CLIP_PROPERTY_KEYS),
        "mediaPoolItem_GetClipProperty_design_claimed": list(DESIGN_CLAIMED_CLIP_KEYS),
        "note_GetClipProperty": (
            "README: no-arg GetClipProperty() returns the full dict. Official "
            "examples use 'File Path', 'File Name', 'Frames', 'Video Codec', "
            "'Date Added'. README names sample rate as an intrinsic property "
            "but does not spell the key; design uses 'Sample Rate' — confirm live."
        ),
        "timelineItem_methods": [
            "GetStart([subframe_precision])",
            "GetEnd([subframe_precision])",
            "GetDuration([subframe_precision])",
            "GetSourceStartTime()",
            "GetSourceEndTime()",
            "GetSourceStartFrame()",
            "GetSourceEndFrame()",
            "GetLeftOffset / GetRightOffset",
            "GetLinkedItems()",
            "GetTrackTypeAndIndex() -> [trackType, trackIndex]",
            "GetSourceAudioChannelMapping() -> JSON string",
            "GetVoiceIsolationState() -> {isEnabled, amount}",
            "GetProperty() / GetProperty(key)",
            "GetMediaPoolItem()",
            "GetFusionCompCount()",
        ],
        "timelineItem_GetProperty_keys": list(README_TIMELINEITEM_GETPROPERTY_KEYS),
        "note_speed_reverse_elastic": (
            "GetProperty documents RetimeProcess (interpolation mode) and FlipX/Y "
            "(video). It does not name clip speed %, audio reverse, or Elastic Wave. "
            "Detect those from a live property snapshot on a retimed / reversed / EW clip."
        ),
        "timeline_GetSelectedClips": (
            "README (24 Jul 2026): Timeline.GetSelectedClips() -> [items...] "
            "'Returns the currently selected timeline items.' Also "
            "MediaPool.GetSelectedClips() -> MediaPoolItems (pool selection, not timeline)."
        ),
        "append_to_timeline_clipInfo_keys": list(APPEND_CLIPINFO_KEYS),
        "append_endFrame_inclusive": (
            "Examples/7_add_subclips_to_timeline.py uses startFrame=0, endFrame=23 "
            "for the first 24 frames → endFrame is inclusive."
        ),
        "import_media": (
            "MediaPool.ImportMedia([paths...]) or ImportMedia([{FilePath, StartIndex, EndIndex}])"
        ),
        "add_track": 'Timeline.AddTrack("audio", "stereo")',
        "voice_isolation_api": (
            "20.1+: TimelineItem.GetVoiceIsolationState / SetVoiceIsolationState; "
            "Timeline.GetVoiceIsolationState(trackIndex)."
        ),
        "audio_mapping": (
            "MediaPoolItem.GetAudioMapping() and TimelineItem.GetSourceAudioChannelMapping() "
            "return JSON with embedded_audio_channels, linked_audio.{n}.{channels,offset,path}, "
            "track_mapping.{n}.{channel_idx,mute,type}."
        ),
    }


def build_offline_report(connect_note: str) -> dict:
    facts = host_facts()
    installed = facts.get("installed_app_version") or ""
    version = None
    if installed.startswith("21.0.4"):
        version = (21, 0, 4)
    return {
        "connected": False,
        "reason": connect_note,
        "host": facts,
        "selection_strategy": selection_strategy(version),
        "readme_dump": readme_offline_dump(),
        "next_step": (
            "Launch DaVinci Resolve Studio, open a project with a linked A/V clip "
            "selected on the Edit page, then re-run this script to fill "
            "GetSetting / GetClipProperty snapshots."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Write JSON to this path as well as stdout")
    args = parser.parse_args()

    resolve, note = connect()
    if resolve is None:
        report = build_offline_report(note)
        text = json.dumps(report, indent=2, ensure_ascii=False)
        print(text)
        print(
            "\nResolve is not running — dump skipped. Exit 0 (spike / CI-safe).",
            file=sys.stderr,
        )
        if args.out:
            args.out.write_text(text + "\n", encoding="utf-8")
        return 0

    try:
        report = dump_live(resolve)
        report["host"] = host_facts()
        report["readme_dump"] = readme_offline_dump()
    except Exception as exc:
        report = {
            "connected": True,
            "error": f"{type(exc).__name__}: {exc}",
            "host": host_facts(),
            "readme_dump": readme_offline_dump(),
        }

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
