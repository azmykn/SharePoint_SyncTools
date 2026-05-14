"""Run the web UI. Application state lives in SQLite under data/app.sqlite.

Copyright (c) 2026 Azmy KN <azmykn@gmail.com>
SPDX-License-Identifier: MIT
git@github.com:azmykn/SharePoint_SyncTools.git
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sp_sync.web.app import app

if __name__ == "__main__":
    print("=" * 50)
    print("  Unified Sync Tool")
    print("  SharePoint + Google Drive")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=False)
