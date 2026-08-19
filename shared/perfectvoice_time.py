"""Appendix A — sample / frame conversion (pure functions, no Resolve).

Normative formulas live in docs/design.md Appendix A. Engine is source of
truth if a later port drifts; this module is the first implementation.

``src_in_sample`` is an extract index. It must never be passed as
``AppendToTimeline`` ``startFrame``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Tuple, Union

# Rational timeline / media fps used by the required fixtures.
FPS_23_976: Tuple[int, int] = (24000, 1001)
FPS_24: Tuple[int, int] = (24, 1)
FPS_25: Tuple[int, int] = (25, 1)
FPS_29_97: Tuple[int, int] = (30000, 1001)
FPS_59_94: Tuple[int, int] = (60000, 1001)

# README: drop-frame is a suffix on the same numeric rate ("29.97 DF").
_NTSC_RATE_STRINGS = {
    "23.976": FPS_23_976,
    "29.97": FPS_29_97,
    "59.94": FPS_59_94,
}

DEFAULT_HANDLE_S = 0.5

FpsLike = Union[Fraction, Tuple[int, int], int, float]


def round_half_up(value: float) -> int:
    """Round half away from zero (not banker's ``round``)."""
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"round_half_up expects a finite number, got {value!r}")
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def as_fps(fps: FpsLike) -> Fraction:
    """Accept ``(num, den)``, ``Fraction``, or a scalar fps."""
    if isinstance(fps, Fraction):
        if fps <= 0:
            raise ValueError(f"fps must be positive, got {fps}")
        return fps
    if isinstance(fps, tuple):
        if len(fps) != 2:
            raise ValueError(f"fps tuple must be (num, den), got {fps!r}")
        frac = Fraction(fps[0], fps[1])
        if frac <= 0:
            raise ValueError(f"fps must be positive, got {fps}")
        return frac
    frac = Fraction(fps).limit_denominator(1001)
    if frac <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    return frac


def parse_timeline_frame_rate(raw: object) -> Fraction:
    """Map ``Project.GetSetting('timelineFrameRate')`` to a rational fps.

    README: DF is a display suffix (``"29.97 DF"``). 23.976 / 29.97 / 59.94
    are 24000/1001, 30000/1001, 60000/1001 — not 24.0 / 30.0 / 60.0.
    """
    if raw is None:
        raise ValueError("timelineFrameRate is empty")
    text = str(raw).strip()
    if not text:
        raise ValueError("timelineFrameRate is empty")
    if text.upper().endswith("DF"):
        text = text[:-2].strip()
    if text in _NTSC_RATE_STRINGS:
        return as_fps(_NTSC_RATE_STRINGS[text])
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"unrecognized timelineFrameRate: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"timelineFrameRate must be positive, got {raw!r}")
    for label, rational in _NTSC_RATE_STRINGS.items():
        if abs(value - float(label)) < 0.001:
            return as_fps(rational)
    if abs(value - round(value)) < 1e-6:
        return as_fps((int(round(value)), 1))
    return as_fps(value)


def actual_handles(
    t0: float,
    t1: float,
    file_dur: float,
    handle_s: float = DEFAULT_HANDLE_S,
) -> Tuple[float, float]:
    """Clamp requested handles into the source file.

    Fixture: t0=0.2, H=0.5 → H_left_actual=0.2 (not 0.5).
    """
    if handle_s < 0:
        raise ValueError(f"handle_s must be >= 0, got {handle_s}")
    h_left = min(handle_s, max(0.0, t0))
    h_right = min(handle_s, max(0.0, file_dur - t1))
    return h_left, h_right


@dataclass(frozen=True)
class ExtractRange:
    """Source-file sample window. ``src_out_sample`` is exclusive."""

    h_left_actual: float
    h_right_actual: float
    src_in_sample: int
    src_out_sample: int
    src_sr: int

    @property
    def src_sample_count(self) -> int:
        return self.src_out_sample - self.src_in_sample


@dataclass(frozen=True)
class PlaceFrames:
    """WAV-grid frames for ``AppendToTimeline``.

    Official example ``7_add_subclips_to_timeline.py`` uses
    ``startFrame=0``, ``endFrame=23`` for the first 24 frames, so
    ``endFrame`` is inclusive. ``handle_end_frame`` is that inclusive
    value. ``handle_end_frame_exclusive`` is the Appendix A formula.
    """

    handle_start_frame: int
    handle_end_frame_exclusive: int
    handle_end_frame: int
    out_fps_num: int
    out_fps_den: int

    @property
    def body_frame_count(self) -> int:
        return self.handle_end_frame_exclusive - self.handle_start_frame


def file_relative_times(t0: float, t1: float, file_dur: float) -> tuple[float, float]:
    """Map reel / time-of-day source TC onto [0, file_dur]."""
    if file_dur <= 0 or t1 <= t0:
        return t0, t1
    if t0 >= -1e-3 and t1 <= file_dur + 1.0:
        return max(0.0, t0), min(t1, file_dur)
    if t0 >= file_dur - 1e-3:
        return 0.0, min(t1 - t0, file_dur)
    return t0, t1


def extract_sample_range(
    t0: float,
    t1: float,
    file_dur: float,
    src_sr: int,
    handle_s: float = DEFAULT_HANDLE_S,
) -> ExtractRange:
    """Sample-accurate extract window on the source file.

    ::

        src_in_sample  = round_half_up((t0 - H_left_actual)  * src_sr)
        src_out_sample = round_half_up((t1 + H_right_actual) * src_sr)  # exclusive
    """
    if src_sr <= 0:
        raise ValueError(f"src_sr must be positive, got {src_sr}")
    h_left, h_right = actual_handles(t0, t1, file_dur, handle_s)
    src_in = round_half_up((t0 - h_left) * src_sr)
    src_out = round_half_up((t1 + h_right) * src_sr)
    if src_out < src_in:
        raise ValueError(
            f"empty extract window: src_in={src_in} src_out={src_out} "
            f"(t0={t0} t1={t1} file_dur={file_dur})"
        )
    return ExtractRange(
        h_left_actual=h_left,
        h_right_actual=h_right,
        src_in_sample=src_in,
        src_out_sample=src_out,
        src_sr=src_sr,
    )


def place_frames(
    t0: float,
    t1: float,
    file_dur: float,
    out_fps: FpsLike,
    handle_s: float = DEFAULT_HANDLE_S,
) -> PlaceFrames:
    """Frames on the *output WAV* grid for ``AppendToTimeline``.

    ::

        handleStartFrm = round_half_up(H_left_actual * out_fps)
        handleEndFrm   = round_half_up((H_left_actual + (t1 - t0)) * out_fps)

    ``recordFrame`` is ``originalItem.GetStart()`` — not computed here.
    """
    fps = as_fps(out_fps)
    h_left, _h_right = actual_handles(t0, t1, file_dur, handle_s)
    fps_f = float(fps)
    start = round_half_up(h_left * fps_f)
    end_excl = round_half_up((h_left + (t1 - t0)) * fps_f)
    if end_excl <= start:
        raise ValueError(
            f"empty place window: start={start} end_excl={end_excl} "
            f"(t0={t0} t1={t1} out_fps={fps})"
        )
    return PlaceFrames(
        handle_start_frame=start,
        handle_end_frame_exclusive=end_excl,
        handle_end_frame=end_excl - 1,
        out_fps_num=fps.numerator,
        out_fps_den=fps.denominator,
    )


def expected_output_sample_count(
    t0: float,
    t1: float,
    file_dur: float,
    proj_sr: int,
    handle_s: float = DEFAULT_HANDLE_S,
) -> int:
    """Expected WAV length @ project rate (Appendix A ``N_out``)."""
    if proj_sr <= 0:
        raise ValueError(f"proj_sr must be positive, got {proj_sr}")
    h_left, h_right = actual_handles(t0, t1, file_dur, handle_s)
    return round_half_up((t1 - t0 + h_left + h_right) * proj_sr)


def append_clip_info(
    media_pool_item: object,
    place: PlaceFrames,
    record_frame: Union[int, float],
    track_index: int,
    media_type: int = 2,
) -> dict:
    """``AppendToTimeline`` clipInfo. ``startFrame`` is WAV-grid, never src_in."""
    return {
        "mediaPoolItem": media_pool_item,
        "startFrame": place.handle_start_frame,
        "endFrame": place.handle_end_frame,
        "mediaType": media_type,
        "trackIndex": track_index,
        "recordFrame": record_frame,
    }
