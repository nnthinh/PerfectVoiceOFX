"""POST /v1/jobs pipeline: extract → mocked Separator → blend → cache.

No 80 MB Demucs download. Synthetic WAV + local fixture repo.

Run: python3 -m unittest tests.unit.test_jobs
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "schemas"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine.cache import compute_input_hash, file_id_from_path  # noqa: E402
from perfectvoice_engine.constants import JobCancelled  # noqa: E402
from perfectvoice_engine.enhance import EnhancerNotInstalled  # noqa: E402
from perfectvoice_engine.ffmpeg_io import (  # noqa: E402
    BWF_ORIGINATOR,
    FFmpegError,
    expected_output_sample_count,
    extract_with_handles,
    ffmpeg_bin,
    inspect_wav,
)
from perfectvoice_engine.models import (  # noqa: E402
    DEFAULT_MODEL,
    MODEL_NOT_INSTALLED,
    ModelNotInstalled,
)
from perfectvoice_engine.pipeline import process_clip, run_job  # noqa: E402
from perfectvoice_engine.serve import (  # noqa: E402
    EngineHTTPServer,
    JobStore,
    validate_job_request,
)

FORBIDDEN_HOST_NEEDLES = (
    "huggingface.co",
    "hf.co",
    "fbaipublicfiles.com",
    "amazonaws.com",
)
# Infer / job modules must not import the user-click fetcher.
# serve.py may import weight_fetch for POST /v1/models/download only.
JOB_INFER_PATHS = (
    ENGINE_DIR / "perfectvoice_engine" / "pipeline.py",
    ENGINE_DIR / "perfectvoice_engine" / "separate.py",
    ENGINE_DIR / "perfectvoice_engine" / "models.py",
)
JOB_LOAD_PATHS = (
    ENGINE_DIR / "perfectvoice_engine" / "serve.py",
    *JOB_INFER_PATHS,
)


def _have_ffmpeg() -> bool:
    try:
        ffmpeg_bin()
        return True
    except FFmpegError:
        return False


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_fixture(name: str) -> object:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


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


def _write_sine_wav(path: Path, *, seconds: float = 2.0, sample_rate: int = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import subprocess

    subprocess.run(
        [
            ffmpeg_bin(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}:sample_rate={sample_rate}",
            "-ac",
            "2",
            "-c:a",
            "pcm_s24le",
            str(path),
        ],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def _job_payload(
    source_path: str,
    output_dir: str,
    allowed_roots: list[str],
    *,
    source_in_sample: int = 9600,
    source_out_sample: int = 57600,
    source_sample_rate: int = 48000,
    project_sample_rate: int = 48000,
    file_duration_seconds: float = 2.0,
    handles_seconds: float = 0.5,
    use_cache: bool = True,
    enhancer: str = "none",
) -> dict:
    clip = _load_fixture("clip.valid.json")
    params = _load_fixture("params.valid.json")
    assert isinstance(clip, dict)
    assert isinstance(params, dict)
    clip["source_path"] = source_path
    clip["source_in_sample"] = source_in_sample
    clip["source_out_sample"] = source_out_sample
    clip["source_sample_rate"] = source_sample_rate
    clip["project_sample_rate"] = project_sample_rate
    clip["file_duration_seconds"] = file_duration_seconds
    clip["handles_seconds"] = handles_seconds
    params["output_dir"] = output_dir
    params["allowed_roots"] = list(allowed_roots)
    params["use_cache"] = use_cache
    params["enhancer"] = enhancer
    return {
        "clips": [clip],
        "params": params,
        "allowed_roots": list(allowed_roots),
        "output_dir": output_dir,
    }


class FakeSeparator:
    instances: list["FakeSeparator"] = []
    separate_calls = 0

    def __init__(self, model: str = DEFAULT_MODEL, repo: Path | None = None, **kwargs: Any) -> None:
        if repo is None:
            raise AssertionError("Separator must be constructed with repo=")
        self.model = model
        self.repo = Path(repo)
        self.kwargs = kwargs
        FakeSeparator.instances.append(self)

    def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
        FakeSeparator.separate_calls += 1
        arr = wav
        if hasattr(arr, "detach"):
            arr = arr.detach()
        if hasattr(arr, "cpu"):
            arr = arr.cpu()
        if hasattr(arr, "numpy"):
            arr = arr.numpy()
        arr = np.asarray(arr, dtype=np.float32)
        callback = self.kwargs.get("callback")
        if callable(callback):
            callback({"segment_offset": 0, "audio_length": int(arr.shape[-1])})
        vocals = arr * np.float32(0.5)
        return arr, {"vocals": vocals, "drums": arr * 0.0, "bass": arr * 0.0, "other": arr * 0.0}


def _is_loopback(target: object) -> bool:
    text = str(target).lower()
    return (
        "127.0.0.1" in text
        or "localhost" in text
        or text.startswith("::1")
        or text == "::1"
    )


def _is_forbidden_host(target: object) -> bool:
    text = str(target).lower()
    return any(needle in text for needle in FORBIDDEN_HOST_NEEDLES)


def _block_and_record_network() -> "_NetBlock":
    import http.client
    import socket
    import urllib.request

    hits: list[str] = []
    orig_urlopen = urllib.request.urlopen
    orig_create = socket.create_connection
    orig_getaddrinfo = socket.getaddrinfo
    orig_https = http.client.HTTPSConnection
    orig_http = http.client.HTTPConnection

    def deny_or_pass(target: object) -> None:
        if _is_forbidden_host(target):
            hits.append(str(target))
            raise OSError(f"network disabled: {target}")

    def urlopen(url: Any, *args: Any, **kwargs: Any) -> Any:
        target = url.get_full_url() if hasattr(url, "get_full_url") else url
        deny_or_pass(target)
        if _is_loopback(target):
            return orig_urlopen(url, *args, **kwargs)
        hits.append(str(target))
        raise OSError(f"network disabled: {target}")

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        deny_or_pass(host)
        if _is_loopback(host):
            return orig_create(address, *args, **kwargs)
        hits.append(str(host))
        raise OSError(f"network disabled: {host}")

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        deny_or_pass(host)
        if _is_loopback(host):
            return orig_getaddrinfo(host, *args, **kwargs)
        hits.append(str(host))
        raise OSError(f"network disabled: {host}")

    class GuardedHTTPSConnection(orig_https):  # type: ignore[valid-type,misc]
        def __init__(self, host: Any, *args: Any, **kwargs: Any) -> None:
            deny_or_pass(host)
            if not _is_loopback(host):
                hits.append(str(host))
                raise OSError(f"network disabled: {host}")
            super().__init__(host, *args, **kwargs)

    class GuardedHTTPConnection(orig_http):  # type: ignore[valid-type,misc]
        def __init__(self, host: Any, *args: Any, **kwargs: Any) -> None:
            deny_or_pass(host)
            if not _is_loopback(host):
                hits.append(str(host))
                raise OSError(f"network disabled: {host}")
            super().__init__(host, *args, **kwargs)

    return _NetBlock(
        hits,
        urlopen,
        create_connection,
        getaddrinfo,
        GuardedHTTPSConnection,
        GuardedHTTPConnection,
    )


class _NetBlock:
    def __init__(
        self,
        hits: list[str],
        urlopen: Any,
        create_connection: Any,
        getaddrinfo: Any,
        https: Any,
        http: Any,
    ) -> None:
        self.hits = hits
        self._cm = (
            patch("urllib.request.urlopen", urlopen),
            patch("socket.create_connection", create_connection),
            patch("socket.getaddrinfo", getaddrinfo),
            patch("http.client.HTTPSConnection", https),
            patch("http.client.HTTPConnection", http),
        )

    def __enter__(self) -> list[str]:
        for cm in self._cm:
            cm.__enter__()
        return self.hits

    def __exit__(self, *exc: object) -> None:
        for cm in reversed(self._cm):
            cm.__exit__(*exc)


def _assert_no_forbidden_hosts(hits: list[str]) -> None:
    joined = "\n".join(hits)
    for needle in FORBIDDEN_HOST_NEEDLES:
        if needle in joined.lower():
            raise AssertionError(f"request to forbidden host {needle!r}: {hits}")


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


@unittest.skipUnless(_have_ffmpeg(), "ffmpeg not installed")
class JobPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-jobs-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.manifest = _write_htdemucs_fixture(self.repo)
        self.manifest_path = self.tmp / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.cache_dir = self.tmp / "cache"
        self.cache_index = self.tmp / "cache-index.sqlite"
        self.media = self.tmp / "media"
        self.out = self.tmp / "out"
        self.media.mkdir()
        self.out.mkdir()
        self.src = self.media / "clip.wav"
        _write_sine_wav(self.src)
        self.roots = [str(self.media), str(self.out)]
        FakeSeparator.instances.clear()
        FakeSeparator.separate_calls = 0
        self._env = patch.dict(
            os.environ,
            {
                "PERFECTVOICE_DEMUCS_REPO": str(self.repo),
                "PERFECTVOICE_MANIFEST": str(self.manifest_path),
                "PERFECTVOICE_CACHE_DIR": str(self.cache_dir),
                "PERFECTVOICE_CACHE_INDEX": str(self.cache_index),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _validated(self, project_sample_rate: int = 48000, **kwargs: Any) -> dict[str, Any]:
        body = _job_payload(
            str(self.src),
            str(self.out),
            self.roots,
            project_sample_rate=project_sample_rate,
            **kwargs,
        )
        return validate_job_request(body)

    def _run(self, validated: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        body = validated if validated is not None else self._validated()
        with (
            patch("perfectvoice_engine.separate._separator_cls", return_value=FakeSeparator),
            patch(
                "perfectvoice_engine.separate._to_model_input",
                side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
            ),
            patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
        ):
            return run_job(
                body["clips"],
                body["params"],
                body["output_dir"],
            )

    def test_synthetic_wav_mocked_separator(self) -> None:
        results = self._run()
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertFalse(row["cache_hit"])
        self.assertEqual(row["handles_left_actual"], 0.2)
        self.assertEqual(row["handles_right_actual"], 0.5)
        self.assertEqual(row["wet_dry_sample_rate"], 44100)
        expected_n = expected_output_sample_count(0.2, 1.2, 2.0, 48000, 0.5)
        self.assertLessEqual(abs(int(row["output_samples"]) - expected_n), 1)
        self.assertGreaterEqual(row["peak"], 0.0)
        self.assertIsNotNone(FakeSeparator.instances[0].kwargs.get("callback"))
        self.assertRegex(row["input_hash"], r"^[0-9a-f]{64}$")
        wav = Path(row["output_path"])
        self.assertTrue(wav.is_file())
        info = inspect_wav(wav)
        self.assertEqual(info.originator, BWF_ORIGINATOR)
        self.assertEqual(info.sample_rate, 48000)
        self.assertEqual(info.sample_count, row["output_samples"])
        self.assertEqual(info.sample_format, "pcm24")
        self.assertEqual(FakeSeparator.separate_calls, 1)
        self.assertEqual(FakeSeparator.instances[0].repo, self.repo)
        job_json = self.out / "job.json"
        self.assertFalse(job_json.exists())  # HTTP worker writes this

    def test_cache_hit_on_second_identical_job(self) -> None:
        first = self._run()
        second = self._run()
        self.assertFalse(first[0]["cache_hit"])
        self.assertTrue(second[0]["cache_hit"])
        self.assertEqual(first[0]["input_hash"], second[0]["input_hash"])
        self.assertEqual(first[0]["output_samples"], second[0]["output_samples"])
        self.assertEqual(first[0]["handles_left_actual"], second[0]["handles_left_actual"])
        self.assertEqual(FakeSeparator.separate_calls, 1)
        self.assertTrue(Path(second[0]["output_path"]).is_file())

    def test_48_vs_96_different_cache_keys(self) -> None:
        r48 = self._run(self._validated(project_sample_rate=48000))
        r96 = self._run(self._validated(project_sample_rate=96000))
        self.assertNotEqual(r48[0]["input_hash"], r96[0]["input_hash"])
        self.assertFalse(r48[0]["cache_hit"])
        self.assertFalse(r96[0]["cache_hit"])
        self.assertEqual(FakeSeparator.separate_calls, 2)
        self.assertNotEqual(r48[0]["output_path"], r96[0]["output_path"])
        self.assertTrue(Path(r48[0]["output_path"]).is_file())
        self.assertTrue(Path(r96[0]["output_path"]).is_file())
        self.assertEqual(inspect_wav(r48[0]["output_path"]).sample_rate, 48000)
        self.assertEqual(inspect_wav(r96[0]["output_path"]).sample_rate, 96000)
        n48 = expected_output_sample_count(0.2, 1.2, 2.0, 48000, 0.5)
        n96 = expected_output_sample_count(0.2, 1.2, 2.0, 96000, 0.5)
        self.assertLessEqual(abs(int(r48[0]["output_samples"]) - n48), 1)
        self.assertLessEqual(abs(int(r96[0]["output_samples"]) - n96), 1)
        # Identity function agrees with the pipeline.
        clip48 = self._validated(project_sample_rate=48000)["clips"][0]
        clip96 = self._validated(project_sample_rate=96000)["clips"][0]
        params = self._validated()["params"]
        from perfectvoice_engine.models import weights_sha256

        digest = weights_sha256(self.manifest[DEFAULT_MODEL])
        self.assertEqual(
            r48[0]["input_hash"],
            compute_input_hash(
                file_id=file_id_from_path(self.src),
                src_in=int(clip48["source_in_sample"]),
                src_out=int(clip48["source_out_sample"]),
                audio_stream_index=int(clip48["audio_stream_index"]),
                channel_map=tuple(clip48["channel_map"]),
                model_name=params["model"],
                weights_sha256=digest,
                vocals_only_bag=params["vocals_only_bag"],
                wet=params["wet"],
                gain=params["output_gain_db"],
                mono=params["mono"],
                handles_requested=clip48["handles_seconds"],
                file_duration_seconds=clip48["file_duration_seconds"],
                segment=params["segment"],
                overlap=params["overlap"],
                shifts=params["shifts"],
                enhancer_id=params["enhancer"],
                project_sample_rate=48000,
                sample_format=params["sample_format"],
                resampler_id=params["resampler_id"],
                clip_policy=params["clip_policy"],
                engine_semver="0.1.0",
            ),
        )
        self.assertNotEqual(
            r48[0]["input_hash"],
            compute_input_hash(
                file_id=file_id_from_path(self.src),
                src_in=int(clip96["source_in_sample"]),
                src_out=int(clip96["source_out_sample"]),
                audio_stream_index=int(clip96["audio_stream_index"]),
                channel_map=tuple(clip96["channel_map"]),
                model_name=params["model"],
                weights_sha256=digest,
                vocals_only_bag=params["vocals_only_bag"],
                wet=params["wet"],
                gain=params["output_gain_db"],
                mono=params["mono"],
                handles_requested=clip96["handles_seconds"],
                file_duration_seconds=clip96["file_duration_seconds"],
                segment=params["segment"],
                overlap=params["overlap"],
                shifts=params["shifts"],
                enhancer_id=params["enhancer"],
                project_sample_rate=96000,
                sample_format=params["sample_format"],
                resampler_id=params["resampler_id"],
                clip_policy=params["clip_policy"],
                engine_semver="0.1.0",
            ),
        )

    def test_content_window_engine_applies_handles_once(self) -> None:
        # Contract: source_in/out are t0/t1 in samples, not the extract window.
        # t0=0.2, t1=1.2, H=0.5, file=2s @ 48k → H_left=0.2, extract starts at 0.
        content = self._run()
        self.assertEqual(content[0]["handles_left_actual"], 0.2)
        self.assertEqual(content[0]["handles_right_actual"], 0.5)
        # A panel that pre-added H would send source_in=0, source_out=81600
        # and the engine would report H_left_actual=0 (clamp-at-SOF lost).
        prehandled = self._run(
            self._validated(source_in_sample=0, source_out_sample=81600)
        )
        self.assertEqual(prehandled[0]["handles_left_actual"], 0.0)
        self.assertNotEqual(content[0]["input_hash"], prehandled[0]["input_hash"])

    def test_dfn_mocked_wet_dry_48000_distinct_key(self) -> None:
        none = self._run()
        FakeSeparator.separate_calls = 0
        FakeSeparator.instances.clear()

        def _identity(samples: np.ndarray, sample_rate: int, **kwargs: object) -> np.ndarray:
            self.assertEqual(int(sample_rate), 48000)
            return np.array(samples, dtype=np.float32, copy=True)

        body = self._validated(enhancer="deepfilternet3")
        with (
            patch("perfectvoice_engine.pipeline.is_enhancer_installed", return_value=True),
            patch("perfectvoice_engine.blend.enhance_vocals", side_effect=_identity),
            patch("perfectvoice_engine.separate._separator_cls", return_value=FakeSeparator),
            patch(
                "perfectvoice_engine.separate._to_model_input",
                side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
            ),
            patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
        ):
            dfn = run_job(body["clips"], body["params"], body["output_dir"])
        self.assertEqual(dfn[0]["wet_dry_sample_rate"], 48000)
        self.assertEqual(none[0]["wet_dry_sample_rate"], 44100)
        self.assertNotEqual(dfn[0]["input_hash"], none[0]["input_hash"])
        self.assertEqual(FakeSeparator.separate_calls, 1)

    def test_missing_dfn_zero_network_no_separator(self) -> None:
        empty_dfn = self.tmp / "empty-dfn"
        empty_dfn.mkdir()
        os.environ["PERFECTVOICE_DFN_REPO"] = str(empty_dfn)
        FakeSeparator.instances.clear()
        FakeSeparator.separate_calls = 0
        body = self._validated(enhancer="deepfilternet3")
        with _block_and_record_network() as hits:
            with patch(
                "perfectvoice_engine.separate._separator_cls",
                side_effect=AssertionError("Separator must not run"),
            ):
                with self.assertRaises(EnhancerNotInstalled) as ctx:
                    process_clip(
                        body["clips"][0],
                        body["params"],
                        output_dir=self.out,
                    )
        self.assertIn("enhancer not installed", str(ctx.exception).lower())
        self.assertEqual(FakeSeparator.separate_calls, 0)
        _assert_no_forbidden_hosts(hits)
        self.assertEqual(hits, [])

    def test_cancel_during_extract(self) -> None:
        cancel = threading.Event()
        cancel.set()
        dest = self.tmp / "cancelled.wav"
        with self.assertRaises(JobCancelled):
            extract_with_handles(
                self.src,
                dest,
                t0=0.2,
                t1=1.2,
                handle_s=0.5,
                cancel_event=cancel,
            )
        self.assertFalse(dest.exists())

    def test_missing_model_zero_network(self) -> None:
        empty = self.tmp / "empty-repo"
        empty.mkdir()
        os.environ["PERFECTVOICE_DEMUCS_REPO"] = str(empty)
        FakeSeparator.instances.clear()
        FakeSeparator.separate_calls = 0
        with _block_and_record_network() as hits:
            with patch(
                "perfectvoice_engine.separate._separator_cls",
                side_effect=AssertionError("Separator must not run"),
            ):
                with self.assertRaises(ModelNotInstalled) as ctx:
                    process_clip(
                        self._validated()["clips"][0],
                        self._validated()["params"],
                        output_dir=self.out,
                        local_repo=empty,
                    )
        self.assertIn("Model not installed", str(ctx.exception))
        self.assertTrue(str(ctx.exception).startswith(MODEL_NOT_INSTALLED))
        self.assertEqual(FakeSeparator.separate_calls, 0)
        _assert_no_forbidden_hosts(hits)
        self.assertEqual(hits, [])

    def test_cancel_during_separate(self) -> None:
        cancel = threading.Event()

        class Watching(FakeSeparator):
            def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
                cancel.set()
                raise JobCancelled("job cancelled")

        body = self._validated()
        with (
            patch("perfectvoice_engine.separate._separator_cls", return_value=Watching),
            patch(
                "perfectvoice_engine.separate._to_model_input",
                side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
            ),
            patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
        ):
            with self.assertRaises(JobCancelled):
                run_job(
                    body["clips"],
                    body["params"],
                    body["output_dir"],
                    cancel_event=cancel,
                )


@unittest.skipUnless(_have_ffmpeg(), "ffmpeg not installed")
class JobHttpInProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-jobhttp-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.manifest = _write_htdemucs_fixture(self.repo)
        self.manifest_path = self.tmp / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.cache_dir = self.tmp / "cache"
        self.cache_index = self.tmp / "cache-index.sqlite"
        self.media = self.tmp / "media"
        self.out = self.tmp / "out"
        self.media.mkdir()
        self.out.mkdir()
        self.src = self.media / "clip.wav"
        _write_sine_wav(self.src)
        self.roots = [str(self.media), str(self.out)]
        FakeSeparator.instances.clear()
        FakeSeparator.separate_calls = 0
        self.token = secrets.token_hex(32)
        self.store = JobStore()
        self.httpd = EngineHTTPServer(("127.0.0.1", 0), self.token, self.store, 0)
        self.stop = threading.Event()
        self.worker = threading.Thread(
            target=self.store.worker_loop, args=(self.stop,), name="job-worker", daemon=True
        )
        self.http_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        self.http_thread.start()
        host, port = self.httpd.server_address
        self.url = f"http://127.0.0.1:{port}"
        self._env = patch.dict(
            os.environ,
            {
                "PERFECTVOICE_DEMUCS_REPO": str(self.repo),
                "PERFECTVOICE_MANIFEST": str(self.manifest_path),
                "PERFECTVOICE_CACHE_DIR": str(self.cache_dir),
                "PERFECTVOICE_CACHE_INDEX": str(self.cache_index),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            },
            clear=False,
        )
        self._env.start()
        self._sep = patch("perfectvoice_engine.separate._separator_cls", return_value=FakeSeparator)
        self._to_np = patch(
            "perfectvoice_engine.separate._to_model_input",
            side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
        )
        self._dev = patch("perfectvoice_engine.separate.resolve_device", return_value="cpu")
        self._sep.start()
        self._to_np.start()
        self._dev.start()

    def tearDown(self) -> None:
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self._dev.stop()
        self._to_np.stop()
        self._sep.stop()
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _request(self, method: str, path: str, body: object | None = None) -> tuple[int, object]:
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                parsed: object = json.loads(raw.decode("utf-8")) if raw else {}
                return resp.status, parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            return exc.code, parsed

    def _wait_terminal(self, job_id: str, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        snap: dict = {}
        while time.time() < deadline:
            status, body = self._request("GET", f"/v1/jobs/{job_id}")
            self.assertEqual(status, 200)
            assert isinstance(body, dict)
            snap = body
            if body.get("status") in {"done", "error", "cancelled"}:
                return snap
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish: {snap}")

    def test_http_job_result_fields_and_cache(self) -> None:
        body = _job_payload(str(self.src), str(self.out), self.roots)
        status, accepted = self._request("POST", "/v1/jobs", body)
        self.assertEqual(status, 202)
        assert isinstance(accepted, dict)
        job_id = accepted["id"]
        snap = self._wait_terminal(job_id)
        self.assertEqual(snap.get("status"), "done", snap)
        self.assertEqual(snap.get("schema"), "perfectvoice.job.v1")
        clips = snap["clips"]
        self.assertEqual(len(clips), 1)
        row = clips[0]
        for key in (
            "clip_id",
            "input_hash",
            "output_path",
            "output_samples",
            "handles_left_actual",
            "handles_right_actual",
            "wet_dry_sample_rate",
            "peak",
            "cache_hit",
        ):
            self.assertIn(key, row)
        self.assertFalse(row["cache_hit"])
        self.assertEqual(row["handles_left_actual"], 0.2)
        self.assertEqual(row["handles_right_actual"], 0.5)
        self.assertEqual(row["wet_dry_sample_rate"], 44100)
        self.assertTrue(Path(row["output_path"]).is_file())
        self.assertTrue((self.out / "job.json").is_file())
        disk = json.loads((self.out / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["schema"], "perfectvoice.job.v1")
        self.assertEqual(disk["clips"][0]["input_hash"], row["input_hash"])

        status, accepted2 = self._request("POST", "/v1/jobs", body)
        self.assertEqual(status, 202)
        assert isinstance(accepted2, dict)
        snap2 = self._wait_terminal(accepted2["id"])
        self.assertEqual(snap2.get("status"), "done", snap2)
        self.assertTrue(snap2["clips"][0]["cache_hit"])
        self.assertEqual(snap2["clips"][0]["input_hash"], row["input_hash"])
        self.assertEqual(FakeSeparator.separate_calls, 1)

    def test_http_48_vs_96_two_keys(self) -> None:
        body48 = _job_payload(str(self.src), str(self.out), self.roots, project_sample_rate=48000)
        body96 = _job_payload(str(self.src), str(self.out), self.roots, project_sample_rate=96000)
        status, a48 = self._request("POST", "/v1/jobs", body48)
        self.assertEqual(status, 202)
        assert isinstance(a48, dict)
        snap48 = self._wait_terminal(a48["id"])
        status, a96 = self._request("POST", "/v1/jobs", body96)
        self.assertEqual(status, 202)
        assert isinstance(a96, dict)
        snap96 = self._wait_terminal(a96["id"])
        self.assertEqual(snap48.get("status"), "done", snap48)
        self.assertEqual(snap96.get("status"), "done", snap96)
        self.assertNotEqual(snap48["clips"][0]["input_hash"], snap96["clips"][0]["input_hash"])
        self.assertFalse(snap48["clips"][0]["cache_hit"])
        self.assertFalse(snap96["clips"][0]["cache_hit"])
        self.assertEqual(FakeSeparator.separate_calls, 2)

    def test_http_missing_model_zero_network(self) -> None:
        empty = self.tmp / "empty-repo"
        empty.mkdir()
        os.environ["PERFECTVOICE_DEMUCS_REPO"] = str(empty)
        FakeSeparator.separate_calls = 0
        body = _job_payload(str(self.src), str(self.out), self.roots)
        with _block_and_record_network() as hits:
            status, accepted = self._request("POST", "/v1/jobs", body)
            self.assertEqual(status, 202)
            assert isinstance(accepted, dict)
            snap = self._wait_terminal(accepted["id"])
        self.assertEqual(snap.get("status"), "error")
        self.assertIn("Model not installed", str(snap.get("error")))
        self.assertEqual(FakeSeparator.separate_calls, 0)
        _assert_no_forbidden_hosts(hits)

    def test_http_source_channels_6_rejected(self) -> None:
        body = _job_payload(str(self.src), str(self.out), self.roots)
        body["clips"][0]["source_channels"] = 6
        status, resp = self._request("POST", "/v1/jobs", body)
        self.assertEqual(status, 400)
        assert isinstance(resp, dict)
        self.assertIn("source_channels", str(resp))

    def test_http_cancel_still_works(self) -> None:
        # Hold the worker so cancel wins the race with extract.
        os.environ["PERFECTVOICE_STUB_HOLD"] = "30"
        # JobStore already read STUB_HOLD at import; patch the module value.
        import perfectvoice_engine.serve as serve_mod

        old = serve_mod.STUB_HOLD_SECONDS
        serve_mod.STUB_HOLD_SECONDS = 30.0
        try:
            body = _job_payload(str(self.src), str(self.out), self.roots)
            status, accepted = self._request("POST", "/v1/jobs", body)
            self.assertEqual(status, 202)
            assert isinstance(accepted, dict)
            job_id = accepted["id"]
            status, cancelled = self._request("POST", f"/v1/jobs/{job_id}/cancel")
            self.assertEqual(status, 202)
            assert isinstance(cancelled, dict)
            self.assertIn(cancelled.get("status"), ("cancelled", "cancelling"))
            snap = self._wait_terminal(job_id)
            self.assertEqual(snap.get("status"), "cancelled")
            status, again = self._request("POST", f"/v1/jobs/{job_id}/cancel")
            self.assertEqual(status, 409)
        finally:
            serve_mod.STUB_HOLD_SECONDS = old
            os.environ.pop("PERFECTVOICE_STUB_HOLD", None)


class JobStaticContractTests(unittest.TestCase):
    def test_jobs_do_not_call_weight_download(self) -> None:
        for path in JOB_INFER_PATHS:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for mod in modules:
                    self.assertNotIn("weight_fetch", mod, f"{path.name} imports {mod}")
                    self.assertNotIn("download_demucs", mod, f"{path.name} imports {mod}")
            self.assertNotIn("huggingface.co/adefossez", text)
            self.assertNotIn("dl.fbaipublicfiles.com", text)

        serve = ENGINE_DIR / "perfectvoice_engine" / "serve.py"
        serve_text = serve.read_text(encoding="utf-8")
        self.assertNotIn("huggingface.co/adefossez", serve_text)
        self.assertNotIn("dl.fbaipublicfiles.com", serve_text)
        tree = ast.parse(serve_text, filename=str(serve))
        job_fns = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name in {"_create_job", "_run_job"}
        }
        self.assertEqual(set(job_fns), {"_create_job", "_run_job"})
        for name, fn in job_fns.items():
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id == "download_model":
                    self.fail(f"{name} references download_model")
                if isinstance(node, ast.Attribute) and node.attr == "download_model":
                    self.fail(f"{name} references download_model")

    def test_pipeline_does_not_import_torch_or_demucs(self) -> None:
        banned = {"torch", "torchaudio", "demucs"}
        path = ENGINE_DIR / "perfectvoice_engine" / "pipeline.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                self.assertNotIn(name.split(".", 1)[0], banned, f"pipeline imports {name}")

    def test_serve_still_lazy_imports_pipeline(self) -> None:
        src = (ENGINE_DIR / "perfectvoice_engine" / "serve.py").read_text(encoding="utf-8")
        self.assertIn("from perfectvoice_engine.pipeline import run_job", src)
        tree = ast.parse(src)
        top_imports = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "perfectvoice_engine.pipeline"
        ]
        self.assertEqual(top_imports, [])


if __name__ == "__main__":
    unittest.main()
