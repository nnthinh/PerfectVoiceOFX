"""10-min window OLA, 2 GiB cap, cancel. No real Demucs weights.

Run: python3 -m unittest tests.unit.test_ola
"""

from __future__ import annotations

import hashlib
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine.models import DEFAULT_MODEL  # noqa: E402
from perfectvoice_engine.resample import MODEL_SAMPLE_RATE  # noqa: E402
from perfectvoice_engine.separate import (  # noqa: E402
    MEMORY_CAP_BYTES,
    WINDOW_OVERLAP_SECONDS,
    WINDOW_SECONDS,
    JobCancelled,
    SeparateRequest,
    _to_model_input,
    exceeds_memory_cap,
    overlap_add,
    pcm_nbytes,
    raise_if_cancelled,
    separate_vocals,
    should_window,
    window_hop_samples,
    window_slices,
)

# Short windows so unit tests do not allocate 10 minutes of audio.
_TEST_WINDOW_S = 0.25
_TEST_OVERLAP_S = 0.05
_SINE_HZ = 440.0
_SINE_AMP = 0.5
# FakeSeparator gain — keep in lockstep with the mock below.
_MOCK_GAIN = 0.5
# Odd-window DC so OLA is tested when adjacent stems disagree.
_ODD_DC = 0.02
_DISC_LIMIT_DBFS = -80.0


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_htdemucs_fixture(root: Path, *, th_payload: bytes = b"fixture-htdemucs") -> dict[str, dict[str, str]]:
    files = {
        "htdemucs.yaml": b"models: ['955717e8']\n",
        "955717e8-8726e21a.th": th_payload,
    }
    mapping: dict[str, str] = {}
    for name, payload in files.items():
        (root / name).write_bytes(payload)
        mapping[name] = _digest(payload)
    return {DEFAULT_MODEL: mapping}


def _sine(frames: int, sr: int = MODEL_SAMPLE_RATE, *, amp: float = _SINE_AMP) -> np.ndarray:
    t = np.arange(frames, dtype=np.float64) / float(sr)
    mono = (amp * np.sin(2.0 * math.pi * _SINE_HZ * t)).astype(np.float32)
    return np.stack([mono, mono], axis=0)


def _discontinuity_dbfs(
    got: np.ndarray,
    expected: np.ndarray,
    boundaries: list[int],
) -> float:
    """Peak extra first-difference at splice points, in dBFS (0 dBFS = 1.0)."""
    worst = 1e-20
    n = int(got.shape[-1])
    for b in boundaries:
        for idx in (b - 1, b, b + 1):
            if idx <= 0 or idx >= n:
                continue
            jump = got[:, idx] - got[:, idx - 1]
            exp_jump = expected[:, idx] - expected[:, idx - 1]
            resid = float(np.max(np.abs(jump - exp_jump)))
            sample = float(np.max(np.abs(got[:, idx] - expected[:, idx])))
            worst = max(worst, resid, sample)
    return 20.0 * math.log10(worst)


class FakeSeparator:
    instances: list["FakeSeparator"] = []

    def __init__(self, model: str = DEFAULT_MODEL, repo: Path | None = None, **kwargs: Any) -> None:
        if repo is None:
            raise AssertionError("Separator must be constructed with repo=")
        self.model = model
        self.repo = Path(repo)
        self.kwargs = kwargs
        self.separate_calls = 0
        self.lengths: list[int] = []
        FakeSeparator.instances.append(self)

    def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
        self.separate_calls += 1
        callback = self.kwargs.get("callback")
        if callable(callback):
            callback({"state": "start"})
        arr = np.asarray(wav, dtype=np.float32)
        self.lengths.append(int(arr.shape[-1]))
        vocals = arr * _MOCK_GAIN
        if callable(callback):
            callback({"state": "end"})
        return arr, {"vocals": vocals, "drums": arr * 0.0, "bass": arr * 0.0, "other": arr * 0.0}


def _separate_mocked(
    req: SeparateRequest,
    repo: Path,
    manifest: dict[str, dict[str, str]],
) -> Any:
    with (
        patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
        patch("perfectvoice_engine.separate._separator_cls", return_value=FakeSeparator),
        patch(
            "perfectvoice_engine.separate._to_model_input",
            side_effect=lambda arr: np.array(arr, dtype=np.float32, copy=True),
        ),
        patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
        patch("perfectvoice_engine.separate.WINDOW_SECONDS", _TEST_WINDOW_S),
        patch("perfectvoice_engine.separate.WINDOW_OVERLAP_SECONDS", _TEST_OVERLAP_S),
    ):
        return separate_vocals(req, repo)


class WindowPlanTests(unittest.TestCase):
    def test_short_clip_is_single_window(self) -> None:
        sr = MODEL_SAMPLE_RATE
        n = int(WINDOW_SECONDS * sr)
        self.assertEqual(window_slices(n, sr), [(0, n)])
        self.assertFalse(should_window(n, 2, sr))
        self.assertFalse(should_window(n - 1, 2, sr))

    def test_long_clip_uses_10_min_windows_1s_overlap(self) -> None:
        sr = MODEL_SAMPLE_RATE
        n = int((2 * WINDOW_SECONDS + 3.0) * sr)
        slices = window_slices(n, sr)
        self.assertGreaterEqual(len(slices), 3)
        window, overlap = window_hop_samples(sr)
        self.assertEqual(window, int(round(WINDOW_SECONDS * sr)))
        self.assertEqual(overlap, int(round(WINDOW_OVERLAP_SECONDS * sr)))
        self.assertEqual(slices[0], (0, window))
        self.assertEqual(slices[1][0], window - overlap)
        self.assertEqual(slices[1][1] - slices[1][0], window)
        self.assertEqual(slices[-1][1], n)
        self.assertTrue(should_window(n, 2, sr))

    def test_memory_cap_is_two_gib(self) -> None:
        self.assertEqual(MEMORY_CAP_BYTES, 2 * 1024 ** 3)
        # duration * rate * ch * 4 > 2 GiB. Do not allocate the buffer.
        ch = 2
        n_over = MEMORY_CAP_BYTES // (ch * 4) + 1
        n_under = MEMORY_CAP_BYTES // (ch * 4)
        self.assertTrue(exceeds_memory_cap(n_over, ch))
        self.assertFalse(exceeds_memory_cap(n_under, ch))
        self.assertGreater(pcm_nbytes(n_over, ch), MEMORY_CAP_BYTES)
        # Cap forces the windowed path even if the 10-min check would not.
        self.assertTrue(should_window(n_over, ch, MODEL_SAMPLE_RATE, window_s=10_000.0))


class OverlapAddSineTests(unittest.TestCase):
    def test_sine_boundary_discontinuity_below_minus_80_dbfs(self) -> None:
        sr = MODEL_SAMPLE_RATE
        n = int(1.1 * sr)
        wav = _sine(n, sr)
        slices = window_slices(n, sr, window_s=_TEST_WINDOW_S, overlap_s=_TEST_OVERLAP_S)
        self.assertGreaterEqual(len(slices), 3)
        _window, overlap = window_hop_samples(
            sr, window_s=_TEST_WINDOW_S, overlap_s=_TEST_OVERLAP_S
        )
        chunks = [wav[:, start:end] * _MOCK_GAIN for start, end in slices]
        out = overlap_add(chunks, slices, n, overlap)
        expected = wav * _MOCK_GAIN
        self.assertEqual(out.shape, expected.shape)
        boundaries = [start for start, _end in slices[1:]]
        db = _discontinuity_dbfs(out, expected, boundaries)
        self.assertLess(
            db,
            _DISC_LIMIT_DBFS,
            f"OLA splice discontinuity {db:.1f} dBFS (limit {_DISC_LIMIT_DBFS})",
        )
        peak_err = float(np.max(np.abs(out - expected)))
        self.assertLess(20.0 * math.log10(max(peak_err, 1e-20)), _DISC_LIMIT_DBFS)

    def test_ola_is_c0_when_adjacent_stems_disagree(self) -> None:
        sr = MODEL_SAMPLE_RATE
        n = int(1.1 * sr)
        wav = _sine(n, sr)
        slices = window_slices(n, sr, window_s=_TEST_WINDOW_S, overlap_s=_TEST_OVERLAP_S)
        _window, overlap = window_hop_samples(
            sr, window_s=_TEST_WINDOW_S, overlap_s=_TEST_OVERLAP_S
        )
        chunks = []
        for i, (start, end) in enumerate(slices):
            piece = wav[:, start:end] * _MOCK_GAIN
            if i % 2 == 1:
                piece = piece + _ODD_DC
            chunks.append(piece)
        out = overlap_add(chunks, slices, n, overlap)
        leftover = out - wav * _MOCK_GAIN
        # Common sine removed: remainder must be a slow DC ramp, not a step.
        leftover_step = float(np.max(np.abs(np.diff(leftover, axis=-1))))
        self.assertLess(
            leftover_step,
            _ODD_DC * 0.1,
            f"OLA not C0 when stems disagree: leftover step {leftover_step}",
        )
        expected = overlap_add(chunks, slices, n, overlap)
        db = _discontinuity_dbfs(out, expected, [s[0] for s in slices[1:]])
        self.assertLess(db, _DISC_LIMIT_DBFS)
        # A hard cut at the first join would jump by ~DC (well above -80 dBFS).
        hard_db = 20.0 * math.log10(_ODD_DC)
        self.assertGreater(hard_db, _DISC_LIMIT_DBFS)


class SeparateWindowedTests(unittest.TestCase):
    def test_mocked_separate_sine_ola_under_minus_80_dbfs(self) -> None:
        FakeSeparator.instances.clear()
        sr = MODEL_SAMPLE_RATE
        n = int(1.1 * sr)
        wav = _sine(n, sr)
        req = SeparateRequest(wav_44100_stereo=wav, model=DEFAULT_MODEL, device="cpu")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manifest = _write_htdemucs_fixture(repo, th_payload=b"ola")
            result = _separate_mocked(req, repo, manifest)
        self.assertEqual(len(FakeSeparator.instances), 1)
        sep = FakeSeparator.instances[0]
        self.assertGreaterEqual(sep.separate_calls, 3)
        self.assertTrue(all(length <= int(_TEST_WINDOW_S * sr) + 1 for length in sep.lengths))
        expected = wav * _MOCK_GAIN
        self.assertEqual(result.vocals.shape, expected.shape)
        slices = window_slices(n, sr, window_s=_TEST_WINDOW_S, overlap_s=_TEST_OVERLAP_S)
        db = _discontinuity_dbfs(result.vocals, expected, [s[0] for s in slices[1:]])
        self.assertLess(
            db,
            _DISC_LIMIT_DBFS,
            f"windowed separate splice {db:.1f} dBFS (limit {_DISC_LIMIT_DBFS})",
        )

    def test_cancel_raises(self) -> None:
        FakeSeparator.instances.clear()
        event = threading.Event()
        event.set()
        wav = _sine(int(1.1 * MODEL_SAMPLE_RATE))
        req = SeparateRequest(
            wav_44100_stereo=wav,
            model=DEFAULT_MODEL,
            device="cpu",
            cancel_event=event,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manifest = _write_htdemucs_fixture(repo, th_payload=b"cancel")
            with self.assertRaises(JobCancelled):
                _separate_mocked(req, repo, manifest)
        # Separator is constructed (callback wired) but no window ran.
        self.assertEqual(len(FakeSeparator.instances), 1)
        self.assertEqual(FakeSeparator.instances[0].separate_calls, 0)

    def test_cancel_callback_raises_mid_job(self) -> None:
        event = threading.Event()
        raise_if_cancelled(None)
        with self.assertRaises(JobCancelled):
            raise_if_cancelled(event.set() or event)
        called = {"n": 0}

        def flag() -> bool:
            called["n"] += 1
            return True

        with self.assertRaises(JobCancelled):
            raise_if_cancelled(flag)
        self.assertEqual(called["n"], 1)

    def test_cancel_after_first_window_raises(self) -> None:
        FakeSeparator.instances.clear()
        event = threading.Event()

        class CancelAfterFirst(FakeSeparator):
            def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
                out = super().separate_tensor(wav, sr)
                event.set()
                return out

        wav = _sine(int(1.1 * MODEL_SAMPLE_RATE))
        req = SeparateRequest(
            wav_44100_stereo=wav,
            model=DEFAULT_MODEL,
            device="cpu",
            cancel_event=event,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manifest = _write_htdemucs_fixture(repo, th_payload=b"mid")
            with (
                patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
                patch("perfectvoice_engine.separate._separator_cls", return_value=CancelAfterFirst),
                patch(
                    "perfectvoice_engine.separate._to_model_input",
                    side_effect=lambda arr: np.array(arr, dtype=np.float32, copy=True),
                ),
                patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
                patch("perfectvoice_engine.separate.WINDOW_SECONDS", _TEST_WINDOW_S),
                patch("perfectvoice_engine.separate.WINDOW_OVERLAP_SECONDS", _TEST_OVERLAP_S),
            ):
                with self.assertRaises(JobCancelled):
                    separate_vocals(req, repo)
        self.assertEqual(FakeSeparator.instances[0].separate_calls, 1)

    def test_cancel_raises_from_demucs_callback_mid_window(self) -> None:
        FakeSeparator.instances.clear()
        event = threading.Event()

        class SetThenCall(FakeSeparator):
            def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
                event.set()
                return super().separate_tensor(wav, sr)

        wav = _sine(int(1.1 * MODEL_SAMPLE_RATE))
        req = SeparateRequest(
            wav_44100_stereo=wav,
            model=DEFAULT_MODEL,
            device="cpu",
            cancel_event=event,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manifest = _write_htdemucs_fixture(repo, th_payload=b"cb")
            with (
                patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
                patch("perfectvoice_engine.separate._separator_cls", return_value=SetThenCall),
                patch(
                    "perfectvoice_engine.separate._to_model_input",
                    side_effect=lambda arr: np.array(arr, dtype=np.float32, copy=True),
                ),
                patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
                patch("perfectvoice_engine.separate.WINDOW_SECONDS", _TEST_WINDOW_S),
                patch("perfectvoice_engine.separate.WINDOW_OVERLAP_SECONDS", _TEST_OVERLAP_S),
            ):
                with self.assertRaises(JobCancelled):
                    separate_vocals(req, repo)
        self.assertEqual(FakeSeparator.instances[0].separate_calls, 1)

    def test_mocked_separate_c0_when_stems_disagree(self) -> None:
        FakeSeparator.instances.clear()

        class Disagreeing(FakeSeparator):
            def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
                mix, stems = super().separate_tensor(wav, sr)
                vocals = np.asarray(stems["vocals"], dtype=np.float32)
                if self.separate_calls % 2 == 0:
                    vocals = vocals + _ODD_DC
                stems = dict(stems)
                stems["vocals"] = vocals
                return mix, stems

        sr = MODEL_SAMPLE_RATE
        n = int(1.1 * sr)
        wav = _sine(n, sr)
        req = SeparateRequest(wav_44100_stereo=wav, model=DEFAULT_MODEL, device="cpu")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manifest = _write_htdemucs_fixture(repo, th_payload=b"dc")
            with (
                patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
                patch("perfectvoice_engine.separate._separator_cls", return_value=Disagreeing),
                patch(
                    "perfectvoice_engine.separate._to_model_input",
                    side_effect=lambda arr: np.array(arr, dtype=np.float32, copy=True),
                ),
                patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
                patch("perfectvoice_engine.separate.WINDOW_SECONDS", _TEST_WINDOW_S),
                patch("perfectvoice_engine.separate.WINDOW_OVERLAP_SECONDS", _TEST_OVERLAP_S),
            ):
                result = separate_vocals(req, repo)
        leftover = result.vocals - wav * _MOCK_GAIN
        leftover_step = float(np.max(np.abs(np.diff(leftover, axis=-1))))
        self.assertLess(leftover_step, _ODD_DC * 0.1)

    def test_window_slice_is_copied_before_demucs(self) -> None:
        FakeSeparator.instances.clear()

        class Mutating(FakeSeparator):
            def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
                arr = np.asarray(wav)
                arr *= 0.0
                return super().separate_tensor(arr, sr)

        sr = MODEL_SAMPLE_RATE
        n = int(1.1 * sr)
        wav = _sine(n, sr)
        original = wav.copy()
        req = SeparateRequest(wav_44100_stereo=wav, model=DEFAULT_MODEL, device="cpu")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manifest = _write_htdemucs_fixture(repo, th_payload=b"alias")
            with (
                patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
                patch("perfectvoice_engine.separate._separator_cls", return_value=Mutating),
                patch(
                    "perfectvoice_engine.separate._to_model_input",
                    side_effect=lambda arr: np.array(arr, dtype=np.float32, copy=True),
                ),
                patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
                patch("perfectvoice_engine.separate.WINDOW_SECONDS", _TEST_WINDOW_S),
                patch("perfectvoice_engine.separate.WINDOW_OVERLAP_SECONDS", _TEST_OVERLAP_S),
            ):
                separate_vocals(req, repo)
        np.testing.assert_array_equal(wav, original)

    def test_to_model_input_copies_parent_view(self) -> None:
        wav = _sine(200)
        view = wav[:, 20:80]
        sentinel = float(view[0, 0])
        with patch(
            "perfectvoice_engine.separate.importlib.import_module",
            side_effect=ImportError("no torch"),
        ):
            owned = _to_model_input(view)
        np.asarray(owned)[:] = 0
        self.assertAlmostEqual(float(view[0, 0]), sentinel)
        self.assertGreater(float(np.max(np.abs(view))), 0.0)

    def test_cap_path_windows_without_allocating_2gib(self) -> None:
        FakeSeparator.instances.clear()
        # Tiny cap forces the windowed path on a short sine — no 2 GiB buffer.
        sr = MODEL_SAMPLE_RATE
        n = int(0.4 * sr)
        wav = _sine(n, sr)
        req = SeparateRequest(wav_44100_stereo=wav, model=DEFAULT_MODEL, device="cpu")
        cap = pcm_nbytes(n, 2) - 1
        self.assertTrue(exceeds_memory_cap(n, 2, cap=cap))
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manifest = _write_htdemucs_fixture(repo, th_payload=b"cap")
            with (
                patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
                patch("perfectvoice_engine.separate._separator_cls", return_value=FakeSeparator),
                patch(
                    "perfectvoice_engine.separate._to_model_input",
                    side_effect=lambda arr: np.array(arr, dtype=np.float32, copy=True),
                ),
                patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
                patch("perfectvoice_engine.separate.WINDOW_SECONDS", _TEST_WINDOW_S),
                patch("perfectvoice_engine.separate.WINDOW_OVERLAP_SECONDS", _TEST_OVERLAP_S),
                patch("perfectvoice_engine.separate.MEMORY_CAP_BYTES", cap),
            ):
                result = separate_vocals(req, repo)
        self.assertGreaterEqual(FakeSeparator.instances[0].separate_calls, 2)
        expected = wav * _MOCK_GAIN
        db = _discontinuity_dbfs(
            result.vocals,
            expected,
            [
                s[0]
                for s in window_slices(n, sr, window_s=_TEST_WINDOW_S, overlap_s=_TEST_OVERLAP_S)[1:]
            ],
        )
        self.assertLess(db, _DISC_LIMIT_DBFS)


if __name__ == "__main__":
    unittest.main()
