"""Run the web UI. Application state lives in SQLite under data/app.sqlite.

Copyright (c) 2026 Azmy KN <azmykn@gmail.com>
SPDX-License-Identifier: MIT
https://github.com/azmykn/SharePoint_SyncTools
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sp_sync.web.app import app
from sp_sync.single_instance import acquire_app_lock, release_app_lock

if __name__ == "__main__":
    host = os.environ.get("SP_SYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("SP_SYNC_PORT", os.environ.get("PORT", "5000")))
    ok, msg = acquire_app_lock(host=host, port=port)
    if not ok:
        print(f"❌ {msg}")
        sys.exit(1)
    print("=" * 50)
    print("  Unified Sync Tool")
    print("  SharePoint + Google Drive")
    print(f"  http://{host}:{port}")
    print(f"  {msg}")
    print("=" * 50)
    try:
        app.run(host=host, port=port, debug=False)
    finally:
        release_app_lock()
