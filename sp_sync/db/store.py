"""SQLite-backed application state (configs, cookies, OAuth token, logs)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Optional

from sp_sync.config.settings import database_file_path

KEY_SP_CONFIGS = "sharepoint_configs"
KEY_GDRIVE_CONFIGS = "gdrive_configs"
KEY_SP_COOKIES = "sharepoint_cookies"
KEY_GDRIVE_TOKEN = "gdrive_oauth_token"
KEY_GUEST_LINK = "sharepoint_guest_link"
KEY_SP_SITE_URL = "sharepoint_site_root_url"
KEY_SYNC_REPORT_SP = "sync_report_sharepoint"
KEY_SYNC_REPORT_GD = "sync_report_gdrive"
KEY_SYNC_REPORT_HISTORY = "sync_report_history"

_lock = threading.RLock()
_store_singleton: Optional["AppStore"] = None


def _default_db_path() -> str:
    return database_file_path()


class AppStore:
    """Thread-safe key-value + append-only logs in SQLite."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or _default_db_path()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS log_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    source TEXT NOT NULL,
                    msg TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_log_entries_ts ON log_entries(ts DESC)"
            )
            # Download ledger: every SharePoint item that was ever downloaded (or matched
            # to an existing local file).  Items listed here are never downloaded again,
            # even if the local file is renamed, moved, trimmed or deleted, unless the
            # user asks for a re-download (redownload_requested = 1).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloaded_files (
                    item_key TEXT PRIMARY KEY,
                    item_id TEXT,
                    rel_url TEXT,
                    original_name TEXT NOT NULL DEFAULT '',
                    size INTEGER,
                    modified TEXT,
                    local_dir TEXT,
                    local_path TEXT,
                    source TEXT NOT NULL DEFAULT 'download',
                    redownload_requested INTEGER NOT NULL DEFAULT 0,
                    downloaded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloaded_files_dir ON downloaded_files(local_dir)"
            )

    def get_text(self, key: str, default: str = "") -> str:
        with _lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            if not row:
                return default
            return row[0] if row[0] is not None else default

    def set_text(self, key: str, value: str) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        with _lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kv(key, value, updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, ts),
            )

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_text(key, "")
        if not raw.strip():
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_json(self, key: str, obj: Any) -> None:
        self.set_text(key, json.dumps(obj, ensure_ascii=False))

    def delete_key(self, key: str) -> None:
        with _lock, self._connect() as conn:
            conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    def append_log(self, ts: str, source: str, msg: str) -> None:
        with _lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO log_entries(ts, source, msg) VALUES (?,?,?)",
                (ts, source, msg),
            )
            conn.execute(
                """
                DELETE FROM log_entries WHERE id NOT IN (
                    SELECT id FROM log_entries ORDER BY id DESC LIMIT 2500
                )
                """
            )

    def recent_logs(self, limit: int = 500) -> list[dict]:
        with _lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, source, msg FROM log_entries
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out = [{"time": r["ts"], "source": r["source"], "msg": r["msg"]} for r in rows]
        out.reverse()
        return out

    def clear_logs(self, source: str | None = None) -> None:
        """Remove log rows; source=None or 'all' clears everything."""
        with _lock, self._connect() as conn:
            if source and source != "all":
                conn.execute("DELETE FROM log_entries WHERE source = ?", (source,))
            else:
                conn.execute("DELETE FROM log_entries")

    # --- Typed helpers ---

    def get_sharepoint_configs(self) -> list:
        v = self.get_json(KEY_SP_CONFIGS, None)
        return v if isinstance(v, list) else []

    def set_sharepoint_configs(self, configs: list) -> None:
        self.set_json(KEY_SP_CONFIGS, configs)

    def get_gdrive_configs(self) -> list:
        v = self.get_json(KEY_GDRIVE_CONFIGS, None)
        return v if isinstance(v, list) else []

    def set_gdrive_configs(self, configs: list) -> None:
        self.set_json(KEY_GDRIVE_CONFIGS, configs)

    def get_sharepoint_cookies(self) -> dict:
        v = self.get_json(KEY_SP_COOKIES, None)
        return v if isinstance(v, dict) else {}

    def set_sharepoint_cookies(self, fedauth: str, rtfa: str = "") -> None:
        self.set_json(KEY_SP_COOKIES, {"FedAuth": fedauth, "rtFa": rtfa or ""})

    def get_gdrive_token_json(self) -> Optional[str]:
        s = self.get_text(KEY_GDRIVE_TOKEN, "").strip()
        return s or None

    def set_gdrive_token_json(self, token_json: str) -> None:
        self.set_text(KEY_GDRIVE_TOKEN, token_json)

    def get_guest_link(self) -> str:
        return self.get_text(KEY_GUEST_LINK, "").strip()

    def get_sharepoint_site_url(self) -> str:
        """SharePoint site root (e.g. personal site URL), set from the web UI / explorer only."""
        return self.get_text(KEY_SP_SITE_URL, "").strip()

    def set_sharepoint_site_url(self, url: str) -> None:
        self.set_text(KEY_SP_SITE_URL, (url or "").strip())

    def get_sync_report(self, service: str) -> dict:
        """Get last sync report for a service ('sharepoint' or 'gdrive')."""
        svc = (service or "").strip().lower()
        key = KEY_SYNC_REPORT_SP if svc == "sharepoint" else KEY_SYNC_REPORT_GD
        v = self.get_json(key, None)
        return v if isinstance(v, dict) else {}

    def set_sync_report(self, service: str, report: dict) -> None:
        """Persist last sync report for a service ('sharepoint' or 'gdrive')."""
        svc = (service or "").strip().lower()
        key = KEY_SYNC_REPORT_SP if svc == "sharepoint" else KEY_SYNC_REPORT_GD
        self.set_json(key, report if isinstance(report, dict) else {})

    def append_sync_report_history(self, report: dict, max_items: int = 1000) -> None:
        """Append one sync report event to history (newest first, capped)."""
        if not isinstance(report, dict) or not report:
            return
        cur = self.get_json(KEY_SYNC_REPORT_HISTORY, None)
        items = cur if isinstance(cur, list) else []
        items.insert(0, report)
        cap = max(1, int(max_items))
        self.set_json(KEY_SYNC_REPORT_HISTORY, items[:cap])

    def get_sync_report_history(self, service: str = "all", limit: int = 20, query: str = "") -> list[dict]:
        """Get sync report history, optionally filtered by service and free-text query."""
        svc = (service or "all").strip().lower()
        lim = max(1, int(limit))
        q = (query or "").strip().lower()
        cur = self.get_json(KEY_SYNC_REPORT_HISTORY, None)
        items = cur if isinstance(cur, list) else []
        out: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if svc in ("sharepoint", "gdrive") and (it.get("service") or "").strip().lower() != svc:
                continue
            if q:
                hay = " ".join([
                    str(it.get("service") or ""),
                    str(it.get("status") or ""),
                    str(it.get("trigger") or ""),
                    str(it.get("started_at") or ""),
                    str(it.get("finished_at") or ""),
                    str(it.get("message") or ""),
                    " ".join(str(x) for x in (it.get("errors") or []) if x is not None),
                ]).lower()
                if q not in hay:
                    continue
            out.append(it)
            if len(out) >= lim:
                break
        return out

    def clear_sync_report_history(self, service: str = "all") -> None:
        """Clear all report history or only one service."""
        svc = (service or "all").strip().lower()
        if svc == "all":
            self.set_json(KEY_SYNC_REPORT_HISTORY, [])
            return
        cur = self.get_json(KEY_SYNC_REPORT_HISTORY, None)
        items = cur if isinstance(cur, list) else []
        kept = [
            it for it in items
            if isinstance(it, dict) and (it.get("service") or "").strip().lower() != svc
        ]
        self.set_json(KEY_SYNC_REPORT_HISTORY, kept)


    # ---------------- download ledger ----------------
    def ledger_get(self, item_key: str) -> Optional[dict]:
        if not item_key:
            return None
        with _lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM downloaded_files WHERE item_key = ?", (item_key,)
            ).fetchone()
        return dict(row) if row else None

    def ledger_record(
        self,
        *,
        item_id: Optional[str],
        rel_url: Optional[str],
        original_name: str,
        local_path: str,
        size: Optional[int] = None,
        modified: Optional[str] = None,
        source: str = "download",
    ) -> str:
        """Insert or update a ledger row (clears any pending re-download request)."""
        import os as _os

        key = ledger_key(item_id, rel_url)
        if not key:
            raise ValueError("ledger_record needs item_id or rel_url")
        local_dir = _os.path.dirname(_os.path.abspath(local_path)) if local_path else ""
        now = datetime.now().isoformat(timespec="seconds")
        with _lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO downloaded_files (item_key, item_id, rel_url, original_name, size,
                    modified, local_dir, local_path, source, redownload_requested, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    item_id = excluded.item_id, rel_url = excluded.rel_url,
                    original_name = excluded.original_name, size = excluded.size,
                    modified = excluded.modified, local_dir = excluded.local_dir,
                    local_path = excluded.local_path, source = excluded.source,
                    redownload_requested = 0, downloaded_at = excluded.downloaded_at
                """,
                (key, item_id or None, rel_url or None, original_name or "",
                 int(size) if size else None, modified, local_dir,
                 _os.path.abspath(local_path) if local_path else "", source, now),
            )
        return key

    def ledger_request_redownload(self, item_key: str) -> bool:
        with _lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE downloaded_files SET redownload_requested = 1 WHERE item_key = ?",
                (item_key,),
            )
        return cur.rowcount > 0

    def ledger_cancel_redownload(self, item_key: str) -> bool:
        with _lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE downloaded_files SET redownload_requested = 0 WHERE item_key = ?",
                (item_key,),
            )
        return cur.rowcount > 0

    def ledger_delete(self, item_key: str) -> bool:
        with _lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM downloaded_files WHERE item_key = ?", (item_key,))
        return cur.rowcount > 0

    def ledger_list(self) -> list[dict]:
        with _lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM downloaded_files ORDER BY local_dir, local_path"
            ).fetchall()
        return [dict(r) for r in rows]

    def ledger_paths(self) -> set:
        with _lock, self._connect() as conn:
            rows = conn.execute("SELECT local_path FROM downloaded_files").fetchall()
        return {r[0] for r in rows if r[0]}

    def ledger_names_in_dir(self, local_dir: str) -> list[str]:
        import os as _os

        d = _os.path.abspath(local_dir)
        with _lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT local_path FROM downloaded_files WHERE local_dir = ?", (d,)
            ).fetchall()
        return [_os.path.basename(r[0]) for r in rows if r[0]]


def ledger_key(item_id: Optional[str], rel_url: Optional[str]) -> str:
    """Stable ledger key: SharePoint item id, else server-relative URL."""
    if item_id and str(item_id).strip():
        return f"id:{str(item_id).strip()}"
    if rel_url and str(rel_url).strip():
        return f"url:{str(rel_url).strip()}"
    return ""


def get_store() -> AppStore:
    global _store_singleton
    with _lock:
        if _store_singleton is None:
            _store_singleton = AppStore()
        return _store_singleton
