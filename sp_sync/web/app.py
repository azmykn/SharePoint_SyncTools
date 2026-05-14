"""
Unified Sync Tool - Flask Web App
==================================
Web interface for SharePoint and Google Drive synchronization.
Runs locally on http://localhost:5000
"""

import os
import sys
import io
import re
import json
import threading
import time
import urllib.parse
from datetime import datetime
from collections import deque

# Force UTF-8
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response

from sp_sync.paths import project_root
from sp_sync.config.settings import sharepoint_site_url

ROOT = project_root()


def sp_site() -> str:
    """SharePoint personal/site root URL from config/app_settings.json or SP_SYNC_SITE_URL."""
    return (sharepoint_site_url() or "").strip()


app = Flask(
    __name__,
    template_folder=os.path.join(ROOT, "templates"),
    static_folder=os.path.join(ROOT, "static"),
    static_url_path="/static",
)
app.secret_key = os.urandom(24)

# ============================================================
# In-memory log buffer (thread-safe)
# ============================================================
log_buffer = deque(maxlen=500)
sync_status = {
    'sharepoint': {'running': False, 'last_run': None, 'last_result': ''},
    'gdrive': {'running': False, 'last_run': None, 'last_result': ''}
}

def add_log(source, message):
    """Add a log entry with timestamp (multi-line messages split for readable copy)."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    text = message if isinstance(message, str) else str(message)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        lines = [text.strip() or "(empty)"]
    for ln in lines:
        entry = {"time": ts, "source": source, "msg": ln}
        log_buffer.append(entry)
        try:
            from sp_sync.db.store import get_store
            get_store().append_log(ts, source, ln)
        except Exception:
            pass


def _hydrate_logs_from_db():
    try:
        from sp_sync.db.store import get_store
        for e in get_store().recent_logs(500):
            log_buffer.append(e)
    except Exception:
        pass


_hydrate_logs_from_db()

# ============================================================
# SharePoint site URL: config/app_settings.json → sharepoint_site_url
# ============================================================


def load_sp_configs():
    """Load SharePoint sync configurations from SQLite."""
    from sp_sync.db.store import get_store

    return get_store().get_sharepoint_configs()

def save_sp_configs(configs):
    """Save SharePoint sync configurations."""
    from sp_sync.config.validator import ConfigValidator
    from sp_sync.db.store import get_store

    validated_configs = []
    for raw in configs:
        c = dict(raw)
        ConfigValidator.fix_common_issues(c, "sharepoint")
        validated = ConfigValidator.validate_sharepoint_config(c)
        if validated["valid"]:
            validated_configs.append(c)
        else:
            add_log("system", f"Invalid SharePoint config skipped: {validated['errors']}")

    get_store().set_sharepoint_configs(validated_configs)
    return validated_configs

# ============================================================
# Cookie Management (SQLite)
# ============================================================
def load_sp_cookies():
    """Load SharePoint cookies from SQLite."""
    from sp_sync.db.store import get_store
    c = get_store().get_sharepoint_cookies()
    return c.get("FedAuth") or "", c.get("rtFa") or ""

def save_sp_cookies(fedauth, rtfa=""):
    """Save SharePoint cookies to SQLite."""
    from sp_sync.db.store import get_store
    get_store().set_sharepoint_cookies(fedauth, rtfa or "")
    add_log("system", "SharePoint cookies saved to local database")
    return True

def validate_sp_cookies(fedauth, rtfa):
    """Validate SharePoint cookies."""
    try:
        from sp_sync.security.cookies import CookieValidator
        validator = CookieValidator()
        result = validator.validate_cookies(fedauth, rtfa)
        return result
    except ImportError:
        if fedauth and rtfa and len(fedauth) > 50 and len(rtfa) > 20:
            return {'valid': True, 'issues': [], 'recommendations': ['Basic validation passed']}
        else:
            return {'valid': False, 'issues': ['Cookies appear invalid'], 'recommendations': ['Re-extract cookies']}
    except Exception as e:
        add_log('system', f'Error validating cookies: {e}')
        return {'valid': False, 'issues': [f'Validation error: {e}'], 'recommendations': ['Try again']}

# ============================================================
# Google Drive Config
# ============================================================
def load_gdrive_configs():
    """Load Google Drive sync configurations from SQLite."""
    from sp_sync.db.store import get_store
    return get_store().get_gdrive_configs()

def save_gdrive_configs(configs):
    """Save Google Drive sync configurations."""
    from sp_sync.config.validator import ConfigValidator
    from sp_sync.db.store import get_store

    validated_configs = []
    for raw in configs:
        c = dict(raw)
        ConfigValidator.fix_common_issues(c, "gdrive")
        validated = ConfigValidator.validate_gdrive_config(c)
        if validated["valid"]:
            validated_configs.append(c)
        else:
            add_log("system", f"Invalid Google Drive config skipped: {validated['errors']}")

    get_store().set_gdrive_configs(validated_configs)
    return validated_configs

# ============================================================
# Routes
# ============================================================
@app.route('/')
def index():
    fedauth, rtfa = load_sp_cookies()
    gdrive_configs = load_gdrive_configs()
    sp_configs = load_sp_configs()
    
    # Check Google Drive auth status
    gd_authenticated = False
    gd_user = None
    try:
        from sp_sync.gdrive import is_authenticated, get_user_info, get_credentials
        gd_authenticated = is_authenticated()
        if gd_authenticated:
            creds = get_credentials()
            gd_user = get_user_info(creds)
    except ImportError as e:
        add_log('system', f'Google Drive module not available: {e}')
    except Exception as e:
        add_log('system', f'Error checking Google Drive auth: {e}')
    
    return render_template('index.html',
        fedauth=fedauth,
        rtfa=rtfa,
        sp_auth=bool(fedauth),
        sp_folders=sp_configs,
        gd_folders=gdrive_configs,
        gd_auth=gd_authenticated,
        gd_user=gd_user,
        sync_status=sync_status,
        sp_site_configured=bool(sp_site().strip()),
    )


@app.route('/sp/save_cookies', methods=['POST'])
def sp_save_cookies():
    data = request.json
    fedauth = data.get('fedauth', '').strip()
    rtfa = data.get('rtfa', '').strip()
    
    if not fedauth:
        return jsonify({'success': False, 'error': 'FedAuth is required.'})
    
    save_sp_cookies(fedauth, rtfa)
    add_log('sharepoint', '✅ Connection data saved.')
    return jsonify({'success': True})


@app.route('/sp/auto_cookies', methods=['POST'])
def sp_auto_cookies():
    """Auto-extract SharePoint cookies from Chrome/Edge browser."""
    try:
        from sp_sync.sharepoint.cookie_extractor import extract_sp_cookies
        result = extract_sp_cookies()
        
        if result['fedauth']:
            save_sp_cookies(result['fedauth'], result['rtfa'])
            add_log('sharepoint', f"Cookies extracted from {result['source']}")
            return jsonify({'success': True, 'source': result['source']})
        else:
            return jsonify({'success': False, 'error': result['error']})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Error: {e}'})


@app.route('/sp/validate_cookies', methods=['POST'])
def sp_validate_cookies():
    """Validate SharePoint cookies."""
    try:
        data = request.get_json()
        fedauth = data.get('fedauth', '').strip()
        rtfa = data.get('rtfa', '').strip()
        
        if not fedauth or not rtfa:
            return jsonify({'success': False, 'error': 'Both cookies are required for validation.'})
        
        result = validate_sp_cookies(fedauth, rtfa)
        return jsonify({'success': True, 'validation': result})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Validation error: {e}'})


@app.route('/validate_configs', methods=['GET'])
def validate_configs():
    """Validate all configurations."""
    try:
        from sp_sync.config.validator import validate_and_fix_configs
        results = validate_and_fix_configs()
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Validation error: {e}'})


@app.route('/sp/sync', methods=['POST'])
def sp_sync():
    if sync_status['sharepoint']['running']:
        return jsonify({'success': False, 'error': 'A sync is already running.'})
    
    fedauth, rtfa = load_sp_cookies()
    if not fedauth:
        return jsonify({'success': False, 'error': 'Please save FedAuth first.'})
    if not sp_site():
        return jsonify({
            'success': False,
            'error': 'SharePoint site URL is not configured. Set sharepoint_site_url in config/app_settings.json or SP_SYNC_SITE_URL.',
        })

    sp_configs = load_sp_configs()
    def run_sync():
        sync_status['sharepoint']['running'] = True
        add_log('sharepoint', '--- SharePoint sync started ---')
        try:
            from sp_sync.sharepoint.sync_engine import start_sync_from_config
            total = 0
            for config in sp_configs:
                add_log('sharepoint', f"📂 Syncing: {config['name']}...")
                count = start_sync_from_config(
                    fedauth, rtfa, sp_site(), config,
                    log_callback=lambda msg: add_log('sharepoint', msg))
                total += count
            
            result = f"✅ Done. Downloaded {total} new file(s)."
            add_log('sharepoint', result)
            sync_status['sharepoint']['last_result'] = result
        except Exception as e:
            error_msg = f"❌ Error: {e}"
            add_log('sharepoint', error_msg)
            sync_status['sharepoint']['last_result'] = error_msg
        finally:
            sync_status['sharepoint']['running'] = False
            sync_status['sharepoint']['last_run'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    threading.Thread(target=run_sync, daemon=True).start()
    return jsonify({'success': True, 'message': 'Sync started in the background...'})


@app.route('/sp/browse', methods=['POST'])
def sp_browse():
    """List folders and files under a SharePoint parent path (server-relative URL)."""
    from sp_sync.sharepoint.sync_engine import prime_sharepoint_rest_session

    fedauth, rtfa = load_sp_cookies()
    if not fedauth or not rtfa:
        return jsonify({'success': False, 'error': 'Please save FedAuth and rtFa first.'}), 401
    if not sp_site():
        return jsonify({
            'success': False,
            'error': 'SharePoint site URL is not configured. Set sharepoint_site_url in config/app_settings.json or SP_SYNC_SITE_URL.',
        }), 400

    data = request.get_json(silent=True) or {}
    parent = (data.get('parent_rel_url') or '').strip()
    prime_url = (data.get('prime_url') or '').strip()
    if "?" in parent:
        parent = parent.split("?", 1)[0].strip()
    if not parent.startswith('/'):
        parent = '/' + parent

    import requests

    host = urllib.parse.urlparse(sp_site()).netloc
    base = sp_site().rstrip('/')
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json;odata=verbose',
    })
    session.cookies.set('FedAuth', fedauth, domain=host, path='/')
    session.cookies.set('rtFa', rtfa, domain=host, path='/')

    prime_sharepoint_rest_session(session, sp_site(), extra_get_url=prime_url or None)

    enc = urllib.parse.quote(parent)
    api_root = f"{base}/_api/web/GetFolderByServerRelativeUrl('{enc}')"
    api_headers = {'Accept': 'application/json;odata=verbose'}

    r_folders = session.get(api_root + '/Folders', headers=api_headers, timeout=60)
    if r_folders.status_code != 200:
        snippet = (r_folders.text or '')[:500].replace('\n', ' ')
        return jsonify({
            'success': False,
            'error': f'Folders HTTP {r_folders.status_code}',
            'parent': parent,
            'body_snippet': snippet,
        }), 400

    r_files = session.get(api_root + '/Files', headers=api_headers, timeout=60)
    if r_files.status_code != 200:
        snippet = (r_files.text or '')[:500].replace('\n', ' ')
        return jsonify({
            'success': False,
            'error': f'Files HTTP {r_files.status_code}',
            'parent': parent,
            'body_snippet': snippet,
        }), 400

    try:
        folders_j = r_folders.json()['d']['results']
        files_j = r_files.json()['d']['results']
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'success': False, 'error': f'Unexpected API response: {e}', 'parent': parent}), 400

    folders_out = []
    for fo in folders_j:
        fn = fo.get('Name') or ''
        if fn == 'Forms' or fn.startswith('_'):
            continue
        folders_out.append({'name': fn, 'rel_url': fo['ServerRelativeUrl']})
    files_out = []
    for fi in files_j:
        files_out.append({
            'name': fi['Name'],
            'rel_url': fi['ServerRelativeUrl'],
            'size': fi.get('Length', 0),
        })

    return jsonify({
        'success': True,
        'parent': parent,
        'folders': folders_out,
        'files': files_out,
    })


@app.route('/sp/explore')
def sp_explore_page():
    """Dedicated SharePoint explorer (paste sharing links or relative paths)."""
    fedauth, rtfa = load_sp_cookies()
    hint = ""
    try:
        for c in load_sp_configs() or []:
            if c.get('rel_url') and str(c.get('entry_type', 'folder')).lower() != 'file':
                hint = c['rel_url']
                break
    except Exception:
        pass
    return render_template(
        'sp_explore.html',
        sp_path_hint=hint,
        sp_auth=bool(fedauth and rtfa),
    )


@app.route('/sp/resolve_url', methods=['POST'])
def sp_resolve_url():
    """Resolve full SharePoint/OneDrive URL to server-relative folder for browsing."""
    fedauth, rtfa = load_sp_cookies()
    if not fedauth or not rtfa:
        return jsonify({'success': False, 'error': 'Please save FedAuth and rtFa first.'}), 401
    if not sp_site():
        return jsonify({
            'success': False,
            'error': 'SharePoint site URL is not configured. Set sharepoint_site_url in config/app_settings.json or SP_SYNC_SITE_URL.',
        }), 400

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'URL is empty.'}), 400

    from sp_sync.sharepoint.sync_engine import resolve_sharepoint_pasted_url

    def log_cb(msg):
        add_log('sharepoint', msg)

    result = resolve_sharepoint_pasted_url(fedauth, rtfa, sp_site(), url, log=log_cb)
    payload = dict(result)
    payload['success'] = bool(payload.pop('ok', False))
    if not payload['success']:
        payload.setdefault('error', 'Could not resolve URL.')
    return jsonify(payload)


def _normalize_sp_rel_key(u):
    u = (u or '').strip()
    if not u.startswith('/'):
        u = '/' + u
    return u


def _normalize_sp_bulk_item(raw):
    rel = _normalize_sp_rel_key(raw.get('rel_url') or '')
    et = (raw.get('entry_type') or 'folder').strip().lower()
    if et not in ('folder', 'file'):
        et = 'folder'
    name = (raw.get('name') or '').strip() or 'SharePoint'
    local_path = (raw.get('local_path') or '').strip()
    out = {'name': name, 'rel_url': rel, 'local_path': local_path, 'entry_type': et}
    if et == 'folder' and 'local_flat' in raw and raw.get('local_flat') is not None:
        out['local_flat'] = raw['local_flat']
    return out


@app.route('/sp/bulk_add', methods=['POST'])
def sp_bulk_add():
    """Merge SharePoint sync entries into config; dedupe by rel_url."""
    data = request.get_json(silent=True) or {}
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return jsonify({'success': False, 'error': 'Non-empty items array is required.'}), 400

    updates = {}
    skipped = 0
    for it in items:
        n = _normalize_sp_bulk_item(it)
        if not n['rel_url'] or not n['local_path']:
            skipped += 1
            continue
        updates[n['rel_url']] = n

    if not updates:
        return jsonify({'success': False, 'error': 'No valid items (each needs rel_url and local_path).'}), 400

    configs = load_sp_configs()
    existing_norm = {_normalize_sp_rel_key(c.get('rel_url', '')) for c in configs}

    new_list = []
    applied_urls = set()
    for c in configs:
        ru = _normalize_sp_rel_key(c.get('rel_url', ''))
        if ru in updates:
            new_list.append(updates[ru])
            applied_urls.add(ru)
        else:
            new_list.append(c)

    for ru, cfg in updates.items():
        if ru not in applied_urls:
            new_list.append(cfg)

    save_sp_configs(new_list)
    new_entries = sum(1 for ru in updates if ru not in existing_norm)
    updated_entries = len(updates) - new_entries
    return jsonify({
        'success': True,
        'merged': len(updates),
        'new': new_entries,
        'updated': updated_entries,
        'skipped_invalid': skipped,
        'total_configs': len(new_list),
    })


@app.route('/sp/add_folder', methods=['POST'])
def sp_add_folder():
    """Add a new SharePoint folder to sync."""
    data = request.json
    name = data.get('name', '').strip()
    rel_url = data.get('rel_url', '').strip()
    local_path = data.get('local_path', '').strip()
    
    if not rel_url or not local_path:
        return jsonify({'success': False, 'error': 'Relative URL and local path are required.'})
    
    entry_type = (data.get('entry_type') or 'folder').strip().lower()
    if entry_type not in ('folder', 'file'):
        entry_type = 'folder'

    row = {
        'name': name or 'SharePoint Folder',
        'rel_url': rel_url,
        'local_path': local_path,
        'entry_type': entry_type,
    }
    if entry_type == 'folder' and 'local_flat' in data and data.get('local_flat') is not None:
        row['local_flat'] = data['local_flat']

    configs = load_sp_configs()
    configs.append(row)
    save_sp_configs(configs)
    add_log('sharepoint', f"📁 Folder added: {name}")
    return jsonify({'success': True})


@app.route('/sp/remove_folder', methods=['POST'])
def sp_remove_folder():
    """Remove a SharePoint folder config."""
    data = request.json
    index = data.get('index', -1)
    configs = load_sp_configs()
    if 0 <= index < len(configs):
        removed = configs.pop(index)
        save_sp_configs(configs)
        add_log('sharepoint', f"🗑️ Folder removed: {removed.get('name', 'unknown')}")
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Invalid index.'})


@app.route('/sp/update_folder', methods=['POST'])
def sp_update_folder():
    """Update an existing SharePoint folder config."""
    data = request.json
    index = data.get('index', -1)
    name = data.get('name', '').strip()
    rel_url = data.get('rel_url', '').strip()
    local_path = data.get('local_path', '').strip()
    if not rel_url or not local_path:
        return jsonify({'success': False, 'error': 'Relative URL and local path are required.'})

    entry_type = (data.get('entry_type') or 'folder').strip().lower()
    if entry_type not in ('folder', 'file'):
        entry_type = 'folder'

    configs = load_sp_configs()
    if 0 <= index < len(configs):
        prev = configs[index]
        row = {
            'name': name or 'SharePoint Folder',
            'rel_url': rel_url,
            'local_path': local_path,
            'entry_type': entry_type,
        }
        if entry_type == 'folder':
            if 'local_flat' in data and data.get('local_flat') is not None:
                row['local_flat'] = data['local_flat']
            elif 'local_flat' in prev:
                row['local_flat'] = prev['local_flat']
        configs[index] = row
        save_sp_configs(configs)
        add_log('sharepoint', f"📝 Folder updated: {name}")
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Invalid index.'})


# ============================================================
# Google Drive Routes
# ============================================================
@app.route('/gdrive/auth')
def gdrive_auth():
    """Redirect to Google Drive auth instructions page."""
    return redirect(url_for('gdrive_auth_page'))


@app.route('/gdrive/auth_page')
def gdrive_auth_page():
    """Show instructions to run the standalone login script."""
    return render_template('gdrive_auth.html',
        login_script=os.path.join(ROOT, 'gdrive_login.py')
    )


@app.route('/gdrive/get_folder_info', methods=['POST'])
def gdrive_get_folder_info():
    """Fetch folder name from Google Drive URL."""
    data = request.json
    folder_url = data.get('folder_url', '').strip()
    
    from sp_sync.gdrive import extract_folder_id, get_credentials, get_folder_metadata
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return jsonify({'success': False, 'error': 'Invalid URL.'})
    
    creds = get_credentials()
    if not creds or not creds.valid:
        return jsonify({'success': False, 'error': 'Please sign in first.'})
    
    metadata = get_folder_metadata(creds, folder_id)
    if metadata:
        return jsonify({'success': True, 'name': metadata.get('name')})
    
    return jsonify({'success': False, 'error': 'Folder not found.'})


@app.route('/gdrive/add_folder', methods=['POST'])
def gdrive_add_folder():
    """Add a new Google Drive folder to sync."""
    data = request.json
    folder_url = data.get('folder_url', '').strip()
    local_path = data.get('local_path', '').strip()
    name = data.get('name', '').strip()
    
    if not folder_url or not local_path:
        return jsonify({'success': False, 'error': 'Folder URL and local path are required.'})
    
    from sp_sync.gdrive import extract_folder_id
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return jsonify({'success': False, 'error': 'Invalid Google Drive URL.'})
    
    configs = load_gdrive_configs()
    configs.append({
        'name': name or f"Google Drive Folder ({folder_id[:8]}...)",
        'folder_url': folder_url,
        'folder_id': folder_id,
        'local_path': local_path
    })
    save_gdrive_configs(configs)
    
    add_log('gdrive', f"📁 Folder added: {name or folder_id}")
    return jsonify({'success': True})


@app.route('/gdrive/update_folder', methods=['POST'])
def gdrive_update_folder():
    """Update an existing Google Drive folder config."""
    data = request.json
    index = data.get('index', -1)
    folder_url = data.get('folder_url', '').strip()
    local_path = data.get('local_path', '').strip()
    name = data.get('name', '').strip()
    
    configs = load_gdrive_configs()
    if 0 <= index < len(configs):
        from sp_sync.gdrive import extract_folder_id
        folder_id = extract_folder_id(folder_url)
        if not folder_id:
            return jsonify({'success': False, 'error': 'Invalid Google Drive URL.'})
            
        configs[index] = {
            'name': name or f"Google Drive Folder ({folder_id[:8]}...)",
            'folder_url': folder_url,
            'folder_id': folder_id,
            'local_path': local_path
        }
        save_gdrive_configs(configs)
        add_log('gdrive', f"📝 Folder updated: {name or folder_id}")
        return jsonify({'success': True})
    
    return jsonify({'success': False, 'error': 'Invalid index.'})


@app.route('/gdrive/remove_folder', methods=['POST'])
def gdrive_remove_folder():
    """Remove a Google Drive folder config."""
    data = request.json
    index = data.get('index', -1)
    
    configs = load_gdrive_configs()
    if 0 <= index < len(configs):
        removed = configs.pop(index)
        save_gdrive_configs(configs)
        add_log('gdrive', f"🗑️ Folder removed: {removed.get('name', 'unknown')}")
        return jsonify({'success': True})
    
    return jsonify({'success': False, 'error': 'Invalid index.'})


@app.route('/gdrive/sync', methods=['POST'])
def gdrive_sync():
    """Sync all configured Google Drive folders."""
    if sync_status['gdrive']['running']:
        return jsonify({'success': False, 'error': 'A sync is already running.'})
    
    from sp_sync.gdrive import is_authenticated
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Please sign in to Google Drive first.'})
    
    configs = load_gdrive_configs()
    if not configs:
        return jsonify({'success': False, 'error': 'No folders configured.'})
    
    def run_sync():
        sync_status['gdrive']['running'] = True
        add_log('gdrive', '--- Google Drive sync started ---')
        try:
            from sp_sync.gdrive import start_gdrive_sync
            total = 0
            for config in configs:
                add_log('gdrive', f"📂 Syncing: {config['name']}...")
                count = start_gdrive_sync(
                    config['folder_url'],
                    config['local_path'],
                    log_callback=lambda msg: add_log('gdrive', msg)
                )
                total += count
            
            result = f"✅ Done. Downloaded {total} new file(s)."
            add_log('gdrive', result)
            sync_status['gdrive']['last_result'] = result
        except Exception as e:
            error_msg = f"❌ Error: {e}"
            add_log('gdrive', error_msg)
            sync_status['gdrive']['last_result'] = error_msg
        finally:
            sync_status['gdrive']['running'] = False
            sync_status['gdrive']['last_run'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    threading.Thread(target=run_sync, daemon=True).start()
    return jsonify({'success': True, 'message': 'Sync started in the background...'})


@app.route('/sync/all', methods=['POST'])
def sync_all():
    """Sync both SharePoint and Google Drive concurrently."""
    results = {}
    
    # Start SP sync if not running
    if not sync_status['sharepoint']['running']:
        fedauth, rtfa = load_sp_cookies()
        if fedauth:
            sp_configs = load_sp_configs()
            def run_sp():
                sync_status['sharepoint']['running'] = True
                add_log('sharepoint', '--- SharePoint sync started ---')
                try:
                    from sp_sync.sharepoint.sync_engine import start_sync_from_config
                    total = 0
                    for config in sp_configs:
                        add_log('sharepoint', f"📂 Syncing: {config['name']}...")
                        count = start_sync_from_config(
                            fedauth, rtfa, sp_site(), config,
                            log_callback=lambda msg: add_log('sharepoint', msg))
                        total += count
                    result = f"✅ Done. Downloaded {total} new file(s)."
                    add_log('sharepoint', result)
                    sync_status['sharepoint']['last_result'] = result
                except Exception as e:
                    add_log('sharepoint', f"❌ Error: {e}")
                    sync_status['sharepoint']['last_result'] = f"❌ Error: {e}"
                finally:
                    sync_status['sharepoint']['running'] = False
                    sync_status['sharepoint']['last_run'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            threading.Thread(target=run_sp, daemon=True).start()
            results['sharepoint'] = 'started'
    
    # Start GDrive sync if not running
    if not sync_status['gdrive']['running']:
        from sp_sync.gdrive import is_authenticated
        if is_authenticated():
            configs = load_gdrive_configs()
            if configs:
                def run_gd():
                    sync_status['gdrive']['running'] = True
                    add_log('gdrive', '--- Google Drive sync started ---')
                    try:
                        from sp_sync.gdrive import start_gdrive_sync
                        total = 0
                        for config in configs:
                            add_log('gdrive', f"📂 Syncing: {config['name']}...")
                            count = start_gdrive_sync(
                                config['folder_url'], config['local_path'],
                                log_callback=lambda msg: add_log('gdrive', msg))
                            total += count
                        result = f"✅ Done. Downloaded {total} new file(s)."
                        add_log('gdrive', result)
                        sync_status['gdrive']['last_result'] = result
                    except Exception as e:
                        add_log('gdrive', f"❌ Error: {e}")
                        sync_status['gdrive']['last_result'] = f"❌ Error: {e}"
                    finally:
                        sync_status['gdrive']['running'] = False
                        sync_status['gdrive']['last_run'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                threading.Thread(target=run_gd, daemon=True).start()
                results['gdrive'] = 'started'
    
    if not results:
        return jsonify({'success': False, 'error': 'No sync started. Check credentials and that no sync is already running.'})
        
    return jsonify({'success': True, 'started': results})


@app.route('/logs')
def get_logs():
    """Get recent logs as JSON."""
    source = request.args.get('source', 'all')
    entries = list(log_buffer)
    if source != 'all':
        entries = [e for e in entries if e['source'] == source]
    return jsonify(entries[-100:])


@app.route('/logs/text')
def get_logs_text():
    """Plain-text log for easy copy (one line per entry)."""
    source = request.args.get('source', 'all')
    entries = list(log_buffer)
    if source != 'all':
        entries = [e for e in entries if e['source'] == source]
    entries = entries[-300:]
    tag = {"sharepoint": "SP", "gdrive": "GD", "system": "SYS"}
    lines = []
    for e in entries:
        lab = tag.get(e['source'], e['source'][:2].upper())
        lines.append(f"{e['time']} [{lab}] {e['msg']}")
    return Response(
        "\n".join(lines),
        mimetype="text/plain; charset=utf-8",
    )


@app.route('/status')
def get_status():
    """Get current sync status."""
    return jsonify(sync_status)


@app.route('/api/browse_folder')
def api_browse_folder():
    """Open a native folder selection dialog."""
    import tkinter as tk
    from tkinter import filedialog
    
    root = tk.Tk()
    root.withdraw()  # Hide the main tkinter window
    root.attributes("-topmost", True)  # Bring to front
    
    folder_selected = filedialog.askdirectory()
    root.destroy()
    
    if folder_selected:
        # Convert backslashes for JSON/JS safety if needed, 
        # but Python handles it fine. Let's make it standard Windows style.
        folder_selected = os.path.normpath(folder_selected)
        return jsonify({'success': True, 'path': folder_selected})
    
    return jsonify({'success': False})
