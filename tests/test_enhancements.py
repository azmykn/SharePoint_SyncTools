"""
Enhancements and Core Utilities Tests
====================================
Tests SQLite store, validation, and retry helpers using standard unittest.
"""

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.config.validator import ConfigValidator, validate_and_fix_configs
from sp_sync.db.store import AppStore
from sp_sync.security.cookies import CookieValidator
from sp_sync.sharepoint.sync_engine import get_sp_site_url, retry_with_backoff


class TestAppStore(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_app.sqlite")
        self.store = AppStore(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_sqlite_store_cookies_and_validation(self):
        self.store.set_sharepoint_cookies(
            "fedauth_test_value_long_enough_for_validation_xxxxxxxx",
            "rtfa_test_value_long_ok",
        )
        c = self.store.get_sharepoint_cookies()
        self.assertTrue(c.get("FedAuth"))

        validator = CookieValidator()
        validation = validator.validate_cookies(c.get("FedAuth", ""), c.get("rtFa", ""))
        self.assertTrue(validation["valid"])

    def test_sharepoint_site_url_in_store(self):
        url = "https://contoso-my.sharepoint.com/personal/user_contoso_onmicrosoft_com"
        self.store.set_sharepoint_site_url(url)
        self.assertEqual(self.store.get_sharepoint_site_url(), url)

    def test_store_clear_logs(self):
        self.store.append_log("2026-05-19 12:00:00", "sharepoint", "Msg 1")
        self.store.append_log("2026-05-19 12:00:01", "gdrive", "Msg 2")
        self.assertEqual(len(self.store.recent_logs(10)), 2)

        # Clear only sharepoint
        self.store.clear_logs("sharepoint")
        logs = self.store.recent_logs(10)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["source"], "gdrive")

        # Clear all
        self.store.clear_logs("all")
        self.assertEqual(len(self.store.recent_logs(10)), 0)


class TestConfigValidation(unittest.TestCase):
    def test_valid_sharepoint_config(self):
        valid_sp_config = {
            "name": "Test Folder",
            "rel_url": "/personal/test/Documents/TestFolder",
            "local_path": "/tmp/test_output",
        }
        res = ConfigValidator.validate_sharepoint_config(valid_sp_config)
        self.assertTrue(res["valid"])
        self.assertEqual(len(res["errors"]), 0)

    def test_site_url_without_personal_does_not_warn(self):
        team_sp_config = {
            "name": "Team Folder",
            "rel_url": "/sites/Engineering/Shared Documents/Design",
            "local_path": "/tmp/test_output",
        }
        res = ConfigValidator.validate_sharepoint_config(team_sp_config)
        self.assertTrue(res["valid"])
        self.assertEqual(len(res["warnings"]), 0)

    def test_invalid_sharepoint_config(self):
        invalid_sp_config = {
            "name": "A",
            "rel_url": "invalid_url_no_slash",
            "local_path": "/tmp/test_output",
        }
        res = ConfigValidator.validate_sharepoint_config(invalid_sp_config)
        self.assertFalse(res["valid"])
        self.assertTrue(any("must start with '/'" in e for e in res["errors"]))

    def test_valid_gdrive_config(self):
        valid_gd_config = {
            "name": "Test GDrive Folder",
            "folder_url": "https://drive.google.com/drive/folders/1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a",
            "folder_id": "1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a",
            "local_path": "/tmp/test_gdrive_output",
        }
        res = ConfigValidator.validate_gdrive_config(valid_gd_config)
        self.assertTrue(res["valid"])


class TestRetryMechanism(unittest.TestCase):
    def test_immediate_success(self):
        success, result = retry_with_backoff(lambda: "immediate", max_retries=2, log=lambda *_: None)
        self.assertTrue(success)
        self.assertEqual(result, "immediate")

    def test_fail_then_succeed(self):
        attempts = 0

        def flaky():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError(f"Attempt {attempts} failed")
            return "ok"

        success, result = retry_with_backoff(flaky, max_retries=3, base_delay=0.01, log=lambda *_: None)
        self.assertTrue(success)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 3)


if __name__ == "__main__":
    unittest.main()
