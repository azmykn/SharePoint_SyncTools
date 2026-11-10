"""Silence trim: longest merged speech region; never cut middle silence."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.sharepoint.silence_trim import (
    compute_trim_window,
    longest_speech_region,
    merge_speech_segments,
    parse_silencedetect_stderr,
    silence_intervals_to_speech,
    silence_trim_settings,
    trim_silence_edges,
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

    def test_merge_keeps_main_block_drops_blips(self):
        speech = [(333.0, 400.0), (818.0, 862.0), (2171.0, 5884.0)]
        merged = merge_speech_segments(speech, merge_gap_sec=120.0)
        # Gaps 418s and 1309s > 120 → three regions; longest is main lecture
        self.assertEqual(len(merged), 3)
        region = longest_speech_region(merged)
        self.assertEqual(region, (2171.0, 5884.0))

    def test_compute_trim_window_with_margin(self):
        pairs = [(st, 6500.0 if en < 0 else en) for st, en in parse_silencedetect_stderr(SAMPLE_STDERR)]
        window = compute_trim_window(pairs, 6500.0, merge_gap_sec=120.0, margin_sec=3.0)
        self.assertIsNotNone(window)
        start, end = window
        self.assertAlmostEqual(start, 2168.0, places=1)
        self.assertAlmostEqual(end, 5887.0, places=1)

    def test_merge_gap_joins_close_segments(self):
        speech = [(10.0, 20.0), (50.0, 80.0), (500.0, 900.0)]
        merged = merge_speech_segments(speech, merge_gap_sec=120.0)
        self.assertEqual(merged[0], (10.0, 80.0))
        self.assertEqual(merged[1], (500.0, 900.0))

    def test_no_trim_when_almost_full(self):
        # Only tiny edge silence
        pairs = [(0.0, 0.02), (99.9, 100.0)]
        self.assertIsNone(compute_trim_window(pairs, 100.0, merge_gap_sec=120.0, margin_sec=3.0))

    def test_settings_defaults(self):
        from sp_sync.config.settings import clear_settings_cache

        clear_settings_cache()
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
                    settings={
                        "enabled": True,
                        "noise_db": -70,
                        "min_silence_dur": 2,
                        "margin_sec": 3,
                        "merge_gap_sec": 120,
                    },
                )
            self.assertTrue(ok)
            self.assertEqual(open(path, "rb").read(), before)
            self.assertTrue(any("keeping original" in m.lower() for m in logs))


if __name__ == "__main__":
    unittest.main()
