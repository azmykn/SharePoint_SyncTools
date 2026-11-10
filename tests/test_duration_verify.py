"""Post-download ffprobe duration verification vs expected source duration."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.config.settings import clear_settings_cache, duration_tolerance
from sp_sync.sharepoint.sync_engine import (
    _duration_outside_tolerance,
    _verify_output_duration,
)


class TestDurationTolerance(unittest.TestCase):
    def setUp(self):
        clear_settings_cache()

    def tearDown(self):
        clear_settings_cache()

    def test_within_tolerance(self):
        # expected 1000s; allowed = max(30, 20) = 30 → 1020 ok
        self.assertFalse(_duration_outside_tolerance(1020, 1000, pct=0.02, abs_sec=30))
        self.assertFalse(_duration_outside_tolerance(970, 1000, pct=0.02, abs_sec=30))

    def test_outside_tolerance(self):
        # allowed = max(30, 20) = 30; 50s off is too much
        self.assertTrue(_duration_outside_tolerance(1050, 1000, pct=0.02, abs_sec=30))
        # long lecture: allowed = max(30, 120) = 120
        self.assertFalse(_duration_outside_tolerance(6100, 6000, pct=0.02, abs_sec=30))
        self.assertTrue(_duration_outside_tolerance(6200, 6000, pct=0.02, abs_sec=30))

    def test_missing_values_skip(self):
        self.assertFalse(_duration_outside_tolerance(None, 1000, pct=0.02, abs_sec=30))
        self.assertFalse(_duration_outside_tolerance(100, None, pct=0.02, abs_sec=30))
        self.assertFalse(_duration_outside_tolerance(0, 1000, pct=0.02, abs_sec=30))

    def test_settings_defaults(self):
        pct, abs_sec = duration_tolerance()
        self.assertAlmostEqual(pct, 0.02)
        self.assertAlmostEqual(abs_sec, 30.0)

    def test_verify_output_duration_mismatch_marks_failed(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.mp4")
            with open(path, "wb") as f:
                f.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 200)
            with patch(
                "sp_sync.sharepoint.sync_engine._resolve_ffprobe_path", return_value="/usr/bin/ffprobe"
            ), patch(
                "sp_sync.sharepoint.sync_engine._ffprobe_duration_seconds", return_value=100.0
            ), patch(
                "sp_sync.sharepoint.sync_engine.duration_tolerance", return_value=(0.02, 30.0)
            ):
                ok, actual = _verify_output_duration(path, expected_duration=500.0, log=logs.append)
            self.assertFalse(ok)
            self.assertEqual(actual, 100.0)
            self.assertTrue(any("Duration mismatch" in m for m in logs))

    def test_verify_output_duration_ok(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.mp4")
            with open(path, "wb") as f:
                f.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 200)
            with patch(
                "sp_sync.sharepoint.sync_engine._resolve_ffprobe_path", return_value="/usr/bin/ffprobe"
            ), patch(
                "sp_sync.sharepoint.sync_engine._ffprobe_duration_seconds", return_value=498.0
            ), patch(
                "sp_sync.sharepoint.sync_engine.duration_tolerance", return_value=(0.02, 30.0)
            ):
                ok, actual = _verify_output_duration(path, expected_duration=500.0, log=logs.append)
            self.assertTrue(ok)
            self.assertEqual(actual, 498.0)
            self.assertTrue(any("Duration verified" in m for m in logs))

    def test_verify_skips_without_expected(self):
        ok, actual = _verify_output_duration("/nope", expected_duration=None, log=lambda *_: None)
        self.assertTrue(ok)
        self.assertIsNone(actual)


if __name__ == "__main__":
    unittest.main()
