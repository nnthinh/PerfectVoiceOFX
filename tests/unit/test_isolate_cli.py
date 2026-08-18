"""isolate_cli fails closed without local weights (no Hub/AWS).

Run: python3 -m unittest tests.unit.test_isolate_cli
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import isolate_cli  # noqa: E402
from perfectvoice_engine.models import DEFAULT_MODEL, MODEL_NOT_INSTALLED  # noqa: E402

FORBIDDEN_HOST_NEEDLES = (
    "huggingface.co",
    "hf.co",
    "fbaipublicfiles.com",
    "amazonaws.com",
)


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


def _assert_no_forbidden_hosts(hits: list[str]) -> None:
    joined = "\n".join(hits)
    for needle in FORBIDDEN_HOST_NEEDLES:
        if needle in joined.lower():
            raise AssertionError(f"request to forbidden host {needle!r}: {hits}")


class IsolateCliMissingWeightsTests(unittest.TestCase):
    def test_empty_repo_exits_2_no_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "clip.wav"
            wav.write_bytes(b"RIFF....WAVE")
            out = root / "out"
            out.mkdir()
            repo = root / "repo"
            repo.mkdir()
            stderr = io.StringIO()
            with (
                _block_and_record_network() as hits,
                patch.object(
                    isolate_cli,
                    "_separate_and_write",
                    side_effect=AssertionError("separate must not run"),
                ),
                patch(
                    "perfectvoice_engine.separate._separator_cls",
                    side_effect=AssertionError("Separator must not run"),
                ),
                contextlib.redirect_stderr(stderr),
            ):
                code = isolate_cli.main(
                    [str(wav), str(out), DEFAULT_MODEL, "--repo", str(repo)]
                )
        self.assertEqual(code, 2)
        err = stderr.getvalue()
        self.assertIn("Model not installed", err)
        self.assertTrue(err.startswith(MODEL_NOT_INSTALLED) or MODEL_NOT_INSTALLED in err)
        _assert_no_forbidden_hosts(hits)
        self.assertEqual(hits, [])
        self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
        self.assertEqual(os.environ.get("TRANSFORMERS_OFFLINE"), "1")

    def test_subprocess_empty_repo_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "clip.wav"
            wav.write_bytes(b"RIFF....WAVE")
            out = root / "out"
            out.mkdir()
            repo = root / "empty"
            repo.mkdir()
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                [str(ENGINE_DIR), env.get("PYTHONPATH", "")]
            )
            # Fail closed if require_model is ever skipped: child cannot Hub-fetch.
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "isolate_cli.py"),
                    str(wav),
                    str(out),
                    DEFAULT_MODEL,
                    "--repo",
                    str(repo),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Model not installed", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_usage_error_is_exit_1_not_2(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = isolate_cli.main([])
        self.assertEqual(code, 1)
        self.assertNotIn("Model not installed", stderr.getvalue())

    def test_no_official_remotes_in_cli_source(self) -> None:
        text = (SCRIPTS_DIR / "isolate_cli.py").read_text(encoding="utf-8")
        self.assertNotIn("huggingface.co/adefossez", text)
        self.assertNotIn("dl.fbaipublicfiles.com", text)

    def test_empty_repo_does_not_call_urlopen(self) -> None:
        calls: list[object] = []

        def boom(url: object, *args: object, **kwargs: object) -> object:
            calls.append(url)
            raise AssertionError("urlopen must not run")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "in.wav"
            wav.write_bytes(b"x")
            with (
                patch.object(urllib.request, "urlopen", boom),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = isolate_cli.main(
                    [str(wav), str(root / "out"), DEFAULT_MODEL, "--repo", str(root)]
                )
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])


class BenchmarksTemplateTests(unittest.TestCase):
    def test_template_has_columns_and_no_fabricated_rtf(self) -> None:
        text = (REPO_ROOT / "docs" / "benchmarks.md").read_text(encoding="utf-8")
        self.assertIn("fill after measuring", text.lower())
        for col in ("device", "model", "duration", "wall time", "RTF"):
            self.assertIn(col, text)
        rows = [
            line
            for line in text.splitlines()
            if line.startswith("|") and "---" not in line
        ]
        self.assertGreaterEqual(len(rows), 2)
        header = rows[0].lower()
        self.assertIn("device", header)
        self.assertIn("model", header)
        self.assertIn("duration", header)
        self.assertIn("wall time", header)
        self.assertIn("rtf", header)
        for row in rows[1:]:
            cells = [c.strip() for c in row.strip("|").split("|")]
            self.assertTrue(
                all(cell == "" for cell in cells),
                f"benchmarks.md must stay empty until measured: {row!r}",
            )


if __name__ == "__main__":
    unittest.main()
