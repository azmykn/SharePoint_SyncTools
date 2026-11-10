"""Final mux failure must log cmd_final (res_final) exit/stderr, not res_mux's."""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.sharepoint.sync_engine import (
    _log_final_mux_failure,
    _mux_captured_tracks,
)


class TestMuxFailureLogging(unittest.TestCase):
    def test_log_prefers_res_final_over_res_mux(self):
        logs = []
        res_mux = SimpleNamespace(returncode=0, stderr="step1 ok noise")
        res_final = SimpleNamespace(
            returncode=42,
            stderr="line1\nUnable to find a suitable output format for 'out.mp4.part'\n",
        )
        rc = _log_final_mux_failure(res_final, res_mux, log=logs.append)
        self.assertEqual(rc, 42)
        self.assertTrue(any("exit 42" in m for m in logs))
        self.assertFalse(any("exit 0" in m for m in logs))
        self.assertTrue(any("res_final stderr" in m for m in logs))
        self.assertTrue(any("suitable output format" in m for m in logs))
        self.assertFalse(any("res_mux stderr" in m for m in logs))

    def test_log_falls_back_to_res_mux_when_final_missing(self):
        logs = []
        res_mux = SimpleNamespace(returncode=7, stderr="mux step blew up\n")
        rc = _log_final_mux_failure(None, res_mux, log=logs.append)
        self.assertEqual(rc, 7)
        self.assertTrue(any("exit 7" in m for m in logs))
        self.assertTrue(any("res_mux stderr" in m for m in logs))

    def test_mux_captured_tracks_logs_final_exit_not_mux(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            v_file = os.path.join(tmp, "video.fmp4")
            a_file = os.path.join(tmp, "audio.fmp4")
            out = os.path.join(tmp, "lecture.mp4")
            with open(v_file, "wb") as f:
                f.write(b"\x00" * 200)
            with open(a_file, "wb") as f:
                f.write(b"\x00" * 200)

            ts_path = os.path.join(tmp, "merged.ts")

            def fake_run(cmd, capture_output=True, text=True, timeout=300):
                # Step 1 (mpegts): succeed and create ts so step 2 runs.
                if "-f" in cmd and cmd[cmd.index("-f") + 1] == "mpegts":
                    with open(ts_path, "wb") as f:
                        f.write(b"\x00" * 300)
                    return SimpleNamespace(returncode=0, stdout="", stderr="mux ok")
                # Step 2 (mp4 part): fail with a distinctive code.
                return SimpleNamespace(
                    returncode=99,
                    stdout="",
                    stderr="ffmpeg: bogus final remux error\n",
                )

            with patch("sp_sync.sharepoint.sync_engine.subprocess.run", side_effect=fake_run):
                ok, size = _mux_captured_tracks(
                    "/usr/bin/ffmpeg",
                    v_file,
                    a_file,
                    out,
                    log=logs.append,
                    tmp_dir=tmp,
                )

            self.assertFalse(ok)
            self.assertEqual(size, 0)
            self.assertTrue(any("exit 99" in m for m in logs), logs)
            self.assertFalse(any("exit 0" in m for m in logs), logs)
            self.assertTrue(any("res_final stderr" in m for m in logs), logs)
            self.assertTrue(any("bogus final remux" in m for m in logs), logs)
            # Temps kept for recovery (also required by later retry work).
            self.assertTrue(os.path.exists(v_file))
            self.assertTrue(os.path.exists(a_file))


if __name__ == "__main__":
    unittest.main()
