"""Tests for new API routes: app_settings, system health, temp cleanup, export/import, guest link safety, GD toggle, logs after_id."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class TestSystemAndNewRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = os.environ.get("SP_SYNC_DB_PATH")
        os.environ["SP_SYNC_DB_PATH"] = os.path.join(self.tmp.name, "routes_test.sqlite")
        from sp_sync.config.settings import clear_settings_cache
        clear_settings_cache()
        import sp_sync.db.store as store_mod
        store_mod._store_singleton = None
        from sp_sync.web.app import app
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        import sp_sync.db.store as store_mod
        from sp_sync.config.settings import clear_settings_cache
        store_mod._store_singleton = None
        if self._old_db is None:
            os.environ.pop("SP_SYNC_DB_PATH", None)
        else:
            os.environ["SP_SYNC_DB_PATH"] = self._old_db
        clear_settings_cache()
        self.tmp.cleanup()

    def test_guest_link_does_not_clobber_per_folder_sharing_links_by_default(self):
        from sp_sync.db.store import get_store

        orig = "https://t-my.sharepoint.com/:f:/g/personal/u/courseA?e=1"
        get_store().set_sharepoint_configs(
            [
                {
                    "name": "Course A",
                    "rel_url": "/personal/u/Documents/A",
                    "local_path": self.tmp.name,
                    "sharing_link": orig,
                }
            ]
        )
        new_guest = "https://t-my.sharepoint.com/:f:/g/personal/u/otherB?e=2"
        res = self.client.post("/sp/save_guest_link", json={"guest_link": new_guest})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["updated_folders"], 0)
        self.assertEqual(get_store().get_sharepoint_configs()[0]["sharing_link"], orig)

        # Explicit apply_to_all=True updates per-folder link
        res2 = self.client.post("/sp/save_guest_link", json={"guest_link": new_guest, "apply_to_all": True})
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.get_json()["updated_folders"], 1)
        self.assertEqual(get_store().get_sharepoint_configs()[0]["sharing_link"], new_guest)

    def test_app_settings_roundtrip_and_validation(self):
        r1 = self.client.get("/api/app_settings")
        self.assertEqual(r1.status_code, 200)
        self.assertIn("trim_silence_enabled", r1.get_json()["settings"])

        r_off = self.client.post("/api/app_settings", json={"trim_silence_enabled": False})
        self.assertEqual(r_off.status_code, 200)
        self.assertFalse(r_off.get_json()["settings"]["trim_silence_enabled"])

        r2 = self.client.post(
            "/api/app_settings",
            json={"trim_silence_enabled": True, "trim_silence_keep_backup": False, "duration_tolerance_seconds": 45},
        )
        self.assertEqual(r2.status_code, 200)
        st = r2.get_json()["settings"]
        self.assertTrue(st["trim_silence_enabled"])
        self.assertFalse(st["trim_silence_keep_backup"])
        self.assertEqual(st["duration_tolerance_seconds"], 45.0)

        r_bad = self.client.post("/api/app_settings", json={"trim_silence_noise_db": 10})
        self.assertEqual(r_bad.status_code, 400)
        self.assertEqual(r_bad.get_json()["code"], "INVALID_APP_SETTINGS")

    def test_system_health_and_temp_cleanup(self):
        fake_tmp = os.path.join(self.tmp.name, "tmp_segments")
        os.makedirs(os.path.join(fake_tmp, "empty_dir"))
        os.makedirs(os.path.join(fake_tmp, "old_dir"))
        with open(os.path.join(fake_tmp, "old_dir", "a.fmp4"), "wb") as f:
            f.write(b"x" * 2048)

        with patch("sp_sync.sharepoint.sync_engine.TMP_DIR", fake_tmp):
            h = self.client.get("/api/system/health").get_json()
            self.assertTrue(h["success"])
            self.assertEqual(h["health"]["tmp_segments"]["dirs"], 2)

            c1 = self.client.post("/api/system/cleanup_temp", json={"empty_only": True}).get_json()
            self.assertTrue(c1["success"])
            self.assertEqual(c1["removed_dirs"], 1)
            self.assertTrue(os.path.isdir(os.path.join(fake_tmp, "old_dir")))

            c2 = self.client.post("/api/system/cleanup_temp", json={"empty_only": False, "min_age_hours": 0}).get_json()
            self.assertTrue(c2["success"])
            self.assertEqual(c2["removed_dirs"], 1)

    def test_config_export_and_import_roundtrip(self):
        self.client.post(
            "/sp/add_folder",
            json={"name": "MBA Course", "rel_url": "/personal/u/Documents/MBA", "local_path": self.tmp.name},
        )
        exp = self.client.get("/api/config/export")
        self.assertEqual(exp.status_code, 200)
        backup = exp.get_json()
        self.assertEqual(len(backup["sharepoint_configs"]), 1)

        # Wipe and re-import
        self.client.post("/sp/remove_folder", json={"index": 0})
        self.assertEqual(len(self.client.get("/sp/configs").get_json()["configs"]), 0)

        imp = self.client.post("/api/config/import", json=backup)
        self.assertEqual(imp.status_code, 200)
        self.assertEqual(imp.get_json()["imported"]["sharepoint_configs"], 1)
        self.assertEqual(len(self.client.get("/sp/configs").get_json()["configs"]), 1)

    def test_gdrive_toggle_and_reorder_and_incremental_logs(self):
        for i in (1, 2):
            r = self.client.post(
                "/gdrive/add_folder",
                json={
                    "name": f"GD {i}",
                    "folder_url": f"https://drive.google.com/drive/folders/1234567890abcd{i}",
                    "local_path": self.tmp.name,
                },
            )
            self.assertEqual(r.status_code, 200)

        t = self.client.post("/gdrive/toggle_sync", json={"index": 0, "enabled": False})
        self.assertEqual(t.status_code, 200)
        self.assertFalse(t.get_json()["enabled"])

        ro = self.client.post("/gdrive/reorder_folders", json={"order": [1, 0]})
        self.assertEqual(ro.status_code, 200)
        cfgs = self.client.get("/gdrive/configs").get_json()["configs"]
        self.assertEqual(cfgs[0]["name"], "GD 2")

        # Incremental logs via after_id
        from sp_sync.web.app import add_log

        add_log("system", "Log line A")
        first = self.client.get("/logs?source=system").get_json()
        last_id = first[-1]["id"]
        add_log("system", "Log line B")
        delta = self.client.get(f"/logs?source=system&after_id={last_id}").get_json()
        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]["msg"], "Log line B")

    def test_index_renders_cleanly_without_exposing_raw_cookies(self):
        from sp_sync.db.store import get_store

        secret_fed = "SUPER_SECRET_FEDAUTH_TOKEN_" + "X" * 40
        get_store().set_sharepoint_cookies(secret_fed, "SUPER_SECRET_RTFA_123456789012345")

        # Every dedicated URL renders cleanly inside the shared sidebar + topbar shell without leaking cookies
        for url in ("/", "/sharepoint", "/explorer", "/gdrive", "/ledger", "/settings", "/activity", "/gdrive/auth_page"):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, f"GET {url} failed")
            html = res.get_data(as_text=True)
            self.assertNotIn(secret_fed, html, f"Raw cookie leaked on {url}")
            self.assertIn("toast-stack", html)
            self.assertIn("class=\"sidebar\"", html)
            self.assertIn("class=\"topbar\"", html)

        # Settings page carries the scheduler controls
        set_html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("scheduler-mode", set_html)
        self.assertIn("scheduler-daily-time", set_html)

        # /logs returns HTML with shared shell for browsers (Accept: text/html) and JSON for API clients
        html_logs = self.client.get("/logs", headers={"Accept": "text/html,application/xhtml+xml"})
        self.assertEqual(html_logs.status_code, 200)
        self.assertIn("terminal-live", html_logs.get_data(as_text=True))
        self.assertIn("class=\"sidebar\"", html_logs.get_data(as_text=True))

        json_logs = self.client.get("/api/logs")
        self.assertEqual(json_logs.status_code, 200)
        self.assertIsInstance(json_logs.get_json(), list)

        # Legacy ?tab= redirects cleanly to dedicated URLs
        redir = self.client.get("/?tab=sp")
        self.assertEqual(redir.status_code, 302)
        self.assertTrue(redir.headers["Location"].endswith("/sharepoint"))

    def test_ledger_groups_follow_configured_sync_order(self):
        from sp_sync.db.store import get_store

        d_zebra = os.path.join(self.tmp.name, "Zebra Course")
        d_alpha = os.path.join(self.tmp.name, "Alpha Course")
        os.makedirs(d_zebra)
        os.makedirs(d_alpha)
        p_z = os.path.join(d_zebra, "Zebra_2026-10-01_01.mp4")
        p_a = os.path.join(d_alpha, "Alpha_2026-10-01_01.mp4")
        with open(p_z, "wb") as f:
            f.write(b"z")
        with open(p_a, "wb") as f:
            f.write(b"a")

        st = get_store()
        # Configure Zebra FIRST (#1) and Alpha SECOND (#2) — opposite of alphabetical order!
        st.set_sharepoint_configs(
            [
                {"name": "Zebra", "rel_url": "/personal/u/Documents/Zebra", "local_path": d_zebra, "sync_enabled": True},
                {"name": "Alpha", "rel_url": "/personal/u/Documents/Alpha", "local_path": d_alpha, "sync_enabled": False},
            ]
        )
        st.ledger_record(item_id="Z1", rel_url="/personal/u/Documents/Zebra/z.mp4", original_name="z.mp4", local_path=p_z, size=10)
        st.ledger_record(item_id="A1", rel_url="/personal/u/Documents/Alpha/a.mp4", original_name="a.mp4", local_path=p_a, size=10)

        groups = self.client.get("/api/ledger").get_json()["groups"]
        self.assertEqual([g["subject"] for g in groups], ["Zebra Course", "Alpha Course"])
        self.assertEqual([g["sync_order"] for g in groups], [1, 2])
        self.assertEqual([g["sync_enabled"] for g in groups], [True, False])


if __name__ == "__main__":
    unittest.main()
