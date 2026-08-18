"""Sample-accurate extract + soxr_hq_v1 length tests (no Resolve, no torch).

Run: python3 -m unittest tests.unit.test_resample_sync
"""

from __future__ import annotations

import ast
import math
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine.ffmpeg_io import (  # noqa: E402
    BWF_ORIGINATOR,
    FFmpegError,
    UnsupportedChannelLayout,
    actual_handles,
    decode_f32,
    expected_output_sample_count,
    extract_sample_range,
    extract_with_handles,
    ffmpeg_bin,
    inspect_wav,
    probe_audio,
    reject_if_multichannel,
    round_half_up,
)
from perfectvoice_engine.resample import (  # noqa: E402
    MODEL_SAMPLE_RATE,
    RESAMPLER_ID,
    resample_array,
    resample_wav,
    resampled_sample_count,
    to_model_rate,
    to_project_rate,
)


def _have_ffmpeg() -> bool:
    try:
        ffmpeg_bin()
        return True
    except FFmpegError:
        return False


def _run_ffmpeg(args: list[str]) -> None:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", *args]
    subprocess.run(cmd, check=True, capture_output=True, stdin=subprocess.DEVNULL)


def _write_pcm16_wav(
    path: Path,
    *,
    sample_rate: int,
    channels: int,
    frames: bytes,
) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)


def _sine_pcm16(seconds: float, sample_rate: int, channels: int, freq: float = 440.0) -> bytes:
    n = int(round(seconds * sample_rate))
    out = bytearray()
    for i in range(n):
        sample = int(16000 * math.sin(2 * math.pi * freq * i / sample_rate))
        frame = struct.pack("<h", sample) * channels
        out.extend(frame)
    return bytes(out)


@unittest.skipUnless(_have_ffmpeg(), "ffmpeg not installed")
class ExtractHandlesTests(unittest.TestCase):
    def test_t0_lt_h_clamps_left_and_length(self) -> None:
        # 2 s 48 kHz stereo sine; t0=0.2 t1=1.2 H=0.5 → H_left_actual=0.2
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_48k.wav"
            dest = Path(tmp) / "extract.wav"
            _run_ffmpeg(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2:sample_rate=48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s24le",
                    str(src),
                ]
            )
            result = extract_with_handles(
                src, dest, t0=0.2, t1=1.2, handle_s=0.5, sample_format="pcm24"
            )
            self.assertAlmostEqual(result.extract.h_left_actual, 0.2)
            self.assertAlmostEqual(result.extract.h_right_actual, 0.5)
            self.assertEqual(result.extract.src_in_sample, 0)
            self.assertEqual(result.extract.src_out_sample, round_half_up((1.2 + 0.5) * 48000))
            # 1.7 s @ 48 k, not 2.0 s of "full" handles
            self.assertEqual(result.sample_count, 81600)
            self.assertEqual(result.sample_rate, 48000)
            self.assertEqual(result.channels, 2)
            self.assertEqual(result.sample_format, "pcm24")
            info = inspect_wav(dest)
            self.assertEqual(info.originator, BWF_ORIGINATOR)
            self.assertEqual(info.sample_count, 81600)

    def test_impulse_stays_sample_accurate(self) -> None:
        sr = 48000
        n = sr * 2
        impulse_at = sr  # t = 1.0 s
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "impulse.wav"
            dest = Path(tmp) / "cut.wav"
            frames = bytearray(n * 2 * 2)
            struct.pack_into("<hh", frames, impulse_at * 4, 20000, 20000)
            _write_pcm16_wav(src, sample_rate=sr, channels=2, frames=bytes(frames))
            extract_with_handles(
                src, dest, t0=0.2, t1=1.2, handle_s=0.5, sample_format="float32"
            )
            audio, probe = decode_f32(dest)
            self.assertEqual(probe.sample_rate, sr)
            peak = int(abs(audio).sum(axis=1).argmax())
            # extract starts at sample 0, so the t=1.0 impulse is still at 48000
            self.assertEqual(peak, impulse_at)

    def test_float32_bwf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.wav"
            dest = Path(tmp) / "f32.wav"
            _write_pcm16_wav(
                src,
                sample_rate=48000,
                channels=2,
                frames=_sine_pcm16(0.5, 48000, 2),
            )
            result = extract_with_handles(
                src, dest, t0=0.1, t1=0.3, handle_s=0.05, sample_format="float32"
            )
            self.assertEqual(result.sample_format, "float32")
            self.assertEqual(inspect_wav(dest).originator, BWF_ORIGINATOR)


class RejectMultichannelTests(unittest.TestCase):
    def test_reject_if_multichannel_without_ffmpeg(self) -> None:
        with self.assertRaises(UnsupportedChannelLayout):
            reject_if_multichannel(6)
        reject_if_multichannel(1)
        reject_if_multichannel(2)

    @unittest.skipUnless(_have_ffmpeg(), "ffmpeg not installed")
    def test_six_channel_source_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "sixch.wav"
            dest = Path(tmp) / "out.wav"
            _write_pcm16_wav(
                src,
                sample_rate=48000,
                channels=6,
                frames=_sine_pcm16(0.2, 48000, 6),
            )
            probe = probe_audio(src)
            self.assertEqual(probe.channels, 6)
            with self.assertRaises(UnsupportedChannelLayout):
                extract_with_handles(src, dest, t0=0.0, t1=0.2, handle_s=0.0)


class AppendixAMathTests(unittest.TestCase):
    def test_round_half_up_not_bankers(self) -> None:
        self.assertEqual(round_half_up(0.5), 1)
        self.assertEqual(round_half_up(2.5), 3)
        self.assertEqual(round(2.5), 2)

    def test_required_sof_fixture(self) -> None:
        h_left, h_right = actual_handles(0.2, 1.2, 2.0, 0.5)
        self.assertAlmostEqual(h_left, 0.2)
        self.assertAlmostEqual(h_right, 0.5)
        ext = extract_sample_range(0.2, 1.2, 2.0, 48000, 0.5)
        self.assertEqual(ext.src_in_sample, 0)
        self.assertEqual(ext.src_sample_count, 81600)
        n_out = expected_output_sample_count(0.2, 1.2, 2.0, 48000, 0.5)
        self.assertLessEqual(abs(n_out - round((1.2 - 0.2 + 0.2 + 0.5) * 48000)), 1)
        self.assertEqual(n_out, 81600)
        naive = round_half_up((1.2 - 0.2 + 0.5 + 0.5) * 48000)
        self.assertNotEqual(n_out, naive)


class ResampleLengthTests(unittest.TestCase):
    def test_resampler_id_pin(self) -> None:
        self.assertEqual(RESAMPLER_ID, "soxr_hq_v1")
        self.assertEqual(MODEL_SAMPLE_RATE, 44100)

    def test_roundtrip_1s_48k_441_48k(self) -> None:
        import numpy as np

        sr = 48000
        n = sr  # 1 s
        t = np.arange(n, dtype=np.float32) / sr
        x = np.stack(
            [0.25 * np.sin(2 * np.pi * 440 * t), 0.25 * np.sin(2 * np.pi * 660 * t)],
            axis=1,
        )
        mid = resample_array(x, 48000, 44100)
        back = resample_array(mid, 44100, 48000)
        self.assertLessEqual(abs(back.shape[0] - n), 1)
        self.assertEqual(mid.shape[1], 2)
        self.assertEqual(to_model_rate(x, 48000).shape, (mid.shape[0], 2))

    def test_441_to_48_and_96(self) -> None:
        import numpy as np

        n = 44100
        x = np.zeros((n, 2), dtype=np.float32)
        x[0, 0] = 0.5
        y48 = to_project_rate(x, 44100, 48000)
        y96 = to_project_rate(x, 44100, 96000)
        self.assertLessEqual(abs(y48.shape[0] - 48000), 1)
        self.assertLessEqual(abs(y96.shape[0] - 96000), 1)

    def test_longer_10s_length(self) -> None:
        n_in = 48000 * 10
        n_out = resampled_sample_count(n_in, 48000, 44100)
        expected = round(n_in * 44100 / 48000)
        self.assertLessEqual(abs(n_out - expected), 1)

    def test_one_hour_length_error_le_one_sample(self) -> None:
        # Stream zeros so this does not allocate 1 hour of audio.
        n_in = 48000 * 3600
        n_out = resampled_sample_count(n_in, 48000, 44100, chunk_frames=48000 * 10)
        expected = round(n_in * 44100 / 48000)
        self.assertLessEqual(abs(n_out - expected), 1)
        n_back = resampled_sample_count(n_out, 44100, 48000, chunk_frames=44100 * 10)
        self.assertLessEqual(abs(n_back - n_in), 1)

    def test_extract_then_project_rate_matches_n_out(self) -> None:
        import numpy as np

        t0, t1, file_dur, h, src_sr, proj_sr = 0.2, 1.2, 2.0, 0.5, 48000, 48000
        n_src = extract_sample_range(t0, t1, file_dur, src_sr, h).src_sample_count
        x = np.zeros((n_src, 2), dtype=np.float32)
        y = to_project_rate(x, src_sr, proj_sr)
        expected = expected_output_sample_count(t0, t1, file_dur, proj_sr, h)
        self.assertLessEqual(abs(y.shape[0] - expected), 1)
        self.assertLessEqual(abs(y.shape[0] - round((t1 - t0 + 0.2 + 0.5) * proj_sr)), 1)


@unittest.skipUnless(_have_ffmpeg(), "ffmpeg not installed")
class ResampleWavTests(unittest.TestCase):
    def test_wav_48k_to_441_to_48k(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "one.wav"
            mid = Path(tmp) / "m.wav"
            back = Path(tmp) / "back.wav"
            _run_ffmpeg(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1:sample_rate=48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s24le",
                    str(src),
                ]
            )
            r1 = resample_wav(src, mid, 44100, sample_format="float32")
            r2 = resample_wav(mid, back, 48000, sample_format="pcm24")
            self.assertEqual(r1.sample_format, "float32")
            self.assertEqual(r2.sample_format, "pcm24")
            self.assertLessEqual(abs(r2.sample_count - 48000), 1)
            self.assertEqual(inspect_wav(back).originator, BWF_ORIGINATOR)


class NoTorchDemucsImportTests(unittest.TestCase):
    def test_engine_modules_do_not_import_torch_or_demucs(self) -> None:
        banned = {"torch", "torchaudio", "demucs"}
        engine_root = ENGINE_DIR / "perfectvoice_engine"
        for path in sorted(engine_root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            # Module body only — enhance/separate lazy-import torch inside functions.
            for node in tree.body:
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    top = name.split(".", 1)[0]
                    self.assertNotIn(
                        top,
                        banned,
                        f"{path.name} imports {name}",
                    )
        for name in banned:
            self.assertNotIn(name, sys.modules)


if __name__ == "__main__":
    unittest.main()
