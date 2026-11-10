"""Resume helpers: reuse .part and completed capture temps instead of starting over."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.sharepoint.sync_engine import (
    _capture_work_dir,
    _clear_capture_progress,
    _load_capture_progress,
    _save_capture_progress,
    _try_promote_part_file,
    _try_resume_from_existing_capture,
)


def _write_min_mp4(path, size=2048):
    with open(path, "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41")
        f.write(b"\x00" * max(0, size - 32))


class TestCaptureResume(unittest.TestCase):
    def test_capture_work_dir_is_stable_per_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "folder", "Lecture A.mp4")
            b = os.path.join(tmp, "folder", "Lecture B.mp4")
            d1 = _capture_work_dir(a)
            d2 = _capture_work_dir(a)
            d3 = _capture_work_dir(b)
            self.assertEqual(d1, d2)
            self.assertNotEqual(d1, d3)
            self.assertTrue(d1.startswith(os.path.join(os.path.dirname(d1), "")))  # under TMP_DIR

    def test_progress_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            _save_capture_progress(tmp, {"last_time": 120.5, "capture_complete": False})
            meta = _load_capture_progress(tmp)
            self.assertEqual(meta["last_time"], 120.5)
            self.assertFalse(meta["capture_complete"])
            _clear_capture_progress(tmp)
            self.assertIsNone(_load_capture_progress(tmp))

    def test_promote_valid_part(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "lecture.mp4")
            part = out + ".part"
            _write_min_mp4(part)
            with patch(
                "sp_sync.sharepoint.sync_engine._ffprobe_duration_seconds", return_value=10.0
            ), patch(
                "sp_sync.sharepoint.sync_engine._resolve_ffprobe_path", return_value="/usr/bin/ffprobe"
            ):
                size = _try_promote_part_file(out, log=logs.append)
            self.assertIsNotNone(size)
            self.assertTrue(os.path.isfile(out))
            self.assertFalse(os.path.exists(part))
            self.assertTrue(any("Resumed incomplete" in m for m in logs))

    def test_promote_rejects_invalid_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "lecture.mp4")
            with open(out + ".part", "wb") as f:
                f.write(b"not an mp4")
            self.assertIsNone(_try_promote_part_file(out, log=lambda *_: None))
            self.assertFalse(os.path.exists(out))

    def test_resume_muxes_completed_capture_temps(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "MBA-09.mp4")
            work = _capture_work_dir(out)
            os.makedirs(work, exist_ok=True)
            v_file = os.path.join(work, "video.fmp4")
            a_file = os.path.join(work, "audio.fmp4")
            with open(v_file, "wb") as f:
                f.write(b"\x00" * 500)
            with open(a_file, "wb") as f:
                f.write(b"\x00" * 200)
            _save_capture_progress(work, {"capture_complete": True, "last_time": 600})

            def fake_mux(ff, vf, af, output_path, log=print, tmp_dir=None):
                _write_min_mp4(output_path)
                return True, os.path.getsize(output_path)

            with patch(
                "sp_sync.sharepoint.sync_engine._mux_captured_tracks", side_effect=fake_mux
            ), patch(
                "sp_sync.sharepoint.sync_engine.resolve_ffmpeg_path", return_value="/usr/bin/ffmpeg"
            ):
                result = _try_resume_from_existing_capture(out, log=logs.append)

            self.assertIsNotNone(result)
            ok, size = result
            self.assertTrue(ok)
            self.assertGreater(size, 0)
            self.assertTrue(os.path.isfile(out))
            self.assertTrue(any("Resuming: remuxing" in m for m in logs))
            self.assertIsNone(_load_capture_progress(work))

    def test_incomplete_capture_does_not_force_mux(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "MBA-09.mp4")
            work = _capture_work_dir(out)
            os.makedirs(work, exist_ok=True)
            with open(os.path.join(work, "video.fmp4"), "wb") as f:
                f.write(b"\x00" * 500)
            _save_capture_progress(work, {"capture_complete": False, "last_time": 90})
            self.assertIsNone(_try_resume_from_existing_capture(out, log=lambda *_: None))


if __name__ == "__main__":
    unittest.main()
