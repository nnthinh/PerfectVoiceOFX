"""Dedupe of linked A/V selection — no Resolve required."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPIKES = ROOT / "scripts" / "spikes"
for path in (str(ROOT), str(SPIKES)):
    if path not in sys.path:
        sys.path.insert(0, path)

from dump_resolve_selection import dedupe_linked_group  # noqa: E402


def _m(name, track_type, path, uid=None, index=1):
    return {
        "name": name,
        "unique_id": uid or name,
        "track_type": track_type,
        "track_index": index,
        "file_path": path,
    }


class DedupeLinkedGroupTests(unittest.TestCase):
    def test_prefers_audio_over_linked_video(self):
        rows = dedupe_linked_group(
            [
                _m("A001", "video", "/media/A001.mov", index=1),
                _m("A001", "audio", "/media/A001.wav", index=1),
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_path"], "/media/A001.wav")
        self.assertTrue(rows[0]["preferred_audio_sibling"])
        self.assertFalse(rows[0]["suppressed_duplicate"])

    def test_two_audio_paths_are_both_jobs(self):
        rows = dedupe_linked_group(
            [
                _m("cam", "video", "/media/cam.mov"),
                _m("boom", "audio", "/media/boom.wav", index=1),
                _m("mix", "audio", "/media/mix.wav", index=2),
            ]
        )
        paths = [r["file_path"] for r in rows if not r["suppressed_duplicate"]]
        self.assertEqual(paths, ["/media/boom.wav", "/media/mix.wav"])
        self.assertTrue(all(r["preferred_audio_sibling"] for r in rows))

    def test_same_audio_path_kept_with_suppressed_flag(self):
        rows = dedupe_linked_group(
            [
                _m("A1", "audio", "/media/same.wav", uid="id-a", index=1),
                _m("A2", "audio", "/media/same.wav", uid="id-b", index=2),
            ]
        )
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["suppressed_duplicate"])
        self.assertTrue(rows[1]["suppressed_duplicate"])
        self.assertEqual(rows[1]["file_path"], "/media/same.wav")
        self.assertEqual(rows[1]["duplicate_of"], "id-a")

    def test_video_only_when_no_audio(self):
        rows = dedupe_linked_group([_m("V", "video", "/media/v.mov")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_path"], "/media/v.mov")
        self.assertFalse(rows[0]["preferred_audio_sibling"])
        self.assertFalse(rows[0]["suppressed_duplicate"])


if __name__ == "__main__":
    unittest.main()
