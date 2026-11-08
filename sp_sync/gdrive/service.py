"""
Google Drive Sync Engine
========================
Downloads files and folders from Google Drive to a local directory.
Supports:
- 1-click in-browser OAuth2 login (no CLI required)
- Manual authorization code / response URL fallback for remote/headless setups
- Shared links (My Drive and Shared Drives / Team Drives)
- Resolution of Google Drive Shortcuts
- Token persistence in SQLite
- Recursive folder download with progress reporting
- Export of Google Docs/Sheets/Slides to Office/PDF
- Skip existing files
"""

import os
import io
import re
import json
import logging
import threading
import webbrowser
import wsgiref.simple_server
from typing import Optional, Dict, Any, List

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, _RedirectWSGIApp, _WSGIRequestHandler
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from sp_sync.paths import project_root
from sp_sync.db.store import get_store, KEY_GDRIVE_TOKEN

_logger = logging.getLogger(__name__)

CREDENTIALS_FILE = os.path.join(project_root(), "credentials.json")
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

FOLDER_MIME = 'application/vnd.google-apps.folder'
SHORTCUT_MIME = 'application/vnd.google-apps.shortcut'

GOOGLE_DOCS_EXPORT = {
    'application/vnd.google-apps.document': (
        'application/pdf', '.pdf'
    ),
    'application/vnd.google-apps.spreadsheet': (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx'
    ),
    'application/vnd.google-apps.presentation': (
        'application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx'
    ),
    'application/vnd.google-apps.drawing': (
        'image/png', '.png'
    ),
}

# In-memory OAuth state tracker
_oauth_lock = threading.RLock()
_oauth_state: Dict[str, Any] = {
    "status": "idle",       # "idle", "pending", "success", "error"
    "auth_url": None,
    "user_email": None,
    "error": None,
    "flow": None,
    "server": None,
}


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
    return True


def extract_folder_id(url_or_id: Optional[str]) -> Optional[str]:
    """Extract folder or file ID from any Google Drive URL format or return raw ID."""
    if not url_or_id:
        return None
    url = str(url_or_id).strip()

    # Pattern: /folders/ID
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    # Pattern: id=ID
    match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    # Pattern: /file/d/ID or /d/ID
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)

    # Raw alphanumeric ID (typical Google Drive ID is 25-45 characters)
    clean = url.split('?')[0].split('#')[0].strip('/')
    if re.match(r'^[a-zA-Z0-9_-]{10,}$', clean):
        return clean

    return None


def get_credentials(credentials_file: Optional[str] = None) -> Optional[Credentials]:
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

    if creds and creds.expired and creds.refresh_token:
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


def is_authenticated() -> bool:
    """Check if valid Google Drive credentials exist."""
    creds = get_credentials()
    return creds is not None and creds.valid


def get_user_info(creds: Credentials) -> Optional[str]:
    """Get current user's email address from Google Drive."""
    try:
        service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        about = service.about().get(fields='user').execute()
        return about.get('user', {}).get('emailAddress', 'Unknown')
    except Exception as exc:
        _logger.warning("Failed to get Google user info: %s", exc)
        return None


def disconnect_gdrive() -> bool:
    """Disconnect Google Drive by clearing the saved token in SQLite."""
    store = get_store()
    store.delete_key(KEY_GDRIVE_TOKEN)
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
    """
    Start interactive OAuth2 login in background thread with local ephemeral redirect server.
    Returns dict with auth_url and status.
    """
    cred_file = credentials_file or CREDENTIALS_FILE
    if not os.path.exists(cred_file):
        raise FileNotFoundError(
            f"Google credentials.json not found at {cred_file}. Please download it from Google Cloud Console."
        )

    with _oauth_lock:
        # If an active server is already running, return its auth_url
        if _oauth_state["status"] == "pending" and _oauth_state.get("auth_url"):
            return {
                "success": True,
                "auth_url": _oauth_state["auth_url"],
                "status": "pending",
                "message": "Authentication already in progress.",
            }

        # Close any lingering server
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
        wsgiref.simple_server.WSGIServer.allow_reuse_address = False
        server = wsgiref.simple_server.make_server("localhost", 0, wsgi_app, handler_class=_WSGIRequestHandler)
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
            email = get_user_info(creds)

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
        # Re-create fresh flow
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
        email = get_user_info(creds)
        with _oauth_lock:
            _oauth_state["status"] = "success"
            _oauth_state["user_email"] = email
            _oauth_state["error"] = None

        return {"success": True, "user_email": email}
    except Exception as exc:
        _logger.exception("Manual token exchange failed: %s", exc)
        return {"success": False, "error": str(exc)}


def get_oauth_status() -> Dict[str, Any]:
    """Get the current Google Drive authentication status and flow state."""
    authenticated = is_authenticated()
    user_email = None

    if authenticated:
        creds = get_credentials()
        if creds:
            user_email = get_user_info(creds)

    with _oauth_lock:
        flow_status = _oauth_state["status"]
        auth_url = _oauth_state.get("auth_url")
        error = _oauth_state.get("error")
        if not user_email:
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
        service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        folder = service.files().get(
            fileId=folder_id,
            fields='id, name, mimeType',
            supportsAllDrives=True,
        ).execute()
        return folder
    except Exception as exc:
        _logger.warning("Error fetching folder metadata for %s: %s", folder_id, exc)
        return None


def list_folder_contents(creds: Credentials, folder_id: str) -> List[Dict[str, Any]]:
    """
    List files and subfolders under folder_id.
    Fully supports Shared Drives (Team Drives) and resolves shortcuts automatically.
    """
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    results: List[Dict[str, Any]] = []
    page_token = None

    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, shortcutDetails)",
            pageSize=100,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        raw_items = response.get('files', [])
        for item in raw_items:
            # Resolve shortcuts to their target
            if item.get('mimeType') == SHORTCUT_MIME:
                details = item.get('shortcutDetails') or {}
                target_id = details.get('targetId')
                target_mime = details.get('targetMimeType')
                if target_id:
                    item['id'] = target_id
                    item['mimeType'] = target_mime or item.get('mimeType')

            results.append(item)

        page_token = response.get('nextPageToken')
        if not page_token:
            break

    return results


def sync_gdrive_folder(
    creds: Credentials,
    folder_id: str,
    local_dir: str,
    log_callback=None,
    depth: int = 0,
) -> int:
    """
    Recursively download all files from a Google Drive folder to local_dir.
    Returns the count of newly downloaded files.
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    os.makedirs(local_dir, exist_ok=True)
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    items = list_folder_contents(creds, folder_id)

    downloaded_count = 0
    indent = "  " * depth

    for item in items:
        name = item.get('name') or "Untitled"
        mime_type = item.get('mimeType') or ""
        file_id = item.get('id')

        # Clean/sanitize filename for local filesystem
        safe_name = re.sub(r'[/\\:*?"<>|]', '_', name)

        if mime_type == FOLDER_MIME:
            sub_dir = os.path.join(local_dir, safe_name)
            log(f"{indent}📁 {safe_name}/")
            downloaded_count += sync_gdrive_folder(creds, file_id, sub_dir, log_callback, depth + 1)
            continue

        # Handle Google Docs / Sheets / Slides exports
        if mime_type in GOOGLE_DOCS_EXPORT:
            export_mime, ext = GOOGLE_DOCS_EXPORT[mime_type]
            if not safe_name.lower().endswith(ext):
                safe_name = safe_name + ext
            local_path = os.path.join(local_dir, safe_name)

            if os.path.exists(local_path):
                log(f"{indent}[-] Skipping '{safe_name}' (exists)")
                continue

            log(f"{indent}[+] Exporting '{safe_name}'...")
            try:
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

                with open(local_path, 'wb') as f:
                    f.write(fh.getvalue())
                log(f"{indent}✅ Exported '{safe_name}'")
                downloaded_count += 1
            except Exception as e:
                log(f"{indent}❌ Export failed for '{safe_name}': {e}")
            continue

        # Regular file download
        local_path = os.path.join(local_dir, safe_name)
        remote_size = int(item.get('size', 0) or 0)

        if os.path.exists(local_path):
            local_sz = os.path.getsize(local_path)
            if remote_size > 0 and local_sz == remote_size:
                log(f"{indent}[-] Skipping '{safe_name}' (exists, {local_sz/1024/1024:.1f} MB)")
                continue
            elif remote_size == 0 and local_sz > 0:
                log(f"{indent}[-] Skipping '{safe_name}' (exists)")
                continue

        sz_mb = remote_size / (1024 * 1024) if remote_size else 0
        sz_str = f" ({sz_mb:.1f} MB)" if sz_mb > 0 else ""
        log(f"{indent}[+] Downloading '{safe_name}'{sz_str}...")

        try:
            request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
            with io.FileIO(local_path, 'wb') as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
                done = False
                last_pct = 0
                while not done:
                    status, done = downloader.next_chunk()
                    if status and sz_mb > 50:
                        pct = int(status.progress() * 100)
                        if pct >= last_pct + 25:
                            log(f"{indent}  📊 Progress: {pct}%")
                            last_pct = pct

            final_sz = os.path.getsize(local_path) / (1024 * 1024)
            log(f"{indent}✅ Downloaded '{safe_name}' ({final_sz:.1f} MB)")
            downloaded_count += 1
        except Exception as e:
            log(f"{indent}❌ Download failed for '{safe_name}': {e}")
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass

    return downloaded_count


def start_gdrive_sync(folder_url: str, local_dir: str, log_callback=None) -> int:
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
        log("❌ Sign in to Google Drive first via the web dashboard.")
        return 0

    log("==================================================")
    log("⏳ Scanning and syncing from Google Drive...")
    log(f"📂 Folder ID: {folder_id}")
    log(f"📍 Local destination: {local_dir}")

    meta = get_folder_metadata(creds, folder_id)
    if meta:
        log(f"📁 Cloud Folder Name: {meta.get('name')}")

    count = sync_gdrive_folder(creds, folder_id, local_dir, log_callback)

    log("==================================================")
    log(f"🎉 Google Drive sync finished. Downloaded {count} new file(s).")
    return count
