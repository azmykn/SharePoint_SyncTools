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
            self.assertTrue(any("retrying mux once" in m.lower() for m in logs), logs)
            # Temps kept for recovery after both attempts fail.
            self.assertTrue(os.path.exists(v_file))
            self.assertTrue(os.path.exists(a_file))
            self.assertTrue(any("Kept temp files for recovery" in m for m in logs), logs)


class TestMuxRetryKeepsTemps(unittest.TestCase):
    def test_mux_retries_once_and_succeeds(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            v_file = os.path.join(tmp, "video.fmp4")
            a_file = os.path.join(tmp, "audio.fmp4")
            out = os.path.join(tmp, "lecture.mp4")
            with open(v_file, "wb") as f:
                f.write(b"\x00" * 200)
            with open(a_file, "wb") as f:
                f.write(b"\x00" * 200)

            state = {"final_calls": 0}

            def fake_run(cmd, capture_output=True, text=True, timeout=300):
                if "-f" in cmd and cmd[cmd.index("-f") + 1] == "mpegts":
                    ts_path = cmd[-1]
                    with open(ts_path, "wb") as f:
                        f.write(b"\x00" * 300)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                # Final remux: fail once, then write a valid-looking mp4.part
                state["final_calls"] += 1
                part_path = cmd[-1]
                if state["final_calls"] == 1:
                    return SimpleNamespace(returncode=1, stdout="", stderr="transient fail\n")
                with open(part_path, "wb") as f:
                    f.write(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 2048)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("sp_sync.sharepoint.sync_engine.subprocess.run", side_effect=fake_run), patch(
                "sp_sync.sharepoint.sync_engine._ffprobe_duration_seconds", return_value=1.0
            ), patch(
                "sp_sync.sharepoint.sync_engine._resolve_ffprobe_path", return_value="/usr/bin/ffprobe"
            ):
                ok, size = _mux_captured_tracks(
                    "/usr/bin/ffmpeg",
                    v_file,
                    a_file,
                    out,
                    log=logs.append,
                    tmp_dir=tmp,
                )

            self.assertTrue(ok)
            self.assertGreater(size, 0)
            self.assertEqual(state["final_calls"], 2)
            self.assertTrue(os.path.isfile(out))
            self.assertFalse(os.path.exists(v_file))  # cleaned after success
            self.assertTrue(any("succeeded on retry" in m.lower() for m in logs), logs)

    def test_mux_keeps_inputs_when_both_attempts_fail(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            v_file = os.path.join(tmp, "video.fmp4")
            a_file = os.path.join(tmp, "audio.fmp4")
            out = os.path.join(tmp, "lecture.mp4")
            with open(v_file, "wb") as f:
                f.write(b"\x00" * 200)
            with open(a_file, "wb") as f:
                f.write(b"\x00" * 200)

            def fake_run(cmd, capture_output=True, text=True, timeout=300):
                if "-f" in cmd and cmd[cmd.index("-f") + 1] == "mpegts":
                    with open(cmd[-1], "wb") as f:
                        f.write(b"\x00" * 300)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=5, stdout="", stderr="always fail\n")

            with patch("sp_sync.sharepoint.sync_engine.subprocess.run", side_effect=fake_run):
                ok, size = _mux_captured_tracks(
                    "/usr/bin/ffmpeg", v_file, a_file, out, log=logs.append, tmp_dir=tmp
                )

            self.assertFalse(ok)
            self.assertEqual(size, 0)
            self.assertTrue(os.path.exists(v_file))
            self.assertTrue(os.path.exists(a_file))
            self.assertTrue(any(v_file in m for m in logs), logs)


if __name__ == "__main__":
    unittest.main()
