"""Isolated vs extract sync: lag ≤ 1 sample (identity Separator).

Run: python3 -m unittest tests.golden.test_sync
"""

from __future__ import annotations

import ast
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

GOLDEN_DIR = Path(__file__).resolve().parent
if str(GOLDEN_DIR) not in sys.path:
    sys.path.insert(0, str(GOLDEN_DIR))

from support import (  # noqa: E402
    ENGINE_DIR,
    HANDLE_S,
    REPO_ROOT,
    SRC_SR,
    T0,
    T1,
    IdentitySeparator,
    generate_suite,
    have_ffmpeg,
    identity_pipeline,
    job_body,
    lag_samples,
    run_clip,
)

from perfectvoice_engine.ffmpeg_io import decode_f32, extract_with_handles  # noqa: E402


@unittest.skipUnless(have_ffmpeg(), "ffmpeg not installed")
class IsolatedVsExtractSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-golden-sync-"))
        self.media = self.tmp / "media"
        self.out = self.tmp / "out"
        self.media.mkdir()
        self.out.mkdir()
        self.paths = generate_suite(self.media)
        self.roots = [str(self.media), str(self.out)]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _extract(self, src: Path, dest: Path):
        return extract_with_handles(
            src, dest, t0=T0, t1=T1, handle_s=HANDLE_S, sample_format="float32"
        )

    def test_sine_lag_le_one_sample(self) -> None:
        src = self.paths["sine_48k_stereo.wav"]
        extract_path = self.tmp / "extract_sine.wav"
        extracted = self._extract(src, extract_path)
        extract_frames, _ = decode_f32(extract_path)
        with identity_pipeline(self.tmp):
            body = job_body(src, self.out, self.roots, wet=1.0)
            row = run_clip(body)
        isolated, probe = decode_f32(row["output_path"])
        self.assertEqual(probe.sample_rate, SRC_SR)
        self.assertLessEqual(abs(isolated.shape[0] - extract_frames.shape[0]), 1)
        self.assertLessEqual(abs(int(row["output_samples"]) - extracted.sample_count), 1)
        self.assertLessEqual(abs(lag_samples(isolated, extract_frames)), 1)
        self.assertGreaterEqual(IdentitySeparator.separate_calls, 1)

    def test_impulse_peak_within_one_sample(self) -> None:
        src = self.paths["impulse_48k_stereo.wav"]
        extract_path = self.tmp / "extract_impulse.wav"
        self._extract(src, extract_path)
        extract_frames, _ = decode_f32(extract_path)
        extract_peak = int(np.abs(extract_frames).sum(axis=1).argmax())
        with identity_pipeline(self.tmp):
            row = run_clip(job_body(src, self.out, self.roots, wet=1.0))
        isolated, _ = decode_f32(row["output_path"])
        isolated_peak = int(np.abs(isolated).sum(axis=1).argmax())
        self.assertLessEqual(abs(isolated_peak - extract_peak), 1)
        self.assertLessEqual(abs(lag_samples(isolated, extract_frames)), 1)

    def test_click_train_same_in_out(self) -> None:
        src = self.paths["click_train_48k.wav"]
        extract_path = self.tmp / "extract_clicks.wav"
        self._extract(src, extract_path)
        extract_frames, _ = decode_f32(extract_path)
        with identity_pipeline(self.tmp):
            row = run_clip(job_body(src, self.out, self.roots, wet=1.0))
        isolated, _ = decode_f32(row["output_path"])
        self.assertLessEqual(abs(isolated.shape[0] - extract_frames.shape[0]), 1)
        self.assertLessEqual(abs(lag_samples(isolated, extract_frames)), 1)

    def test_speech_plus_bed_same_in_out(self) -> None:
        src = self.paths["speech_plus_bed_48k.wav"]
        extract_path = self.tmp / "extract_speech.wav"
        self._extract(src, extract_path)
        extract_frames, _ = decode_f32(extract_path)
        with identity_pipeline(self.tmp):
            row = run_clip(job_body(src, self.out, self.roots, wet=1.0))
        isolated, _ = decode_f32(row["output_path"])
        self.assertLessEqual(abs(lag_samples(isolated, extract_frames)), 1)
        self.assertEqual(row["handles_left_actual"], T0)
        self.assertEqual(row["handles_right_actual"], HANDLE_S)


class GoldenContractTests(unittest.TestCase):
    def test_no_committed_wavs_or_weights(self) -> None:
        golden = REPO_ROOT / "tests" / "golden"
        banned = []
        for pattern in ("*.wav", "*.aiff", "*.aif", "*.flac", "*.mp3", "*.th", "*.bin", "*.safetensors"):
            banned.extend(golden.rglob(pattern))
        self.assertEqual(banned, [], "generate fixtures in-test; do not commit audio/weights")

    def test_golden_tests_do_not_import_torch_or_demucs(self) -> None:
        banned = {"torch", "torchaudio", "demucs"}
        for path in sorted((REPO_ROOT / "tests" / "golden").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    self.assertNotIn(name.split(".", 1)[0], banned, f"{path.name} imports {name}")
        for name in banned:
            self.assertNotIn(name, sys.modules)

    def test_engine_load_paths_have_no_official_remotes(self) -> None:
        for path in (
            ENGINE_DIR / "perfectvoice_engine" / "pipeline.py",
            ENGINE_DIR / "perfectvoice_engine" / "separate.py",
        ):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("huggingface.co/adefossez", text)
            self.assertNotIn("dl.fbaipublicfiles.com", text)


if __name__ == "__main__":
    unittest.main()
