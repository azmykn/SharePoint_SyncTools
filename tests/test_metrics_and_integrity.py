"""
Metrics, Integrity, and Enhancement Route Tests
===============================================
"""

import os
import sys
import tempfile
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.sync_metrics import (
    reset_metrics,
    set_sync_active,
    start_file_progress,
    update_file_progress,
    complete_file_progress,
    get_live_metrics,
)
from sp_sync.sharepoint.sync_engine import _is_valid_mp4


class TestMetricsAndIntegrity(unittest.TestCase):
    def setUp(self):
        reset_metrics()

    def tearDown(self):
        reset_metrics()

    def test_sync_metrics_lifecycle(self):
        metrics = get_live_metrics()
        self.assertFalse(metrics["is_syncing"])
        self.assertFalse(metrics["is_active"])
        self.assertEqual(metrics["file_progress_pct"], 0)

        # Activate sync
        set_sync_active(True, service="sharepoint", queue_total=2)
        metrics = get_live_metrics()
        self.assertTrue(metrics["is_syncing"])
        self.assertEqual(metrics["queue_total"], 2)

        # Start file 1 (1000 bytes)
        start_file_progress("MBA-08", "lecture1.mp4", total_bytes=1000)
        time.sleep(0.01)
        update_file_progress(500)
        metrics = get_live_metrics()
        self.assertEqual(metrics["current_file"], "lecture1.mp4")
        self.assertEqual(metrics["current_folder"], "MBA-08")
        self.assertEqual(metrics["downloaded_bytes"], 500)
        self.assertEqual(metrics["total_bytes"], 1000)
        self.assertEqual(metrics["file_progress_pct"], 50)

        # Complete file 1
        complete_file_progress("lecture1.mp4")
        metrics = get_live_metrics()
        self.assertEqual(metrics["queue_completed"], 1)

        # End sync
        set_sync_active(False)
        metrics = get_live_metrics()
        self.assertFalse(metrics["is_syncing"])

    def test_is_valid_mp4(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            # 1. Non-existent file
            self.assertFalse(_is_valid_mp4(os.path.join(tmp_dir, "missing.mp4")))

            # 2. Empty file (0 bytes)
            empty_file = os.path.join(tmp_dir, "empty.mp4")
            with open(empty_file, "wb") as f:
                pass
            self.assertFalse(_is_valid_mp4(empty_file))

            # 3. Text / HTML error response saved as mp4
            html_file = os.path.join(tmp_dir, "error.mp4")
            with open(html_file, "wb") as f:
                f.write(b"<!DOCTYPE html><html>404 Not Found</html>")
            self.assertFalse(_is_valid_mp4(html_file))

            # 4. Valid MP4 with ftyp signature
            valid_mp4 = os.path.join(tmp_dir, "valid.mp4")
            with open(valid_mp4, "wb") as f:
                f.write(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 200)
            self.assertTrue(_is_valid_mp4(valid_mp4))

            # 5. Valid MP4 with moov atom
            valid_moov = os.path.join(tmp_dir, "moov.mp4")
            with open(valid_moov, "wb") as f:
                f.write(b"\x00\x00\x00\x08moov" + b"\x00" * 200)
            self.assertTrue(_is_valid_mp4(valid_moov))


class TestEnhancementRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_enhancement_routes.sqlite")
        os.environ["SP_SYNC_DB_PATH"] = self.db_path

        from sp_sync.config.settings import clear_settings_cache
        clear_settings_cache()

        import sp_sync.db.store as store_mod
        store_mod._store_singleton = None

        from sp_sync.web.app import app
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_live_metrics_endpoint(self):
        res = self.client.get("/sync/live_metrics")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("is_syncing", data)
        self.assertIn("file_progress_pct", data)
        self.assertIn("speed_mbps", data)

    def test_scheduler_settings_endpoints(self):
        # GET settings
        res = self.client.get("/scheduler/settings")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("settings", data)
        self.assertIn("is_due", data)
        self.assertIn("daemon_alive", data)
        self.assertFalse(data["settings"]["enabled"])

        # POST settings (valid)
        res_post = self.client.post("/scheduler/settings", json={
            "enabled": True,
            "interval_hours": 4,
            "target": "gdrive"
        })
        self.assertEqual(res_post.status_code, 200)
        post_data = res_post.get_json()
        self.assertTrue(post_data["success"])
        self.assertIn("is_due", post_data)
        self.assertIn("daemon_alive", post_data)
        self.assertTrue(post_data["settings"]["enabled"])
        self.assertEqual(post_data["settings"]["interval_hours"], 4)
        self.assertEqual(post_data["settings"]["target"], "gdrive")

        # POST settings (invalid)
        res_bad = self.client.post("/scheduler/settings", json={
            "interval_hours": 3,
            "target": "invalid-target"
        })
        self.assertEqual(res_bad.status_code, 400)
        bad_data = res_bad.get_json()
        self.assertFalse(bad_data["success"])
        self.assertEqual(bad_data.get("code"), "INVALID_SCHEDULER_SETTINGS")

    def test_scheduler_run_now_endpoint(self):
        from unittest.mock import patch

        with patch("sp_sync.web.app.run_scheduler_now", return_value={
            "success": True,
            "target": "sharepoint",
            "last_run": "2026-01-01 10:00:00",
            "next_run": "2026-01-01 16:00:00",
        }):
            res_ok = self.client.post("/scheduler/run_now", json={"target": "sharepoint"})
            self.assertEqual(res_ok.status_code, 200)
            ok_data = res_ok.get_json()
            self.assertTrue(ok_data["success"])
            self.assertEqual(ok_data["target"], "sharepoint")

        with patch("sp_sync.web.app.run_scheduler_now", side_effect=ValueError("target must be one of: all, sharepoint, gdrive")):
            res_bad = self.client.post("/scheduler/run_now", json={"target": "bad"})
            self.assertEqual(res_bad.status_code, 400)
            bad_data = res_bad.get_json()
            self.assertFalse(bad_data["success"])
            self.assertEqual(bad_data.get("code"), "INVALID_SCHEDULER_TARGET")

    def test_open_local_folder_endpoint(self):
        from unittest.mock import patch

        # Missing path
        res_missing = self.client.post("/api/open_local_folder", json={})
        self.assertEqual(res_missing.status_code, 400)

        # Existing directory
        with patch("subprocess.Popen") as mock_popen, patch("os.startfile", create=True):
            res_ok = self.client.post("/api/open_local_folder", json={"folder_path": self.tmp_dir.name})
            self.assertEqual(res_ok.status_code, 200)
            self.assertTrue(res_ok.get_json()["success"])

        # Non-existent uncreatable path
        res_none = self.client.post("/api/open_local_folder", json={"folder_path": "/nonexistent/path/123xyz"})
        self.assertEqual(res_none.status_code, 400)
        self.assertFalse(res_none.get_json()["success"])

    def test_sync_report_endpoint(self):
        from sp_sync.db.store import get_store

        store = get_store()
        store.set_sync_report("sharepoint", {
            "service": "sharepoint",
            "status": "success",
            "downloaded_files": 7,
        })
        store.set_sync_report("gdrive", {
            "service": "gdrive",
            "status": "partial",
            "downloaded_files": 3,
        })

        res_all = self.client.get("/sync/report")
        self.assertEqual(res_all.status_code, 200)
        all_data = res_all.get_json()
        self.assertTrue(all_data["success"])
        self.assertIn("reports", all_data)
        self.assertEqual(all_data["reports"]["sharepoint"].get("downloaded_files"), 7)
        self.assertEqual(all_data["reports"]["gdrive"].get("downloaded_files"), 3)

        res_sp = self.client.get("/sync/report?service=sharepoint")
        self.assertEqual(res_sp.status_code, 200)
        sp_data = res_sp.get_json()
        self.assertEqual(sp_data["service"], "sharepoint")
        self.assertEqual(sp_data["report"].get("status"), "success")

        res_bad = self.client.get("/sync/report?service=dropbox")
        self.assertEqual(res_bad.status_code, 400)
        bad_data = res_bad.get_json()
        self.assertFalse(bad_data["success"])
        self.assertEqual(bad_data.get("code"), "INVALID_SERVICE")

        # Clear one service report only
        res_clear_sp = self.client.post("/sync/report/clear", json={"service": "sharepoint"})
        self.assertEqual(res_clear_sp.status_code, 200)
        self.assertTrue(res_clear_sp.get_json()["success"])

        res_all_after_sp = self.client.get("/sync/report")
        after_sp_data = res_all_after_sp.get_json()
        self.assertEqual(after_sp_data["reports"]["sharepoint"], {})
        self.assertEqual(after_sp_data["reports"]["gdrive"].get("downloaded_files"), 3)

        # Clear all
        res_clear_all = self.client.post("/sync/report/clear", json={"service": "all"})
        self.assertEqual(res_clear_all.status_code, 200)
        self.assertTrue(res_clear_all.get_json()["success"])

        res_all_after = self.client.get("/sync/report")
        after_data = res_all_after.get_json()
        self.assertEqual(after_data["reports"]["sharepoint"], {})
        self.assertEqual(after_data["reports"]["gdrive"], {})

        # Invalid clear service
        res_clear_bad = self.client.post("/sync/report/clear", json={"service": "dropbox"})
        self.assertEqual(res_clear_bad.status_code, 400)
        clear_bad_data = res_clear_bad.get_json()
        self.assertFalse(clear_bad_data["success"])
        self.assertEqual(clear_bad_data.get("code"), "INVALID_SERVICE")


if __name__ == "__main__":
    unittest.main()
