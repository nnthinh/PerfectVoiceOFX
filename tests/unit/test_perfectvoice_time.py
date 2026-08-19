"""Appendix A fixtures — no Resolve required."""

from __future__ import annotations

import sys
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.perfectvoice_time import (
    FPS_23_976,
    FPS_24,
    FPS_25,
    FPS_29_97,
    FPS_59_94,
    append_clip_info,
    as_fps,
    extract_sample_range,
    expected_output_sample_count,
    file_relative_times,
    parse_timeline_frame_rate,
    place_frames,
    round_half_up,
)


class RoundHalfUpTests(unittest.TestCase):
    def test_half_up_not_bankers(self):
        self.assertEqual(round_half_up(0.5), 1)
        self.assertEqual(round_half_up(1.5), 2)
        self.assertEqual(round_half_up(2.5), 3)
        # Python 3 round is half-even: round(2.5) == 2
        self.assertEqual(round(2.5), 2)

    def test_typical_and_negative(self):
        self.assertEqual(round_half_up(4.795204795204795), 5)
        self.assertEqual(round_half_up(-0.5), -1)
        self.assertEqual(round_half_up(-1.5), -2)

    def test_rejects_nan_inf(self):
        with self.assertRaises(ValueError):
            round_half_up(float("nan"))
        with self.assertRaises(ValueError):
            round_half_up(float("inf"))


class FileRelativeTimesTests(unittest.TestCase):
    def test_file_relative_unchanged(self):
        t0, t1 = file_relative_times(0.2, 1.2, 10.0)
        self.assertAlmostEqual(t0, 0.2)
        self.assertAlmostEqual(t1, 1.2)

    def test_tod_timecode_maps_onto_file(self):
        t0, t1 = file_relative_times(75940.36, 75977.78, 37.42)
        self.assertEqual(t0, 0.0)
        self.assertAlmostEqual(t1, 37.42)


class ClampHandlesTests(unittest.TestCase):
    def test_t0_lt_h_clamps_left(self):
        ext = extract_sample_range(
            t0=0.2, t1=1.2, file_dur=10.0, src_sr=48000, handle_s=0.5
        )
        self.assertAlmostEqual(ext.h_left_actual, 0.2)
        self.assertAlmostEqual(ext.h_right_actual, 0.5)
        self.assertEqual(ext.src_in_sample, 0)
        self.assertEqual(ext.src_out_sample, round_half_up((1.2 + 0.5) * 48000))

    def test_interior_clip_keeps_full_handles(self):
        ext = extract_sample_range(
            t0=1.0, t1=2.0, file_dur=10.0, src_sr=48000, handle_s=0.5
        )
        self.assertAlmostEqual(ext.h_left_actual, 0.5)
        self.assertAlmostEqual(ext.h_right_actual, 0.5)
        self.assertEqual(ext.src_in_sample, 24000)
        self.assertEqual(ext.src_out_sample, 120000)

    def test_eof_clamps_right(self):
        ext = extract_sample_range(
            t0=1.0, t1=1.8, file_dur=2.0, src_sr=48000, handle_s=0.5
        )
        self.assertAlmostEqual(ext.h_left_actual, 0.5)
        self.assertAlmostEqual(ext.h_right_actual, 0.2)
        self.assertEqual(ext.src_out_sample, round_half_up(2.0 * 48000))

    def test_t0_zero_no_left_handle(self):
        ext = extract_sample_range(
            t0=0.0, t1=1.0, file_dur=5.0, src_sr=44100, handle_s=0.5
        )
        self.assertEqual(ext.h_left_actual, 0.0)
        self.assertEqual(ext.src_in_sample, 0)

    def test_t1_past_eof_right_handle_zero(self):
        ext = extract_sample_range(
            t0=0.5, t1=5.0, file_dur=4.0, src_sr=48000, handle_s=0.5
        )
        self.assertEqual(ext.h_right_actual, 0.0)


class RationalFpsPlaceTests(unittest.TestCase):
    FIXTURES = (FPS_23_976, FPS_24, FPS_25, FPS_29_97)

    def test_clamp_sof_on_required_fps(self):
        for fps in self.FIXTURES:
            with self.subTest(fps=fps):
                place = place_frames(
                    t0=0.2, t1=1.2, file_dur=10.0, out_fps=fps, handle_s=0.5
                )
                fps_f = fps[0] / fps[1]
                self.assertEqual(
                    place.handle_start_frame, round_half_up(0.2 * fps_f)
                )
                self.assertEqual(
                    place.handle_end_frame_exclusive,
                    round_half_up((0.2 + 1.0) * fps_f),
                )
                self.assertEqual(
                    place.handle_end_frame,
                    place.handle_end_frame_exclusive - 1,
                )
                self.assertGreater(place.body_frame_count, 0)

    def test_24fps_interior_is_exact(self):
        place = place_frames(
            t0=1.0, t1=2.0, file_dur=10.0, out_fps=FPS_24, handle_s=0.5
        )
        # H_left=0.5s @ 24/1 → frame 12; body 1.0s → exclusive 36
        self.assertEqual(place.handle_start_frame, 12)
        self.assertEqual(place.handle_end_frame_exclusive, 36)
        self.assertEqual(place.handle_end_frame, 35)
        self.assertEqual(place.body_frame_count, 24)

    def test_23976_matches_fraction(self):
        place = place_frames(
            t0=0.2, t1=1.2, file_dur=10.0, out_fps=FPS_23_976, handle_s=0.5
        )
        fps = Fraction(24000, 1001)
        self.assertEqual(place.out_fps_num, 24000)
        self.assertEqual(place.out_fps_den, 1001)
        self.assertEqual(place.handle_start_frame, round_half_up(0.2 * float(fps)))

    def test_drop_frame_is_display_only(self):
        # 29.97 DF vs NDF share 30000/1001; DF is a timecode label.
        ndf = place_frames(t0=0.2, t1=1.2, file_dur=10.0, out_fps=FPS_29_97)
        as_frac = place_frames(
            t0=0.2, t1=1.2, file_dur=10.0, out_fps=Fraction(30000, 1001)
        )
        self.assertEqual(ndf.handle_start_frame, as_frac.handle_start_frame)
        self.assertEqual(ndf.handle_end_frame, as_frac.handle_end_frame)


class NeverSrcInAsStartFrameTests(unittest.TestCase):
    def test_start_frame_is_wav_grid_not_source_sample(self):
        ext = extract_sample_range(
            t0=10.0, t1=12.0, file_dur=60.0, src_sr=48000, handle_s=0.5
        )
        place = place_frames(
            t0=10.0, t1=12.0, file_dur=60.0, out_fps=FPS_24, handle_s=0.5
        )
        self.assertEqual(ext.src_in_sample, 456000)
        self.assertEqual(place.handle_start_frame, 12)
        self.assertNotEqual(place.handle_start_frame, ext.src_in_sample)
        clip_info = append_clip_info(
            media_pool_item="dummy",
            place=place,
            record_frame=1001,
            track_index=1,
        )
        self.assertEqual(clip_info["startFrame"], place.handle_start_frame)
        self.assertEqual(clip_info["endFrame"], place.handle_end_frame)
        self.assertNotEqual(clip_info["startFrame"], ext.src_in_sample)
        self.assertEqual(clip_info["mediaType"], 2)
        self.assertEqual(clip_info["recordFrame"], 1001)

    def test_end_frame_inclusive_matches_official_example_shape(self):
        # Official 7_add_subclips_to_timeline.py: start=0, end=23 → 24 frames.
        place = place_frames(
            t0=0.5, t1=1.5, file_dur=10.0, out_fps=FPS_24, handle_s=0.5
        )
        self.assertEqual(place.handle_start_frame, 12)
        self.assertEqual(place.handle_end_frame, 35)
        self.assertEqual(
            place.handle_end_frame - place.handle_start_frame + 1, 24
        )


class OutputLengthTests(unittest.TestCase):
    def test_n_out_within_one_sample_of_duration(self):
        t0, t1, file_dur, h, proj_sr = 0.2, 1.2, 10.0, 0.5, 48000
        n_out = expected_output_sample_count(t0, t1, file_dur, proj_sr, h)
        # H_left clamps to 0.2 → extract 1.7 s, not 2.0 s
        expected_seconds = (t1 - t0) + 0.2 + 0.5
        self.assertLessEqual(
            abs(n_out - round(expected_seconds * proj_sr)), 1
        )
        self.assertEqual(n_out, 81600)

    def test_unclamped_h_would_be_wrong(self):
        n_out = expected_output_sample_count(0.2, 1.2, 10.0, 48000, 0.5)
        naive = round_half_up((1.2 - 0.2 + 0.5 + 0.5) * 48000)
        self.assertNotEqual(n_out, naive)
        self.assertLess(n_out, naive)


class FpsParseTests(unittest.TestCase):
    def test_tuple_and_fraction(self):
        self.assertEqual(as_fps((24000, 1001)), Fraction(24000, 1001))
        self.assertEqual(as_fps(Fraction(24, 1)), Fraction(24, 1))
        self.assertEqual(as_fps(25), Fraction(25, 1))

    def test_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            as_fps((0, 1))
        with self.assertRaises(ValueError):
            as_fps(-24)

    def test_timeline_frame_rate_readme_strings(self):
        self.assertEqual(parse_timeline_frame_rate("23.976"), Fraction(*FPS_23_976))
        self.assertEqual(parse_timeline_frame_rate("24"), Fraction(*FPS_24))
        self.assertEqual(parse_timeline_frame_rate("25"), Fraction(*FPS_25))
        self.assertEqual(parse_timeline_frame_rate("29.97"), Fraction(*FPS_29_97))
        self.assertEqual(parse_timeline_frame_rate("29.97 DF"), Fraction(*FPS_29_97))
        self.assertEqual(parse_timeline_frame_rate("59.94"), Fraction(*FPS_59_94))
        self.assertEqual(parse_timeline_frame_rate(24), Fraction(24, 1))

    def test_timeline_frame_rate_rejects_empty(self):
        with self.assertRaises(ValueError):
            parse_timeline_frame_rate("")
        with self.assertRaises(ValueError):
            parse_timeline_frame_rate(None)


if __name__ == "__main__":
    unittest.main()
