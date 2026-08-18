"""DeepFilterNet 3 wrapper tests (no weights, no torch, no Demucs fetch).

Run: python3 -m unittest tests.unit.test_enhance
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from perfectvoice_engine import enhance as enhance_mod  # noqa: E402
from perfectvoice_engine.enhance import (  # noqa: E402
    ENHANCER_ID,
    ENHANCER_SAMPLE_RATE,
    EnhancerNotInstalled,
    default_model_dir,
    enhance,
    is_enhancer_installed,
)


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".", 1)[0])
    return names


class EnhanceContractTests(unittest.TestCase):
    def test_default_id_and_rate(self) -> None:
        self.assertEqual(ENHANCER_ID, "deepfilternet3")
        self.assertEqual(ENHANCER_SAMPLE_RATE, 48000)
        self.assertEqual(enhance_mod.ENHANCER_NOT_INSTALLED[:22], "enhancer not installed")

    def test_params_fixture_default_is_none(self) -> None:
        params = json.loads(
            (REPO_ROOT / "tests" / "fixtures" / "schemas" / "params.valid.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(params["enhancer"], "none")

    def test_wrong_rate_rejected_before_backend(self) -> None:
        with patch.object(enhance_mod, "_run_dfn") as backend:
            with self.assertRaises(ValueError) as ctx:
                enhance(np.zeros((16, 2), dtype=np.float32), 44100)
            self.assertIn("48000", str(ctx.exception))
            backend.assert_not_called()

    def test_missing_raises_enhancer_not_installed(self) -> None:
        with patch.object(enhance_mod, "is_enhancer_installed", return_value=False):
            with patch.object(enhance_mod, "_run_dfn") as backend:
                with self.assertRaises(EnhancerNotInstalled) as ctx:
                    enhance(np.zeros((16, 2), dtype=np.float32), 48000)
        self.assertIn("enhancer not installed", str(ctx.exception).lower())
        backend.assert_not_called()

    def test_empty_dir_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_enhancer_installed(Path(tmp)))

    def test_mocked_backend_runs_at_48k(self) -> None:
        frames = np.full((32, 2), 0.25, dtype=np.float32)

        def fake_run(arr: np.ndarray, model_dir: Path) -> np.ndarray:
            return np.asarray(arr, dtype=np.float32) * np.float32(2.0)

        with (
            patch.object(enhance_mod, "is_enhancer_installed", return_value=True),
            patch.object(enhance_mod, "_run_dfn", side_effect=fake_run) as backend,
        ):
            out = enhance(frames, 48000, model_dir=Path("/no/weights"))
        backend.assert_called_once()
        np.testing.assert_allclose(out, frames * 2.0, rtol=0, atol=1e-6)
        self.assertFalse(np.shares_memory(out, frames) or out.base is frames)

    def test_enhance_module_does_not_import_torch_at_toplevel(self) -> None:
        banned = {"torch", "torchaudio", "demucs", "df"}
        path = ENGINE_DIR / "perfectvoice_engine" / "enhance.py"
        self.assertTrue(banned.isdisjoint(_top_level_imports(path)))

    def test_default_model_dir_is_not_demucs(self) -> None:
        dest = default_model_dir()
        self.assertEqual(dest.name, "deepfilternet")
        self.assertNotIn("demucs", str(dest).lower())


def _load_download_script() -> ModuleType:
    path = REPO_ROOT / "scripts" / "download_deepfilternet.py"
    spec = importlib.util.spec_from_file_location("download_deepfilternet", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DownloadScriptTests(unittest.TestCase):
    def test_script_documents_official_dfn_url_not_demucs(self) -> None:
        path = REPO_ROOT / "scripts" / "download_deepfilternet.py"
        text = path.read_text(encoding="utf-8")
        self.assertIn(
            "https://github.com/Rikorose/DeepFilterNet/raw/main/models/DeepFilterNet3.zip",
            text,
        )
        self.assertIn("SHA256", text)
        self.assertIn("0000000000000000000000000000000000000000000000000000000000000000", text)
        self.assertNotIn("huggingface.co/adefossez", text)
        self.assertNotIn("dl.fbaipublicfiles.com", text)
        self.assertNotIn("facebookresearch", text)

    def test_print_url_and_placeholder_refuses_fetch(self) -> None:
        dl = _load_download_script()
        self.assertEqual(dl.main(["--print-url"]), 0)
        self.assertTrue(dl.sha_is_placeholder())
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                dl.fetch(Path(tmp))
            self.assertIn("placeholder", str(ctx.exception).lower())
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(dl.main(["--dest", str(Path("/tmp/pv-dfn-unused"))]), 2)


if __name__ == "__main__":
    unittest.main()
