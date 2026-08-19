"""Unit tests for Target Speaker Extraction (TSE).

Tests:
1. ECAPA-TDNN Speaker Encoder embedding shape and L2 normalization
2. SpeakerStore profile persistence, enrollment, listing, and deletion
3. TSE Extractor target isolation model forward pass
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np


class SpeakerEncoderTests(unittest.TestCase):
    def test_embedding_dim_and_normalization(self) -> None:
        from perfectvoice_engine.tse import EMBEDDING_DIM, extract_embedding

        sr = 44100
        dur_s = 2.0
        t = np.linspace(0, dur_s, int(sr * dur_s), dtype=np.float32)
        audio = (
            0.5 * np.sin(2 * np.pi * 150 * t)
            + 0.3 * np.sin(2 * np.pi * 300 * t)
            + 0.2 * np.sin(2 * np.pi * 450 * t)
        )[np.newaxis, :]

        embed = extract_embedding(audio, sample_rate=sr, device="cpu")
        self.assertEqual(embed.shape, (EMBEDDING_DIM,))
        self.assertEqual(embed.dtype, np.float32)
        norm = float(np.linalg.norm(embed))
        self.assertAlmostEqual(norm, 1.0, places=4)

    def test_same_audio_produces_identical_embedding(self) -> None:
        from perfectvoice_engine.tse import extract_embedding

        sr = 44100
        audio = np.random.RandomState(42).randn(1, sr * 2).astype(np.float32)
        e1 = extract_embedding(audio, sample_rate=sr, device="cpu")
        e2 = extract_embedding(audio, sample_rate=sr, device="cpu")
        cos_sim = float(np.dot(e1, e2) / (np.linalg.norm(e1) * np.linalg.norm(e2)))
        self.assertAlmostEqual(cos_sim, 1.0, places=5)


class SpeakerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-tse-store-"))
        self.store_file = self.tmp / "speakers.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_enroll_list_get_delete(self) -> None:
        from perfectvoice_engine.tse import SpeakerStore

        store = SpeakerStore(self.store_file)
        self.assertEqual(len(store.list_all()), 0)

        vec_a = np.random.randn(192).astype(np.float32)
        vec_a /= np.linalg.norm(vec_a)
        p_a = store.enroll("Host", vec_a, sample_duration_s=3.2)
        self.assertTrue(p_a.speaker_id.startswith("spk_"))
        self.assertEqual(p_a.name, "Host")
        self.assertEqual(p_a.sample_duration_s, 3.2)

        vec_b = np.random.randn(192).astype(np.float32)
        p_b = store.enroll("Guest", vec_b, sample_duration_s=2.5)

        all_spks = store.list_all()
        self.assertEqual(len(all_spks), 2)

        found = store.get(p_a.speaker_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Host")
        np.testing.assert_allclose(found.to_numpy(), vec_a, rtol=1e-5)

        store2 = SpeakerStore(self.store_file)
        self.assertEqual(len(store2.list_all()), 2)

        self.assertTrue(store2.delete(p_a.speaker_id))
        self.assertEqual(len(store2.list_all()), 1)
        self.assertIsNone(store2.get(p_a.speaker_id))


class TSEExtractorTests(unittest.TestCase):
    def test_extractor_shape_and_ola(self) -> None:
        from perfectvoice_engine.tse import extract_target_speaker

        sr = 44100
        dur_s = 1.5
        audio = np.random.RandomState(42).randn(2, int(sr * dur_s)).astype(np.float32)
        embed = np.random.randn(192).astype(np.float32)
        embed /= np.linalg.norm(embed)

        out = extract_target_speaker(audio, embed, sample_rate=sr, device="cpu")
        self.assertEqual(out.shape, audio.shape)
        self.assertEqual(out.dtype, np.float32)
        self.assertFalse(np.isnan(out).any())
        self.assertFalse(np.isinf(out).any())


class SidecarSpeakerHttpTests(unittest.TestCase):
    def test_enroll_endpoint(self) -> None:
        from perfectvoice_engine.ffmpeg_io import write_wav
        from perfectvoice_engine.serve import EngineHTTPServer, JobStore
        import http.client
        import json

        tmp = Path(tempfile.mkdtemp(prefix="pv-tse-http-"))
        try:
            # 1. Create a dummy test WAV
            sr = 48000
            dur_s = 3.0
            t = np.linspace(0, dur_s, int(sr * dur_s), dtype=np.float32)
            audio = 0.5 * np.sin(2 * np.pi * 200 * t)[:, np.newaxis]
            wav_path = tmp / "sample.wav"
            write_wav(wav_path, audio, sample_rate=sr)

            # 2. Start in-process sidecar server
            token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            server = EngineHTTPServer(("127.0.0.1", 0), token, JobStore(), idle_seconds=0)
            port = server.server_address[1]
            import threading
            th = threading.Thread(target=server.serve_forever, daemon=True)
            th.start()

            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }

                # 3. Test POST /v1/speakers/enroll
                body = json.dumps({
                    "audio_path": str(wav_path),
                    "name": "HostVoice",
                    "t0": 0.0,
                    "t1": 3.0,
                })
                conn.request("POST", "/v1/speakers/enroll", body=body, headers=headers)
                res = conn.getresponse()
                self.assertEqual(res.status, 200)
                data = json.loads(res.read().decode("utf-8"))
                self.assertTrue(data.get("ok"))
                spk = data.get("speaker", {})
                self.assertEqual(spk.get("name"), "HostVoice")
                spk_id = spk.get("speaker_id")
                self.assertTrue(spk_id.startswith("spk_"))

                # 4. Test GET /v1/speakers
                conn.request("GET", "/v1/speakers", headers=headers)
                res = conn.getresponse()
                self.assertEqual(res.status, 200)
                list_data = json.loads(res.read().decode("utf-8"))
                self.assertTrue(list_data.get("ok"))
                self.assertTrue(any(s["speaker_id"] == spk_id for s in list_data.get("speakers", [])))

                # 5. Test DELETE /v1/speakers/:id
                conn.request("DELETE", f"/v1/speakers/{spk_id}", headers=headers)
                res = conn.getresponse()
                self.assertEqual(res.status, 200)

            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
