"""Shared Web API helpers, logging, config management, ledger grouping, and system diagnostics."""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import jsonify, request

from sp_sync.paths import project_root

ROOT = project_root()

_log_lock = threading.RLock()
log_buffer: deque = deque(maxlen=500)


def _json_error(message: str, status: int = 400, *, code: Optional[str] = None, **extra):
    """Consistent JSON API error payload."""
    payload: Dict[str, Any] = {"success": False, "error": str(message)}
    if code:
        payload["code"] = code
    if extra:
        payload.update(extra)
    return jsonify(payload), status


def _request_json() -> dict:
    """Safe request JSON extractor (never returns None)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def add_log(source: str, message: Any) -> None:
    """Add a log entry with timestamp to both in-memory ring buffer and SQLite."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = message if isinstance(message, str) else str(message)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        lines = [text.strip() or "(empty)"]
    for ln in lines:
        row_id = 0
        try:
            from sp_sync.db.store import get_store

            row_id = get_store().append_log(ts, source, ln)
        except Exception:
            pass
        entry = {"id": row_id, "time": ts, "source": source, "msg": ln}
        with _log_lock:
            log_buffer.append(entry)


def hydrate_logs_from_db() -> None:
    """Load recent persisted logs into the in-memory ring buffer."""
    try:
        from sp_sync.db.store import get_store

        rows = get_store().recent_logs(500)
        with _log_lock:
            log_buffer.clear()
            for e in rows:
                log_buffer.append(e)
    except Exception:
        pass


def detect_sharepoint_site_root_from_url(url: str) -> str:
    """Derive SharePoint site root (https://host/personal/user or /sites/Site) from a full URL."""
    u = (url or "").strip()
    if not u.lower().startswith("http"):
        return ""
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return ""
    if "sharepoint.com" not in (p.netloc or "").lower():
        return ""
    parts = [seg for seg in (p.path or "").split("/") if seg]
    for i, seg in enumerate(parts):
        sl = seg.lower()
        if sl == "personal" and i + 1 < len(parts):
            return f"{p.scheme}://{p.netloc}/personal/{parts[i + 1]}".rstrip("/")
        if sl == "sites" and i + 1 < len(parts):
            return f"{p.scheme}://{p.netloc}/sites/{parts[i + 1]}".rstrip("/")
    return ""


def validate_sharepoint_site_url_for_save(url: str) -> Tuple[bool, str]:
    """Validate manual site root URL before saving to SQLite."""
    u = (url or "").strip().rstrip("/")
    if not u.lower().startswith("https://"):
        return False, "URL must start with https://"
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return False, "Invalid URL"
    if "sharepoint.com" not in (p.netloc or "").lower():
        return False, "Host must be a SharePoint Online host (*.sharepoint.com)."
    path = (p.path or "").lower()
    if "/personal/" not in path and "/sites/" not in path:
        return False, "Path must include /personal/... or /sites/..."
    return True, ""


def sp_site() -> str:
    """SharePoint site root URL saved via web UI / explorer (SQLite only)."""
    from sp_sync.db.store import get_store

    return (get_store().get_sharepoint_site_url() or "").strip().rstrip("/")


def _normalize_sp_rel_key(u: str) -> str:
    u = (u or "").strip()
    if not u.startswith("/"):
        u = "/" + u
    return u


def _sp_sync_enabled(config: dict) -> bool:
    return config.get("sync_enabled", True) is not False


def _gd_sync_enabled(config: dict) -> bool:
    return config.get("sync_enabled", True) is not False


def _normalize_sp_bulk_item(raw: dict) -> dict:
    rel = _normalize_sp_rel_key(raw.get("rel_url") or "")
    et = (raw.get("entry_type") or "folder").strip().lower()
    if et not in ("folder", "file"):
        et = "folder"
    name = (raw.get("name") or "").strip() or "SharePoint"
    local_path = (raw.get("local_path") or "").strip()
    out: Dict[str, Any] = {"name": name, "rel_url": rel, "local_path": local_path, "entry_type": et}
    if et == "folder" and "local_flat" in raw and raw.get("local_flat") is not None:
        out["local_flat"] = raw["local_flat"]
    share = (raw.get("sharing_link") or "").strip()
    if share.lower().startswith("http"):
        out["sharing_link"] = share
    root = (raw.get("sharing_root") or "").strip()
    if root.startswith("/"):
        out["sharing_root"] = root
    if "sync_enabled" in raw:
        out["sync_enabled"] = raw["sync_enabled"] is not False
    if "video_only" in raw:
        out["video_only"] = bool(raw["video_only"])
    return out


def load_sp_configs() -> list:
    from sp_sync.db.store import get_store

    return get_store().get_sharepoint_configs()


def validate_sp_config_row(raw: dict) -> Tuple[dict, dict]:
    from sp_sync.config.validator import ConfigValidator

    c = dict(raw)
    ConfigValidator.fix_common_issues(c, "sharepoint")
    res = ConfigValidator.validate_sharepoint_config(c)
    return c, res


def save_sp_configs(configs: list, *, strict: bool = False) -> list:
    """Persist SharePoint configs.

    Normalizes every row; when ``strict=False`` preserves existing rows instead of silently
    deleting folders whose external drive may temporarily be unmounted.
    """
    from sp_sync.config.validator import ConfigValidator
    from sp_sync.db.store import get_store

    saved = []
    for raw in configs:
        c = dict(raw)
        ConfigValidator.fix_common_issues(c, "sharepoint")
        if strict:
            v = ConfigValidator.validate_sharepoint_config(c)
            if not v["valid"]:
                raise ValueError("; ".join(v["errors"]))
        saved.append(c)
    get_store().set_sharepoint_configs(saved)
    return saved


def load_gdrive_configs() -> list:
    from sp_sync.db.store import get_store

    return get_store().get_gdrive_configs()


def validate_gdrive_config_row(raw: dict) -> Tuple[dict, dict]:
    from sp_sync.config.validator import ConfigValidator

    c = dict(raw)
    ConfigValidator.fix_common_issues(c, "gdrive")
    res = ConfigValidator.validate_gdrive_config(c)
    return c, res


def save_gdrive_configs(configs: list, *, strict: bool = False) -> list:
    from sp_sync.config.validator import ConfigValidator
    from sp_sync.db.store import get_store

    saved = []
    for raw in configs:
        c = dict(raw)
        ConfigValidator.fix_common_issues(c, "gdrive")
        if strict:
            v = ConfigValidator.validate_gdrive_config(c)
            if not v["valid"]:
                raise ValueError("; ".join(v["errors"]))
        saved.append(c)
    get_store().set_gdrive_configs(saved)
    return saved


def load_sp_cookies() -> Tuple[str, str]:
    from sp_sync.db.store import get_store

    c = get_store().get_sharepoint_cookies()
    return c.get("FedAuth") or "", c.get("rtFa") or ""


def save_sp_cookies(fedauth: str, rtfa: str = "") -> bool:
    from sp_sync.db.store import get_store

    get_store().set_sharepoint_cookies(fedauth, rtfa or "")
    add_log("system", "SharePoint cookies saved to local database (encrypted)")
    return True


def validate_sp_cookies(fedauth: str, rtfa: str) -> dict:
    try:
        from sp_sync.security.cookies import CookieValidator

        return CookieValidator().validate_cookies(fedauth, rtfa)
    except Exception as e:
        add_log("system", f"Error validating cookies: {e}")
        return {"valid": False, "issues": [f"Validation error: {e}"], "recommendations": ["Try again"]}


def _normalize_config_results(results: Optional[List[dict]], *, cap: int = 200) -> List[dict]:
    out: List[dict] = []
    for row in results or []:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "name": str(row.get("name") or "unknown"),
                "status": str(row.get("status") or "unknown"),
                "downloaded_files": int(row.get("downloaded_files") or 0),
                "error": str(row.get("error") or ""),
            }
        )
        if len(out) >= cap:
            break
    return out


def _prepare_sync_report(report: dict) -> dict:
    r = dict(report or {})
    elapsed = float(r.get("elapsed_sec") or 0.0)
    downloaded = int(r.get("downloaded_files") or 0)
    r["downloaded_files_per_sec"] = round((downloaded / elapsed), 4) if elapsed > 0 else 0.0

    processed = int(r.get("configs_processed") or 0)
    failed = int(r.get("configs_failed") or 0)
    skipped = int(r.get("configs_skipped") or 0)
    r["configs_ok"] = max(0, processed - failed)

    skip_reasons = r.get("skipped_reasons")
    if not isinstance(skip_reasons, dict):
        skip_reasons = {"disabled": skipped} if skipped else {}
    r["skipped_reasons"] = skip_reasons
    r["config_results"] = _normalize_config_results(r.get("config_results"))
    return r


def _save_sync_report(service: str, report: dict) -> None:
    try:
        from sp_sync.db.store import get_store

        store = get_store()
        prepared = _prepare_sync_report(report)
        store.set_sync_report(service, prepared)
        store.append_sync_report_history(prepared)
    except Exception as exc:
        add_log("system", f"⚠️ Failed to save sync report ({service}): {exc}")


def _ledger_groups(query: str = "", status_filter: str = "all") -> List[dict]:
    """Ledger rows grouped by subject folder, lectures in sequence order."""
    from sp_sync.db.store import get_store
    from sp_sync.sharepoint.ledger import resolve_ledger_path
    from sp_sync.sharepoint.naming import (
        lecture_datetime,
        parse_lecture_filename,
        subject_dir,
        subject_from_folder,
    )

    q = (query or "").strip().lower()
    st = (status_filter or "all").strip().lower()
    groups: Dict[str, dict] = {}
    for row in get_store().ledger_list():
        local_path = row.get("local_path") or ""
        folder = subject_dir(row.get("local_dir") or os.path.dirname(local_path))
        subject = subject_from_folder(folder)
        name = os.path.basename(local_path)
        parsed = parse_lecture_filename(name) or {}
        when = lecture_datetime(row.get("original_name") or "", row.get("modified"))
        found = resolve_ledger_path(local_path)
        exists = bool(found)
        redl = bool(row.get("redownload_requested"))

        if st == "exists" and not exists:
            continue
        if st == "missing" and exists:
            continue
        if st == "pending" and not redl:
            continue
        if q:
            hay = f"{subject} {name} {row.get('original_name') or ''} {folder}".lower()
            if q not in hay:
                continue

        size = row.get("size") or (os.path.getsize(found) if exists else 0)
        g = groups.setdefault(
            folder,
            {
                "folder": folder,
                "folder_name": os.path.basename(folder.rstrip("/\\")),
                "subject": subject,
                "items": [],
                "total_mb": 0.0,
            },
        )
        size_mb = round((size or 0) / (1024 * 1024), 1)
        g["total_mb"] = round(g["total_mb"] + size_mb, 1)
        g["items"].append(
            {
                "item_key": row.get("item_key"),
                "name": name,
                "original_name": row.get("original_name") or "",
                "seq": parsed.get("seq"),
                "date": parsed.get("date") or (when.strftime("%Y-%m-%d") if when else ""),
                "size_mb": size_mb,
                "local_exists": exists,
                "local_path": found or local_path,
                "redownload_requested": redl,
                "source": row.get("source"),
                "downloaded_at": row.get("downloaded_at"),
            }
        )
    out = []
    for folder in sorted(groups, key=lambda f: groups[f]["subject"]):
        g = groups[folder]
        g["items"].sort(key=lambda it: (it["seq"] is None, it["seq"] or 0, it["date"], it["name"]))
        out.append(g)
    return out


def _dir_size_and_counts(root_dir: str) -> Tuple[int, int, int]:
    total_bytes = 0
    file_count = 0
    dir_count = 0
    if not os.path.isdir(root_dir):
        return 0, 0, 0
    for cur, dirs, files in os.walk(root_dir):
        if cur != root_dir:
            dir_count += 1
        for fn in files:
            fp = os.path.join(cur, fn)
            try:
                total_bytes += os.path.getsize(fp)
                file_count += 1
            except OSError:
                pass
    return total_bytes, file_count, dir_count


def get_system_health() -> dict:
    """Return operational diagnostics (ffmpeg, ffprobe, chromium, disk, tmp_segments, DB, scheduler)."""
    from sp_sync.config.settings import database_file_path
    from sp_sync.media.ffmpeg_utils import _resolve_ffprobe_path, resolve_ffmpeg_path
    from sp_sync.scheduler import is_scheduler_daemon_alive
    from sp_sync.sharepoint.sync_engine import TMP_DIR, _find_system_chromium_executable

    ff = resolve_ffmpeg_path(log=lambda *_: None)
    probe = _resolve_ffprobe_path(ff) if ff else _resolve_ffprobe_path()
    chromium = _find_system_chromium_executable()
    playwright_ok = False
    try:
        import playwright  # noqa: F401

        playwright_ok = True
    except ImportError:
        playwright_ok = False

    db_path = database_file_path()
    db_size_kb = round(os.path.getsize(db_path) / 1024, 1) if os.path.isfile(db_path) else 0.0

    tmp_bytes, tmp_files, tmp_dirs = _dir_size_and_counts(TMP_DIR)
    disk_free_gb = 0.0
    disk_total_gb = 0.0
    try:
        tot, _used, free = shutil.disk_usage(os.path.dirname(db_path) or ROOT)
        disk_free_gb = round(free / (1024**3), 2)
        disk_total_gb = round(tot / (1024**3), 2)
    except OSError:
        pass

    return {
        "ffmpeg": {"available": bool(ff), "path": ff or ""},
        "ffprobe": {"available": bool(probe), "path": probe or ""},
        "playwright": {"installed": playwright_ok, "system_chromium": chromium or ""},
        "database": {"path": db_path, "size_kb": db_size_kb},
        "disk": {"free_gb": disk_free_gb, "total_gb": disk_total_gb, "low_space": disk_free_gb < 2.0},
        "tmp_segments": {
            "path": TMP_DIR,
            "size_mb": round(tmp_bytes / (1024 * 1024), 2),
            "files": tmp_files,
            "dirs": tmp_dirs,
        },
        "scheduler_daemon_alive": is_scheduler_daemon_alive(),
    }


def cleanup_tmp_segments(*, min_age_hours: float = 0.0, active_only_empty: bool = False) -> dict:
    """Safely remove stale temporary capture folders/files inside ``data/tmp_segments``."""
    from sp_sync.sharepoint.sync_engine import TMP_DIR

    removed_dirs = 0
    removed_files = 0
    freed_bytes = 0
    if not os.path.isdir(TMP_DIR):
        return {"removed_dirs": 0, "removed_files": 0, "freed_mb": 0.0}

    cutoff = time.time() - max(0.0, float(min_age_hours)) * 3600.0
    for entry in list(os.listdir(TMP_DIR)):
        full = os.path.join(TMP_DIR, entry)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if min_age_hours > 0 and st.st_mtime > cutoff:
            continue
        if os.path.isdir(full):
            if active_only_empty and os.listdir(full):
                continue
            b, fcnt, _dcnt = _dir_size_and_counts(full)
            try:
                shutil.rmtree(full)
                removed_dirs += 1
                removed_files += fcnt
                freed_bytes += b
            except OSError:
                pass
        elif os.path.isfile(full) and not active_only_empty:
            try:
                sz = os.path.getsize(full)
                os.remove(full)
                removed_files += 1
                freed_bytes += sz
            except OSError:
                pass

    return {
        "removed_dirs": removed_dirs,
        "removed_files": removed_files,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
    }


def is_allowed_open_folder_target(folder_path: str) -> Tuple[bool, str]:
    """Validate folder path for /api/open_local_folder (rejects files/executables & arbitrary mkdir)."""
    abs_p = os.path.abspath(os.path.expanduser(folder_path))
    if os.path.isfile(abs_p):
        return False, "Target path is a file, not a directory."
    if os.path.isdir(abs_p):
        return True, abs_p
    # Only auto-create if the path matches (or is directly inside) a configured sync folder
    configured_roots = []
    for cfg in load_sp_configs() + load_gdrive_configs():
        lp = (cfg.get("local_path") or "").strip()
        if lp:
            configured_roots.append(os.path.abspath(os.path.expanduser(lp)))
    for root in configured_roots:
        try:
            if abs_p == root or os.path.commonpath([root, abs_p]) == root:
                os.makedirs(abs_p, exist_ok=True)
                return True, abs_p
        except (ValueError, OSError):
            continue
    return False, f"Directory does not exist: {abs_p}"


def build_shell_context(active_page: str, page_title_key: str, **extra) -> Dict[str, Any]:
    """Build lightweight shared sidebar/topbar context for every rendered HTML page.

    Zero blocking network calls; never exposes raw cookie or OAuth secrets.
    """
    from sp_sync.db.store import get_store

    fedauth, rtfa = load_sp_cookies()
    sp_configs = load_sp_configs()
    gd_configs = load_gdrive_configs()

    gd_authenticated = False
    gd_user = None
    gd_has_credentials = False
    try:
        from sp_sync.gdrive import get_cached_user_email, has_credentials, is_authenticated

        gd_authenticated = is_authenticated(allow_network_refresh=False)
        gd_user = get_cached_user_email() if gd_authenticated else None
        gd_has_credentials = has_credentials()
    except Exception:
        pass

    sp_active_count = sum(1 for c in sp_configs if _sp_sync_enabled(c))
    gd_active_count = sum(1 for c in gd_configs if _gd_sync_enabled(c))

    ledger_count = 0
    try:
        ledger_count = len(get_store().ledger_list())
    except Exception:
        ledger_count = 0

    ctx: Dict[str, Any] = {
        "active_page": active_page,
        "page_title_key": page_title_key,
        "fedauth_len": len(fedauth),
        "rtfa_len": len(rtfa),
        "sp_auth": bool(fedauth and rtfa),
        "sp_total_count": len(sp_configs),
        "sp_active_count": sp_active_count,
        "gd_auth": gd_authenticated,
        "gd_user": gd_user,
        "gd_has_credentials": gd_has_credentials,
        "gd_total_count": len(gd_configs),
        "gd_active_count": gd_active_count,
        "ledger_count": ledger_count,
        "sp_site_configured": bool(sp_site().strip()),
        "sp_site_url_display": sp_site(),
    }
    ctx.update(extra)
    return ctx
