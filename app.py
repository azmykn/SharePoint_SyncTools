"""Run the web UI via Waitress WSGI server (multi-threaded, production-ready) with Flask fallback.

Application state lives in SQLite under data/app.sqlite.

Copyright (c) 2026 Azmy KN <azmykn@gmail.com>
SPDX-License-Identifier: MIT
https://github.com/azmykn/SharePoint_SyncTools
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sp_sync.single_instance import acquire_app_lock, release_app_lock
from sp_sync.web.app import app, ensure_scheduler_started, hydrate_logs_from_db


def _serve(host: str, port: int) -> None:
    use_dev = os.environ.get("SP_SYNC_DEV_SERVER", "").strip().lower() in ("1", "true", "yes")
    if not use_dev:
        try:
            from waitress import serve as waitress_serve

            threads = int(os.environ.get("SP_SYNC_THREADS", "8"))
            print(f"  Server: Waitress WSGI ({threads} threads)")
            print("=" * 50)
            waitress_serve(app, host=host, port=port, threads=threads, _quiet=False)
            return
        except ImportError:
            print("  Server: Flask dev server (install 'waitress' for production WSGI)")
    print("=" * 50)
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    host = os.environ.get("SP_SYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("SP_SYNC_PORT", os.environ.get("PORT", "5000")))
    ok, msg = acquire_app_lock(host=host, port=port)
    if not ok:
        print(f"❌ {msg}")
        sys.exit(1)
    hydrate_logs_from_db()
    ensure_scheduler_started()
    print("=" * 50)
    print("  Unified Sync Tool")
    print("  SharePoint + Google Drive")
    print(f"  http://{host}:{port}")
    print(f"  {msg}")
    try:
        _serve(host, port)
    finally:
        release_app_lock()
