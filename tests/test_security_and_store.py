"""Tests for SQLite encryption vault, WAL & connection closure, CSRF guard, and path safety."""

import os
import sqlite3
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.config.validator import ConfigValidator
from sp_sync.db.store import AppStore, KEY_SP_COOKIES
from sp_sync.security.vault import is_encrypted


class TestSecurityAndStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = os.environ.get("SP_SYNC_DB_PATH")
        self.db_path = os.path.join(self.tmp.name, "sec_test.sqlite")
        os.environ["SP_SYNC_DB_PATH"] = self.db_path
        from sp_sync.config.settings import clear_settings_cache
        clear_settings_cache()
        import sp_sync.db.store as store_mod
        store_mod._store_singleton = None
        self.store = AppStore(self.db_path)

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

    def test_cookies_encrypted_at_rest_and_legacy_plaintext_readable(self):
        self.store.set_sharepoint_cookies("F" * 60, "R" * 30)
        # Raw DB value must be encrypted (enc:v1:...) when cryptography is installed
        conn = sqlite3.connect(self.db_path)
        try:
            raw = conn.execute("SELECT value FROM kv WHERE key = ?", (KEY_SP_COOKIES,)).fetchone()[0]
        finally:
            conn.close()
        self.assertTrue(is_encrypted(raw))
        self.assertNotIn("FFFFFF", raw)

        # Round-trip decrypts cleanly
        got = self.store.get_sharepoint_cookies()
        self.assertEqual(got["FedAuth"], "F" * 60)
        self.assertEqual(got["rtFa"], "R" * 30)

        # Legacy plaintext value written directly still reads transparently
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE kv SET value = ? WHERE key = ?",
                ('{"FedAuth": "legacy_fed", "rtFa": "legacy_rt"}', KEY_SP_COOKIES),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.store.get_sharepoint_cookies()["FedAuth"], "legacy_fed")

    def test_sqlite_wal_mode_enabled_and_ledger_zero_size_preserved(self):
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

        key = self.store.ledger_record(
            item_id="Z0", rel_url="/x/empty.txt", original_name="empty.txt", local_path="/tmp/empty.txt", size=0
        )
        self.assertEqual(self.store.ledger_get(key)["size"], 0)

    def test_gdrive_validator_accepts_multi_account_and_open_urls(self):
        for url in (
            "https://drive.google.com/drive/u/0/folders/1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a",
            "https://drive.google.com/drive/mobile/folders/1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a",
            "https://drive.google.com/open?id=1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a",
        ):
            res = ConfigValidator.validate_gdrive_config(
                {
                    "name": "Course Drive",
                    "folder_url": url,
                    "folder_id": "1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a",
                    "local_path": self.tmp.name,
                }
            )
            self.assertTrue(res["valid"], f"Expected valid for {url}: {res['errors']}")

    def test_csrf_guard_blocks_cross_origin_post_and_allows_localhost(self):
        from sp_sync.web.app import create_app

        prod_app = create_app(start_scheduler=False)
        prod_app.config["TESTING"] = False
        client = prod_app.test_client()

        blocked = client.post(
            "/logs/clear",
            json={"source": "all"},
            headers={"Origin": "https://evil.example.com"},
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json()["code"], "CSRF_ORIGIN_BLOCKED")

        allowed = client.post(
            "/logs/clear",
            json={"source": "all"},
            headers={"Origin": "http://127.0.0.1:5000"},
        )
        self.assertEqual(allowed.status_code, 200)

    def test_open_local_folder_rejects_executable_file(self):
        from sp_sync.web.app import app

        app.config["TESTING"] = True
        client = app.test_client()
        file_path = os.path.join(self.tmp.name, "script.sh")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho hi\n")
        res = client.post("/api/open_local_folder", json={"folder_path": file_path})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "INVALID_FOLDER_PATH")


if __name__ == "__main__":
    unittest.main()
