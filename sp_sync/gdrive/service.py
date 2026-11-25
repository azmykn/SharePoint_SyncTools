"""
Google Drive Sync Engine
========================
Downloads files and folders from Google Drive to a local directory.
Supports:
- 1-click in-browser OAuth2 login (no CLI required)
- Manual authorization code / response URL fallback for remote/headless setups
- Shared links (My Drive and Shared Drives / Team Drives)
- Resolution of Google Drive Shortcuts with cycle detection
- Token persistence in SQLite (with cached user-info lookups)
- Recursive folder download with streaming exports, retries, cancellation, and dry-run
- Skip existing healthy files
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import random
import re
import shutil
import threading
import time
import webbrowser
import wsgiref.simple_server
from typing import Any, Dict, List, Optional, Set

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, _RedirectWSGIApp, _WSGIRequestHandler
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from sp_sync.db.store import KEY_GDRIVE_TOKEN, get_store
from sp_sync.paths import project_root
from sp_sync.sync_metrics import (
    complete_file_progress,
    start_file_progress,
    update_file_progress,
)

_logger = logging.getLogger(__name__)

CREDENTIALS_FILE = os.path.join(project_root(), "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

GOOGLE_DOCS_EXPORT = {
    "application/vnd.google-apps.document": (
        "application/pdf",
        ".pdf",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": (
        "image/png",
        ".png",
    ),
}

_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}

# In-memory OAuth state tracker + short-lived user info cache (avoids hammering Google API on polls)
_oauth_lock = threading.RLock()
_oauth_state: Dict[str, Any] = {
    "status": "idle",       # "idle", "pending", "success", "error"
    "auth_url": None,
    "user_email": None,
    "error": None,
    "flow": None,
    "server": None,
}
_user_info_cache: Dict[str, Any] = {
    "token_hash": None,
    "email": None,
    "fetched_at": 0.0,
}
_USER_INFO_TTL_SEC = 300.0


class _NoReuseWSGIServer(wsgiref.simple_server.WSGIServer):
    """Local subclass so we never mutate stdlib WSGIServer.allow_reuse_address globally."""

    allow_reuse_address = False


def _token_fingerprint(creds: Optional[Credentials]) -> str:
    if creds is None:
        return ""
    raw = (getattr(creds, "refresh_token", "") or "") + "|" + (getattr(creds, "token", "") or "")
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _invalidate_user_cache() -> None:
    with _oauth_lock:
        _user_info_cache["token_hash"] = None
        _user_info_cache["email"] = None
        _user_info_cache["fetched_at"] = 0.0


def _execute_with_retry(fn, *, retries: int = 3, base_delay: float = 0.8, cancel_event: Optional[threading.Event] = None):
    """Execute a Google Drive API callable with bounded exponential backoff on transient failures."""
    delay = base_delay
    last_exc = None
    for attempt in range(retries + 1):
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Google Drive sync cancelled by user")
        try:
            return fn()
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            last_exc = exc
            if status not in _RETRYABLE_HTTP_CODES or attempt >= retries:
                raise
        except (requests.RequestException, TimeoutError, ConnectionError, OSError) as exc:
            last_exc = exc
            if attempt >= retries:
                raise
        sleep_for = delay + random.uniform(0.0, 0.35)
        if cancel_event:
            if cancel_event.wait(sleep_for):
                raise InterruptedError("Google Drive sync cancelled by user")
        else:
            time.sleep(sleep_for)
        delay = min(delay * 2.0, 12.0)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Drive retry loop exhausted unexpectedly")


def _safe_local_name(name: Optional[str], fallback: str = "Untitled") -> str:
    """Sanitize a Drive filename/folder name and prevent path traversal or OS-illegal names."""
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]+', "_", str(name or "").strip())
    s = s.strip(" .")
    if not s or s in (".", ".."):
        return fallback
    if len(s) > 200:
        stem, ext = os.path.splitext(s)
        s = (stem[: 200 - len(ext)].rstrip(" .") or fallback) + ext
    return s


def _safe_child_path(base_dir: str, child_name: str) -> str:
    """Resolve child path under base_dir, guaranteeing it cannot escape base_dir."""
    base_abs = os.path.abspath(base_dir)
    cand = os.path.abspath(os.path.join(base_abs, _safe_local_name(child_name)))
    if os.path.commonpath([base_abs, cand]) != base_abs:
        raise ValueError(f"Unsafe path rejected: {child_name!r}")
    return cand


def has_credentials(credentials_file: Optional[str] = None) -> bool:
    """Check if credentials.json file exists on disk."""
    p = credentials_file or CREDENTIALS_FILE
    return os.path.isfile(p) and os.path.getsize(p) > 10


def save_credentials_content(content_str: str, credentials_file: Optional[str] = None) -> bool:
    """Save raw credentials.json content to disk after validating JSON structure."""
    data = json.loads(content_str)
    if "installed" not in data and "web" not in data:
        raise ValueError("Invalid client credentials JSON. Must contain 'installed' or 'web' key.")
    p = credentials_file or CREDENTIALS_FILE
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    if os.name == "posix":
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return True


def extract_folder_id(url_or_id: Optional[str]) -> Optional[str]:
    """Extract folder or file ID from any Google Drive URL format or return raw ID."""
    if not url_or_id:
        return None
    url = str(url_or_id).strip()

    match = re.search(r"folders/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)

    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)

    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)

    clean = url.split("?")[0].split("#")[0].strip("/")
    if re.match(r"^[a-zA-Z0-9_-]{10,}$", clean):
        return clean

    return None


def get_credentials(credentials_file: Optional[str] = None, *, refresh_if_expired: bool = True) -> Optional[Credentials]:
    """Get valid credentials from SQLite, refreshing if expired."""
    creds = None
    store = get_store()
    raw = store.get_gdrive_token_json()
    if raw:
        try:
            info = json.loads(raw)
            creds = Credentials.from_authorized_user_info(info, SCOPES)
        except Exception as exc:
            _logger.warning("Failed to parse saved Google Drive token: %s", exc)
            creds = None

    if refresh_if_expired and creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
        except Exception as exc:
            _logger.warning("Failed to refresh Google Drive token: %s", exc)
            creds = None

    return creds


def _save_token(creds: Credentials) -> None:
    """Persist OAuth token in SQLite database."""
    get_store().set_gdrive_token_json(creds.to_json())


def is_authenticated(*, allow_network_refresh: bool = True) -> bool:
    """Check if valid Google Drive credentials exist."""
    creds = get_credentials(refresh_if_expired=allow_network_refresh)
    if creds is None:
        return False
    if creds.valid:
        return True
    # When skipping live network refresh (e.g. fast UI render), presence of a refresh_token implies connected.
    return (not allow_network_refresh) and bool(creds.refresh_token)


def get_user_info(creds: Credentials, *, force_refresh: bool = False) -> Optional[str]:
    """Get current user's email address from Google Drive (cached for 5 minutes)."""
    if creds is None:
        return None
    fp = _token_fingerprint(creds)
    now = time.time()
    if not force_refresh:
        with _oauth_lock:
            if (
                _user_info_cache["token_hash"] == fp
                and _user_info_cache["email"]
                and (now - float(_user_info_cache["fetched_at"] or 0.0)) < _USER_INFO_TTL_SEC
            ):
                return _user_info_cache["email"]
    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        about = _execute_with_retry(lambda: service.about().get(fields="user").execute(), retries=1)
        email = about.get("user", {}).get("emailAddress", "Unknown")
        with _oauth_lock:
            _user_info_cache["token_hash"] = fp
            _user_info_cache["email"] = email
            _user_info_cache["fetched_at"] = time.time()
        return email
    except Exception as exc:
        _logger.warning("Failed to get Google user info: %s", exc)
        with _oauth_lock:
            if _user_info_cache["token_hash"] == fp and _user_info_cache["email"]:
                return _user_info_cache["email"]
        return None


def get_cached_user_email() -> Optional[str]:
    """Return cached Google email without making any network call."""
    with _oauth_lock:
        return _user_info_cache.get("email") or _oauth_state.get("user_email")


def disconnect_gdrive() -> bool:
    """Disconnect Google Drive by clearing the saved token in SQLite."""
    store = get_store()
    store.delete_key(KEY_GDRIVE_TOKEN)
    _invalidate_user_cache()
    with _oauth_lock:
        server = _oauth_state.get("server")
        if server:
            try:
                server.server_close()
            except Exception:
                pass
        _oauth_state["status"] = "idle"
        _oauth_state["auth_url"] = None
        _oauth_state["user_email"] = None
        _oauth_state["error"] = None
        _oauth_state["flow"] = None
        _oauth_state["server"] = None
    return True


def start_oauth_flow(
    credentials_file: Optional[str] = None,
    open_browser: bool = True,
    timeout_seconds: int = 240,
) -> Dict[str, Any]:
    """Start interactive OAuth2 login in background thread with local ephemeral redirect server."""
    cred_file = credentials_file or CREDENTIALS_FILE
    if not os.path.exists(cred_file):
        raise FileNotFoundError(
            f"Google credentials.json not found at {cred_file}. Please download it from Google Cloud Console."
        )

    with _oauth_lock:
        if _oauth_state["status"] == "pending" and _oauth_state.get("auth_url"):
            return {
                "success": True,
                "auth_url": _oauth_state["auth_url"],
                "status": "pending",
                "message": "Authentication already in progress.",
            }

        old_server = _oauth_state.get("server")
        if old_server:
            try:
                old_server.server_close()
            except Exception:
                pass

        flow = InstalledAppFlow.from_client_secrets_file(
            cred_file,
            scopes=SCOPES,
        )

        success_html = """
        <html>
        <head><meta charset="utf-8"><title>Authentication Complete</title></head>
        <body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;background:#0f172a;color:#f8fafc;margin:0;">
          <div style="text-align:center;padding:40px;background:#1e293b;border-radius:16px;box-shadow:0 10px 40px rgba(0,0,0,0.5);">
            <div style="font-size:3rem;margin-bottom:16px;">✅</div>
            <h1 style="margin:0 0 8px 0;font-size:1.5rem;">Google Drive Connected!</h1>
            <p style="color:#94a3b8;margin:0;">You can now close this tab and return to the Unified Sync Tool.</p>
          </div>
        </body>
        </html>
        """
        wsgi_app = _RedirectWSGIApp(success_html)
        server = wsgiref.simple_server.make_server(
            "localhost",
            0,
            wsgi_app,
            server_class=_NoReuseWSGIServer,
            handler_class=_WSGIRequestHandler,
        )
        port = server.server_port
        flow.redirect_uri = f"http://localhost:{port}/"

        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

        _oauth_state["status"] = "pending"
        _oauth_state["auth_url"] = auth_url
        _oauth_state["user_email"] = None
        _oauth_state["error"] = None
        _oauth_state["flow"] = flow
        _oauth_state["server"] = server

    def _worker():
        try:
            server.timeout = timeout_seconds
            server.handle_request()

            auth_response = getattr(wsgi_app, "last_request_uri", None)
            if not auth_response:
                raise TimeoutError("Timed out waiting for authentication response.")

            auth_response = auth_response.replace("http://", "https://", 1)
            flow.fetch_token(authorization_response=auth_response)
            creds = flow.credentials
            _save_token(creds)
            email = get_user_info(creds, force_refresh=True)

            with _oauth_lock:
                _oauth_state["status"] = "success"
                _oauth_state["user_email"] = email
                _oauth_state["error"] = None
        except Exception as exc:
            _logger.exception("OAuth authentication worker error: %s", exc)
            with _oauth_lock:
                _oauth_state["status"] = "error"
                _oauth_state["error"] = str(exc)
        finally:
            try:
                server.server_close()
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True, name="GDriveOAuthWorker")
    t.start()

    if open_browser:
        try:
            webbrowser.open(auth_url, new=1, autoraise=True)
        except Exception as exc:
            _logger.warning("Could not automatically launch system browser: %s", exc)

    return {
        "success": True,
        "auth_url": auth_url,
        "status": "pending",
        "message": "Authentication initiated. Please authorize in your browser.",
    }


def complete_oauth_with_code(code_or_url: str) -> Dict[str, Any]:
    """Fallback: exchange an authorization code or redirect response URL manually."""
    raw = (code_or_url or "").strip()
    if not raw:
        return {"success": False, "error": "No code or URL provided."}

    with _oauth_lock:
        flow = _oauth_state.get("flow")

    if not flow:
        if not os.path.exists(CREDENTIALS_FILE):
            return {"success": False, "error": "credentials.json not found."}
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES)
        flow.redirect_uri = "http://localhost/"

    try:
        if raw.startswith("http://") or raw.startswith("https://"):
            resp = raw.replace("http://", "https://", 1)
            flow.fetch_token(authorization_response=resp)
        else:
            flow.fetch_token(code=raw)

        creds = flow.credentials
        _save_token(creds)
        email = get_user_info(creds, force_refresh=True)
        with _oauth_lock:
            _oauth_state["status"] = "success"
            _oauth_state["user_email"] = email
            _oauth_state["error"] = None

        return {"success": True, "user_email": email}
    except Exception as exc:
        _logger.exception("Manual token exchange failed: %s", exc)
        return {"success": False, "error": str(exc)}


def get_oauth_status(*, refresh_user: bool = False) -> Dict[str, Any]:
    """Get the current Google Drive authentication status without hammering Google API on polls."""
    authenticated = is_authenticated(allow_network_refresh=refresh_user)
    user_email = get_cached_user_email() if authenticated else None

    if authenticated and (refresh_user or not user_email):
        creds = get_credentials(refresh_if_expired=True)
        if creds and creds.valid:
            user_email = get_user_info(creds, force_refresh=refresh_user)
        elif not creds:
            authenticated = False

    with _oauth_lock:
        flow_status = _oauth_state["status"]
        auth_url = _oauth_state.get("auth_url")
        error = _oauth_state.get("error")
        if authenticated and not user_email:
            user_email = _oauth_state.get("user_email")

    return {
        "authenticated": authenticated,
        "user_email": user_email,
        "flow_status": flow_status,
        "auth_url": auth_url if flow_status == "pending" else None,
        "error": error,
        "has_credentials": has_credentials(),
    }


def authenticate_google(credentials_file: Optional[str] = None) -> Credentials:
    """CLI synchronous helper for local server authentication."""
    cred_file = credentials_file or CREDENTIALS_FILE
    if not os.path.exists(cred_file):
        raise FileNotFoundError(f"credentials.json not found at {cred_file}")

    flow = InstalledAppFlow.from_client_secrets_file(cred_file, scopes=SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds)
    return creds


# ============================================================
# Google Drive API Operations (with Shared Drive & Shortcut Support)
# ============================================================

def get_folder_metadata(creds: Credentials, folder_id: str) -> Optional[Dict[str, Any]]:
    """Get folder metadata from Google Drive (supports regular and Shared Drives)."""
    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        folder = _execute_with_retry(
            lambda: service.files()
            .get(fileId=folder_id, fields="id, name, mimeType", supportsAllDrives=True)
            .execute(),
            retries=2,
        )
        return folder
    except Exception as exc:
        _logger.warning("Error fetching folder metadata for %s: %s", folder_id, exc)
        return None


def list_folder_contents(
    creds: Credentials,
    folder_id: str,
    *,
    service=None,
    cancel_event: Optional[threading.Event] = None,
) -> List[Dict[str, Any]]:
    """List files and subfolders under folder_id with Shared Drive + shortcut resolution."""
    drv = service or build("drive", "v3", credentials=creds, cache_discovery=False)
    results: List[Dict[str, Any]] = []
    page_token = None

    while True:
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Google Drive sync cancelled by user")
        tok = page_token
        response = _execute_with_retry(
            lambda: drv.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, shortcutDetails)",
                pageSize=100,
                pageToken=tok,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute(),
            retries=3,
            cancel_event=cancel_event,
        )

        raw_items = response.get("files", [])
        for item in raw_items:
            if item.get("mimeType") == SHORTCUT_MIME:
                details = item.get("shortcutDetails") or {}
                target_id = details.get("targetId")
                target_mime = details.get("targetMimeType")
                if target_id:
                    item["id"] = target_id
                    item["mimeType"] = target_mime or item.get("mimeType")
            results.append(item)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


def download_public_gdrive_file(
    file_id: str,
    local_path: str,
    log=print,
    *,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    """Download a publicly shared Google Drive file without buffering binary payloads in RAM."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    part_path = local_path + ".part"
    res = None

    try:
        res = session.get(url, stream=True, timeout=30)
        confirm_token = None
        for k, v in res.cookies.items():
            if k.startswith("download_warning"):
                confirm_token = v
                break

        ct = (res.headers.get("Content-Type") or "").lower()
        # Only inspect HTML body for confirm tokens — NEVER call res.text on binary streams!
        if not confirm_token and "text/html" in ct:
            snippet_bytes = b""
            for chunk in res.iter_content(chunk_size=16384):
                snippet_bytes += chunk
                if len(snippet_bytes) >= 262144:
                    break
            res.close()
            html_head = snippet_bytes.decode("utf-8", errors="ignore")
            match = re.search(r"confirm=([0-9A-Za-z_-]+)", html_head)
            if match:
                confirm_token = match.group(1)
            else:
                log("    ❌ Public download returned an HTML page (file may be private or require sign-in)")
                return False

        if confirm_token:
            res.close()
            confirm_url = f"https://drive.google.com/uc?export=download&confirm={confirm_token}&id={file_id}"
            res = session.get(confirm_url, stream=True, timeout=30)
            ct = (res.headers.get("Content-Type") or "").lower()
            if "text/html" in ct:
                log("    ❌ Public download still returned HTML after confirm token")
                return False

        if res.status_code in (200, 206):
            os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".", exist_ok=True)
            raw_cl = res.headers.get("Content-Length") or "0"
            try:
                tot = int(raw_cl)
            except (TypeError, ValueError):
                tot = 0
            dl = 0
            start_file_progress(
                os.path.basename(os.path.dirname(local_path)),
                os.path.basename(local_path),
                tot,
                service="gdrive",
            )
            with open(part_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=65536):
                    if cancel_event and cancel_event.is_set():
                        raise InterruptedError("Google Drive sync cancelled by user")
                    if not chunk:
                        continue
                    f.write(chunk)
                    dl += len(chunk)
                    update_file_progress(dl, tot, service="gdrive")
            if tot > 0 and dl < tot:
                raise IOError(f"Truncated public download: got {dl} of {tot} bytes")
            if dl == 0:
                raise IOError("Public download produced empty file")
            os.replace(part_path, local_path)
            complete_file_progress(os.path.basename(local_path), service="gdrive")
            return True
    except InterruptedError:
        log("    🛑 Public Google Drive download cancelled.")
        raise
    except Exception as exc:
        log(f"    ❌ Public download failed: {exc}")
    finally:
        if res is not None:
            try:
                res.close()
            except Exception:
                pass
        session.close()
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
    return False


def sync_gdrive_folder(
    creds: Optional[Credentials],
    folder_id: str,
    local_dir: str,
    log_callback=None,
    depth: int = 0,
    *,
    visited_folders: Optional[Set[str]] = None,
    cancel_event: Optional[threading.Event] = None,
    dry_run: bool = False,
    stats: Optional[Dict[str, Any]] = None,
    _service=None,
) -> int:
    """Recursively download all files from a Google Drive folder to local_dir.

    Returns the count of newly downloaded files (or planned files in dry-run).
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    if cancel_event and cancel_event.is_set():
        if stats is not None:
            stats["cancelled"] = True
        return 0

    visited = visited_folders if visited_folders is not None else set()
    if folder_id in visited:
        log(f"{'  ' * depth}⚠️ Skipping cyclic Google Drive folder shortcut ({folder_id})")
        return 0
    visited.add(folder_id)

    if not dry_run:
        os.makedirs(local_dir, exist_ok=True)
        try:
            _, _, free = shutil.disk_usage(local_dir)
            if free < 2 * 1024 * 1024 * 1024:
                log(f"⚠️ Warning: Low disk space! Only {free / (1024**3):.1f} GB remaining on target drive.")
        except Exception:
            pass

    if not creds:
        if dry_run:
            log("❌ Cannot preview private folder without Google sign-in.")
            return 0
        target_path = os.path.join(local_dir, f"drive_file_{_safe_local_name(folder_id)}")
        log(f"ℹ️ Trying public Google Drive download for ID {folder_id}...")
        if download_public_gdrive_file(folder_id, target_path, log, cancel_event=cancel_event):
            log(f"✅ Downloaded public Google Drive file: {os.path.basename(target_path)}")
            if stats is not None:
                stats["downloaded"] = int(stats.get("downloaded", 0)) + 1
                stats.setdefault("new_files", []).append(os.path.basename(target_path))
            return 1
        log("❌ Cannot list private folder without Google sign-in. Sign in via web dashboard.")
        return 0

    service = _service or build("drive", "v3", credentials=creds, cache_discovery=False)
    items = list_folder_contents(creds, folder_id, service=service, cancel_event=cancel_event)

    downloaded_count = 0
    indent = "  " * depth

    for item in items:
        if cancel_event and cancel_event.is_set():
            log(f"{indent}🛑 Google Drive sync cancelled.")
            if stats is not None:
                stats["cancelled"] = True
            break

        name = item.get("name") or "Untitled"
        mime_type = item.get("mimeType") or ""
        file_id = item.get("id")
        if not file_id:
            continue

        safe_name = _safe_local_name(name)

        if mime_type == FOLDER_MIME:
            sub_dir = _safe_child_path(local_dir, safe_name)
            log(f"{indent}📁 {safe_name}/")
            downloaded_count += sync_gdrive_folder(
                creds,
                file_id,
                sub_dir,
                log_callback,
                depth + 1,
                visited_folders=visited,
                cancel_event=cancel_event,
                dry_run=dry_run,
                stats=stats,
                _service=service,
            )
            continue

        # Handle Google Docs / Sheets / Slides exports
        if mime_type in GOOGLE_DOCS_EXPORT:
            export_mime, ext = GOOGLE_DOCS_EXPORT[mime_type]
            if not safe_name.lower().endswith(ext):
                safe_name = safe_name + ext
            local_path = _safe_child_path(local_dir, safe_name)

            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                log(f"{indent}[-] Skipping '{safe_name}' (exists)")
                if stats is not None:
                    stats["skipped"] = int(stats.get("skipped", 0)) + 1
                continue

            if dry_run:
                log(f"{indent}[DRY-RUN] Would export '{safe_name}'")
                if stats is not None:
                    stats.setdefault("planned_files", []).append({"name": safe_name, "local_path": local_path, "size": 0})
                downloaded_count += 1
                continue

            log(f"{indent}[+] Exporting '{safe_name}'...")
            part_path = local_path + ".part"
            start_file_progress(os.path.basename(local_dir), safe_name, 0, service="gdrive")
            try:
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
                with io.FileIO(part_path, "wb") as fh:
                    downloader = MediaIoBaseDownload(fh, request, chunksize=4 * 1024 * 1024)
                    done = False
                    while not done:
                        if cancel_event and cancel_event.is_set():
                            raise InterruptedError("Google Drive sync cancelled by user")
                        status, done = _execute_with_retry(
                            downloader.next_chunk, retries=3, cancel_event=cancel_event
                        )
                        if status:
                            update_file_progress(int(status.resumable_progress or 0), int(status.total_size or 0), service="gdrive")

                if os.path.getsize(part_path) == 0:
                    raise IOError("Exported file is empty")
                os.replace(part_path, local_path)
                complete_file_progress(safe_name, service="gdrive")
                log(f"{indent}✅ Exported '{safe_name}'")
                downloaded_count += 1
                if stats is not None:
                    stats["downloaded"] = int(stats.get("downloaded", 0)) + 1
                    stats.setdefault("new_files", []).append(safe_name)
            except InterruptedError:
                if stats is not None:
                    stats["cancelled"] = True
                if os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
                break
            except Exception as e:
                log(f"{indent}❌ Export failed for '{safe_name}': {e}")
                if stats is not None:
                    stats["failed"] = int(stats.get("failed", 0)) + 1
                    stats.setdefault("errors", []).append(f"{safe_name}: {e}")
                if os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
            continue

        # Regular file download
        local_path = _safe_child_path(local_dir, safe_name)
        try:
            remote_size = int(item.get("size", 0) or 0)
        except (TypeError, ValueError):
            remote_size = 0

        if os.path.exists(local_path):
            local_sz = os.path.getsize(local_path)
            if remote_size > 0 and local_sz == remote_size:
                log(f"{indent}[-] Skipping '{safe_name}' (exists, {local_sz/1024/1024:.1f} MB)")
                if stats is not None:
                    stats["skipped"] = int(stats.get("skipped", 0)) + 1
                continue
            elif remote_size == 0 and local_sz > 0:
                log(f"{indent}[-] Skipping '{safe_name}' (exists)")
                if stats is not None:
                    stats["skipped"] = int(stats.get("skipped", 0)) + 1
                continue

        sz_mb = remote_size / (1024 * 1024) if remote_size else 0
        if dry_run:
            log(f"{indent}[DRY-RUN] Would download '{safe_name}' ({sz_mb:.1f} MB)")
            if stats is not None:
                stats.setdefault("planned_files", []).append({"name": safe_name, "local_path": local_path, "size": remote_size})
            downloaded_count += 1
            continue

        sz_str = f" ({sz_mb:.1f} MB)" if sz_mb > 0 else ""
        log(f"{indent}[+] Downloading '{safe_name}'{sz_str}...")

        part_path = local_path + ".part"
        start_file_progress(os.path.basename(local_dir), safe_name, remote_size, service="gdrive")
        try:
            request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
            with io.FileIO(part_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
                done = False
                last_pct = 0
                while not done:
                    if cancel_event and cancel_event.is_set():
                        raise InterruptedError("Google Drive sync cancelled by user")
                    status, done = _execute_with_retry(
                        downloader.next_chunk, retries=3, cancel_event=cancel_event
                    )
                    if status:
                        cur_bytes = int(status.resumable_progress or 0)
                        if cur_bytes == 0 and remote_size:
                            cur_bytes = int(status.progress() * remote_size)
                        update_file_progress(cur_bytes, remote_size, service="gdrive")
                        if sz_mb > 50:
                            pct = int(status.progress() * 100)
                            if pct >= last_pct + 25:
                                log(f"{indent}  📊 Progress: {pct}%")
                                last_pct = pct

            part_size = os.path.getsize(part_path)
            if remote_size > 0 and part_size != remote_size:
                raise IOError(f"Size mismatch: downloaded {part_size} bytes, expected {remote_size} bytes")
            os.replace(part_path, local_path)
            complete_file_progress(safe_name, service="gdrive")
            final_sz = os.path.getsize(local_path) / (1024 * 1024)
            log(f"{indent}✅ Downloaded '{safe_name}' ({final_sz:.1f} MB)")
            downloaded_count += 1
            if stats is not None:
                stats["downloaded"] = int(stats.get("downloaded", 0)) + 1
                stats.setdefault("new_files", []).append(safe_name)
        except InterruptedError:
            if stats is not None:
                stats["cancelled"] = True
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except Exception:
                    pass
            break
        except Exception as e:
            log(f"{indent}❌ Download failed for '{safe_name}': {e}")
            if stats is not None:
                stats["failed"] = int(stats.get("failed", 0)) + 1
                stats.setdefault("errors", []).append(f"{safe_name}: {e}")
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except Exception:
                    pass

    return downloaded_count


def start_gdrive_sync(
    folder_url: str,
    local_dir: str,
    log_callback=None,
    *,
    cancel_event: Optional[threading.Event] = None,
    dry_run: bool = False,
    stats: Optional[Dict[str, Any]] = None,
) -> int:
    """Main entry point to sync a Google Drive folder by URL or ID."""
    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        log("❌ Invalid Google Drive URL or folder ID.")
        return 0

    creds = get_credentials()
    if not creds or not creds.valid:
        if dry_run:
            log("❌ Sign in to Google Drive first to preview folders.")
            return 0
        target_path = os.path.join(local_dir, f"drive_file_{_safe_local_name(folder_id)}")
        if download_public_gdrive_file(folder_id, target_path, log, cancel_event=cancel_event):
            log(f"🎉 Downloaded public Google Drive file: {os.path.basename(target_path)}")
            return 1
        log("❌ Sign in to Google Drive first via the web dashboard (or provide a public file link).")
        return 0

    log("==================================================")
    log("🔍 Dry-run scanning Google Drive..." if dry_run else "⏳ Scanning and syncing from Google Drive...")
    log(f"📂 Folder ID: {folder_id}")
    log(f"📍 Local destination: {local_dir}")

    meta = get_folder_metadata(creds, folder_id)
    if meta:
        log(f"📁 Cloud Folder Name: {meta.get('name')}")

    count = sync_gdrive_folder(
        creds,
        folder_id,
        local_dir,
        log_callback,
        cancel_event=cancel_event,
        dry_run=dry_run,
        stats=stats,
    )

    log("==================================================")
    if dry_run:
        log(f"🔍 Google Drive dry-run finished. {count} file(s) would be downloaded.")
    else:
        log(f"🎉 Google Drive sync finished. Downloaded {count} new file(s).")
    return count
