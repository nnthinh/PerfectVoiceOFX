"""Appendix A clamp: t0=0.2, H=0.5 → H_left_actual=0.2 (not 0.5).

Run: python3 -m unittest tests.golden.test_appendix_a
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent
if str(GOLDEN_DIR) not in sys.path:
    sys.path.insert(0, str(GOLDEN_DIR))

from support import (  # noqa: E402
    FILE_DUR,
    HANDLE_S,
    SRC_SR,
    T0,
    T1,
    generate_suite,
    have_ffmpeg,
    identity_pipeline,
    job_body,
    run_clip,
)

from perfectvoice_engine.ffmpeg_io import (  # noqa: E402
    actual_handles,
    expected_output_sample_count,
    extract_sample_range,
    extract_with_handles,
    inspect_wav,
    round_half_up,
)


@unittest.skipUnless(have_ffmpeg(), "ffmpeg not installed")
class AppendixAClampTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-golden-appa-"))
        self.media = self.tmp / "media"
        self.out = self.tmp / "out"
        self.media.mkdir()
        self.out.mkdir()
        self.paths = generate_suite(self.media)
        self.src = self.paths["sine_48k_stereo.wav"]
        self.roots = [str(self.media), str(self.out)]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t0_lt_h_clamps_left_handle(self) -> None:
        h_left, h_right = actual_handles(T0, T1, FILE_DUR, HANDLE_S)
        self.assertAlmostEqual(h_left, 0.2)
        self.assertAlmostEqual(h_right, 0.5)
        self.assertNotEqual(h_left, HANDLE_S)

        ext = extract_sample_range(T0, T1, FILE_DUR, SRC_SR, HANDLE_S)
        self.assertEqual(ext.src_in_sample, 0)
        self.assertEqual(ext.src_sample_count, 81600)

        dest = self.tmp / "extract.wav"
        result = extract_with_handles(
            self.src, dest, t0=T0, t1=T1, handle_s=HANDLE_S, sample_format="pcm24"
        )
        self.assertAlmostEqual(result.extract.h_left_actual, 0.2)
        self.assertAlmostEqual(result.extract.h_right_actual, 0.5)
        self.assertEqual(result.extract.src_in_sample, 0)
        # 1.7 s @ 48 k, not 2.0 s of unclamped handles.
        self.assertEqual(result.sample_count, 81600)
        naive_full_handles = round_half_up((T1 - T0 + HANDLE_S + HANDLE_S) * SRC_SR)
        self.assertEqual(naive_full_handles, 96000)
        self.assertNotEqual(result.sample_count, naive_full_handles)
        n_out = expected_output_sample_count(T0, T1, FILE_DUR, SRC_SR, HANDLE_S)
        self.assertEqual(n_out, 81600)
        self.assertLessEqual(
            abs(n_out - round((T1 - T0 + h_left + h_right) * SRC_SR)), 1
        )
        info = inspect_wav(dest)
        self.assertEqual(info.sample_count, 81600)

    def test_pipeline_reports_clamped_handles(self) -> None:
        with identity_pipeline(self.tmp):
            row = run_clip(job_body(self.src, self.out, self.roots))
        self.assertEqual(row["handles_left_actual"], 0.2)
        self.assertEqual(row["handles_right_actual"], 0.5)
        expected = expected_output_sample_count(T0, T1, FILE_DUR, SRC_SR, HANDLE_S)
        self.assertLessEqual(abs(int(row["output_samples"]) - expected), 1)
        self.assertEqual(inspect_wav(row["output_path"]).sample_count, row["output_samples"])

    def test_five_synthetic_clips_are_short(self) -> None:
        # Guardrail: suite stays in-test and tiny (no committed footage).
        self.assertEqual(len(self.paths), 5)
        for name, path in self.paths.items():
            self.assertTrue(path.is_file(), name)
            self.assertLess(path.stat().st_size, 2_000_000, name)


if __name__ == "__main__":
    unittest.main()
