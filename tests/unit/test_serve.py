"""Localhost sidecar contract tests (no Resolve, no torch).

Run: python3 -m unittest tests.unit.test_serve
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "schemas"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine.serve import (  # noqa: E402
    BindError,
    MEMORY_CAP_BYTES,
    PathNotAllowed,
    TokenError,
    check_allowed_root,
    check_path_under_roots,
    load_token_file,
    needs_low_memory,
    parse_token,
    pcm_nbytes,
    validate_bind,
    validate_job_request,
)


def _token() -> str:
    return secrets.token_hex(32)


def _load_fixture(name: str) -> object:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _job_payload(source_path: str, output_dir: str, allowed_roots: list[str]) -> dict:
    clip = _load_fixture("clip.valid.json")
    params = _load_fixture("params.valid.json")
    assert isinstance(clip, dict)
    assert isinstance(params, dict)
    clip["source_path"] = source_path
    params["output_dir"] = output_dir
    params["allowed_roots"] = list(allowed_roots)
    return {
        "clips": [clip],
        "params": params,
        "allowed_roots": list(allowed_roots),
        "output_dir": output_dir,
    }


class _RunningEngine:
    def __init__(
        self,
        *,
        bind: str = "127.0.0.1",
        token_file: bool = True,
        token: str | None = None,
        idle_seconds: int = 0,
        extra: list[str] | None = None,
        hold: str = "2",
    ) -> None:
        self.token = token or _token()
        self.token_path: Path | None = None
        self.url = ""
        self.proc: subprocess.Popen[bytes] | None = None
        self._stderr: list[bytes] = []
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ENGINE_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        env["PERFECTVOICE_STUB_HOLD"] = hold
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "perfectvoice_engine.serve",
            "serve",
            "--bind",
            bind,
            "--port",
            "0",
            "--idle-seconds",
            str(idle_seconds),
        ]
        if extra:
            cmd.extend(extra)
        stdin = subprocess.PIPE
        if token_file:
            tmp = Path(tempfile.mkdtemp(prefix="pv-token-"))
            self.token_path = tmp / "run.token"
            self.token_path.write_text(self.token, encoding="ascii")
            os.chmod(self.token_path, 0o600)
            cmd.extend(["--token-file", str(self.token_path)])
        self.proc = subprocess.Popen(
            cmd,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(REPO_ROOT),
        )
        if not token_file:
            assert self.proc.stdin is not None
            self.proc.stdin.write((self.token + "\n").encode("ascii"))
            self.proc.stdin.close()
        self._err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._err_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                self._stderr.append(line)
        except Exception:
            pass

    def stderr_text(self) -> str:
        return b"".join(self._stderr).decode("utf-8", errors="replace")

    def wait_ready(self, timeout: float = 8.0) -> str:
        assert self.proc is not None and self.proc.stdout is not None
        holder: list[bytes] = []

        def _read() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            holder.append(self.proc.stdout.readline())

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if self.proc.poll() is not None and not holder:
            raise RuntimeError(
                f"engine exited {self.proc.returncode} before READY: {self.stderr_text()}"
            )
        if not holder:
            raise TimeoutError(f"no READY line: {self.stderr_text()}")
        line = holder[0].decode("utf-8", errors="replace").strip()
        if not line.startswith("READY "):
            raise RuntimeError(f"expected READY, got {line!r}: {self.stderr_text()}")
        self.url = line.split(" ", 1)[1].strip().rstrip("/")
        return self.url

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        token: str | None | object = ...,
        timeout: float = 5.0,
    ) -> tuple[int, object, dict[str, str]]:
        headers: dict[str, str] = {}
        auth = self.token if token is ... else token
        if auth is not None:
            headers["Authorization"] = f"Bearer {auth}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                parsed: object
                try:
                    parsed = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    parsed = raw
                return resp.status, parsed, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                try:
                    parsed = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    parsed = raw
                return exc.code, parsed, {k.lower(): v for k, v in exc.headers.items()}
            finally:
                exc.close()

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        for stream in (self.proc.stdout, self.proc.stdin, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._err_thread.join(timeout=1)


def _run_cli(bind: str, extra: list[str] | None = None, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ENGINE_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "perfectvoice_engine.serve",
        "serve",
        "--bind",
        bind,
        "--port",
        "0",
        "--token-file",
        "/nonexistent.token",
    ]
    if extra:
        cmd.extend(extra)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
        env=env,
    )


class BindAndTokenUnitTests(unittest.TestCase):
    def test_low_memory_cap_is_two_gib(self) -> None:
        # duration * rate * ch * 4 > 2 GiB trips the windowed / low-memory path.
        self.assertEqual(MEMORY_CAP_BYTES, 2 * 1024 ** 3)
        rate, ch = 44100, 2
        # ~90 min stereo f32 @ 44.1 k is under; ~102 min is over.
        self.assertFalse(needs_low_memory(90 * 60, rate, ch))
        self.assertTrue(needs_low_memory(102 * 60, rate, ch))
        self.assertGreater(pcm_nbytes(102 * 60, rate, ch), MEMORY_CAP_BYTES)

    def test_reject_wan_bind_values(self) -> None:
        for host in ("0.0.0.0", "::", "", "localhost", "127.0.0.2"):
            with self.subTest(host=host):
                with self.assertRaises(BindError):
                    validate_bind(host)

    def test_accept_loopback_only(self) -> None:
        validate_bind("127.0.0.1")

    def test_token_must_be_256_bit_hex(self) -> None:
        parse_token(_token())
        with self.assertRaises(TokenError):
            parse_token("deadbeef")
        with self.assertRaises(TokenError):
            parse_token("g" * 64)

    def test_token_file_unlinked_and_mode_enforced(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="pv-tok-"))
        path = tmp / "a.token"
        token = _token()
        path.write_text(token + "\n", encoding="ascii")
        os.chmod(path, 0o600)
        self.assertEqual(load_token_file(path), token)
        self.assertFalse(path.exists())

        path.write_text(token, encoding="ascii")
        os.chmod(path, 0o644)
        with self.assertRaises(TokenError):
            load_token_file(path)
        self.assertFalse(path.exists())

        path.write_bytes(b"\xff\xfe not-ascii")
        os.chmod(path, 0o600)
        with self.assertRaises(TokenError):
            load_token_file(path)
        self.assertFalse(path.exists())

    def test_path_outside_and_dotdot_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="pv-root-"))
        media = tmp / "media"
        media.mkdir()
        roots = [str(media)]
        check_path_under_roots(str(media / "clip.mov"), roots)
        with self.assertRaises(PathNotAllowed):
            check_path_under_roots(str(tmp / "other" / "x.mov"), roots)
        with self.assertRaises(PathNotAllowed):
            check_path_under_roots(str(media / ".." / "other" / "x.mov"), roots)

    def test_filesystem_root_is_not_an_allowed_root(self) -> None:
        with self.assertRaises(PathNotAllowed) as ctx:
            check_allowed_root(os.sep)
        self.assertIn("filesystem root", str(ctx.exception))

    def test_symlink_escape_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="pv-link-"))
        media = tmp / "media"
        media.mkdir()
        outside = tmp / "secret.mov"
        outside.write_bytes(b"x")
        link = media / "link.mov"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink not permitted: {exc}")
        with self.assertRaises(PathNotAllowed):
            check_path_under_roots(str(link), [str(media)])


class SchemaGateUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-job-"))
        self.media = self.tmp / "media"
        self.out = self.tmp / "out"
        self.media.mkdir()
        self.out.mkdir()
        self.src = self.media / "clip.mov"
        self.src.write_bytes(b"")
        self.roots = [str(self.media), str(self.out)]

    def test_wet_dry_sample_rate_rejected(self) -> None:
        body = _job_payload(str(self.src), str(self.out), self.roots)
        body["params"]["wet_dry_sample_rate"] = 44100
        with self.assertRaises(Exception) as ctx:
            validate_job_request(body)
        self.assertIn("wet_dry_sample_rate", str(ctx.exception))

    def test_source_channels_6_rejected(self) -> None:
        body = _job_payload(str(self.src), str(self.out), self.roots)
        body["clips"][0]["source_channels"] = 6
        with self.assertRaises(Exception) as ctx:
            validate_job_request(body)
        self.assertIn("source_channels", str(ctx.exception))

    def test_allowed_roots_must_match_params(self) -> None:
        body = _job_payload(str(self.src), str(self.out), self.roots)
        body["params"]["allowed_roots"] = [str(self.media)]
        with self.assertRaises(Exception) as ctx:
            validate_job_request(body)
        self.assertIn("allowed_roots", str(ctx.exception))

    def test_filesystem_root_job_rejected(self) -> None:
        body = _job_payload("/etc/passwd", os.sep, [os.sep])
        with self.assertRaises(PathNotAllowed):
            validate_job_request(body)

    def test_persists_canonical_paths(self) -> None:
        body = _job_payload(str(self.src), str(self.out), self.roots)
        got = validate_job_request(body)
        self.assertEqual(got["output_dir"], str(self.out.resolve()))
        self.assertEqual(got["params"]["output_dir"], str(self.out.resolve()))
        self.assertEqual(got["clips"][0]["source_path"], str(self.src.resolve()))
        self.assertEqual(got["allowed_roots"], [str(p.resolve()) for p in (self.media, self.out)])
        self.assertEqual(got["params"]["allowed_roots"], got["allowed_roots"])


class CliBindTests(unittest.TestCase):
    def test_reject_wan_bind_cli(self) -> None:
        for host in ("0.0.0.0", "::", ""):
            with self.subTest(host=host):
                proc = _run_cli(host)
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn("READY", proc.stdout)
                self.assertIn("refusing bind", proc.stderr)

    def test_reject_token_fd(self) -> None:
        proc = _run_cli("127.0.0.1", extra=["--token-fd", "3"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("READY", proc.stdout)
        self.assertIn("--token-fd is not supported", proc.stderr)
        self.assertIn("token-file or stdin", proc.stderr)


class SidecarHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engines: list[_RunningEngine] = []
        self.tmp = Path(tempfile.mkdtemp(prefix="pv-http-"))
        self.media = self.tmp / "media"
        self.out = self.tmp / "out"
        self.media.mkdir()
        self.out.mkdir()
        self.src = self.media / "clip.mov"
        self.src.write_bytes(b"")
        self.roots = [str(self.media), str(self.out)]

    def tearDown(self) -> None:
        for eng in self.engines:
            eng.stop()

    def start(self, **kwargs: object) -> _RunningEngine:
        eng = _RunningEngine(**kwargs)  # type: ignore[arg-type]
        self.engines.append(eng)
        eng.wait_ready()
        return eng

    def payload(self) -> dict:
        return _job_payload(str(self.src), str(self.out), self.roots)

    def test_token_file_unlinked_and_bearer_required(self) -> None:
        eng = self.start()
        assert eng.token_path is not None
        self.assertFalse(eng.token_path.exists())
        status, body, _ = eng.request("GET", "/v1/health", token=None)
        self.assertEqual(status, 401)
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("error"), "unauthorized")
        status, body, _ = eng.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        assert isinstance(body, dict)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("protocol_version"), 1)
        self.assertNotIn(eng.token, eng.stderr_text())

    def test_stdin_token(self) -> None:
        eng = self.start(token_file=False)
        status, body, _ = eng.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        assert isinstance(body, dict)
        self.assertEqual(body.get("protocol_version"), 1)

    def test_capabilities_and_no_model_download(self) -> None:
        eng = self.start()
        status, body, _ = eng.request("GET", "/v1/capabilities")
        self.assertEqual(status, 200)
        assert isinstance(body, dict)
        self.assertEqual(body.get("protocol_version"), 1)
        self.assertIn("devices", body)
        self.assertIn("models_ready", body)
        self.assertFalse(body["models_ready"])
        self.assertEqual(body.get("window_seconds"), 600.0)
        self.assertEqual(body.get("window_overlap_seconds"), 1.0)
        self.assertEqual(body.get("memory_cap_bytes"), MEMORY_CAP_BYTES)
        status, body, _ = eng.request("POST", "/v1/models/download", body={"name": "htdemucs"})
        self.assertEqual(status, 404)

    def test_serve_does_not_import_ml(self) -> None:
        self.assertNotIn("demucs", sys.modules)
        self.assertNotIn("torch", sys.modules)
        src = (ENGINE_DIR / "perfectvoice_engine" / "serve.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"(?m)^\s*(import|from)\s+(demucs|torch)\b")

    def test_job_path_outside_allowed_roots(self) -> None:
        eng = self.start()
        body = self.payload()
        body["clips"][0]["source_path"] = "/etc/passwd"
        status, resp, _ = eng.request("POST", "/v1/jobs", body)
        self.assertEqual(status, 400)
        assert isinstance(resp, dict)
        self.assertEqual(resp.get("error"), "path_not_allowed")

    def test_job_dotdot_escape_http(self) -> None:
        eng = self.start()
        body = self.payload()
        body["clips"][0]["source_path"] = str(self.media / ".." / "outside.mov")
        status, resp, _ = eng.request("POST", "/v1/jobs", body)
        self.assertEqual(status, 400)
        assert isinstance(resp, dict)
        self.assertEqual(resp.get("error"), "path_not_allowed")

    def test_job_output_dir_outside_roots(self) -> None:
        eng = self.start()
        other = self.tmp / "other"
        other.mkdir()
        body = self.payload()
        body["output_dir"] = str(other)
        body["params"]["output_dir"] = str(other)
        status, resp, _ = eng.request("POST", "/v1/jobs", body)
        self.assertEqual(status, 400)
        assert isinstance(resp, dict)
        self.assertEqual(resp.get("error"), "path_not_allowed")

    def test_job_symlink_escape_http(self) -> None:
        outside = self.tmp / "secret.mov"
        outside.write_bytes(b"x")
        link = self.media / "link.mov"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink not permitted: {exc}")
        eng = self.start()
        body = self.payload()
        body["clips"][0]["source_path"] = str(link)
        status, resp, _ = eng.request("POST", "/v1/jobs", body)
        self.assertEqual(status, 400)
        assert isinstance(resp, dict)
        self.assertEqual(resp.get("error"), "path_not_allowed")

    def test_job_filesystem_root_allowed_roots(self) -> None:
        eng = self.start()
        body = self.payload()
        body["allowed_roots"] = [os.sep]
        body["params"]["allowed_roots"] = [os.sep]
        body["clips"][0]["source_path"] = "/etc/passwd"
        body["output_dir"] = os.sep
        body["params"]["output_dir"] = os.sep
        status, resp, _ = eng.request("POST", "/v1/jobs", body)
        self.assertEqual(status, 400)
        assert isinstance(resp, dict)
        self.assertEqual(resp.get("error"), "path_not_allowed")

    def test_job_allowed_roots_mismatch(self) -> None:
        eng = self.start()
        body = self.payload()
        body["params"]["allowed_roots"] = [str(self.media)]
        status, resp, _ = eng.request("POST", "/v1/jobs", body)
        self.assertEqual(status, 400)
        assert isinstance(resp, dict)
        self.assertEqual(resp.get("error"), "validation_error")

    def test_job_wet_dry_sample_rate_http(self) -> None:
        eng = self.start()
        body = self.payload()
        body["params"]["wet_dry_sample_rate"] = 44100
        status, resp, _ = eng.request("POST", "/v1/jobs", body)
        self.assertEqual(status, 400)
        assert isinstance(resp, dict)
        self.assertIn("wet_dry_sample_rate", str(resp))

    def test_job_source_channels_6_http(self) -> None:
        eng = self.start()
        body = self.payload()
        body["clips"][0]["source_channels"] = 6
        status, resp, _ = eng.request("POST", "/v1/jobs", body)
        self.assertEqual(status, 400)
        assert isinstance(resp, dict)
        self.assertIn("source_channels", str(resp))

    def test_cancel_transitions(self) -> None:
        eng = self.start(hold="30")
        status, accepted, _ = eng.request("POST", "/v1/jobs", self.payload())
        self.assertEqual(status, 202)
        assert isinstance(accepted, dict)
        self.assertEqual(accepted.get("status"), "queued")
        job_id = accepted["id"]
        status, cancelled, _ = eng.request("POST", f"/v1/jobs/{job_id}/cancel")
        self.assertEqual(status, 202)
        assert isinstance(cancelled, dict)
        self.assertIn(cancelled.get("status"), ("cancelled", "cancelling"))
        status, snap, _ = eng.request("GET", f"/v1/jobs/{job_id}")
        self.assertEqual(status, 200)
        assert isinstance(snap, dict)
        self.assertEqual(snap.get("status"), "cancelled")
        status, again, _ = eng.request("POST", f"/v1/jobs/{job_id}/cancel")
        self.assertEqual(status, 409)
        assert isinstance(again, dict)
        self.assertEqual(again.get("error"), "conflict")

    def test_stub_worker_errors_not_wired(self) -> None:
        eng = self.start(hold="0.2")
        status, accepted, _ = eng.request("POST", "/v1/jobs", self.payload())
        self.assertEqual(status, 202)
        assert isinstance(accepted, dict)
        job_id = accepted["id"]
        deadline = time.time() + 5
        snap: dict = {}
        while time.time() < deadline:
            status, body, _ = eng.request("GET", f"/v1/jobs/{job_id}")
            self.assertEqual(status, 200)
            assert isinstance(body, dict)
            snap = body
            if body.get("status") == "error":
                break
            time.sleep(0.05)
        self.assertEqual(snap.get("status"), "error")
        self.assertEqual(snap.get("error"), "engine_io_not_wired")

    def test_sse_error_event(self) -> None:
        eng = self.start(hold="0.2")
        status, accepted, _ = eng.request("POST", "/v1/jobs", self.payload())
        self.assertEqual(status, 202)
        assert isinstance(accepted, dict)
        job_id = accepted["id"]
        req = urllib.request.Request(
            f"{eng.url}/v1/jobs/{job_id}/events",
            headers={"Authorization": f"Bearer {eng.token}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            payload = resp.read().decode("utf-8")
        self.assertIn("event: error", payload)
        self.assertIn("engine_io_not_wired", payload)

    def test_chaos_process_death_drops_connection(self) -> None:
        eng = self.start()
        status, _, _ = eng.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        assert eng.proc is not None
        eng.proc.kill()
        eng.proc.wait(timeout=5)
        with self.assertRaises(urllib.error.URLError):
            eng.request("GET", "/v1/health")

    def test_wrong_bearer_is_401(self) -> None:
        eng = self.start()
        status, body, _ = eng.request("GET", "/v1/health", token=_token())
        self.assertEqual(status, 401)
        assert isinstance(body, dict)
        self.assertEqual(body.get("error"), "unauthorized")

    def test_bearer_required_before_method_dispatch(self) -> None:
        eng = self.start()
        status, body, _ = eng.request("POST", "/v1/jobs", self.payload(), token=None)
        self.assertEqual(status, 401)
        assert isinstance(body, dict)
        self.assertEqual(body.get("error"), "unauthorized")
        status, body, _ = eng.request("PUT", "/v1/health", token=None)
        self.assertEqual(status, 401)
        status, body, _ = eng.request("DELETE", "/v1/health", token=None)
        self.assertEqual(status, 401)
        status, body, _ = eng.request("PUT", "/v1/health")
        self.assertEqual(status, 405)
        assert isinstance(body, dict)
        self.assertEqual(body.get("error"), "method_not_allowed")


if __name__ == "__main__":
    unittest.main()
