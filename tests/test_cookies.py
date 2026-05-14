"""
Simple Cookie Test Script
========================
Tests SharePoint cookie persistence in SQLite.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

_test_db = os.path.join(_REPO_ROOT, "data", ".test_cookies.sqlite")
os.makedirs(os.path.dirname(_test_db), exist_ok=True)
if os.path.isfile(_test_db):
    os.remove(_test_db)
os.environ["SP_SYNC_DB_PATH"] = _test_db
import sp_sync.db.store as _store_mod

_store_mod._store_singleton = None


def test_cookies():
    """Test cookie load/save via AppStore."""
    print("Cookie test (SQLite)...")

    try:
        from sp_sync.db.store import get_store

        store = get_store()
        store.set_sharepoint_cookies("x" * 60, "y" * 25)
        c = store.get_sharepoint_cookies()
        if c.get("FedAuth") and len(c["FedAuth"]) >= 50:
            print(f"Save/read round-trip OK")
            print(f"   FedAuth: {len(c['FedAuth'])} chars")
            print(f"   rtFa: {len(c.get('rtFa', ''))} chars")
            return True
        print("Unexpected data after read")
    except Exception as e:
        print(f"Error: {e}")

    return False


if __name__ == "__main__":
    try:
        success = test_cookies()
        if success:
            print("\nCookie test passed.")
        else:
            print("\nCookie test failed.")

    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as e:
        print(f"\nUnexpected error: {e}")
