"""Golden cache-key pairs: 48 vs 96, pcm24 vs f32. No real Demucs weights.

Run: python3 -m unittest tests.golden.test_cache_keys
"""

from __future__ import annotations

import json
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
    T0,
    T1,
    IdentitySeparator,
    generate_suite,
    have_ffmpeg,
    identity_pipeline,
    job_body,
    run_clip,
)

from perfectvoice_engine.cache import compute_input_hash, file_id_from_path  # noqa: E402
from perfectvoice_engine.ffmpeg_io import expected_output_sample_count, inspect_wav  # noqa: E402
from perfectvoice_engine.models import DEFAULT_MODEL, weights_sha256  # noqa: E402
from perfectvoice_engine.pipeline import clip_input_hash  # noqa: E402


@unittest.skipUnless(have_ffmpeg(), "ffmpeg not installed")
class CacheKeyPairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-golden-cache-"))
        self.media = self.tmp / "media"
        self.out = self.tmp / "out"
        self.media.mkdir()
        self.out.mkdir()
        self.paths = generate_suite(self.media)
        self.src = self.paths["sine_48k_stereo.wav"]
        self.roots = [str(self.media), str(self.out)]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_48_vs_96_two_keys(self) -> None:
        with identity_pipeline(self.tmp):
            r48 = run_clip(
                job_body(self.src, self.out, self.roots, project_sample_rate=48000)
            )
            r96 = run_clip(
                job_body(self.src, self.out, self.roots, project_sample_rate=96000)
            )
        self.assertNotEqual(r48["input_hash"], r96["input_hash"])
        self.assertFalse(r48["cache_hit"])
        self.assertFalse(r96["cache_hit"])
        self.assertNotEqual(r48["output_path"], r96["output_path"])
        self.assertEqual(inspect_wav(r48["output_path"]).sample_rate, 48000)
        self.assertEqual(inspect_wav(r96["output_path"]).sample_rate, 96000)
        n48 = expected_output_sample_count(T0, T1, FILE_DUR, 48000, HANDLE_S)
        n96 = expected_output_sample_count(T0, T1, FILE_DUR, 96000, HANDLE_S)
        self.assertLessEqual(abs(int(r48["output_samples"]) - n48), 1)
        self.assertLessEqual(abs(int(r96["output_samples"]) - n96), 1)
        self.assertEqual(IdentitySeparator.separate_calls, 2)
        body48 = job_body(self.src, self.out, self.roots, project_sample_rate=48000)
        manifest = json.loads((self.tmp / "manifest.json").read_text(encoding="utf-8"))
        digest = weights_sha256(manifest[DEFAULT_MODEL])
        self.assertEqual(
            r48["input_hash"],
            clip_input_hash(
                body48["clips"][0],
                body48["params"],
                file_id=file_id_from_path(self.src),
                weights_digest=digest,
            ),
        )

    def test_pcm24_vs_f32_two_keys(self) -> None:
        with identity_pipeline(self.tmp):
            pcm = run_clip(job_body(self.src, self.out, self.roots, sample_format="pcm24"))
            f32 = run_clip(job_body(self.src, self.out, self.roots, sample_format="float32"))
        self.assertNotEqual(pcm["input_hash"], f32["input_hash"])
        self.assertFalse(pcm["cache_hit"])
        self.assertFalse(f32["cache_hit"])
        self.assertEqual(inspect_wav(pcm["output_path"]).sample_format, "pcm24")
        self.assertEqual(inspect_wav(f32["output_path"]).sample_format, "float32")
        self.assertEqual(IdentitySeparator.separate_calls, 2)

    def test_same_identity_is_cache_hit(self) -> None:
        with identity_pipeline(self.tmp):
            first = run_clip(job_body(self.src, self.out, self.roots))
            second = run_clip(job_body(self.src, self.out, self.roots))
        self.assertEqual(first["input_hash"], second["input_hash"])
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(IdentitySeparator.separate_calls, 1)

    def test_hash_function_48_vs_96_and_pcm24_vs_f32(self) -> None:
        # Pure identity function — same pairs the pipeline must produce.
        file_id = (1, 2, 3, 4)
        base = dict(
            file_id=file_id,
            src_in=9600,
            src_out=57600,
            audio_stream_index=0,
            channel_map=(0, 1),
            model_name="htdemucs",
            weights_sha256="ab" * 32,
            vocals_only_bag=False,
            wet=1.0,
            gain=0.0,
            mono=False,
            handles_requested=0.5,
            file_duration_seconds=2.0,
            segment=7.8,
            overlap=0.25,
            shifts=1,
            enhancer_id="none",
            project_sample_rate=48000,
            sample_format="pcm24",
            resampler_id="soxr_hq_v1",
            clip_policy="no_demucs_rescale",
            engine_semver="0.1.0",
        )
        k48 = compute_input_hash(**base)
        k96 = compute_input_hash(**{**base, "project_sample_rate": 96000})
        k_f32 = compute_input_hash(**{**base, "sample_format": "f32"})
        k_float32 = compute_input_hash(**{**base, "sample_format": "float32"})
        self.assertNotEqual(k48, k96)
        self.assertNotEqual(k48, k_f32)
        self.assertEqual(k_f32, k_float32)
        self.assertEqual(len(k48), 64)


if __name__ == "__main__":
    unittest.main()
