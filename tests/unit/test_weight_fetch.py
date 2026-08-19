"""User-click official weight download (mock HTTP only — no 80 MB pull).

Run: python3 -m unittest tests.unit.test_weight_fetch
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import secrets
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urljoin

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine.models import (  # noqa: E402
    DEFAULT_MODEL,
    QUALITY_MODEL,
    ModelNotInstalled,
    default_local_repo,
    require_model,
    sha256_file,
)
from perfectvoice_engine.separate import SeparateRequest, separate_vocals  # noqa: E402
from perfectvoice_engine import serve as serve_mod  # noqa: E402
from perfectvoice_engine.serve import EngineHTTPServer, JobStore  # noqa: E402
from perfectvoice_engine.weight_fetch import (  # noqa: E402
    ALLOWED_URL_PREFIXES,
    _ssl_context,
    FB_HYBRID,
    HF_CACHE_HTDEMUCS,
    HF_CACHE_HTDEMUCS_FT,
    HF_HTDEMUCS,
    ChecksumMismatch,
    HostNotAllowed,
    _AllowlistRedirectHandler,
    assert_url_allowed,
    candidate_urls,
    download_model,
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _htdemucs_payloads() -> dict[str, bytes]:
    return {
        "htdemucs.yaml": b"models: ['955717e8']\n",
        "955717e8-8726e21a.th": b"fixture-htdemucs-click",
    }


def _htdemucs_manifest(payloads: dict[str, bytes]) -> dict[str, dict[str, str]]:
    return {DEFAULT_MODEL: {name: _digest(data) for name, data in payloads.items()}}


class FakeResponse:
    def __init__(self, data: bytes, url: str) -> None:
        self._buf = io.BytesIO(data)
        self.url = url
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _http_error(url: str, code: int = 404) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "not found", hdrs=Message(), fp=io.BytesIO())


def _mock_urlopen(payloads: dict[str, bytes], hits: list[str], *, prefer_fb: bool = False):
    def urlopen(url: str, timeout: float | None = None) -> FakeResponse:
        hits.append(url)
        filename = url.rsplit("/", 1)[-1]
        if filename not in payloads:
            raise _http_error(url)
        if url.startswith(HF_HTDEMUCS) and filename.endswith(".th") and prefer_fb:
            raise _http_error(url)
        return FakeResponse(payloads[filename], url)

    return urlopen


class AllowlistTests(unittest.TestCase):
    def test_ssl_context_still_verifies(self) -> None:
        ctx = _ssl_context()
        self.assertEqual(ctx.verify_mode, __import__("ssl").CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_official_prefixes_are_listed(self) -> None:
        self.assertEqual(
            ALLOWED_URL_PREFIXES,
            (
                "https://huggingface.co/adefossez/HTDemucs",
                "https://huggingface.co/adefossez/HTDemucs-ft",
                "https://huggingface.co/api/resolve-cache/models/adefossez/HTDemucs",
                "https://huggingface.co/api/resolve-cache/models/adefossez/HTDemucs-ft",
                "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/",
            ),
        )

    def test_reject_off_allowlist(self) -> None:
        for url in (
            "https://evil.example/htdemucs.yaml",
            "http://huggingface.co/adefossez/HTDemucs/resolve/main/x",
            "https://huggingface.co/adefossez/other/resolve/main/x",
            "https://huggingface.co/api/resolve-cache/models/adefossez/other/x",
            "https://dl.fbaipublicfiles.com/demucs/mdx_final/x.th",
            f"{HF_HTDEMUCS}/resolve/main/../secret",
        ):
            with self.subTest(url=url):
                with self.assertRaises(HostNotAllowed):
                    assert_url_allowed(url)

    def test_candidates_stay_on_allowlist(self) -> None:
        urls = candidate_urls(DEFAULT_MODEL, "955717e8-8726e21a.th")
        self.assertTrue(urls[0].startswith(HF_HTDEMUCS + "/"))
        self.assertTrue(any(u.startswith(FB_HYBRID) for u in urls))
        for url in urls:
            assert_url_allowed(url)

    def test_hub_resolve_cache_307_is_allowed(self) -> None:
        # Live Hub 307s /resolve/main/* to this same-host path. Do not mock
        # the handler — this is the hop that used to raise HostNotAllowed.
        start = f"{HF_HTDEMUCS}/resolve/main/htdemucs.yaml"
        relative = "/api/resolve-cache/models/adefossez/HTDemucs/deadbeef/htdemucs.yaml"
        joined = urljoin(start, relative)
        self.assertTrue(joined.startswith(HF_CACHE_HTDEMUCS + "/"))
        assert_url_allowed(joined)
        handler = _AllowlistRedirectHandler()
        nxt = handler.redirect_request(
            urllib.request.Request(start),
            fp=None,
            code=307,
            msg="Temporary Redirect",
            headers=Message(),
            newurl=joined,
        )
        self.assertIsNotNone(nxt)
        assert nxt is not None
        self.assertEqual(nxt.full_url, joined)
        ft = urljoin(
            "https://huggingface.co/adefossez/HTDemucs-ft/resolve/main/htdemucs_ft.yaml",
            "/api/resolve-cache/models/adefossez/HTDemucs-ft/cafebabe/htdemucs_ft.yaml",
        )
        self.assertTrue(ft.startswith(HF_CACHE_HTDEMUCS_FT + "/"))
        assert_url_allowed(ft)

    def test_redirect_to_evil_is_rejected(self) -> None:
        handler = _AllowlistRedirectHandler()
        start = f"{HF_HTDEMUCS}/resolve/main/htdemucs.yaml"
        with self.assertRaises(HostNotAllowed):
            handler.redirect_request(
                urllib.request.Request(start),
                fp=None,
                code=307,
                msg="Temporary Redirect",
                headers=Message(),
                newurl="https://evil.example/htdemucs.yaml",
            )

    def test_dotdot_filename_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                download_model(
                    DEFAULT_MODEL,
                    Path(tmp),
                    manifest={DEFAULT_MODEL: {"..": "0" * 64}},
                )


class NoClickZeroRequestTests(unittest.TestCase):
    def test_empty_repo_separate_does_not_fetch(self) -> None:
        hits: list[str] = []
        payloads = _htdemucs_payloads()
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "perfectvoice_engine.weight_fetch._urlopen",
                _mock_urlopen(payloads, hits),
            ):
                with self.assertRaises(ModelNotInstalled):
                    separate_vocals(
                        SeparateRequest(
                            wav_44100_stereo=np.zeros((2, 8), dtype=np.float32),
                            device="cpu",
                        ),
                        Path(tmp),
                    )
        self.assertEqual(hits, [])

    def test_download_model_not_imported_by_load_path(self) -> None:
        banned = {"weight_fetch", "download_model"}
        for rel in (
            "perfectvoice_engine/separate.py",
            "perfectvoice_engine/models.py",
        ):
            path = ENGINE_DIR / rel
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module, *(alias.name for alias in node.names)]
                for name in names:
                    self.assertNotIn("weight_fetch", name)
                    self.assertNotIn(name, banned)

    def test_jobs_worker_never_calls_download(self) -> None:
        serve_path = ENGINE_DIR / "perfectvoice_engine" / "serve.py"
        tree = ast.parse(serve_path.read_text(encoding="utf-8"), filename=str(serve_path))
        create_fn = None
        stub_fn = None
        download_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "_create_job":
                    create_fn = node
                elif node.name == "_run_job":
                    stub_fn = node
                elif node.name == "_download_model":
                    download_fn = node
        self.assertIsNotNone(create_fn)
        self.assertIsNotNone(stub_fn)
        self.assertIsNotNone(download_fn)
        for fn in (create_fn, stub_fn):
            assert fn is not None
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id == "download_model":
                    self.fail(f"{fn.name} references download_model")
                if isinstance(node, ast.Attribute) and node.attr == "download_model":
                    self.fail(f"{fn.name} references download_model")


class ClickWritesHashTests(unittest.TestCase):
    def test_click_writes_files_matching_hash(self) -> None:
        payloads = _htdemucs_payloads()
        manifest = _htdemucs_manifest(payloads)
        hits: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch(
                "perfectvoice_engine.weight_fetch._urlopen",
                _mock_urlopen(payloads, hits),
            ):
                files = download_model(DEFAULT_MODEL, repo, manifest=manifest)
            for name, payload in payloads.items():
                path = repo / name
                self.assertTrue(path.is_file())
                self.assertEqual(path.read_bytes(), payload)
                self.assertEqual(sha256_file(path), _digest(payload))
                self.assertEqual(files[name], _digest(payload))
            require_model(DEFAULT_MODEL, repo, manifest=manifest)
        self.assertGreater(len(hits), 0)
        for url in hits:
            assert_url_allowed(url)

    def test_already_present_makes_zero_requests(self) -> None:
        payloads = _htdemucs_payloads()
        manifest = _htdemucs_manifest(payloads)
        hits: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for name, payload in payloads.items():
                (repo / name).write_bytes(payload)
            with patch(
                "perfectvoice_engine.weight_fetch._urlopen",
                _mock_urlopen(payloads, hits),
            ):
                download_model(DEFAULT_MODEL, repo, manifest=manifest)
        self.assertEqual(hits, [])

    def test_checksum_mismatch_does_not_publish(self) -> None:
        payloads = _htdemucs_payloads()
        manifest = {
            DEFAULT_MODEL: {
                "htdemucs.yaml": _digest(payloads["htdemucs.yaml"]),
                "955717e8-8726e21a.th": "0" * 64,
            }
        }
        hits: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch(
                "perfectvoice_engine.weight_fetch._urlopen",
                _mock_urlopen(payloads, hits),
            ):
                with self.assertRaises(ChecksumMismatch):
                    download_model(DEFAULT_MODEL, repo, manifest=manifest)
            self.assertFalse((repo / "955717e8-8726e21a.th").exists())
            self.assertFalse(list(repo.glob("*.part")))

    def test_hf_miss_falls_back_to_facebook(self) -> None:
        payloads = _htdemucs_payloads()
        manifest = _htdemucs_manifest(payloads)
        hits: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch(
                "perfectvoice_engine.weight_fetch._urlopen",
                _mock_urlopen(payloads, hits, prefer_fb=True),
            ):
                download_model(DEFAULT_MODEL, repo, manifest=manifest)
            self.assertEqual(
                (repo / "955717e8-8726e21a.th").read_bytes(),
                payloads["955717e8-8726e21a.th"],
            )
        self.assertTrue(any(u.startswith(HF_HTDEMUCS) for u in hits))
        self.assertTrue(any(u.startswith(FB_HYBRID) for u in hits))

    def test_unknown_model_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                download_model("mdx_extra", Path(tmp))


class DownloadEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-dl-"))
        self.token = secrets.token_hex(32)
        self.store = JobStore()
        self.httpd = EngineHTTPServer(
            ("127.0.0.1", 0),
            self.token,
            self.store,
            0,
            local_repo=self.tmp,
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        _host, port = self.httpd.server_address
        self.url = f"http://127.0.0.1:{port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)

    def _request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        accept: str | None = None,
    ) -> tuple[int, Any]:
        import urllib.request

        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if accept:
            headers["Accept"] = accept
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                try:
                    parsed: Any = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    parsed = raw.decode("utf-8")
                return resp.status, parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                parsed = raw
            return exc.code, parsed

    def test_click_endpoint_writes_hashed_file(self) -> None:
        payloads = _htdemucs_payloads()
        manifest = _htdemucs_manifest(payloads)
        man = self.tmp / "manifest.json"
        man.write_text(json.dumps(manifest), encoding="utf-8")
        hits: list[str] = []
        with (
            patch.dict(os.environ, {"PERFECTVOICE_MANIFEST": str(man)}),
            patch(
                "perfectvoice_engine.weight_fetch._urlopen",
                _mock_urlopen(payloads, hits),
            ),
        ):
            status, body = self._request("POST", "/v1/models/download", {"name": DEFAULT_MODEL})
        self.assertEqual(status, 200)
        assert isinstance(body, dict)
        self.assertTrue(body.get("ready"))
        self.assertEqual(body.get("name"), DEFAULT_MODEL)
        for name, payload in payloads.items():
            self.assertEqual((self.tmp / name).read_bytes(), payload)
            self.assertEqual(sha256_file(self.tmp / name), _digest(payload))
        self.assertGreater(len(hits), 0)

    def test_job_post_does_not_fetch(self) -> None:
        hits: list[str] = []
        payloads = _htdemucs_payloads()
        media = self.tmp / "media"
        out = self.tmp / "out"
        media.mkdir()
        out.mkdir()
        src = media / "clip.mov"
        src.write_bytes(b"")
        fixture = REPO_ROOT / "tests" / "fixtures" / "schemas"
        clip = json.loads((fixture / "clip.valid.json").read_text(encoding="utf-8"))
        params = json.loads((fixture / "params.valid.json").read_text(encoding="utf-8"))
        clip["source_path"] = str(src)
        params["output_dir"] = str(out)
        params["allowed_roots"] = [str(media), str(out)]
        job = {
            "clips": [clip],
            "params": params,
            "allowed_roots": [str(media), str(out)],
            "output_dir": str(out),
        }
        with patch(
            "perfectvoice_engine.weight_fetch._urlopen",
            _mock_urlopen(payloads, hits),
        ):
            status, body = self._request("POST", "/v1/jobs", job)
            self.assertEqual(status, 202)
            assert isinstance(body, dict)
            self.assertEqual(body.get("status"), "queued")
            with patch.object(serve_mod, "STUB_HOLD_SECONDS", 0):
                self.store._run_job(str(body["id"]))
        job_row = self.store.get(str(body["id"]))
        assert job_row is not None
        self.assertEqual(job_row.status, "error")
        self.assertEqual(hits, [])
        self.assertEqual(list(self.tmp.glob("*.th")), [])

    def test_unknown_name_is_400(self) -> None:
        status, body = self._request("POST", "/v1/models/download", {"name": "mdx_extra"})
        self.assertEqual(status, 400)
        assert isinstance(body, dict)
        self.assertEqual(body.get("error"), "validation_error")

    def test_fetch_error_is_502_and_does_not_publish(self) -> None:
        payloads = _htdemucs_payloads()
        manifest = _htdemucs_manifest(payloads)
        man = self.tmp / "manifest.json"
        man.write_text(json.dumps(manifest), encoding="utf-8")

        def boom(url: str, timeout: float | None = None) -> FakeResponse:
            raise urllib.error.URLError("mocked down")

        with (
            patch.dict(os.environ, {"PERFECTVOICE_MANIFEST": str(man)}),
            patch("perfectvoice_engine.weight_fetch._urlopen", boom),
        ):
            status, body = self._request("POST", "/v1/models/download", {"name": DEFAULT_MODEL})
        self.assertEqual(status, 502)
        assert isinstance(body, dict)
        self.assertEqual(body.get("error"), "download_failed")
        self.assertFalse((self.tmp / "955717e8-8726e21a.th").exists())
        self.assertFalse((self.tmp / "htdemucs.yaml").exists())

    def test_sse_progress_on_accept(self) -> None:
        payloads = _htdemucs_payloads()
        manifest = _htdemucs_manifest(payloads)
        man = self.tmp / "manifest.json"
        man.write_text(json.dumps(manifest), encoding="utf-8")
        hits: list[str] = []
        with (
            patch.dict(os.environ, {"PERFECTVOICE_MANIFEST": str(man)}),
            patch(
                "perfectvoice_engine.weight_fetch._urlopen",
                _mock_urlopen(payloads, hits),
            ),
        ):
            status, body = self._request(
                "POST",
                "/v1/models/download",
                {"name": DEFAULT_MODEL},
                accept="text/event-stream",
            )
        self.assertEqual(status, 200)
        text = body if isinstance(body, str) else str(body)
        self.assertIn("event: done", text)
        self.assertIn(DEFAULT_MODEL, text)


class CliTests(unittest.TestCase):
    def test_cli_writes_hashed_file(self) -> None:
        script = REPO_ROOT / "scripts" / "download_demucs.py"
        spec = __import__("importlib.util", fromlist=["spec_from_file_location"]).spec_from_file_location(
            "download_demucs", script
        )
        assert spec is not None and spec.loader is not None
        mod = __import__("importlib.util", fromlist=["module_from_spec"]).module_from_spec(spec)
        spec.loader.exec_module(mod)
        payloads = _htdemucs_payloads()
        manifest = _htdemucs_manifest(payloads)
        hits: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            man = Path(tmp) / "manifest.json"
            man.write_text(json.dumps(manifest), encoding="utf-8")
            dest = Path(tmp) / "repo"
            dest.mkdir()
            with (
                patch.dict(os.environ, {"PERFECTVOICE_MANIFEST": str(man)}),
                patch(
                    "perfectvoice_engine.weight_fetch._urlopen",
                    _mock_urlopen(payloads, hits),
                ),
            ):
                rc = mod.main(["--name", DEFAULT_MODEL, "--repo", str(dest)])
            self.assertEqual(rc, 0)
            self.assertEqual(
                (dest / "955717e8-8726e21a.th").read_bytes(),
                payloads["955717e8-8726e21a.th"],
            )
        self.assertGreater(len(hits), 0)

    def test_cli_mentions_allowlist_hosts(self) -> None:
        text = (REPO_ROOT / "scripts" / "download_demucs.py").read_text(encoding="utf-8")
        self.assertIn("https://huggingface.co/adefossez/HTDemucs", text)
        self.assertIn("https://huggingface.co/adefossez/HTDemucs-ft", text)
        self.assertIn("https://huggingface.co/api/resolve-cache/models/adefossez/HTDemucs", text)
        self.assertIn("https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/", text)


class DefaultRepoTests(unittest.TestCase):
    def test_default_repo_is_not_engine_tree(self) -> None:
        repo = default_local_repo()
        self.assertNotEqual(repo, ENGINE_DIR / "models")


class QualityCandidatesTests(unittest.TestCase):
    def test_ft_uses_ft_hub_repo(self) -> None:
        urls = candidate_urls(QUALITY_MODEL, "htdemucs_ft.yaml")
        self.assertTrue(urls[0].startswith("https://huggingface.co/adefossez/HTDemucs-ft/"))
        self.assertFalse(any(u.startswith(FB_HYBRID) for u in urls))


if __name__ == "__main__":
    unittest.main()
