"""Tests for shared HTTP session reuse, cookie health caching, bulk actions, untrimmed cleanup, and encrypted Telegram notifications."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class TestPowerFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = os.environ.get("SP_SYNC_DB_PATH")
        os.environ["SP_SYNC_DB_PATH"] = os.path.join(self.tmp.name, "power_test.sqlite")
        from sp_sync.config.settings import clear_settings_cache
        import sp_sync.db.store as store_mod

        clear_settings_cache()
        store_mod._store_singleton = None
        self.store = store_mod.get_store()
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

    def test_shared_sync_session_warms_each_url_only_once_across_folders(self):
        from sp_sync.sharepoint.sync_engine import (
            close_shared_sync_context,
            create_shared_sync_context,
            sync_sharepoint_files,
        )

        ctx = create_shared_sync_context("A" * 60, "B" * 25, "https://t.sharepoint.com/personal/u")
        share_link = "https://t.sharepoint.com/:f:/g/personal/u/tok123?e=1"
        prime_calls = []
        resolve_calls = []

        def fake_prime(_sess, _base, prime_url="", **_kw):
            prime_calls.append(prime_url)

        def fake_v2(_sess, u):
            resolve_calls.append(u)
            return "/personal/u/Documents", 200

        try:
            with patch("sp_sync.sharepoint.sync_engine.prime_sharepoint_session_for_browse", side_effect=fake_prime), patch(
                "sp_sync.sharepoint.sync_engine._sp_resolve_via_v2_share_driveitem", side_effect=fake_v2
            ), patch("sp_sync.sharepoint.sync_engine._sync_folder_recursive", return_value=0):
                for folder in ("/personal/u/Documents/F1", "/personal/u/Documents/F2", "/personal/u/Documents/F3"):
                    sync_sharepoint_files(
                        "A" * 60,
                        "B" * 25,
                        "https://t.sharepoint.com/personal/u",
                        folder,
                        self.tmp.name,
                        sharing_link=share_link,
                        dry_run=True,
                        sync_ctx=ctx,
                    )
        finally:
            close_shared_sync_context(ctx)

        # Across 3 folders sharing the same site/sharing link, share root & prime URLs are only resolved/warmed ONCE!
        self.assertEqual(len(resolve_calls), 1)
        self.assertEqual(prime_calls.count(share_link), 1)
        self.assertEqual(prime_calls.count("https://t.sharepoint.com/personal/u/"), 1)

    def test_cookie_health_cache_and_sidebar_expired_badge(self):
        from sp_sync.security.cookies import check_sharepoint_cookie_health, record_cookie_health

        self.store.set_sharepoint_cookies("A" * 60, "B" * 25)
        self.store.set_sharepoint_site_url("https://t.sharepoint.com/personal/u")

        with patch(
            "sp_sync.security.cookies.CookieValidator.test_cookie_connectivity",
            return_value={"success": False, "status_code": 302, "message": "Cookies expired — redirected to sign-in"},
        ) as mock_probe:
            h1 = check_sharepoint_cookie_health("A" * 60, "B" * 25, force=False)
            self.assertEqual(h1["status"], "expired")
            self.assertFalse(h1["cached"])
            # Second call within TTL hits cache (no second HTTP request!)
            h2 = check_sharepoint_cookie_health("A" * 60, "B" * 25, force=False)
            self.assertEqual(h2["status"], "expired")
            self.assertTrue(h2["cached"])
            self.assertEqual(mock_probe.call_count, 1)

        # Sidebar renders warning badge when expired
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("dot-warn", html)
        self.assertIn("statusExpired", html)

        # Saving fresh cookies resets expired badge cleanly
        record_cookie_health("ok", "Valid", "alice@example.com")
        res = self.client.get("/sp/cookie_health").get_json()
        self.assertEqual(res["health"]["status"], "ok")

    def test_sharepoint_bulk_enable_disable_and_sync_selected(self):
        self.store.set_sharepoint_configs(
            [
                {"name": "C1", "rel_url": "/personal/u/Documents/C1", "local_path": self.tmp.name, "sync_enabled": True},
                {"name": "C2", "rel_url": "/personal/u/Documents/C2", "local_path": self.tmp.name, "sync_enabled": True},
                {"name": "C3", "rel_url": "/personal/u/Documents/C3", "local_path": self.tmp.name, "sync_enabled": False},
            ]
        )
        r_dis = self.client.post("/sp/bulk_action", json={"action": "disable", "indices": [0, 1]})
        self.assertEqual(r_dis.status_code, 200)
        self.assertEqual(r_dis.get_json()["updated"], 2)
        cfgs = self.store.get_sharepoint_configs()
        self.assertFalse(cfgs[0]["sync_enabled"])
        self.assertFalse(cfgs[1]["sync_enabled"])
        self.assertFalse(cfgs[2]["sync_enabled"])

        r_en = self.client.post("/sp/bulk_action", json={"action": "enable", "indices": [0, 2]})
        self.assertEqual(r_en.status_code, 200)
        cfgs2 = self.store.get_sharepoint_configs()
        self.assertTrue(cfgs2[0]["sync_enabled"])
        self.assertFalse(cfgs2[1]["sync_enabled"])
        self.assertTrue(cfgs2[2]["sync_enabled"])

        # Bulk sync selected launches orchestrator with exact config_indices
        self.store.set_sharepoint_cookies("A" * 60, "B" * 25)
        self.store.set_sharepoint_site_url("https://t.sharepoint.com/personal/u")
        ran = []

        def fake_sp(_f, _r, _s, cfg, **_kw):
            ran.append(cfg["name"])
            return 0

        with patch("sp_sync.sharepoint.sync_engine.start_sync_from_config", side_effect=fake_sp), patch(
            "sp_sync.sharepoint.sync_engine._cleanup_playwright"
        ):
            r_sync = self.client.post("/sp/bulk_action", json={"action": "sync", "indices": [1, 2]})
            self.assertEqual(r_sync.status_code, 200)
            from sp_sync.web.app import _orchestrator

            t = _orchestrator._threads.get("sharepoint")
            if t:
                t.join(timeout=3.0)
        self.assertEqual(ran, ["C2", "C3"])

    def test_ledger_redownload_missing_and_safe_untrimmed_cleanup(self):
        course_dir = os.path.join(self.tmp.name, "CourseA")
        os.makedirs(course_dir)
        present_mp4 = os.path.join(course_dir, "lec1.mp4")
        missing_mp4 = os.path.join(course_dir, "lec2.mp4")
        safe_backup = os.path.join(course_dir, "lec1.untrimmed.mp4")
        orphan_backup = os.path.join(course_dir, "orphan.untrimmed.mp4")

        # Paired trimmed MP4 >= 1MB -> safe_backup is eligible; orphan_backup has NO paired .mp4 -> MUST be kept!
        with open(present_mp4, "wb") as f:
            f.write(b"\x00" * (1024 * 1024 + 10))
        with open(safe_backup, "wb") as f:
            f.write(b"B" * 4096)
        with open(orphan_backup, "wb") as f:
            f.write(b"O" * 4096)

        self.store.set_sharepoint_configs(
            [{"name": "CourseA", "rel_url": "/personal/u/Documents/CourseA", "local_path": course_dir}]
        )
        k1 = self.store.ledger_record(item_id="L1", rel_url="/a/1.mp4", original_name="1.mp4", local_path=present_mp4, size=100)
        k2 = self.store.ledger_record(item_id="L2", rel_url="/a/2.mp4", original_name="2.mp4", local_path=missing_mp4, size=100)

        # Queue all missing -> only k2 queued, k1 untouched
        r_m = self.client.post("/api/ledger/redownload_missing")
        self.assertEqual(r_m.status_code, 200)
        self.assertEqual(r_m.get_json()["queued"], 1)
        self.assertEqual(self.store.ledger_get(k1)["redownload_requested"], 0)
        self.assertEqual(self.store.ledger_get(k2)["redownload_requested"], 1)

        # Scan untrimmed backups -> finds ONLY safe_backup, ignores orphan_backup
        st = self.client.get("/api/ledger/untrimmed").get_json()
        self.assertEqual(st["files"], 1)

        cl = self.client.post("/api/ledger/cleanup_untrimmed").get_json()
        self.assertEqual(cl["removed"], 1)
        self.assertFalse(os.path.exists(safe_backup))
        self.assertTrue(os.path.exists(orphan_backup))

    def test_telegram_encrypted_at_rest_and_notification_delivery(self):
        from sp_sync.db.store import KEY_TELEGRAM_SETTINGS
        from sp_sync.notifications.telegram import notify_sync_summary

        secret_token = "987654321:ABCdefGhIjKlMnOpQrStUvWxYz123456"
        res = self.client.post(
            "/api/notifications/telegram",
            json={"enabled": True, "bot_token": secret_token, "chat_id": "55512345", "notify_on_success": True},
        )
        self.assertEqual(res.status_code, 200)
        masked = res.get_json()["settings"]
        self.assertTrue(masked["enabled"])
        self.assertTrue(masked["has_bot_token"])
        self.assertNotIn(secret_token, str(masked))

        # Verify encrypted on disk in SQLite KV table!
        with self.store._connect() as conn:
            raw_val = conn.execute("SELECT value FROM kv WHERE key = ?", (KEY_TELEGRAM_SETTINGS,)).fetchone()[0]
        self.assertTrue(raw_val.startswith("enc:v1:"))
        self.assertNotIn(secret_token, raw_val)

        # Verify summary delivery calls Telegram Bot API with decrypted token
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {"ok": True}
        with patch("sp_sync.notifications.telegram.requests.post", return_value=fake_resp) as mock_post:
            sent = notify_sync_summary(
                {
                    "target": "all",
                    "finished_at": "2026-11-25 02:00:05",
                    "downloaded_files": 2,
                    "new_files": ["Lecture_01.mp4", "Lecture_02.mp4"],
                    "errors": [],
                }
            )
            self.assertTrue(sent)
            self.assertEqual(mock_post.call_count, 1)
            called_url = mock_post.call_args[0][0]
            self.assertIn(secret_token, called_url)
            called_json = mock_post.call_args[1]["json"]
            self.assertEqual(called_json["chat_id"], "55512345")
            self.assertIn("Lecture_01.mp4", called_json["text"])


if __name__ == "__main__":
    unittest.main()
