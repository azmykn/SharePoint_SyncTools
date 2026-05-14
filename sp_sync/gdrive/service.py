"""
Google Drive Sync Engine
========================
Downloads files from a shared Google Drive folder to a local directory.
Supports:
- Shared links (folders)
- OAuth2 authentication with token persistence
- Recursive folder download
- Skip existing files
"""

import os
import io
import re
import json

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from sp_sync.paths import project_root
from sp_sync.db.store import get_store

CREDENTIALS_FILE = os.path.join(project_root(), "credentials.json")

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# Google Drive MIME types
FOLDER_MIME = 'application/vnd.google-apps.folder'

# Google Docs export MIME types (these can't be downloaded directly)
GOOGLE_DOCS_EXPORT = {
    'application/vnd.google-apps.document': ('application/pdf', '.pdf'),
    'application/vnd.google-apps.spreadsheet': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx'),
    'application/vnd.google-apps.presentation': ('application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx'),
    'application/vnd.google-apps.drawing': ('image/png', '.png'),
}


def extract_folder_id(url_or_id):
    """Extract folder ID from a Google Drive URL or return as-is if already an ID."""
    if not url_or_id:
        return None
    
    # Pattern: https://drive.google.com/drive/folders/FOLDER_ID
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url_or_id)
    if match:
        return match.group(1)
    
    # Pattern: https://drive.google.com/open?id=FOLDER_ID
    match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url_or_id)
    if match:
        return match.group(1)
    
    # Assume it's already a folder ID
    if re.match(r'^[a-zA-Z0-9_-]+$', url_or_id):
        return url_or_id
    
    return None


def get_credentials(credentials_file=None):
    """Get valid credentials, refreshing or initiating OAuth2 flow as needed."""
    creds = None

    store = get_store()
    raw = store.get_gdrive_token_json()
    if raw:
        try:
            info = json.loads(raw)
            creds = Credentials.from_authorized_user_info(info, SCOPES)
        except Exception:
            creds = None

    # Refresh or get new credentials
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
        except Exception:
            creds = None

    return creds


def authenticate_google(credentials_file=None):
    """Run local server flow to authenticate with Google Drive."""
    cred_file = credentials_file or CREDENTIALS_FILE
    
    if not os.path.exists(cred_file):
        raise FileNotFoundError("credentials.json file not found. Please download it from Google Cloud Console.")
    
    flow = InstalledAppFlow.from_client_secrets_file(
        cred_file,
        scopes=SCOPES
    )
    
    # This opens the browser and handles the entire PKCE check locally
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    
    return creds


def _save_token(creds):
    """Persist OAuth token in SQLite."""
    get_store().set_gdrive_token_json(creds.to_json())


def is_authenticated():
    """Check if we have valid Google Drive credentials."""
    creds = get_credentials()
    return creds is not None and creds.valid


def get_user_info(creds):
    """Get current user's email from Google Drive."""
    try:
        service = build('drive', 'v3', credentials=creds)
        about = service.about().get(fields='user').execute()
        return about.get('user', {}).get('emailAddress', 'Unknown')
    except Exception:
        return None


def get_folder_metadata(creds, folder_id):
    """Get folder name and basic info from Google Drive."""
    try:
        service = build('drive', 'v3', credentials=creds)
        folder = service.files().get(fileId=folder_id, fields='name').execute()
        return folder
    except Exception as e:
        print(f"Error fetching folder metadata: {e}")
        return None


def list_folder_contents(creds, folder_id):
    """List files and folders in a Google Drive folder."""
    service = build('drive', 'v3', credentials=creds)
    
    results = []
    page_token = None
    
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            pageSize=100,
            pageToken=page_token
        ).execute()
        
        results.extend(response.get('files', []))
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    
    return results


def sync_gdrive_folder(creds, folder_id, local_dir, log_callback=None, depth=0):
    """
    Recursively download all files from a Google Drive folder.
    Returns the count of newly downloaded files.
    """
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)
    
    os.makedirs(local_dir, exist_ok=True)
    
    service = build('drive', 'v3', credentials=creds)
    
    # List all files in folder
    items = list_folder_contents(creds, folder_id)
    
    downloaded_count = 0
    indent = "  " * depth
    
    for item in items:
        name = item['name']
        mime_type = item['mimeType']
        file_id = item['id']
        
        if mime_type == FOLDER_MIME:
            # Recurse into subfolder
            sub_dir = os.path.join(local_dir, name)
            log(f"{indent}📁 {name}/")
            downloaded_count += sync_gdrive_folder(creds, file_id, sub_dir, log_callback, depth + 1)
            continue
        
        # Handle Google Docs exports
        if mime_type in GOOGLE_DOCS_EXPORT:
            export_mime, ext = GOOGLE_DOCS_EXPORT[mime_type]
            if not name.endswith(ext):
                name = name + ext
            local_path = os.path.join(local_dir, name)
            
            if os.path.exists(local_path):
                log(f"{indent}[-] Skipping '{name}' — file already exists.")
                continue
            
            log(f"{indent}[+] Exporting '{name}'...")
            try:
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                
                with open(local_path, 'wb') as f:
                    f.write(fh.getvalue())
                log(f"{indent}✅ Export finished for '{name}'")
                downloaded_count += 1
            except Exception as e:
                log(f"{indent}❌ Export failed for '{name}': {e}")
            continue
        
        # Regular file download
        local_path = os.path.join(local_dir, name)
        
        if os.path.exists(local_path):
            log(f"{indent}[-] Skipping '{name}' — file already exists.")
            continue
        
        size_mb = int(item.get('size', 0)) / (1024 * 1024)
        log(f"{indent}[+] Downloading '{name}' ({size_mb:.1f} MB)...")
        
        try:
            request = service.files().get_media(fileId=file_id)
            fh = io.FileIO(local_path, 'wb')
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.close()
            log(f"{indent}✅ Download finished for '{name}'")
            downloaded_count += 1
        except Exception as e:
            log(f"{indent}❌ Download failed for '{name}': {e}")
            # Clean up partial file
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except:
                    pass
    
    return downloaded_count


def start_gdrive_sync(folder_url, local_dir, log_callback=None):
    """Main entry point for Google Drive sync."""
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)
    
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        log("❌ Invalid Google Drive URL.")
        return 0
    
    creds = get_credentials()
    if not creds or not creds.valid:
        log("❌ Sign in to Google Drive first (run gdrive_login.py or use the web UI).")
        return 0
    
    log("==================================================")
    log("⏳ Syncing from Google Drive...")
    log(f"📂 Folder ID: {folder_id}")
    log(f"📍 Local path: {local_dir}")
    
    count = sync_gdrive_folder(creds, folder_id, local_dir, log_callback)
    
    log("==================================================")
    log(f"🎉 Sync finished. Downloaded {count} new file(s).")
    
    return count
