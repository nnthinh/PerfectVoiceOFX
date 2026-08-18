"""Local-repo Separator contract (no Hub/AWS, no 80 MB download).

Run: python3 -m unittest tests.unit.test_separate
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine.models import (  # noqa: E402
    DEFAULT_MODEL,
    MODEL_NOT_INSTALLED,
    QUALITY_MODEL,
    VOCALS_ONLY_SIG,
    ModelNotInstalled,
    files_for,
    is_model_ready,
    load_manifest,
    manifest_path,
    models_ready,
    require_model,
    sha256_file,
    weights_sha256,
)
from perfectvoice_engine.separate import (  # noqa: E402
    CLIP_POLICY,
    JobCancelled,
    SeparateRequest,
    resolve_device,
    separate_vocals,
    separator_model_name,
)

FORBIDDEN_HOST_NEEDLES = (
    "huggingface.co",
    "hf.co",
    "fbaipublicfiles.com",
    "amazonaws.com",
)
LOAD_PATHS = (
    ENGINE_DIR / "perfectvoice_engine" / "separate.py",
    ENGINE_DIR / "perfectvoice_engine" / "models.py",
    ENGINE_DIR / "models" / "manifest.json",
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _req(
    *,
    frames: int = 4410,
    model: str = DEFAULT_MODEL,
    device: str = "cpu",
    vocals_only_bag: bool = False,
    cancel_event: object | None = None,
    peak: float = 0.25,
) -> SeparateRequest:
    wav = np.full((2, frames), peak, dtype=np.float32)
    return SeparateRequest(
        wav_44100_stereo=wav,
        model=model,
        device=device,
        vocals_only_bag=vocals_only_bag,
        cancel_event=cancel_event,
    )


def _write_repo(root: Path, files: dict[str, bytes]) -> dict[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    for name, payload in files.items():
        (root / name).write_bytes(payload)
        mapping[name] = _digest(payload)
    return {DEFAULT_MODEL: mapping}


def _write_htdemucs_fixture(root: Path, *, th_payload: bytes = b"fixture-htdemucs") -> dict[str, dict[str, str]]:
    # Layout stock 4.1.0 Separator(repo=) opens: bag YAML + {sig}-{checksum}.th
    return _write_repo(
        root,
        {
            "htdemucs.yaml": b"models: ['955717e8']\n",
            "955717e8-8726e21a.th": th_payload,
        },
    )


class FakeSeparator:
    instances: list["FakeSeparator"] = []

    def __init__(self, model: str = DEFAULT_MODEL, repo: Path | None = None, **kwargs: Any) -> None:
        if repo is None:
            raise AssertionError("Separator must be constructed with repo=")
        self.model = model
        self.repo = Path(repo)
        self.kwargs = kwargs
        self.separate_calls = 0
        FakeSeparator.instances.append(self)

    def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
        self.separate_calls += 1
        arr = np.asarray(wav, dtype=np.float32)
        vocals = arr * 0.5
        return arr, {"vocals": vocals, "drums": arr * 0.0, "bass": arr * 0.0, "other": arr * 0.0}


@contextmanager
def _block_and_record_network() -> Iterator[list[str]]:
    hits: list[str] = []

    def deny(target: object) -> None:
        hits.append(str(target))
        raise OSError(f"network disabled: {target}")

    def urlopen(url: Any, *args: Any, **kwargs: Any) -> Any:
        target = url.get_full_url() if hasattr(url, "get_full_url") else url
        deny(target)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        deny(host)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        deny(host)

    class BlockingHTTPSConnection:
        def __init__(self, host: Any, *args: Any, **kwargs: Any) -> None:
            deny(host)

    with (
        patch("urllib.request.urlopen", urlopen),
        patch("socket.create_connection", create_connection),
        patch("socket.getaddrinfo", getaddrinfo),
        patch("http.client.HTTPSConnection", BlockingHTTPSConnection),
        patch("http.client.HTTPConnection", BlockingHTTPSConnection),
    ):
        yield hits


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_separator_ctor(node: ast.Call) -> bool:
    if _call_name(node.func) == "Separator":
        return True
    # _separator_cls()(model=..., repo=...) — func is itself a Call.
    return isinstance(node.func, ast.Call) and _call_name(node.func.func) == "_separator_cls"


def _assert_no_forbidden_hosts(hits: list[str]) -> None:
    joined = "\n".join(hits)
    for needle in FORBIDDEN_HOST_NEEDLES:
        if needle in joined.lower():
            raise AssertionError(f"request to forbidden host {needle!r}: {hits}")


class ManifestTests(unittest.TestCase):
    def test_manifest_name_filename_sha256_no_urls(self) -> None:
        raw = manifest_path().read_text(encoding="utf-8")
        self.assertNotIn("http", raw.lower())
        self.assertNotIn("url", raw.lower())
        data = load_manifest()
        self.assertEqual(DEFAULT_MODEL, "htdemucs")
        self.assertEqual(QUALITY_MODEL, "htdemucs_ft")
        self.assertIn(DEFAULT_MODEL, data)
        self.assertIn(QUALITY_MODEL, data)
        self.assertEqual(len(data[DEFAULT_MODEL]), 2)
        self.assertEqual(len(data[QUALITY_MODEL]), 5)
        self.assertIn("htdemucs.yaml", data[DEFAULT_MODEL])
        self.assertIn("htdemucs_ft.yaml", data[QUALITY_MODEL])
        for files in data.values():
            for filename, digest in files.items():
                self.assertNotIn("://", filename)
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
                self.assertTrue(filename.endswith(".yaml") or filename.endswith(".th"))
                self.assertFalse(filename.endswith(".safetensors"))

    def test_vocals_only_signature_resolves_local_file(self) -> None:
        files = files_for(VOCALS_ONLY_SIG)
        self.assertEqual(set(files), {"04573f0d-f3cf25b2.th"})


class RequireModelTests(unittest.TestCase):
    def test_empty_repo_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with self.assertRaises(ModelNotInstalled) as ctx:
                require_model(DEFAULT_MODEL, repo)
            self.assertIn("Model not installed", str(ctx.exception))

    def test_missing_dir_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "nope"
            with self.assertRaises(ModelNotInstalled):
                require_model(DEFAULT_MODEL, repo)

    def test_checksum_mismatch_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            name = next(iter(files_for(DEFAULT_MODEL)))
            (repo / name).write_bytes(b"wrong")
            with self.assertRaises(ModelNotInstalled) as ctx:
                require_model(DEFAULT_MODEL, repo)
            self.assertIn("Model not installed", str(ctx.exception))

    def test_fixture_hash_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            payload = b"fixture-htdemucs"
            manifest = _write_htdemucs_fixture(repo, th_payload=payload)
            got = require_model(DEFAULT_MODEL, repo, manifest=manifest)
            self.assertEqual(got["955717e8-8726e21a.th"], _digest(payload))
            self.assertEqual(sha256_file(repo / "955717e8-8726e21a.th"), _digest(payload))
            ready = models_ready(repo, manifest=manifest)
            self.assertTrue(ready[DEFAULT_MODEL])

    def test_safetensors_only_is_not_installed(self) -> None:
        # Separator(repo=) never opens *.safetensors; ready must stay false.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "955717e8.safetensors").write_bytes(b"not-a-local-repo")
            (repo / "htdemucs.yaml").write_bytes(b"models: ['955717e8']\n")
            with self.assertRaises(ModelNotInstalled):
                require_model(DEFAULT_MODEL, repo)
            self.assertFalse(is_model_ready(DEFAULT_MODEL, repo))


class NoNetworkTests(unittest.TestCase):
    def test_empty_repo_mocked_http_zero_hub_aws(self) -> None:
        FakeSeparator.instances.clear()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with _block_and_record_network() as hits:
                with patch(
                    "perfectvoice_engine.separate._separator_cls",
                    side_effect=AssertionError("Separator must not run"),
                ):
                    with self.assertRaises(ModelNotInstalled) as ctx:
                        separate_vocals(_req(), repo)
            self.assertIn("Model not installed", str(ctx.exception))
            self.assertTrue(str(ctx.exception).startswith(MODEL_NOT_INSTALLED))
            self.assertEqual(FakeSeparator.instances, [])
            _assert_no_forbidden_hosts(hits)
            self.assertEqual(hits, [])

    def test_empty_repo_does_not_call_urlopen(self) -> None:
        calls: list[object] = []

        def boom(url: object, *args: object, **kwargs: object) -> object:
            calls.append(url)
            raise AssertionError("urlopen must not run")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(urllib.request, "urlopen", boom):
                with self.assertRaises(ModelNotInstalled):
                    separate_vocals(_req(), Path(tmp))
        self.assertEqual(calls, [])


class OfflineFixtureTests(unittest.TestCase):
    def test_fixture_hf_hub_offline_separate_ok(self) -> None:
        FakeSeparator.instances.clear()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            payload = b"fixture-htdemucs-offline"
            manifest = _write_htdemucs_fixture(repo, th_payload=payload)
            env = {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch(
                    "perfectvoice_engine.models.load_manifest",
                    return_value=manifest,
                ),
                patch(
                    "perfectvoice_engine.separate._separator_cls",
                    return_value=FakeSeparator,
                ),
                patch(
                    "perfectvoice_engine.separate._to_model_input",
                    side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
                ),
                patch(
                    "perfectvoice_engine.separate.resolve_device",
                    return_value="cpu",
                ),
            ):
                self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
                result = separate_vocals(_req(peak=0.4), repo)
                self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
            self.assertEqual(len(FakeSeparator.instances), 1)
            sep = FakeSeparator.instances[0]
            self.assertEqual(sep.model, DEFAULT_MODEL)
            self.assertEqual(sep.repo, repo)
            self.assertEqual(sep.separate_calls, 1)
            self.assertEqual(result.vocals.shape[0], 2)
            self.assertAlmostEqual(result.peak, 0.2, places=5)
            self.assertEqual(result.device_used, "cpu")
            self.assertEqual(result.model_sha256, weights_sha256(manifest[DEFAULT_MODEL]))

    def test_no_auto_rescale(self) -> None:
        self.assertEqual(CLIP_POLICY, "no_demucs_rescale")
        FakeSeparator.instances.clear()

        class HotSeparator(FakeSeparator):
            def separate_tensor(self, wav: Any, sr: int | None = None) -> tuple[Any, dict[str, Any]]:
                hot = np.full((2, 16), 2.0, dtype=np.float32)
                return hot, {"vocals": hot}

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manifest = _write_htdemucs_fixture(repo, th_payload=b"hot")
            with (
                patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
                patch("perfectvoice_engine.separate._separator_cls", return_value=HotSeparator),
                patch(
                    "perfectvoice_engine.separate._to_model_input",
                    side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
                ),
                patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
            ):
                result = separate_vocals(_req(frames=16, peak=2.0), repo)
        # Rescale would squash 2.0 → ~1.0. We keep the sample peak.
        self.assertGreater(result.peak, 1.0)
        self.assertAlmostEqual(result.peak, 2.0, places=5)

    def test_vocals_only_bag_uses_local_signature(self) -> None:
        FakeSeparator.instances.clear()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            payload = b"vocals-specialist"
            (repo / "04573f0d-f3cf25b2.th").write_bytes(payload)
            manifest = {
                QUALITY_MODEL: {"04573f0d-f3cf25b2.th": _digest(payload)},
            }
            with (
                patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
                patch("perfectvoice_engine.separate._separator_cls", return_value=FakeSeparator),
                patch(
                    "perfectvoice_engine.separate._to_model_input",
                    side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
                ),
                patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
            ):
                req = _req(vocals_only_bag=True)
                self.assertEqual(separator_model_name(req), VOCALS_ONLY_SIG)
                separate_vocals(req, repo)
        self.assertEqual(FakeSeparator.instances[0].model, VOCALS_ONLY_SIG)
        self.assertEqual(FakeSeparator.instances[0].repo, repo)

    def test_cancel_callback_raises(self) -> None:
        event = threading.Event()
        event.set()
        cb_holder: dict[str, Any] = {}

        class Watching(FakeSeparator):
            def __init__(self, model: str = DEFAULT_MODEL, repo: Path | None = None, **kwargs: Any) -> None:
                super().__init__(model, repo, **kwargs)
                cb_holder["callback"] = kwargs.get("callback")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            manifest = _write_htdemucs_fixture(repo, th_payload=b"c")
            with (
                patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
                patch("perfectvoice_engine.separate._separator_cls", return_value=Watching),
                patch(
                    "perfectvoice_engine.separate._to_model_input",
                    side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
                ),
                patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
            ):
                with self.assertRaises(JobCancelled):
                    separate_vocals(_req(cancel_event=event), repo)
        callback = cb_holder["callback"]
        self.assertIsNotNone(callback)
        with self.assertRaises(JobCancelled):
            callback({"state": "start"})


class StaticLoadContractTests(unittest.TestCase):
    def test_no_official_remotes_in_load_path(self) -> None:
        for path in LOAD_PATHS:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("huggingface.co/adefossez", text)
            self.assertNotIn("dl.fbaipublicfiles.com", text)

    def test_no_bare_get_model_or_separator_without_repo(self) -> None:
        seen_ctor = 0
        for path in (
            ENGINE_DIR / "perfectvoice_engine" / "separate.py",
            ENGINE_DIR / "perfectvoice_engine" / "models.py",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _call_name(node.func) == "get_model":
                    self.fail(f"{path.name}:{node.lineno} calls get_model")
                if _is_separator_ctor(node):
                    seen_ctor += 1
                    keywords = {kw.arg for kw in node.keywords}
                    self.assertIn("repo", keywords, f"{path.name}:{node.lineno} Separator lacks repo=")
                    for kw in node.keywords:
                        if kw.arg == "repo":
                            self.assertFalse(
                                isinstance(kw.value, ast.Constant) and kw.value.value is None,
                                f"{path.name}:{node.lineno} Separator(repo=None)",
                            )
        self.assertGreaterEqual(seen_ctor, 1)

    def test_hub_offline_env_restored_after_separate(self) -> None:
        previous = os.environ.pop("HF_HUB_OFFLINE", None)
        previous_tf = os.environ.pop("TRANSFORMERS_OFFLINE", None)
        try:
            FakeSeparator.instances.clear()
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                manifest = _write_htdemucs_fixture(repo)
                with (
                    patch("perfectvoice_engine.models.load_manifest", return_value=manifest),
                    patch("perfectvoice_engine.separate._separator_cls", return_value=FakeSeparator),
                    patch(
                        "perfectvoice_engine.separate._to_model_input",
                        side_effect=lambda arr: np.ascontiguousarray(arr, dtype=np.float32),
                    ),
                    patch("perfectvoice_engine.separate.resolve_device", return_value="cpu"),
                ):
                    separate_vocals(_req(), repo)
            self.assertNotIn("HF_HUB_OFFLINE", os.environ)
            self.assertNotIn("TRANSFORMERS_OFFLINE", os.environ)
        finally:
            if previous is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = previous
            if previous_tf is None:
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
            else:
                os.environ["TRANSFORMERS_OFFLINE"] = previous_tf

    def test_load_modules_do_not_import_torch_or_demucs(self) -> None:
        banned = {"torch", "torchaudio", "demucs"}
        for path in (
            ENGINE_DIR / "perfectvoice_engine" / "separate.py",
            ENGINE_DIR / "perfectvoice_engine" / "models.py",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    self.assertNotIn(name.split(".", 1)[0], banned, f"{path.name} imports {name}")
        for name in banned:
            self.assertNotIn(name, sys.modules)

    def test_mps_stft_on_cpu_is_documented(self) -> None:
        src = (ENGINE_DIR / "perfectvoice_engine" / "separate.py").read_text(encoding="utf-8")
        self.assertIn("STFT", src)
        self.assertIn("MPS", src)
        from perfectvoice_engine import separate as sep_mod

        self.assertTrue(sep_mod._MPS_STFT_ON_CPU)

    def test_auto_device_without_torch_is_cpu(self) -> None:
        with patch("importlib.import_module", side_effect=ImportError("no torch")):
            self.assertEqual(resolve_device("auto"), "cpu")
        self.assertEqual(resolve_device("cpu"), "cpu")


if __name__ == "__main__":
    unittest.main()
