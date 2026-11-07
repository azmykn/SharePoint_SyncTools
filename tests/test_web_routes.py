"""
Web Application Flask Route Tests
=================================
"""

import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class TestWebRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_web.sqlite")
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

    def test_status_endpoint(self):
        res = self.client.get("/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("sharepoint", data)
        self.assertIn("gdrive", data)

    def test_logs_and_clear_endpoint(self):
        from sp_sync.web.app import add_log
        add_log("sharepoint", "Test SP log")
        add_log("gdrive", "Test GD log")

        res = self.client.get("/logs?source=all")
        self.assertEqual(res.status_code, 200)
        logs = res.get_json()
        self.assertGreaterEqual(len(logs), 2)

        # Clear sharepoint logs
        res_clear_sp = self.client.post("/logs/clear", json={"source": "sharepoint"})
        self.assertEqual(res_clear_sp.status_code, 200)
        self.assertTrue(res_clear_sp.get_json()["success"])

        # Clear all logs
        res_clear_all = self.client.post("/logs/clear", json={"source": "all"})
        self.assertEqual(res_clear_all.status_code, 200)
        self.assertTrue(res_clear_all.get_json()["success"])

    def test_toggle_sync_and_reorder_folders(self):
        from sp_sync.db.store import get_store
        store = get_store()
        initial_configs = [
            {"name": "Folder 1", "rel_url": "/personal/u/Documents/F1", "local_path": "/tmp/f1", "sync_enabled": True},
            {"name": "Folder 2", "rel_url": "/personal/u/Documents/F2", "local_path": "/tmp/f2", "sync_enabled": True},
        ]
        store.set_sharepoint_configs(initial_configs)

        # Toggle first folder sync off
        res_toggle = self.client.post("/sp/toggle_sync", json={"index": 0, "enabled": False})
        self.assertEqual(res_toggle.status_code, 200)
        data = res_toggle.get_json()
        self.assertTrue(data["success"])
        self.assertFalse(data["enabled"])

        # Reorder folders
        res_reorder = self.client.post("/sp/reorder_folders", json={"order": [1, 0]})
        self.assertEqual(res_reorder.status_code, 200)
        configs = store.get_sharepoint_configs()
        self.assertEqual(configs[0]["name"], "Folder 2")
        self.assertEqual(configs[1]["name"], "Folder 1")

    def test_api_browse_folder_headless_safe(self):
        """Ensure /api/browse_folder does not raise 500 when display is absent."""
        res = self.client.get("/api/browse_folder")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("success", data)


if __name__ == "__main__":
    unittest.main()
