"""Localhost sidecar HTTP server.

Auth is a 256-bit hex token from a one-shot 0600 file or one stdin line.
``--token-fd`` is rejected: fd 3 is not portable to Win32.

Idle-exit after ``--idle-seconds`` (default 1800). Lock file is not
implemented in this revision.

The worker runs extract → resample → separate → [dfn] → blend → cache → WAV.
Weight download is a sibling PR; jobs only load a local repo.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from perfectvoice_engine import PROTOCOL_VERSION
from perfectvoice_engine import __version__ as ENGINE_VERSION
from perfectvoice_engine.constants import (
    MEMORY_CAP_BYTES,
    WINDOW_OVERLAP_SECONDS,
    WINDOW_SECONDS,
    JobCancelled,
    pcm_nbytes,
    raise_if_cancelled,
)
from perfectvoice_engine.models import (
    ALLOWED_MODELS,
    VOCALS_ONLY_SIG,
    ModelNotInstalled,
    default_local_repo,
    models_ready,
    require_model,
)
from perfectvoice_engine.weight_fetch import WeightFetchError, download_model

ALLOWED_BIND = "127.0.0.1"
TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_BODY = 16 * 1024 * 1024
# Test-only: hold after dequeue so cancel can win before extract.
STUB_HOLD_SECONDS = float(os.environ.get("PERFECTVOICE_STUB_HOLD", "0"))
TERMINAL = frozenset({"done", "error", "cancelled"})
JOB_PATH = re.compile(r"^/v1/jobs/([^/]+)$")
JOB_CANCEL = re.compile(r"^/v1/jobs/([^/]+)/cancel$")
JOB_EVENTS = re.compile(r"^/v1/jobs/([^/]+)/events$")

CREATE_JOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["clips", "params", "allowed_roots", "output_dir"],
    "properties": {
        "clips": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "https://perfectvoice.local/schema/clip.v1.json"},
        },
        "params": {"$ref": "https://perfectvoice.local/schema/params.v1.json"},
        "allowed_roots": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "output_dir": {"type": "string", "minLength": 1},
    },
}


class BindError(ValueError):
    pass


class TokenError(ValueError):
    pass


class PathNotAllowed(ValueError):
    def __init__(self, path: str, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.reason = reason


class JobConflict(Exception):
    def __init__(self, job: Job) -> None:
        super().__init__("job already terminal")
        self.job = job


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def validate_bind(host: str) -> None:
    if host != ALLOWED_BIND:
        raise BindError(f"refusing bind {host!r}: only {ALLOWED_BIND} is allowed")


def parse_token(raw: str) -> str:
    token = raw.strip()
    if not TOKEN_RE.fullmatch(token):
        raise TokenError("token must be 256-bit hex")
    return token.lower()


def load_token_file(path: Path) -> str:
    try:
        st = path.stat()
    except OSError as exc:
        raise TokenError("token file not readable") from exc
    too_open = os.name != "nt" and (st.st_mode & 0o077) != 0
    try:
        raw = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise TokenError("token file not readable") from exc
    finally:
        # One-shot: unlink even if the contents or mode are bad.
        try:
            path.unlink()
        except OSError as exc:
            raise TokenError("failed to unlink token file") from exc
    if too_open:
        raise TokenError("token file mode must be 0600")
    return parse_token(raw)


def load_token_stdin() -> str:
    line = sys.stdin.readline()
    return parse_token(line)


def contains_dotdot(path: str) -> bool:
    return any(part == ".." for part in path.replace("\\", "/").split("/"))


def canonicalize(path: str) -> Path:
    return Path(path).expanduser().resolve()


def is_filesystem_root(path: Path) -> bool:
    # Panel roots are user-picked media/output dirs, never "/" or "C:\\".
    return path == Path(path.anchor)


def is_under_root(path: Path, root: Path) -> bool:
    try:
        if os.name == "nt":
            path = Path(os.path.normcase(os.path.normpath(str(path))))
            root = Path(os.path.normcase(os.path.normpath(str(root))))
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_raw_path(path: str) -> None:
    if not path or "\x00" in path:
        raise PathNotAllowed(path, "empty or NUL path")
    # Reject `..` on the raw string so we never rely on resolve() alone.
    if contains_dotdot(path):
        raise PathNotAllowed(path, "path contains '..'")


def check_allowed_root(root: str) -> Path:
    _reject_raw_path(root)
    resolved = canonicalize(root)
    if is_filesystem_root(resolved):
        raise PathNotAllowed(root, "filesystem root is not an allowed_root")
    return resolved


def check_path_under_roots(path: str, roots: list[str]) -> Path:
    _reject_raw_path(path)
    resolved = canonicalize(path)
    canon_roots = [canonicalize(root) for root in roots]
    if not any(is_under_root(resolved, root) for root in canon_roots):
        raise PathNotAllowed(path, "path is outside allowed_roots")
    return resolved


def find_schema_dir() -> Path:
    env = os.environ.get("PERFECTVOICE_SCHEMA_DIR")
    if env:
        candidate = Path(env)
        if (candidate / "clip.v1.json").is_file():
            return candidate
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "shared" / "schema"
        if (candidate / "clip.v1.json").is_file():
            return candidate
    raise RuntimeError("cannot find shared/schema (set PERFECTVOICE_SCHEMA_DIR)")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


class SchemaBundle:
    def __init__(self, schema_dir: Path) -> None:
        resources: list[tuple[str, Resource]] = []
        for path in sorted(schema_dir.glob("*.json")):
            contents = _load_json(path)
            if isinstance(contents, dict) and "$id" in contents:
                resources.append((contents["$id"], Resource.from_contents(contents)))
        registry = Registry().with_resources(resources)
        self.create_job = Draft202012Validator(
            CREATE_JOB_SCHEMA,
            registry=registry,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )


_SCHEMAS: SchemaBundle | None = None


def schemas() -> SchemaBundle:
    global _SCHEMAS
    if _SCHEMAS is None:
        _SCHEMAS = SchemaBundle(find_schema_dir())
    return _SCHEMAS


def validation_detail(exc: ValidationError) -> str:
    path = ".".join(str(p) for p in exc.absolute_path) or "(root)"
    return f"{path}: {exc.message}"


def _canon_list(paths: list[str]) -> list[Path]:
    return [canonicalize(p) for p in paths]


def validate_job_request(body: object) -> dict[str, Any]:
    try:
        schemas().create_job.validate(body)
    except ValidationError as exc:
        raise ValidationError(validation_detail(exc)) from exc
    assert isinstance(body, dict)
    top_roots = [str(r) for r in body["allowed_roots"]]
    param_roots = [str(r) for r in body["params"]["allowed_roots"]]
    if _canon_list(top_roots) != _canon_list(param_roots):
        raise ValidationError("allowed_roots does not match params.allowed_roots")
    if canonicalize(str(body["output_dir"])) != canonicalize(str(body["params"]["output_dir"])):
        raise ValidationError("output_dir does not match params.output_dir")
    # Persist resolved paths so a later I/O PR does not reopen the raw client strings.
    canon_roots = [str(check_allowed_root(r)) for r in top_roots]
    output_dir = str(check_path_under_roots(str(body["output_dir"]), canon_roots))
    clips: list[dict[str, Any]] = []
    for clip in body["clips"]:
        rewritten = dict(clip)
        rewritten["source_path"] = str(
            check_path_under_roots(str(clip["source_path"]), canon_roots)
        )
        clips.append(rewritten)
    params = dict(body["params"])
    params["allowed_roots"] = canon_roots
    params["output_dir"] = output_dir
    return {
        "clips": clips,
        "params": params,
        "allowed_roots": canon_roots,
        "output_dir": output_dir,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_local_model(params: dict[str, Any]) -> None:
    """Numpy-free fail-closed check. Never fetches weights."""
    name = VOCALS_ONLY_SIG if params.get("vocals_only_bag") else str(params["model"])
    require_model(name, default_local_repo())


def write_job_json(job: Job, results: list[dict[str, Any]]) -> Path:
    payload = {
        "schema": "perfectvoice.job.v1",
        "id": job.id,
        "created_at": job.created_at,
        "engine_version": ENGINE_VERSION,
        "params": job.params,
        "clips": results,
    }
    dest = Path(job.output_dir) / "job.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return dest


def needs_low_memory(
    n_samples: int,
    channels: int,
    *,
    cap: int | None = None,
) -> bool:
    """Integer-sample form of the §3.4 cap (same ``pcm_nbytes`` as separate)."""
    limit = MEMORY_CAP_BYTES if cap is None else int(cap)
    return pcm_nbytes(n_samples, channels) > limit


@dataclass
class Job:
    id: str
    status: str
    created_at: str
    params: dict[str, Any]
    clips: list[dict[str, Any]]
    allowed_roots: list[str]
    output_dir: str
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    subscribers: list[queue.Queue] = field(default_factory=list)
    clip_results: list[dict[str, Any]] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        clips_out: list[dict[str, Any]]
        if self.clip_results:
            clips_out = list(self.clip_results)
        else:
            clips_out = [{"clip_id": c.get("clip_id")} for c in self.clips]
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "engine_version": ENGINE_VERSION,
            "clips": clips_out,
        }
        if self.status == "done":
            out["schema"] = "perfectvoice.job.v1"
            out["params"] = self.params
        if self.error:
            out["error"] = self.error
        return out


def event_for(job: Job) -> dict[str, Any] | None:
    if job.status == "running":
        clip_id = job.progress.get("clip_id")
        if clip_id is None and job.clips:
            clip_id = job.clips[0].get("clip_id")
        return {
            "event": "progress",
            "data": {
                "clip_id": clip_id,
                "segment_offset": job.progress.get("segment_offset", 0),
                "audio_length": job.progress.get("audio_length", 0),
            },
        }
    if job.status == "done":
        return {"event": "done", "data": {"id": job.id}}
    if job.status == "error":
        return {"event": "error", "data": {"id": job.id, "message": job.error or "error"}}
    if job.status == "cancelled":
        return {"event": "error", "data": {"id": job.id, "message": "cancelled"}}
    return None


class JobStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[str] = queue.Queue()

    def create(self, body: dict[str, Any]) -> Job:
        job = Job(
            id=str(uuid.uuid4()),
            status="queued",
            created_at=utc_now(),
            params=body["params"],
            clips=list(body["clips"]),
            allowed_roots=list(body["allowed_roots"]),
            output_dir=str(body["output_dir"]),
        )
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status in TERMINAL:
                raise JobConflict(job)
            job.status = "cancelled"
            job.cancel_event.set()
            ev = event_for(job)
            self._notify_locked(job, ev)
            return job

    def subscribe(self, job_id: str) -> tuple[Job, queue.Queue]:
        q: queue.Queue = queue.Queue(maxsize=32)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.subscribers.append(q)
            snap = event_for(job)
        if snap is not None:
            try:
                q.put_nowait(snap)
            except queue.Full:
                pass
        return job, q

    def unsubscribe(self, job_id: str, q: queue.Queue) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            try:
                job.subscribers.remove(q)
            except ValueError:
                pass

    def worker_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._run_job(job_id)
            except Exception as exc:  # noqa: BLE001 — last-resort worker fence
                log(f"worker failed for {job_id}: {type(exc).__name__}")

    def _finish_locked(self, job: Job, status: str, error: str | None = None) -> None:
        if job.status in TERMINAL:
            return
        job.status = status
        job.error = error
        self._notify_locked(job, event_for(job))

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "queued":
                return
            job.status = "running"
            cancel_event = job.cancel_event
            self._notify_locked(job, event_for(job))

        def on_progress(data: dict[str, Any]) -> None:
            with self._lock:
                if job.status != "running":
                    return
                job.progress = dict(data)
                self._notify_locked(job, {"event": "progress", "data": dict(data)})

        try:
            raise_if_cancelled(cancel_event)
            if STUB_HOLD_SECONDS > 0:
                cancel_event.wait(timeout=STUB_HOLD_SECONDS)
                raise_if_cancelled(cancel_event)
            try:
                # Lazy: sidecar unit tests import serve without numpy/soxr.
                from perfectvoice_engine.pipeline import run_job
            except ImportError:
                # Fail closed on missing weights even when the I/O stack is absent.
                _require_local_model(job.params)
                raise
            results = run_job(
                job.clips,
                job.params,
                job.output_dir,
                cancel_event=cancel_event,
                on_progress=on_progress,
            )
        except JobCancelled:
            with self._lock:
                self._finish_locked(job, "cancelled")
            return
        except ModelNotInstalled as exc:
            with self._lock:
                self._finish_locked(job, "error", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — surface to GET /v1/jobs/:id
            with self._lock:
                if cancel_event.is_set():
                    self._finish_locked(job, "cancelled")
                    return
                self._finish_locked(job, "error", str(exc))
            return

        with self._lock:
            if job.status in TERMINAL:
                return
            if job.cancel_event.is_set():
                self._finish_locked(job, "cancelled")
                return
            job.clip_results = results
            try:
                write_job_json(job, results)
            except OSError as exc:
                self._finish_locked(job, "error", f"failed to write job.json: {exc}")
                return
            self._finish_locked(job, "done")

    def _notify_locked(self, job: Job, ev: dict[str, Any] | None) -> None:
        if ev is None:
            return
        for q in list(job.subscribers):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


class EngineHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        token: str,
        store: JobStore,
        idle_seconds: int,
        local_repo: Path | None = None,
    ) -> None:
        self.token = token
        self.store = store
        self.idle_seconds = idle_seconds
        self.local_repo = Path(local_repo) if local_repo is not None else default_local_repo()
        self.download_lock = threading.Lock()
        self.last_request = time.monotonic()
        super().__init__(server_address, EngineHandler)


class EngineHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server: EngineHTTPServer

    def log_message(self, fmt: str, *args: object) -> None:
        # Path/status only — never headers (Authorization).
        sys.stderr.write("%s\n" % (fmt % args))

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            self.server.last_request = time.monotonic()
            if not self._authorized():
                return
            handler = getattr(self, "do_" + self.command, None)
            if handler is None:
                self._json(405, {"error": "method_not_allowed"})
                return
            handler()
            self.wfile.flush()
        except TimeoutError as exc:
            self.log_error("Request timed out: %r", exc)
            self.close_connection = True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/health":
            self._json(
                200,
                {
                    "ok": True,
                    "status": "ok",
                    "protocol_version": PROTOCOL_VERSION,
                    "models_ready": models_ready(self.server.local_repo),
                },
            )
            return
        if path == "/v1/capabilities":
            self._json(
                200,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "devices": ["cpu"],
                    "models_ready": models_ready(self.server.local_repo),
                    "window_seconds": WINDOW_SECONDS,
                    "window_overlap_seconds": WINDOW_OVERLAP_SECONDS,
                    "memory_cap_bytes": MEMORY_CAP_BYTES,
                },
            )
            return
        if path == "/v1/speakers":
            try:
                from perfectvoice_engine.tse import SpeakerStore
                store = SpeakerStore()
                speakers = [p.to_dict(include_embedding=False) for p in store.list_all()]
                self._json(200, {"ok": True, "speakers": speakers})
            except Exception as exc:
                self._json(500, {"error": "internal_error", "detail": str(exc)})
            return
        m = JOB_EVENTS.match(path)
        if m:
            self._events(m.group(1))
            return
        m = JOB_PATH.match(path)
        if m:
            self._get_job(m.group(1))
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/jobs":
            self._create_job()
            return
        if path == "/v1/models/download":
            self._download_model()
            return
        if path == "/v1/speakers/enroll":
            self._enroll_speaker()
            return
        m = JOB_CANCEL.match(path)
        if m:
            self._cancel_job(m.group(1))
            return
        self._json(404, {"error": "not_found"})

    def do_DELETE(self) -> None:
        self.server.last_request = time.monotonic()
        if not self._authorized():
            return
        path = urlparse(self.path).path
        m = re.compile(r"^/v1/speakers/([^/]+)$").match(path)
        if m:
            try:
                from perfectvoice_engine.tse import SpeakerStore
                store = SpeakerStore()
                ok = store.delete(m.group(1))
                self._json(200, {"ok": ok})
            except Exception as exc:
                self._json(500, {"error": "internal_error", "detail": str(exc)})
            return
        self._json(404, {"error": "not_found"})

    def _enroll_speaker(self) -> None:
        try:
            body = self._read_json()
        except ValueError as exc:
            self._json(400, {"error": "validation_error", "detail": str(exc)})
            return

        audio_path = body.get("audio_path")
        name = str(body.get("name") or "Speaker")
        t0 = float(body.get("t0", 0.0))
        t1 = float(body.get("t1", 0.0))

        if not audio_path or not Path(audio_path).exists():
            self._json(400, {"error": "file_not_found", "detail": f"audio_path {audio_path} not found"})
            return

        try:
            from perfectvoice_engine.ffmpeg_io import decode_f32
            from perfectvoice_engine.models import default_local_repo, is_model_ready
            from perfectvoice_engine.resample import to_model_rate
            from perfectvoice_engine.separate import SeparateRequest, separate_vocals
            from perfectvoice_engine.tse import SpeakerStore, extract_embedding

            frames, probe = decode_f32(Path(audio_path))
            sr = probe.sample_rate or 44100
            # decode_f32 returns [num_samples, channels], transpose to [channels, num_samples]
            if frames.ndim == 2:
                frames = frames.T
            elif frames.ndim == 1:
                frames = frames[np.newaxis, :]

            if t1 > t0:
                s0 = max(0, int(t0 * sr))
                s1 = min(int(t1 * sr), frames.shape[-1])
                if s1 > s0:
                    frames = frames[:, s0:s1]

            # 1. Pre-clean reference sample with fast Demucs separation to strip music/drums/bass
            repo = default_local_repo()
            sample_441 = to_model_rate(frames.T, sr).T  # [channels, samples] at 44.1k
            if sample_441.shape[0] == 1:
                sample_441 = np.repeat(sample_441, 2, axis=0)
            elif sample_441.shape[0] > 2:
                sample_441 = sample_441[:2]

            voice_sample = sample_441
            if is_model_ready("htdemucs", repo):
                try:
                    dur = float(sample_441.shape[-1]) / 44100.0
                    sep_res = separate_vocals(
                        SeparateRequest(
                            wav_44100_stereo=np.ascontiguousarray(sample_441.T, dtype=np.float32),
                            model="htdemucs",
                            device="auto",
                            segment=float(min(7.8, max(1.0, dur))),
                            overlap=0.25,
                            shifts=1,
                            vocals_only_bag=False,
                        ),
                        repo,
                    )
                    voice_sample = sep_res.vocals.T
                except Exception:
                    voice_sample = sample_441

            dur_s = round(float(frames.shape[-1]) / sr, 2)
            embedding = extract_embedding(voice_sample, sample_rate=44100)
            store = SpeakerStore()
            profile = store.enroll(name=name, embedding=embedding, sample_duration_s=dur_s)
            self._json(200, {"ok": True, "speaker": profile.to_dict()})
        except Exception as exc:
            self._json(500, {"error": "enrollment_failed", "detail": str(exc)})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self._json(401, {"error": "unauthorized"})
            return False
        offered = parse_offered_token(header[len("Bearer ") :])
        if offered is None or not hmac.compare_digest(offered, self.server.token):
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _read_json(self) -> Any:
        length = self.headers.get("Content-Length")
        if length is None:
            raise ValueError("missing Content-Length")
        try:
            n = int(length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if n < 0 or n > MAX_BODY:
            raise ValueError("invalid Content-Length")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid json") from exc

    def _download_model(self) -> None:
        # User-click only. Jobs / Separator never call download_model().
        try:
            body = self._read_json()
        except ValueError as exc:
            self._json(400, {"error": "validation_error", "detail": str(exc)})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "validation_error", "detail": "object required"})
            return
        extra = set(body) - {"name"}
        if extra or "name" not in body:
            self._json(400, {"error": "validation_error", "detail": "expected {name}"})
            return
        name = body["name"]
        if not isinstance(name, str) or name not in ALLOWED_MODELS:
            self._json(400, {"error": "validation_error", "detail": "unknown model"})
            return
        want_sse = "text/event-stream" in (self.headers.get("Accept") or "")
        started_sse = False

        def progress(filename: str, done: int, total: int) -> None:
            nonlocal started_sse
            self.server.last_request = time.monotonic()
            if not want_sse:
                return
            if not started_sse:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                started_sse = True
            self._write_sse(
                {
                    "event": "progress",
                    "data": {
                        "filename": filename,
                        "bytes_done": done,
                        "bytes_total": total,
                    },
                }
            )

        try:
            with self.server.download_lock:
                files = download_model(name, self.server.local_repo, progress=progress)
        except ValueError as exc:
            if started_sse:
                self._write_sse({"event": "error", "data": {"message": str(exc)}})
                return
            self._json(400, {"error": "validation_error", "detail": str(exc)})
            return
        except (WeightFetchError, OSError) as exc:
            if started_sse:
                self._write_sse({"event": "error", "data": {"message": str(exc)}})
                return
            self._json(502, {"error": "download_failed", "detail": str(exc)})
            return
        payload = {"name": name, "ready": True, "files": files}
        if want_sse:
            if not started_sse:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
            self._write_sse({"event": "done", "data": payload})
            return
        self._json(200, payload)

    def _create_job(self) -> None:
        try:
            body = self._read_json()
        except ValueError as exc:
            self._json(400, {"error": "validation_error", "detail": str(exc)})
            return
        try:
            validated = validate_job_request(body)
        except ValidationError as exc:
            self._json(400, {"error": "validation_error", "detail": str(exc)})
            return
        except PathNotAllowed as exc:
            self._json(400, {"error": "path_not_allowed", "detail": exc.reason})
            return
        job = self.server.store.create(validated)
        self._json(202, {"id": job.id, "status": "queued"})

    def _get_job(self, job_id: str) -> None:
        job = self.server.store.get(job_id)
        if job is None:
            self._json(404, {"error": "not_found"})
            return
        self._json(200, job.record())

    def _cancel_job(self, job_id: str) -> None:
        try:
            job = self.server.store.cancel(job_id)
        except KeyError:
            self._json(404, {"error": "not_found"})
            return
        except JobConflict:
            self._json(409, {"error": "conflict", "detail": "job already terminal"})
            return
        self._json(202, {"id": job.id, "status": job.status})

    def _events(self, job_id: str) -> None:
        try:
            job, q = self.server.store.subscribe(job_id)
        except KeyError:
            self._json(404, {"error": "not_found"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            if job.status in TERMINAL:
                ev = event_for(job)
                if ev is not None:
                    self._write_sse(ev)
                return
            while True:
                try:
                    ev = q.get(timeout=15.0)
                except queue.Empty:
                    try:
                        self.wfile.write(b": ka\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        return
                    continue
                try:
                    self._write_sse(ev)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                if ev.get("event") in {"done", "error"}:
                    return
        finally:
            self.server.store.unsubscribe(job_id, q)

    def _write_sse(self, ev: dict[str, Any]) -> None:
        payload = "event: %s\ndata: %s\n\n" % (
            ev["event"],
            json.dumps(ev["data"], separators=(",", ":")),
        )
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _json(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def parse_offered_token(raw: str) -> str | None:
    token = raw.strip()
    if not TOKEN_RE.fullmatch(token):
        return None
    return token.lower()


def refuse_token_fd(argv: list[str]) -> None:
    if any(a == "--token-fd" or a.startswith("--token-fd=") for a in argv):
        raise TokenError("--token-fd is not supported; use --token-file or stdin")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    refuse_token_fd(argv)
    parser = argparse.ArgumentParser(prog="perfectvoice-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser(
        "serve",
        help="Run the localhost sidecar",
        description=(
            "Bind 127.0.0.1 only. Token is 256-bit hex from --token-file "
            "(mode 0600, unlinked after read) or one line on stdin. "
            "Idle-exit after --idle-seconds with no requests (default 1800); "
            "0 disables. Lock file is not implemented in this revision."
        ),
    )
    serve.add_argument("--bind", required=True, help="Must be 127.0.0.1")
    serve.add_argument("--port", required=True, type=int, help="TCP port; 0 = ephemeral")
    serve.add_argument(
        "--token-file",
        default=None,
        help="One-shot token file path (never pass the token on argv)",
    )
    serve.add_argument(
        "--idle-seconds",
        type=int,
        default=1800,
        help="Exit after N idle seconds (default 1800 = 30 min). 0 disables.",
    )
    return parser.parse_args(argv)


def idle_watchdog(httpd: EngineHTTPServer, stop: threading.Event) -> None:
    while not stop.wait(1.0):
        if httpd.idle_seconds <= 0:
            continue
        if httpd.download_lock.locked():
            continue
        if time.monotonic() - httpd.last_request > httpd.idle_seconds:
            log("idle timeout, exiting")
            threading.Thread(target=httpd.shutdown, daemon=True).start()
            return


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except TokenError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2

    if args.cmd != "serve":
        return 2
    if not (0 <= args.port <= 65535):
        print("invalid port", file=sys.stderr)
        return 2

    try:
        validate_bind(args.bind)
    except BindError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.token_file:
            token = load_token_file(Path(args.token_file))
        else:
            token = load_token_stdin()
    except TokenError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    store = JobStore()
    try:
        httpd = EngineHTTPServer((args.bind, args.port), token, store, args.idle_seconds)
    except OSError as exc:
        print(f"bind failed: {exc}", file=sys.stderr)
        return 1

    _host, port = httpd.server_address
    print(f"READY http://127.0.0.1:{port}", flush=True)

    stop = threading.Event()
    worker = threading.Thread(target=store.worker_loop, args=(stop,), name="job-worker", daemon=True)
    worker.start()
    if args.idle_seconds > 0:
        threading.Thread(
            target=idle_watchdog,
            args=(httpd, stop),
            name="idle-watchdog",
            daemon=True,
        ).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
