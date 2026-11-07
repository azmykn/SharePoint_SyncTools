"""
Cookie Storage and Validation Tests
====================================
Tests SQLite cookie persistence and validation rules.
"""

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.db.store import AppStore
from sp_sync.security.cookies import CookieValidator


class TestCookiePersistenceAndValidation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_cookies.sqlite")
        os.environ["SP_SYNC_DB_PATH"] = self.db_path
        from sp_sync.config.settings import clear_settings_cache
        clear_settings_cache()
        import sp_sync.db.store as store_mod
        store_mod._store_singleton = None
        self.store = store_mod.get_store()

    def tearDown(self):
        import sp_sync.db.store as store_mod
        store_mod._store_singleton = None
        os.environ.pop("SP_SYNC_DB_PATH", None)
        from sp_sync.config.settings import clear_settings_cache
        clear_settings_cache()
        self.tmp_dir.cleanup()

    def test_cookie_save_and_read_roundtrip(self):
        fedauth = "A" * 60
        rtfa = "B" * 25
        self.store.set_sharepoint_cookies(fedauth, rtfa)
        cookies = self.store.get_sharepoint_cookies()
        self.assertEqual(cookies.get("FedAuth"), fedauth)
        self.assertEqual(cookies.get("rtFa"), rtfa)

    def test_cookie_validator_rules(self):
        validator = CookieValidator()

        # Both missing
        res = validator.validate_cookies("", "")
        self.assertFalse(res["valid"])
        self.assertIn("FedAuth cookie is missing", res["issues"])
        self.assertIn("rtFa cookie is missing", res["issues"])

        # Too short
        res_short = validator.validate_cookies("short", "short")
        self.assertFalse(res_short["valid"])
        self.assertIn("FedAuth cookie appears too short", res_short["issues"])

        # Valid cookies
        res_valid = validator.validate_cookies("x" * 60, "y" * 25)
        self.assertTrue(res_valid["valid"])
        self.assertEqual(len(res_valid["issues"]), 0)

    def test_cookie_connectivity_no_site_configured(self):
        """Ensure connectivity test does not leak credentials to third parties if no site is set."""
        validator = CookieValidator()
        res = validator.test_cookie_connectivity("x" * 60, "y" * 25, test_url=None)
        self.assertFalse(res["success"])
        self.assertIn("site URL", res["message"])


if __name__ == "__main__":
    unittest.main()
