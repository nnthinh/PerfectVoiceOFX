"""Wet/dry graph tests (no Resolve, no torch, no DeepFilterNet).

Run: python3 -m unittest tests.unit.test_blend
"""

from __future__ import annotations

import ast
import inspect
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine.blend import (  # noqa: E402
    DEFAULT_GAIN_DB,
    DEFAULT_WET,
    ENHANCER_DEEPFILTERNET3,
    ENHANCER_NONE,
    apply_output_gain,
    blend,
    blend_to_wav,
    derive_wet_dry_sample_rate,
    fold_mono_mid,
    to_wet_dry_rate,
    wet_dry_mix,
)
from perfectvoice_engine.ffmpeg_io import (  # noqa: E402
    BWF_ORIGINATOR,
    FFmpegError,
    ffmpeg_bin,
    inspect_wav,
)


def _have_ffmpeg() -> bool:
    try:
        ffmpeg_bin()
        return True
    except FFmpegError:
        return False


def _stereo_sines(n: int, sr: int, freqs: tuple[float, float], amp: float) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / np.float32(sr)
    return np.stack(
        [
            amp * np.sin(2 * np.pi * freqs[0] * t),
            amp * np.sin(2 * np.pi * freqs[1] * t),
        ],
        axis=1,
    ).astype(np.float32)


class DeriveWetDryRateTests(unittest.TestCase):
    def test_none_is_44100(self) -> None:
        self.assertEqual(derive_wet_dry_sample_rate("none"), 44100)
        self.assertEqual(derive_wet_dry_sample_rate(ENHANCER_NONE), 44100)

    def test_deepfilternet3_is_48000(self) -> None:
        self.assertEqual(derive_wet_dry_sample_rate("deepfilternet3"), 48000)
        self.assertEqual(derive_wet_dry_sample_rate(ENHANCER_DEEPFILTERNET3), 48000)

    def test_unknown_enhancer_rejected(self) -> None:
        with self.assertRaises(ValueError):
            derive_wet_dry_sample_rate("voicefixer")

    def test_blend_does_not_accept_client_rate(self) -> None:
        self.assertNotIn("wet_dry_sample_rate", inspect.signature(blend).parameters)
        self.assertNotIn(
            "wet_dry_sample_rate", inspect.signature(blend_to_wav).parameters
        )
        self.assertEqual(DEFAULT_WET, 0.85)
        self.assertEqual(DEFAULT_GAIN_DB, 0.0)


class WetDryIdentityTests(unittest.TestCase):
    def test_wet_one_equals_vocals_same_rate(self) -> None:
        n = 44100
        x = _stereo_sines(n, 44100, (440.0, 550.0), 0.2)
        v = _stereo_sines(n, 44100, (660.0, 770.0), 0.1)
        mixed = wet_dry_mix(x, v, wet=1.0)
        np.testing.assert_array_equal(mixed, v)
        result = blend(
            x,
            v,
            in_sample_rate=44100,
            enhancer="none",
            project_sample_rate=44100,
            wet=1.0,
        )
        self.assertEqual(result.wet_dry_sample_rate, 44100)
        np.testing.assert_array_equal(result.samples, v)

    def test_wet_zero_equals_dry_same_rate(self) -> None:
        n = 44100
        x = _stereo_sines(n, 44100, (440.0, 550.0), 0.2)
        v = _stereo_sines(n, 44100, (660.0, 770.0), 0.1)
        mixed = wet_dry_mix(x, v, wet=0.0)
        np.testing.assert_array_equal(mixed, x)
        result = blend(
            x,
            v,
            in_sample_rate=44100,
            enhancer="none",
            project_sample_rate=44100,
            wet=0.0,
        )
        np.testing.assert_array_equal(result.samples, x)

    def test_default_wet_is_085(self) -> None:
        x = np.ones((8, 2), dtype=np.float32)
        v = np.zeros((8, 2), dtype=np.float32)
        y = wet_dry_mix(x, v)
        np.testing.assert_allclose(y, np.full_like(x, 0.15), rtol=0, atol=1e-6)

    def test_wet_one_dfn_domain_equals_resampled_vocals(self) -> None:
        n = 44100
        x = _stereo_sines(n, 44100, (440.0, 550.0), 0.2)
        v = _stereo_sines(n, 44100, (660.0, 770.0), 0.1)
        result = blend(
            x,
            v,
            in_sample_rate=44100,
            enhancer="deepfilternet3",
            project_sample_rate=48000,
            wet=1.0,
        )
        expect = to_wet_dry_rate(v, 44100, "deepfilternet3")
        self.assertEqual(result.wet_dry_sample_rate, 48000)
        self.assertEqual(result.sample_count, result.wet_dry_sample_count)
        np.testing.assert_array_equal(result.samples, expect)


class GainMonoTests(unittest.TestCase):
    def test_gain_zero_db_is_identity(self) -> None:
        x = _stereo_sines(128, 44100, (440.0, 550.0), 0.25)
        np.testing.assert_array_equal(apply_output_gain(x, 0.0), x)

    def test_gain_plus_six_db_doubles(self) -> None:
        x = np.full((16, 2), 0.1, dtype=np.float32)
        doubled = apply_output_gain(x, 20.0 * np.log10(2.0))
        np.testing.assert_allclose(doubled, x * 2.0, rtol=0, atol=1e-6)

    def test_mono_mid_is_mean(self) -> None:
        frames = np.stack(
            [np.full(8, 0.5, dtype=np.float32), np.full(8, -0.25, dtype=np.float32)],
            axis=1,
        )
        mid = fold_mono_mid(frames)
        self.assertEqual(mid.shape, (8, 1))
        np.testing.assert_allclose(mid[:, 0], np.full(8, 0.125), rtol=0, atol=1e-6)

    def test_blend_mono_collapses_channels(self) -> None:
        x = np.ones((441, 2), dtype=np.float32)
        v = np.zeros((441, 2), dtype=np.float32)
        result = blend(
            x,
            v,
            in_sample_rate=44100,
            enhancer="none",
            project_sample_rate=44100,
            wet=0.0,
            mono=True,
        )
        self.assertEqual(result.channels, 1)
        self.assertEqual(result.samples.shape, (441, 1))


class DfnProjectRateGraphTests(unittest.TestCase):
    def test_dfn3_blends_at_48k_before_96k_project(self) -> None:
        # DFN inference is identity (PR 05d). Only the wet/dry domain matters.
        duration = 1.0
        in_sr = 44100
        n = int(round(duration * in_sr))
        x = _stereo_sines(n, in_sr, (440.0, 550.0), 0.2)
        v = _stereo_sines(n, in_sr, (660.0, 770.0), 0.1)
        result = blend(
            x,
            v,
            in_sample_rate=in_sr,
            enhancer="deepfilternet3",
            project_sample_rate=96000,
        )
        self.assertEqual(result.wet_dry_sample_rate, 48000)
        self.assertEqual(result.project_sample_rate, 96000)
        self.assertLessEqual(abs(result.wet_dry_sample_count - round(duration * 48000)), 1)
        self.assertLessEqual(abs(result.sample_count - round(duration * 96000)), 1)
        self.assertNotEqual(result.wet_dry_sample_count, result.sample_count)

    def test_none_blends_at_44100_even_when_project_is_96k(self) -> None:
        duration = 1.0
        in_sr = 44100
        n = int(round(duration * in_sr))
        x = _stereo_sines(n, in_sr, (440.0, 550.0), 0.2)
        v = _stereo_sines(n, in_sr, (660.0, 770.0), 0.1)
        result = blend(
            x,
            v,
            in_sample_rate=in_sr,
            enhancer="none",
            project_sample_rate=96000,
        )
        self.assertEqual(result.wet_dry_sample_rate, 44100)
        self.assertEqual(result.wet_dry_sample_count, n)
        self.assertLessEqual(abs(result.sample_count - round(duration * 96000)), 1)


@unittest.skipUnless(_have_ffmpeg(), "ffmpeg not installed")
class BlendWavTests(unittest.TestCase):
    def test_dfn3_project_96k_wav(self) -> None:
        duration = 1.0
        in_sr = 44100
        n = int(round(duration * in_sr))
        x = _stereo_sines(n, in_sr, (440.0, 550.0), 0.2)
        v = _stereo_sines(n, in_sr, (660.0, 770.0), 0.1)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "voice.wav"
            result = blend_to_wav(
                dest,
                x,
                v,
                in_sample_rate=in_sr,
                enhancer="deepfilternet3",
                project_sample_rate=96000,
                sample_format="pcm24",
            )
            self.assertEqual(result.wet_dry_sample_rate, 48000)
            self.assertLessEqual(
                abs(result.wet_dry_sample_count - round(duration * 48000)), 1
            )
            self.assertEqual(result.project_sample_rate, 96000)
            self.assertEqual(result.sample_format, "pcm24")
            self.assertEqual(result.originator, BWF_ORIGINATOR)
            info = inspect_wav(dest)
            self.assertEqual(info.sample_rate, 96000)
            self.assertEqual(info.sample_format, "pcm24")
            self.assertEqual(info.originator, BWF_ORIGINATOR)
            self.assertEqual(info.channels, 2)
            self.assertLessEqual(abs(info.sample_count - round(duration * 96000)), 1)
            self.assertEqual(info.sample_count, result.sample_count)

    def test_float32_bwf_originator(self) -> None:
        x = np.zeros((4410, 2), dtype=np.float32)
        v = np.ones((4410, 2), dtype=np.float32) * 0.1
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "f32.wav"
            result = blend_to_wav(
                dest,
                x,
                v,
                in_sample_rate=44100,
                enhancer="none",
                project_sample_rate=44100,
                sample_format="float32",
            )
            self.assertEqual(result.sample_format, "float32")
            self.assertEqual(inspect_wav(dest).originator, BWF_ORIGINATOR)


class NoTorchDemucsImportTests(unittest.TestCase):
    def test_blend_module_does_not_import_torch_or_demucs(self) -> None:
        banned = {"torch", "torchaudio", "demucs"}
        path = ENGINE_DIR / "perfectvoice_engine" / "blend.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".", 1)[0]
                self.assertNotIn(top, banned, f"blend.py imports {name}")
        for name in banned:
            self.assertNotIn(name, sys.modules)


if __name__ == "__main__":
    unittest.main()
