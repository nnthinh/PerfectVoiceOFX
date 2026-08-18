"""Full-identity cache key tests (§3.7). No Demucs, no Resolve.

Run: python3 -m unittest tests.unit.test_cache
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine.cache import (  # noqa: E402
    HASH_FIELDS,
    CacheError,
    CacheIndex,
    clip_hash12,
    compute_input_hash,
    file_id_from_path,
    normalize_identity,
    wet_dry_sample_rate_for,
)

WEIGHTS_A = "ab" * 32
WEIGHTS_B = "cd" * 32


def _base_kwargs(**overrides: object) -> dict:
    fields: dict = {
        "file_id": (1, 2, 3, 4),
        "src_in": 0,
        "src_out": 48000,
        "audio_stream_index": 0,
        "channel_map": (0, 1),
        "model_name": "htdemucs",
        "weights_sha256": WEIGHTS_A,
        "vocals_only_bag": False,
        "wet": 0.85,
        "gain": 0.0,
        "mono": False,
        "handles_requested": 0.5,
        "file_duration_seconds": 60.0,
        "segment": 7.8,
        "overlap": 0.25,
        "shifts": 1,
        "enhancer_id": "none",
        "project_sample_rate": 48000,
        "sample_format": "pcm24",
        "resampler_id": "soxr_hq_v1",
        "clip_policy": "no_demucs_rescale",
        "engine_semver": "0.1.0",
    }
    fields.update(overrides)
    return fields


def _hash(**overrides: object) -> str:
    return compute_input_hash(**_base_kwargs(**overrides))


class ComputeInputHashTests(unittest.TestCase):
    def test_same_inputs_same_key(self) -> None:
        self.assertEqual(_hash(), _hash())
        self.assertEqual(len(_hash()), 64)
        self.assertEqual(_hash(), _hash().lower())

    def test_project_rate_48_vs_96_two_keys(self) -> None:
        key_48 = _hash(project_sample_rate=48000)
        key_96 = _hash(project_sample_rate=96000)
        self.assertNotEqual(key_48, key_96)

    def test_pcm24_vs_f32_two_keys(self) -> None:
        self.assertNotEqual(_hash(sample_format="pcm24"), _hash(sample_format="f32"))
        self.assertEqual(_hash(sample_format="f32"), _hash(sample_format="float32"))

    def test_enhancer_none_vs_dfn3_two_keys(self) -> None:
        none_key = _hash(enhancer_id="none")
        dfn_key = _hash(enhancer_id="dfn3")
        self.assertNotEqual(none_key, dfn_key)
        self.assertEqual(dfn_key, _hash(enhancer_id="deepfilternet3"))

    def test_changing_one_field_changes_key(self) -> None:
        baseline = _hash()
        mutations = {
            "file_id": (1, 99, 3, 4),
            "src_in": 1,
            "src_out": 48001,
            "audio_stream_index": 1,
            "channel_map": (0,),
            "model_name": "htdemucs_ft",
            "weights_sha256": WEIGHTS_B,
            "vocals_only_bag": True,
            "wet": 0.84,
            "gain": 1.0,
            "mono": True,
            "handles_requested": 0.25,
            "file_duration_seconds": 61.0,
            "segment": 7.0,
            "overlap": 0.10,
            "shifts": 2,
            "enhancer_id": "dfn3",
            "project_sample_rate": 96000,
            "sample_format": "float32",
            "resampler_id": "soxr_hq_v2",
            "clip_policy": "other_policy",
            "engine_semver": "0.1.1",
        }
        self.assertEqual(set(mutations), set(HASH_FIELDS))
        for name, value in mutations.items():
            with self.subTest(field=name):
                self.assertNotEqual(baseline, _hash(**{name: value}), name)

    def test_hash_fields_match_schema_required_order(self) -> None:
        schema_path = REPO_ROOT / "shared" / "schema" / "hash-fields.v1.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(HASH_FIELDS, tuple(schema["required"]))
        self.assertEqual(set(schema["properties"]), set(HASH_FIELDS))

    def test_handles_actual_is_not_an_identity_field(self) -> None:
        with self.assertRaises(TypeError):
            compute_input_hash(  # type: ignore[call-arg]
                **_base_kwargs(),
                handles_left_actual=0.4,
            )

    def test_client_wet_dry_sample_rate_rejected(self) -> None:
        fields = _base_kwargs()
        fields["wet_dry_sample_rate"] = 44100
        with self.assertRaises(CacheError) as ctx:
            normalize_identity(fields)
        self.assertIn("wet_dry_sample_rate", str(ctx.exception))

    def test_derived_wet_dry_rate(self) -> None:
        self.assertEqual(wet_dry_sample_rate_for("none"), 44100)
        self.assertEqual(wet_dry_sample_rate_for("dfn3"), 48000)
        self.assertEqual(wet_dry_sample_rate_for("deepfilternet3"), 48000)
        identity = normalize_identity(_base_kwargs(enhancer_id="dfn3"))
        self.assertEqual(identity["enhancer_id"], "deepfilternet3")
        self.assertEqual(identity["wet_dry_sample_rate"], 48000)

    def test_clip_hash12(self) -> None:
        digest = _hash()
        self.assertEqual(clip_hash12(digest), digest[:12])

    def test_uint64_file_index_hashes_stably(self) -> None:
        high = 1 << 63
        key = _hash(file_id=(1, high, 3, 4))
        self.assertEqual(key, _hash(file_id=(1, high, 3, 4)))
        self.assertNotEqual(key, _hash(file_id=(1, high - 1, 3, 4)))

    def test_file_id_rejects_bool_and_float(self) -> None:
        with self.assertRaises(CacheError):
            _hash(file_id=(True, 2, 3, 4))
        with self.assertRaises(CacheError):
            _hash(file_id=(1.0, 2, 3, 4))

    def test_int_out_of_uint64_raises_cache_error(self) -> None:
        with self.assertRaises(CacheError):
            _hash(file_id=(1, 1 << 64, 3, 4))
        with self.assertRaises(CacheError):
            _hash(src_in=-1)


class FileIdTests(unittest.TestCase):
    def test_posix_tuple_matches_stat(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"perfectvoice-cache-id")
            path = handle.name
        try:
            st = os.stat(path)
            file_id = file_id_from_path(path)
            self.assertEqual(len(file_id), 4)
            if sys.platform != "win32":
                self.assertEqual(
                    file_id,
                    (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns),
                )
            else:
                self.assertEqual(file_id[2], st.st_size)
                self.assertEqual(file_id[3], st.st_mtime_ns)
        finally:
            os.unlink(path)

    def test_copied_file_is_a_different_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.wav"
            dst = Path(tmp) / "b.wav"
            src.write_bytes(b"same-bytes")
            shutil.copy2(src, dst)
            if sys.platform == "win32":
                self.skipTest("copy2 may share file index on some Windows FS")
            self.assertNotEqual(file_id_from_path(src), file_id_from_path(dst))
            self.assertNotEqual(
                _hash(file_id=file_id_from_path(src)),
                _hash(file_id=file_id_from_path(dst)),
            )


class CacheIndexTests(unittest.TestCase):
    def test_put_get_roundtrip_and_stale_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "voice.wav"
            artifact.write_bytes(b"wav")
            digest = _hash()
            with CacheIndex(root / "cache-index.sqlite") as index:
                index.put(digest, artifact)
                hit = index.get(digest)
                self.assertIsNotNone(hit)
                assert hit is not None
                self.assertEqual(hit.input_hash, digest)
                self.assertEqual(hit.path, str(artifact.resolve()))
                artifact.write_bytes(b"wav-changed")
                self.assertIsNone(index.get(digest))

    def test_put_resolves_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "voice.wav"
            artifact.write_bytes(b"wav")
            digest = _hash()
            cwd = os.getcwd()
            try:
                os.chdir(root)
                with CacheIndex(root / "cache-index.sqlite") as index:
                    index.put(digest, "voice.wav")
                    os.chdir(cwd)
                    hit = index.get(digest)
                    self.assertIsNotNone(hit)
                    assert hit is not None
                    self.assertEqual(hit.path, str(artifact.resolve()))
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
