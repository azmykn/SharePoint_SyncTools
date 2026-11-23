"""Silence trim: first speech to last speech; never cut middle silence."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.sharepoint.silence_trim import (
    MIN_KEPT_RATIO,
    compute_trim_window,
    kept_duration_is_safe,
    longest_speech_region,
    merge_speech_segments,
    parse_silencedetect_stderr,
    silence_intervals_to_speech,
    silence_trim_settings,
    trim_silence_edges,
    untrimmed_backup_path,
)


SAMPLE_STDERR = """
[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 333.0 | silence_duration: 333.0
[silencedetect @ 0x1] silence_start: 400.0
[silencedetect @ 0x1] silence_end: 818.0 | silence_duration: 418.0
[silencedetect @ 0x1] silence_start: 862.0
[silencedetect @ 0x1] silence_end: 2171.0 | silence_duration: 1309.0
[silencedetect @ 0x1] silence_start: 5884.0
"""


class TestSilenceTrimLogic(unittest.TestCase):
    def test_parse_silencedetect(self):
        pairs = parse_silencedetect_stderr(SAMPLE_STDERR)
        self.assertEqual(pairs[0], (0.0, 333.0))
        self.assertEqual(pairs[-1][0], 5884.0)
        self.assertEqual(pairs[-1][1], -1.0)  # unclosed trailing

    def test_speech_from_silence_lecture_reality(self):
        duration = 6500.0
        pairs = [(st, duration if en < 0 else en) for st, en in parse_silencedetect_stderr(SAMPLE_STDERR)]
        speech = silence_intervals_to_speech(pairs, duration)
        # Short phrases + main lecture block
        self.assertIn((333.0, 400.0), speech)
        self.assertIn((818.0, 862.0), speech)
        self.assertIn((2171.0, 5884.0), speech)

    def test_merge_keeps_separate_regions_across_wide_gaps(self):
        speech = [(333.0, 400.0), (818.0, 862.0), (2171.0, 5884.0)]
        merged = merge_speech_segments(speech, merge_gap_sec=120.0)
        # Gaps 418s and 1309s > 120 → three regions; longest is main lecture
        self.assertEqual(len(merged), 3)
        region = longest_speech_region(merged)
        self.assertEqual(region, (2171.0, 5884.0))

    def test_compute_trim_window_first_to_last_with_margin(self):
        pairs = [(st, 6500.0 if en < 0 else en) for st, en in parse_silencedetect_stderr(SAMPLE_STDERR)]
        window = compute_trim_window(pairs, 6500.0, merge_gap_sec=120.0, margin_sec=3.0)
        self.assertIsNotNone(window)
        start, end = window
        # First speech 333, last speech 5884 — middle silence is kept.
        self.assertAlmostEqual(start, 330.0, places=1)
        self.assertAlmostEqual(end, 5887.0, places=1)
        self.assertTrue(kept_duration_is_safe(start, end, 6500.0))

    def test_compute_trim_window_does_not_use_longest_region(self):
        # Silence (0,100) and (400,2000) with duration 2500 → speech 100-400 and 2000-2500.
        # Window must span both islands, not only the longest one.
        pairs = [(0.0, 100.0), (400.0, 2000.0)]
        window = compute_trim_window(pairs, 2500.0, merge_gap_sec=120.0, margin_sec=3.0)
        self.assertIsNotNone(window)
        self.assertAlmostEqual(window[0], 97.0, places=1)
        self.assertAlmostEqual(window[1], 2500.0, places=1)

    def test_merge_gap_joins_close_segments(self):
        speech = [(10.0, 20.0), (50.0, 80.0), (500.0, 900.0)]
        merged = merge_speech_segments(speech, merge_gap_sec=120.0)
        self.assertEqual(merged[0], (10.0, 80.0))
        self.assertEqual(merged[1], (500.0, 900.0))

    def test_no_trim_when_almost_full(self):
        # Only tiny edge silence
        pairs = [(0.0, 0.02), (99.9, 100.0)]
        self.assertIsNone(compute_trim_window(pairs, 100.0, merge_gap_sec=120.0, margin_sec=3.0))

    def test_kept_duration_ratio(self):
        self.assertTrue(kept_duration_is_safe(0, 80, 100))
        self.assertTrue(kept_duration_is_safe(0, 80.0, 100.0, min_ratio=MIN_KEPT_RATIO))
        self.assertFalse(kept_duration_is_safe(4243.0, 5159.0, 12879.4))
        self.assertFalse(kept_duration_is_safe(0, 79.9, 100))

    def test_untrimmed_backup_path(self):
        self.assertEqual(
            untrimmed_backup_path("/rec/lecture.mp4"),
            "/rec/lecture.untrimmed.mp4",
        )

    def test_settings_defaults(self):
        with patch("sp_sync.sharepoint.silence_trim.load_settings", return_value={}):
            cfg = silence_trim_settings()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["merge_gap_sec"], 120.0)
        self.assertEqual(cfg["noise_db"], -70.0)


class TestSilenceTrimIntegration(unittest.TestCase):
    def test_disabled_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp4")
            with open(path, "wb") as f:
                f.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 200)
            before = os.path.getsize(path)
            ok = trim_silence_edges(path, log=lambda *_: None, settings={"enabled": False})
            self.assertTrue(ok)
            self.assertEqual(os.path.getsize(path), before)

    def _settings(self):
        return {
            "enabled": True,
            "noise_db": -70,
            "min_silence_dur": 2,
            "margin_sec": 3,
            "merge_gap_sec": 120,
        }

    def test_keeps_original_when_trim_invalid(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp4")
            with open(path, "wb") as f:
                f.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2000)
            before = open(path, "rb").read()

            def fake_detect(*_a, **_k):
                return SAMPLE_STDERR + "silence_end: 6500.0\n"

            with patch(
                "sp_sync.sharepoint.silence_trim.resolve_ffmpeg_path", return_value="/usr/bin/ffmpeg"
            ), patch(
                "sp_sync.sharepoint.silence_trim._resolve_ffprobe_path", return_value="/usr/bin/ffprobe"
            ), patch(
                "sp_sync.sharepoint.silence_trim._ffprobe_duration_seconds", side_effect=[6500.0, None]
            ), patch(
                "sp_sync.sharepoint.silence_trim._run_silencedetect", side_effect=fake_detect
            ), patch(
                "sp_sync.sharepoint.silence_trim.subprocess.run",
                return_value=type("R", (), {"returncode": 1, "stderr": "fail", "stdout": ""})(),
            ):
                ok = trim_silence_edges(
                    path,
                    log=logs.append,
                    settings=self._settings(),
                )
            self.assertTrue(ok)
            self.assertEqual(open(path, "rb").read(), before)
            self.assertTrue(any("keeping original" in m.lower() for m in logs))
            self.assertFalse(os.path.exists(untrimmed_backup_path(path)))

    def test_keeps_full_file_when_window_under_80_percent(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp4")
            with open(path, "wb") as f:
                f.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2000)
            before = open(path, "rb").read()
            # Speech only in a short middle island: silence 0-4000 and 5000-10000.
            stderr = (
                "[silencedetect @ 0x1] silence_start: 0\n"
                "[silencedetect @ 0x1] silence_end: 4000.0\n"
                "[silencedetect @ 0x1] silence_start: 5000.0\n"
                "[silencedetect @ 0x1] silence_end: 10000.0\n"
            )
            with patch(
                "sp_sync.sharepoint.silence_trim.resolve_ffmpeg_path", return_value="/usr/bin/ffmpeg"
            ), patch(
                "sp_sync.sharepoint.silence_trim._resolve_ffprobe_path", return_value="/usr/bin/ffprobe"
            ), patch(
                "sp_sync.sharepoint.silence_trim._ffprobe_duration_seconds", return_value=10000.0
            ), patch(
                "sp_sync.sharepoint.silence_trim._run_silencedetect", return_value=stderr
            ):
                ok = trim_silence_edges(path, log=logs.append, settings=self._settings())
            self.assertTrue(ok)
            self.assertEqual(open(path, "rb").read(), before)
            self.assertTrue(any("keeping full untrimmed file" in m.lower() for m in logs))
            self.assertFalse(os.path.exists(untrimmed_backup_path(path)))

    def test_successful_trim_keeps_untrimmed_copy(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lecture.mp4")
            original = b"\x00\x00\x00\x18ftypisom" + b"O" * 4000
            with open(path, "wb") as f:
                f.write(original)

            def fake_remux(cmd, **_k):
                # Last arg is output path
                out = cmd[-1]
                with open(out, "wb") as f:
                    f.write(b"\x00\x00\x00\x18ftypisom" + b"T" * 3000)
                return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

            # Original 6500s; first-to-last window is 330-5887 = 5557s (~85.5%).
            with patch(
                "sp_sync.sharepoint.silence_trim.resolve_ffmpeg_path", return_value="/usr/bin/ffmpeg"
            ), patch(
                "sp_sync.sharepoint.silence_trim._resolve_ffprobe_path", return_value="/usr/bin/ffprobe"
            ), patch(
                "sp_sync.sharepoint.silence_trim._ffprobe_duration_seconds", side_effect=[6500.0, 5557.0]
            ), patch(
                "sp_sync.sharepoint.silence_trim._run_silencedetect",
                return_value=SAMPLE_STDERR + "silence_end: 6500.0\n",
            ), patch(
                "sp_sync.sharepoint.silence_trim.subprocess.run", side_effect=fake_remux
            ), patch(
                "sp_sync.sharepoint.silence_trim._is_valid_mp4", return_value=True
            ):
                ok = trim_silence_edges(path, log=logs.append, settings=self._settings())
            self.assertTrue(ok)
            backup = untrimmed_backup_path(path)
            self.assertTrue(os.path.isfile(backup))
            self.assertEqual(open(backup, "rb").read(), original)
            self.assertEqual(open(path, "rb").read()[12:13], b"T")
            self.assertTrue(any("Kept untrimmed copy" in m for m in logs))
            self.assertTrue(any("Silence trimmed; new duration 5557.0s" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
