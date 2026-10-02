"""Unit tests for Target Speaker Extraction (TSE).

Torch-free: gain mapping, envelope, gating with an injected encoder,
speaker store versioning, fail-closed weight checks, cache digest.
Torch: ECAPA layout vs the SpeechBrain checkpoint, numerical parity with
SpeechBrain (when installed), embeddings, the enroll endpoint.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

STATE_DICT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ecapa_voxceleb_state_dict.json"


def _has_torch() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def _has_speechbrain() -> bool:
    try:
        import speechbrain.lobes.models.ECAPA_TDNN  # noqa: F401
        return True
    except Exception:
        return False


def _unit(v: np.ndarray) -> np.ndarray:
    return (v / np.linalg.norm(v)).astype(np.float32)


class _AppSupportCase(unittest.TestCase):
    """Isolates PERFECTVOICE_APP_SUPPORT / USER_DIR and the encoder cache."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-tse-"))
        self._env = patch.dict(
            os.environ,
            {
                "PERFECTVOICE_APP_SUPPORT": str(self.tmp / "app"),
                "PERFECTVOICE_USER_DIR": str(self.tmp / "user"),
            },
        )
        self._env.start()
        self._clear_encoder_cache()

    def tearDown(self) -> None:
        self._clear_encoder_cache()
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clear_encoder_cache() -> None:
        mod = sys.modules.get("perfectvoice_engine.tse.encoder")
        if mod is not None:
            mod._ENCODERS.clear()

    def install_random_checkpoint(self, seed: int = 0) -> None:
        import torch
        from perfectvoice_engine.tse.encoder import ECAPAEncoder
        from perfectvoice_engine.tse.weights import ecapa_checkpoint_path

        torch.manual_seed(seed)
        model = ECAPAEncoder()
        for m in model.modules():
            if isinstance(m, torch.nn.BatchNorm1d):
                m.running_mean.uniform_(-0.5, 0.5)
                m.running_var.uniform_(0.5, 2.0)
        path = ecapa_checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(model.state_dict(), path)


class GainMappingTests(unittest.TestCase):
    def test_similarity_to_gain_endpoints(self) -> None:
        from perfectvoice_engine.tse import similarity_to_gain

        g = similarity_to_gain(np.array([-1.0, 0.2, 0.325, 0.45, 1.0]), low=0.2, high=0.45, min_gain=0.001)
        self.assertAlmostEqual(float(g[0]), 0.001, places=6)
        self.assertAlmostEqual(float(g[1]), 0.001, places=6)
        self.assertTrue(0.001 < float(g[2]) < 1.0)
        self.assertAlmostEqual(float(g[3]), 1.0, places=6)
        self.assertAlmostEqual(float(g[4]), 1.0, places=6)

    def test_inverted_thresholds_rejected(self) -> None:
        from perfectvoice_engine.tse import similarity_to_gain

        with self.assertRaises(ValueError):
            similarity_to_gain(0.3, low=0.5, high=0.4)

    def test_envelope_holds_full_gain_at_edges(self) -> None:
        from perfectvoice_engine.tse import gain_envelope

        env = gain_envelope(np.array([100.0, 900.0]), np.array([1.0, 1.0]), 1000, 1000, 0.001)
        self.assertEqual(env.shape, (1000,))
        np.testing.assert_allclose(env, 1.0, atol=1e-6)


class GatingTests(unittest.TestCase):
    """extract_target_speaker with an injected encoder (no torch)."""

    SR = 16000

    def setUp(self) -> None:
        rng = np.random.RandomState(7)
        self.target = _unit(rng.randn(192))
        other = rng.randn(192)
        other -= other.dot(self.target) * self.target
        self.other = _unit(other)

    def _fake_embed(self, segs: np.ndarray) -> np.ndarray:
        # Speaker identity by sign of the segment mean: >0 target, <0 other.
        return np.stack([self.target if s.mean() > 0 else self.other for s in segs])

    def test_keeps_target_and_suppresses_other(self) -> None:
        from perfectvoice_engine.tse import extract_target_speaker

        sr = self.SR
        half = 3 * sr
        rng = np.random.RandomState(0)
        target_part = 0.2 + 0.05 * rng.randn(half)
        other_part = -0.2 + 0.05 * rng.randn(half)
        audio = np.concatenate([target_part, other_part]).astype(np.float32)[np.newaxis, :]
        events: list[dict] = []
        out = extract_target_speaker(
            audio,
            self.target,
            sample_rate=sr,
            embed_fn=self._fake_embed,
            on_progress=events.append,
        )
        self.assertEqual(out.shape, audio.shape)
        self.assertEqual(out.dtype, np.float32)
        mid_target = slice(int(0.5 * sr), int(2.0 * sr))
        mid_other = slice(int(4.0 * sr), int(5.5 * sr))
        np.testing.assert_allclose(out[0, mid_target], audio[0, mid_target], rtol=1e-5)
        self.assertLess(np.abs(out[0, mid_other]).max(), 1e-2 * np.abs(audio[0, mid_other]).max())
        self.assertEqual(events[-1]["chunk_pct"], 100.0)
        self.assertEqual(events[-1]["overall_pct"], 100.0)

    def test_silence_is_floored_without_embedding(self) -> None:
        from perfectvoice_engine.tse import extract_target_speaker

        calls: list[int] = []

        def embed(segs: np.ndarray) -> np.ndarray:
            calls.append(len(segs))
            return self._fake_embed(segs)

        audio = np.zeros((2, self.SR * 2), dtype=np.float32)
        out = extract_target_speaker(audio, self.target, sample_rate=self.SR, embed_fn=embed)
        self.assertEqual(out.shape, audio.shape)
        self.assertEqual(calls, [])

    def test_short_clip_and_other_rate(self) -> None:
        from perfectvoice_engine.tse import extract_target_speaker

        sr = 44100
        audio = (0.3 + 0.01 * np.random.RandomState(1).randn(2, sr // 4)).astype(np.float32)
        out = extract_target_speaker(audio, self.target, sample_rate=sr, embed_fn=self._fake_embed)
        self.assertEqual(out.shape, audio.shape)
        self.assertFalse(np.isnan(out).any())

    def test_cancel_is_honoured(self) -> None:
        from perfectvoice_engine.constants import JobCancelled
        from perfectvoice_engine.tse import extract_target_speaker

        audio = np.ones((1, self.SR), dtype=np.float32) * 0.1
        with self.assertRaises(JobCancelled):
            extract_target_speaker(audio, self.target, sample_rate=self.SR, embed_fn=self._fake_embed, cancel_event=lambda: True)


class WeightsFailClosedTests(_AppSupportCase):
    def test_missing_checkpoint_not_ready(self) -> None:
        from perfectvoice_engine.tse.weights import SpeakerEncoderNotInstalled, is_ecapa_ready, require_ecapa

        self.assertFalse(is_ecapa_ready())
        with self.assertRaises(SpeakerEncoderNotInstalled):
            require_ecapa()

    def test_truncated_checkpoint_not_ready(self) -> None:
        from perfectvoice_engine.tse.weights import ecapa_checkpoint_path, is_ecapa_ready

        path = ecapa_checkpoint_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\0" * 1024)
        self.assertFalse(is_ecapa_ready())

    def test_job_gate_requires_encoder_for_tse(self) -> None:
        from perfectvoice_engine.serve import _require_local_model
        from perfectvoice_engine.tse.weights import SpeakerEncoderNotInstalled

        with self.assertRaises(SpeakerEncoderNotInstalled):
            _require_local_model({"model": "htdemucs", "mode": "tse"})

    def test_extractor_fails_closed_without_weights(self) -> None:
        if not _has_torch():
            self.skipTest("PyTorch required")
        from perfectvoice_engine.tse import SpeakerEncoderNotInstalled, extract_target_speaker

        audio = np.ones((1, 16000), dtype=np.float32) * 0.1
        with self.assertRaises(SpeakerEncoderNotInstalled):
            extract_target_speaker(audio, np.ones(192, dtype=np.float32), sample_rate=16000, device="cpu")


class SpeakerStoreTests(_AppSupportCase):
    def test_enroll_list_get_delete(self) -> None:
        from perfectvoice_engine.tse import SpeakerStore

        store_file = self.tmp / "speakers.json"
        store = SpeakerStore(store_file)
        self.assertEqual(len(store.list_all()), 0)

        vec_a = _unit(np.random.randn(192))
        p_a = store.enroll("Host", vec_a, sample_duration_s=3.2)
        self.assertTrue(p_a.speaker_id.startswith("spk_"))
        self.assertEqual(p_a.name, "Host")
        self.assertEqual(p_a.sample_duration_s, 3.2)
        store.enroll("Guest", np.random.randn(192).astype(np.float32), sample_duration_s=2.5)
        self.assertEqual(len(store.list_all()), 2)

        found = store.get(p_a.speaker_id)
        self.assertIsNotNone(found)
        np.testing.assert_allclose(found.to_numpy(), vec_a, rtol=1e-5)

        store2 = SpeakerStore(store_file)
        self.assertEqual(len(store2.list_all()), 2)
        self.assertTrue(store2.delete(p_a.speaker_id))
        self.assertEqual(len(store2.list_all()), 1)
        self.assertIsNone(store2.get(p_a.speaker_id))

    def test_profiles_from_other_encoder_are_dropped(self) -> None:
        from perfectvoice_engine.tse import ENCODER_ID, SpeakerStore

        store_file = self.tmp / "speakers.json"
        legacy = {"speaker_id": "spk_old", "name": "Old", "embedding": [0.0] * 192, "created_at": ""}
        current = dict(legacy, speaker_id="spk_new", name="New", encoder_id=ENCODER_ID)
        store_file.write_text(json.dumps({"version": 1, "speakers": [legacy, current]}), encoding="utf-8")
        store = SpeakerStore(store_file)
        self.assertIsNone(store.get("spk_old"))
        self.assertIsNotNone(store.get("spk_new"))


class CacheDigestTests(_AppSupportCase):
    def test_music_mode_digest_unchanged(self) -> None:
        from perfectvoice_engine.pipeline import tse_weights_digest

        self.assertEqual(tse_weights_digest({"mode": "music"}, "abc"), "abc")
        self.assertEqual(tse_weights_digest({}, "abc"), "abc")

    def test_tse_digest_tracks_speaker_and_reference(self) -> None:
        from perfectvoice_engine.pipeline import tse_weights_digest
        from perfectvoice_engine.tse import SpeakerStore

        base = {"mode": "tse", "ref_sample_t0": 1.0, "ref_sample_t1": 3.5}
        d0 = tse_weights_digest(base, "abc")
        self.assertNotEqual(d0, "abc")
        self.assertEqual(d0, tse_weights_digest(dict(base), "abc"))
        self.assertNotEqual(d0, tse_weights_digest(dict(base, ref_sample_t0=2.0), "abc"))
        self.assertNotEqual(d0, tse_weights_digest(base, "abd"))

        store = SpeakerStore()
        a = store.enroll("A", _unit(np.random.RandomState(1).randn(192)))
        b = store.enroll("B", _unit(np.random.RandomState(2).randn(192)))
        da = tse_weights_digest(dict(base, speaker_id=a.speaker_id), "abc")
        db = tse_weights_digest(dict(base, speaker_id=b.speaker_id), "abc")
        self.assertNotEqual(da, db)
        self.assertNotEqual(da, d0)


class EncoderLayoutTests(unittest.TestCase):
    def test_state_dict_matches_speechbrain_checkpoint(self) -> None:
        if not _has_torch():
            self.skipTest("PyTorch required")
        from perfectvoice_engine.tse.encoder import ECAPAEncoder

        expected = json.loads(STATE_DICT_FIXTURE.read_text(encoding="utf-8"))["tensors"]
        actual = {k: list(v.shape) for k, v in ECAPAEncoder().state_dict().items()}
        self.assertEqual(sorted(actual), sorted(expected))
        self.assertEqual(actual, expected)


class SpeechBrainParityTests(_AppSupportCase):
    def test_fbank_and_embedding_match_speechbrain(self) -> None:
        if not (_has_torch() and _has_speechbrain()):
            self.skipTest("PyTorch + speechbrain required")
        import torch
        from speechbrain.lobes.features import Fbank as SBFbank
        from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
        from speechbrain.processing.features import InputNormalization
        from perfectvoice_engine.tse.encoder import get_speaker_encoder
        from perfectvoice_engine.tse.weights import ecapa_checkpoint_path

        torch.manual_seed(0)
        sb = ECAPA_TDNN(
            80,
            channels=[1024, 1024, 1024, 1024, 3072],
            kernel_sizes=[5, 3, 3, 3, 1],
            dilations=[1, 2, 3, 4, 1],
            attention_channels=128,
            lin_neurons=192,
        )
        for m in sb.modules():
            if isinstance(m, torch.nn.BatchNorm1d):
                m.running_mean.uniform_(-0.5, 0.5)
                m.running_var.uniform_(0.5, 2.0)
        sb.eval()
        path = ecapa_checkpoint_path()
        path.parent.mkdir(parents=True)
        torch.save(sb.state_dict(), path)

        fbank, mine = get_speaker_encoder("cpu")
        sb_fbank = SBFbank(n_mels=80)
        sb_norm = InputNormalization(norm_type="sentence", std_norm=False)
        wav = torch.from_numpy((np.random.RandomState(3).randn(2, 24000) * 0.1).astype(np.float32))
        with torch.no_grad():
            feats_sb = sb_norm(sb_fbank(wav), torch.ones(2))
            emb_sb = sb(feats_sb, torch.ones(2)).squeeze(1)
            feats_me = fbank(wav)
            emb_me = mine(feats_me)
        torch.testing.assert_close(feats_me, feats_sb, rtol=1e-5, atol=1e-4)
        torch.testing.assert_close(emb_me, emb_sb, rtol=1e-4, atol=1e-4)


class EncoderEmbeddingTests(_AppSupportCase):
    def setUp(self) -> None:
        super().setUp()
        if not _has_torch():
            self.skipTest("PyTorch required")
        self.install_random_checkpoint()

    def test_embedding_dim_norm_and_determinism(self) -> None:
        from perfectvoice_engine.tse import EMBEDDING_DIM, extract_embedding

        sr = 44100
        audio = np.random.RandomState(42).randn(2, sr * 2).astype(np.float32) * 0.1
        e1 = extract_embedding(audio, sample_rate=sr, device="cpu")
        e2 = extract_embedding(audio, sample_rate=sr, device="cpu")
        self.assertEqual(e1.shape, (EMBEDDING_DIM,))
        self.assertEqual(e1.dtype, np.float32)
        self.assertAlmostEqual(float(np.linalg.norm(e1)), 1.0, places=4)
        np.testing.assert_allclose(e1, e2, rtol=1e-5, atol=1e-6)

    def test_batch_matches_single(self) -> None:
        from perfectvoice_engine.tse.encoder import embed_batch_16k, extract_embedding

        segs = (np.random.RandomState(5).randn(3, 12000) * 0.1).astype(np.float32)
        batch = embed_batch_16k(segs, device="cpu")
        single = np.stack([extract_embedding(s, sample_rate=16000, device="cpu") for s in segs])
        np.testing.assert_allclose(batch, single, rtol=1e-4, atol=1e-5)

    def test_extractor_end_to_end_preserves_shape(self) -> None:
        from perfectvoice_engine.tse import extract_embedding, extract_target_speaker

        sr = 44100
        audio = (np.random.RandomState(42).randn(2, int(sr * 1.5)) * 0.1).astype(np.float32)
        embed = extract_embedding(audio, sample_rate=sr, device="cpu")
        out = extract_target_speaker(audio, embed, sample_rate=sr, device="cpu")
        self.assertEqual(out.shape, audio.shape)
        self.assertEqual(out.dtype, np.float32)
        self.assertTrue(np.isfinite(out).all())


class SidecarSpeakerHttpTests(_AppSupportCase):
    def _serve(self):
        from perfectvoice_engine.serve import EngineHTTPServer, JobStore
        import threading

        token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        server = EngineHTTPServer(("127.0.0.1", 0), token, JobStore(), idle_seconds=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1], {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _wav(self) -> Path:
        from perfectvoice_engine.ffmpeg_io import write_wav

        sr = 48000
        t = np.linspace(0, 3.0, sr * 3, dtype=np.float32)
        path = self.tmp / "sample.wav"
        write_wav(path, (0.5 * np.sin(2 * np.pi * 200 * t))[:, np.newaxis], sample_rate=sr)
        return path

    def _enroll(self, port: int, headers: dict, wav: Path):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30.0)
        body = json.dumps({"audio_path": str(wav), "name": "HostVoice", "t0": 0.0, "t1": 3.0})
        conn.request("POST", "/v1/speakers/enroll", body=body, headers=headers)
        res = conn.getresponse()
        return conn, res.status, json.loads(res.read().decode("utf-8"))

    def test_enroll_without_encoder_is_409(self) -> None:
        if not _has_torch():
            self.skipTest("PyTorch required")
        port, headers = self._serve()
        _conn, status, data = self._enroll(port, headers, self._wav())
        self.assertEqual(status, 409)
        self.assertEqual(data.get("error"), "model_not_installed")
        self.assertEqual(data.get("model"), "ecapa_voxceleb")

    def test_enroll_list_delete(self) -> None:
        if not _has_torch():
            self.skipTest("PyTorch required")
        self.install_random_checkpoint()
        port, headers = self._serve()
        conn, status, data = self._enroll(port, headers, self._wav())
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        spk_id = data["speaker"]["speaker_id"]
        self.assertTrue(spk_id.startswith("spk_"))

        conn.request("GET", "/v1/speakers", headers=headers)
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        listed = json.loads(res.read().decode("utf-8"))
        self.assertTrue(any(s["speaker_id"] == spk_id for s in listed.get("speakers", [])))

        conn.request("DELETE", f"/v1/speakers/{spk_id}", headers=headers)
        res = conn.getresponse()
        res.read()
        self.assertEqual(res.status, 200)


if __name__ == "__main__":
    unittest.main()
