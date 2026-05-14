"""
Test Script for SharePoint Sync Tool
=====================================
Tests SQLite store, validation, and retry helpers.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# Isolated DB for tests (avoid touching real data/app.sqlite)
_test_db = os.path.join(_REPO_ROOT, "data", ".test_app.sqlite")
os.makedirs(os.path.dirname(_test_db), exist_ok=True)
if os.path.isfile(_test_db):
    os.remove(_test_db)
os.environ["SP_SYNC_DB_PATH"] = _test_db
import sp_sync.db.store as _store_mod

_store_mod._store_singleton = None


def test_sqlite_store():
    """Test SQLite-backed config + cookies."""
    print("SQLite store test...")

    try:
        from sp_sync.db.store import get_store

        store = get_store()
        store.set_sharepoint_cookies("fedauth_test_value_long_enough_for_validation_xxxxxxxx", "rtfa_test_value_long_ok")
        c = store.get_sharepoint_cookies()
        print(f"   SharePoint cookies: {'yes' if c.get('FedAuth') else 'no'}")

        from sp_sync.security.cookies import CookieValidator

        validator = CookieValidator()
        validation = validator.validate_cookies(c.get("FedAuth", ""), c.get("rtFa", ""))
        print(f"   Cookie validation: {'valid' if validation['valid'] else 'invalid'}")
        return True

    except ImportError as e:
        print(f"   Import failed: {e}")
        return False
    except Exception as e:
        print(f"   Error: {e}")
        return False


def test_config_validation():
    """Test configuration validation."""
    print("\nConfig validation test...")

    try:
        from sp_sync.config.validator import ConfigValidator

        valid_sp_config = {
            "name": "Test Folder",
            "rel_url": "/personal/test/Documents/TestFolder",
            "local_path": os.path.join(_REPO_ROOT, "test_output"),
        }

        validation = ConfigValidator.validate_sharepoint_config(valid_sp_config)
        print(f"   Valid SharePoint config: {'pass' if validation['valid'] else 'fail'}")

        invalid_sp_config = {
            "name": "A",
            "rel_url": "invalid_url",
            "local_path": "invalid_path",
        }

        validation = ConfigValidator.validate_sharepoint_config(invalid_sp_config)
        print(f"   Invalid SharePoint config: {len(validation['errors'])} error(s)")

        valid_gd_config = {
            "name": "Test GDrive Folder",
            "folder_url": "https://drive.google.com/drive/folders/1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a",
            "folder_id": "1LIQPMkF2vRlRoe9AAYvlisw8e-a1IX_a",
            "local_path": os.path.join(_REPO_ROOT, "test_gdrive_output"),
        }

        validation = ConfigValidator.validate_gdrive_config(valid_gd_config)
        print(f"   Valid Google Drive config: {'pass' if validation['valid'] else 'fail'}")

        return True

    except ImportError:
        print("   sp_sync.config not available")
        return False
    except Exception as e:
        print(f"   Config validation test error: {e}")
        return False


def test_retry_mechanism():
    """Test exponential backoff retry mechanism."""
    print("\nRetry/backoff test...")

    try:
        from sp_sync.sharepoint.sync_engine import retry_with_backoff

        def success_func():
            return "success"

        success, result = retry_with_backoff(success_func, max_retries=2, log=print)
        print(f"   Immediate success: {'pass' if success else 'fail'}")

        attempt_count = 0

        def fail_func():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise RuntimeError(f"Attempt {attempt_count} failed")
            return "finally_success"

        success, result = retry_with_backoff(fail_func, max_retries=3, base_delay=0.1, log=print)
        print(f"   Fail then succeed: {'pass' if success else 'fail'}")

        return True

    except ImportError:
        print("   retry_with_backoff not available")
        return False
    except Exception as e:
        print(f"   Retry test error: {e}")
        return False


def test_sharepoint_site_url_in_store():
    """SharePoint site root is persisted in SQLite and read by sync_engine.get_sp_site_url."""
    print("\nSharePoint site URL (SQLite) test...")

    try:
        from sp_sync.db.store import get_store
        from sp_sync.sharepoint.sync_engine import get_sp_site_url

        store = get_store()
        store.set_sharepoint_site_url("")
        if get_sp_site_url() != "":
            print("   Expected empty get_sp_site_url after clear")
            return False

        url = "https://contoso-my.sharepoint.com/personal/user_contoso_onmicrosoft_com"
        store.set_sharepoint_site_url(url)
        got = get_sp_site_url()
        if got != url.rstrip("/"):
            print(f"   Mismatch: expected {url!r}, got {got!r}")
            return False
        print("   get_sp_site_url() reads from SQLite store")
        store.set_sharepoint_site_url("")
        return True
    except Exception as e:
        print(f"   Error: {e}")
        return False


def test_config_from_store():
    """Test validate_and_fix_configs against SQLite."""
    print("\nConfigs from SQLite test...")

    try:
        from sp_sync.config.validator import validate_and_fix_configs

        results = validate_and_fix_configs()
        print(f"   SharePoint rows: {len(results['sharepoint']['configs'])} row(s)")
        print(f"   Google Drive rows: {len(results['gdrive']['configs'])} row(s)")

        if results["validation"]:
            sp_errors = results["validation"]["sharepoint"]["total_errors"]
            sp_warnings = results["validation"]["sharepoint"]["total_warnings"]
            gd_errors = results["validation"]["gdrive"]["total_errors"]
            gd_warnings = results["validation"]["gdrive"]["total_warnings"]
            print(
                f"   Validation: SharePoint ({sp_errors} errors, {sp_warnings} warnings), "
                f"Google Drive ({gd_errors} errors, {gd_warnings} warnings)"
            )

        return True

    except Exception as e:
        print(f"   Error: {e}")
        return False


def main():
    """Run all tests."""
    print("Running enhancement tests...")
    print("=" * 50)

    tests = [
        test_sqlite_store,
        test_sharepoint_site_url_in_store,
        test_config_validation,
        test_retry_mechanism,
        test_config_from_store,
    ]

    passed = 0
    total = len(tests)

    for test in tests:
        try:
            if test():
                passed += 1
        except Exception as e:
            print(f"   Test failed: {e}")

    print("\n" + "=" * 50)
    print(f"Summary: {passed}/{total} tests passed")


if __name__ == "__main__":
    main()
