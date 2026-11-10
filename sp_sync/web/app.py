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

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Force UTF-8
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response

from sp_sync.paths import project_root
from sp_sync.sync_metrics import get_live_metrics, set_sync_active
from sp_sync.scheduler import (
    get_scheduler_settings,
    save_scheduler_settings,
    start_scheduler_daemon,
    is_run_due,
    is_scheduler_daemon_alive,
    run_scheduler_now,
)

ROOT = project_root()


def detect_sharepoint_site_root_from_url(url: str) -> str:
    """
    Derive SharePoint site root (https://host/personal/user or https://host/sites/SiteName)
    from a pasted full browser URL.
    """
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
            root_path = "/personal/" + parts[i + 1]
            return f"{p.scheme}://{p.netloc}{root_path}".rstrip("/")
        if sl == "sites" and i + 1 < len(parts):
            root_path = "/sites/" + parts[i + 1]
            return f"{p.scheme}://{p.netloc}{root_path}".rstrip("/")
    return ""


def validate_sharepoint_site_url_for_save(url: str) -> tuple[bool, str]:
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


app = Flask(
    __name__,
    template_folder=os.path.join(ROOT, "templates"),
    static_folder=os.path.join(ROOT, "static"),
    static_url_path="/static",
)
app.secret_key = os.urandom(24)


def _json_error(message: str, status: int = 400, *, code: str | None = None, **extra):
    """Consistent JSON API error payload."""
    payload = {"success": False, "error": str(message)}
    if code:
        payload["code"] = code
    if extra:
        payload.update(extra)
    return jsonify(payload), status


def _request_json() -> dict:
    """Safe request JSON extractor (never returns None)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _normalize_config_results(results: list[dict] | None, *, cap: int = 200) -> list[dict]:
    out: list[dict] = []
    for row in (results or []):
        if not isinstance(row, dict):
            continue
        out.append({
            'name': str(row.get('name') or 'unknown'),
            'status': str(row.get('status') or 'unknown'),
            'downloaded_files': int(row.get('downloaded_files') or 0),
            'error': str(row.get('error') or ''),
        })
        if len(out) >= cap:
            break
    return out


def _prepare_sync_report(report: dict) -> dict:
    """Fill derived report fields to keep API output consistent."""
    r = dict(report or {})
    elapsed = float(r.get('elapsed_sec') or 0.0)
    downloaded = int(r.get('downloaded_files') or 0)
    r['downloaded_files_per_sec'] = round((downloaded / elapsed), 4) if elapsed > 0 else 0.0

    processed = int(r.get('configs_processed') or 0)
    failed = int(r.get('configs_failed') or 0)
    skipped = int(r.get('configs_skipped') or 0)
    r['configs_ok'] = max(0, processed - failed)

    skip_reasons = r.get('skipped_reasons')
    if not isinstance(skip_reasons, dict):
        skip_reasons = {'disabled': skipped} if skipped else {}
    r['skipped_reasons'] = skip_reasons

    r['config_results'] = _normalize_config_results(r.get('config_results'))
    return r


def _save_sync_report(service: str, report: dict) -> None:
    """Persist last sync report + append history entry; best-effort only."""
    try:
        from sp_sync.db.store import get_store
        store = get_store()
        prepared = _prepare_sync_report(report)
        store.set_sync_report(service, prepared)
        store.append_sync_report_history(prepared)
    except Exception as exc:
        add_log('system', f"⚠️ Failed to save sync report ({service}): {exc}")


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
# SharePoint site URL: persisted in SQLite via web UI / explorer
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
            if creds is not None:
                gd_user = get_user_info(creds)
    except ImportError as e:
        add_log('system', f'Google Drive module not available: {e}')
    except Exception as e:
        add_log('system', f'Error checking Google Drive auth: {e}')
    
    sp_path_hint = ""
    for c in sp_configs or []:
        if c.get('rel_url') and str(c.get('entry_type', 'folder')).lower() != 'file':
            sp_path_hint = c['rel_url']
            break

    import sys as _sys
    from sp_sync.db.store import get_store

    return render_template('index.html',
        fedauth=fedauth,
        rtfa=rtfa,
        fedauth_len=len(fedauth),
        rtfa_len=len(rtfa),
        sp_auth=bool(fedauth and rtfa),
        sp_explore_auth=bool(fedauth and rtfa),
        sp_auto_cookies_supported=_sys.platform == "win32",
        sp_path_hint=sp_path_hint,
        sp_folders=sp_configs,
        gd_folders=gdrive_configs,
        gd_auth=gd_authenticated,
        gd_user=gd_user,
        sync_status=sync_status,
        sp_site_configured=bool(sp_site().strip()),
        sp_site_url_display=sp_site(),
        sp_guest_link_display=get_store().get_guest_link(),
        scheduler_settings=get_scheduler_settings(),
    )


@app.route('/sp/save_cookies', methods=['POST'])
def sp_save_cookies():
    data = _request_json()
    fedauth = (data.get('fedauth') or '').strip()
    rtfa = (data.get('rtfa') or '').strip()

    if not fedauth:
        return _json_error('FedAuth is required.', 400, code='MISSING_FEDAUTH')

    save_sp_cookies(fedauth, rtfa)
    add_log('sharepoint', '✅ Connection data saved.')
    return jsonify({'success': True})


@app.route('/sp/save_site_url', methods=['POST'])
def sp_save_site_url():
    """Save SharePoint site root URL (manual fallback; explorer auto-detect also writes here)."""
    from sp_sync.db.store import get_store

    data = request.get_json(silent=True) or {}
    raw = (data.get('site_url') or '').strip()
    ok, err = validate_sharepoint_site_url_for_save(raw)
    if not ok:
        return jsonify({'success': False, 'error': err}), 400
    u = raw.strip().rstrip("/")
    get_store().set_sharepoint_site_url(u)
    add_log('system', 'SharePoint site URL saved.')
    return jsonify({'success': True, 'site_url': u})


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
        data = _request_json()
        fedauth = (data.get('fedauth') or '').strip()
        rtfa = (data.get('rtfa') or '').strip()

        if not fedauth or not rtfa:
            return _json_error('Both cookies are required for validation.', 400, code='MISSING_COOKIES')

        result = validate_sp_cookies(fedauth, rtfa)
        return jsonify({'success': True, 'validation': result})
    except Exception as e:
        return _json_error(f'Validation error: {e}', 500, code='COOKIE_VALIDATION_ERROR')


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
            'error': (
                'SharePoint site URL is not set yet. Paste a full SharePoint link once in '
                'SharePoint explorer (it will be saved automatically), or set it under Connection settings.'
            ),
        })

    sp_configs = load_sp_configs()

    def run_sync():
        sync_status['sharepoint']['running'] = True
        started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        started_ts = time.time()
        add_log('sharepoint', '--- SharePoint sync started ---')
        errors = []
        total = 0
        processed = 0
        failed = 0
        skipped = 0
        config_results: list[dict] = []

        try:
            from sp_sync.sharepoint.sync_engine import start_sync_from_config

            for config in sp_configs:
                if not _sp_sync_enabled(config):
                    skipped += 1
                    cfg_name = config.get('name', 'SharePoint')
                    config_results.append({'name': cfg_name, 'status': 'skipped', 'downloaded_files': 0, 'error': 'sync_disabled'})
                    add_log('sharepoint', f"⏭️ Skipped (sync off): {cfg_name}")
                    continue

                cfg_name = config.get('name', 'SharePoint')
                processed += 1
                add_log('sharepoint', f"📂 Syncing: {cfg_name}...")
                try:
                    count = start_sync_from_config(
                        fedauth, rtfa, sp_site(), config,
                        log_callback=lambda msg: add_log('sharepoint', msg)
                    )
                    downloaded = int(count or 0)
                    total += downloaded
                    config_results.append({'name': cfg_name, 'status': 'ok', 'downloaded_files': downloaded, 'error': ''})
                except Exception as cfg_exc:
                    failed += 1
                    msg = f"{cfg_name}: {cfg_exc}"
                    errors.append(msg)
                    config_results.append({'name': cfg_name, 'status': 'failed', 'downloaded_files': 0, 'error': str(cfg_exc)})
                    add_log('sharepoint', f"❌ Config failed: {msg}")

            if errors:
                result = f"⚠️ Done with errors. Downloaded {total} new file(s). Failed configs: {failed}."
            else:
                result = f"✅ Done. Downloaded {total} new file(s)."
            add_log('sharepoint', result)
            sync_status['sharepoint']['last_result'] = result
        except Exception as e:
            error_msg = f"❌ Error: {e}"
            add_log('sharepoint', error_msg)
            sync_status['sharepoint']['last_result'] = error_msg
            errors.append(str(e))
            failed = max(1, failed)
        finally:
            finished_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            elapsed = round(time.time() - started_ts, 3)
            sync_status['sharepoint']['running'] = False
            sync_status['sharepoint']['last_run'] = finished_at
            _save_sync_report('sharepoint', {
                'service': 'sharepoint',
                'trigger': 'manual',
                'status': 'success' if not errors else ('partial' if processed > failed else 'error'),
                'started_at': started_at,
                'finished_at': finished_at,
                'elapsed_sec': elapsed,
                'downloaded_files': total,
                'configs_total': len(sp_configs),
                'configs_processed': processed,
                'configs_failed': failed,
                'configs_skipped': skipped,
                'skipped_reasons': {'disabled': skipped} if skipped else {},
                'config_results': config_results,
                'errors': errors[:20],
                'message': sync_status['sharepoint']['last_result'],
            })

    threading.Thread(target=run_sync, daemon=True).start()
    return jsonify({'success': True, 'message': 'Sync started in the background...'})


@app.route('/sp/browse', methods=['POST'])
def sp_browse():
    """List folders and files under a SharePoint parent path (server-relative URL)."""
    from sp_sync.sharepoint.sync_engine import (
        prime_sharepoint_session_for_browse,
        _sp_share_requests_session,
    )
    from sp_sync.sharepoint.rest_folder_list import (
        list_folder_children_rest,
        browse_empty_hint_i18n,
    )
    from sp_sync.db.store import get_store

    fedauth, rtfa = load_sp_cookies()
    data = request.get_json(silent=True) or {}
    prime_url = (data.get('prime_url') or '').strip()
    sharing_link_url = (data.get('sharing_link_url') or '').strip()
    if not sharing_link_url and prime_url and ":f:/" in prime_url.lower():
        sharing_link_url = prime_url
    for detect_u in (sharing_link_url, prime_url):
        if detect_u.lower().startswith("http"):
            detected = detect_sharepoint_site_root_from_url(detect_u)
            if detected:
                get_store().set_sharepoint_site_url(detected)
                break

    if not fedauth or not rtfa:
        return jsonify({'success': False, 'error': 'Please save FedAuth and rtFa first.'}), 401
    if not sp_site():
        return jsonify({
            'success': False,
            'error': (
                'SharePoint site URL is not set yet. Paste a full SharePoint link once in '
                'SharePoint explorer (it will be saved automatically), or set it under Connection settings.'
            ),
        }), 400

    parent_raw = (data.get('parent_rel_url') or '').strip()
    parent_item_id = (data.get('parent_item_id') or '').strip() or None
    share_browse_root = (data.get('share_browse_root') or '').strip() or None

    base = sp_site().rstrip('/')
    session = _sp_share_requests_session(sp_site(), fedauth, rtfa, accept_html=False)
    for warm_url in (sharing_link_url, prime_url):
        if warm_url.lower().startswith("http"):
            prime_sharepoint_session_for_browse(
                session,
                sp_site(),
                prime_url=warm_url,
                fedauth_cookie=fedauth,
                rtfa_cookie=rtfa,
            )

    result = list_folder_children_rest(
        session,
        base,
        parent_raw,
        timeout=60,
        sharing_link_url=sharing_link_url or None,
        parent_item_id=parent_item_id,
        share_root_norm=share_browse_root,
    )
    if not result.get("ok"):
        payload = {
            'success': False,
            'error': result.get('error') or 'Browse failed',
            'parent': result.get('normalized_parent') or parent_raw,
            'body_snippet': (result.get('body_snippet') or '')[:500].replace('\n', ' '),
        }
        if result.get('content_type'):
            payload['content_type'] = result['content_type']
        if result.get('upstream_http_status') is not None:
            payload['upstream_http_status'] = result['upstream_http_status']
        if result.get('upstream_http_status') == 403:
            payload['permission_hint_en'] = (
                'SharePoint returned 403 (access denied) for this folder listing. '
                'Paste FedAuth and rtFa from the same browser profile where this link opens while you are signed in, '
                'then use Resolve link again. If the library belongs to another user, you need their account cookies or an explicit share.'
            )
            payload['permission_hint_ar'] = (
                'رفض SharePoint عرض محتويات المجلد (403). '
                'الصق FedAuth و rtFa من نفس المتصفح والحساب الذي يفتح الرابط وهو مسجّل، ثم أعد Resolve. '
                'إن كان المجلد لمستخدم آخر فتحتاج صلاحية مشاركة أو كوكيز ذلك الحساب.'
            )
        http_out = result.get('upstream_http_status')
        if http_out in (401, 403, 404):
            return jsonify(payload), int(http_out)
        return jsonify(payload), 400

    folders_out = result['folders']
    files_out = result['files']
    norm = result['normalized_parent']
    out = {
        'success': True,
        'parent': norm,
        'folders': folders_out,
        'files': files_out,
        'browse_method': result.get('method') or '',
    }
    if not folders_out and not files_out:
        hi = browse_empty_hint_i18n()
        out["hint_en"] = hi["en"]
        out["hint_ar"] = hi["ar"]
    return jsonify(out)


@app.route('/sp/explore')
def sp_explore_page():
    """SharePoint explorer lives in the main dashboard tab (sidebar)."""
    return redirect(url_for('index') + '?tab=sp-explore')


@app.route('/sp/resolve_url', methods=['POST'])
def sp_resolve_url():
    """Resolve full SharePoint/OneDrive URL to server-relative folder for browsing."""
    from sp_sync.sharepoint.sync_engine import resolve_sharepoint_pasted_url
    from sp_sync.db.store import KEY_GUEST_LINK, get_store

    fedauth, rtfa = load_sp_cookies()
    if not fedauth or not rtfa:
        return jsonify({'success': False, 'error': 'Please save FedAuth and rtFa first.'}), 401

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'URL is empty.'}), 400

    if url.lower().startswith("http"):
        detected = detect_sharepoint_site_root_from_url(url)
        if detected:
            get_store().set_sharepoint_site_url(detected)

    if not sp_site():
        return jsonify({
            'success': False,
            'error': (
                'SharePoint site URL is not set yet. Paste a full https SharePoint link once '
                '(site root is detected automatically), or set it under Connection settings.'
            ),
        }), 400

    def log_cb(msg):
        add_log('sharepoint', msg)

    result = resolve_sharepoint_pasted_url(fedauth, rtfa, sp_site(), url, log=log_cb)
    payload = dict(result)
    payload['success'] = bool(payload.pop('ok', False))
    if not payload['success']:
        payload.setdefault('error', 'Could not resolve URL.')
    if payload['success'] and url.lower().startswith('http'):
        from sp_sync.sharepoint.sync_engine import _sp_sharing_url_has_stream_path
        if _sp_sharing_url_has_stream_path(url):
            share_u = url.strip()
            payload.setdefault('sharing_url', share_u)
            get_store().set_text(KEY_GUEST_LINK, share_u)
            add_log('sharepoint', 'Guest :f:/ link saved from resolve')
        bp = (payload.get('browse_parent') or '').strip()
        if bp:
            payload.setdefault('sharing_root', bp)
    return jsonify(payload)


def _normalize_sp_rel_key(u):
    u = (u or '').strip()
    if not u.startswith('/'):
        u = '/' + u
    return u


def _sp_sync_enabled(config: dict) -> bool:
    """Whether this config participates in sync runs (default: enabled)."""
    return config.get('sync_enabled', True) is not False


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
    share = (raw.get('sharing_link') or '').strip()
    if share.lower().startswith('http'):
        out['sharing_link'] = share
    root = (raw.get('sharing_root') or '').strip()
    if root.startswith('/'):
        out['sharing_root'] = root
    if 'sync_enabled' in raw:
        out['sync_enabled'] = raw['sync_enabled'] is not False
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
            merged = dict(updates[ru])
            if 'sync_enabled' not in merged and 'sync_enabled' in c:
                merged['sync_enabled'] = c['sync_enabled']
            new_list.append(merged)
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
    data = _request_json()
    name = (data.get('name') or '').strip()
    rel_url = (data.get('rel_url') or '').strip()
    local_path = (data.get('local_path') or '').strip()

    if not rel_url or not local_path:
        return _json_error('Relative URL and local path are required.', 400, code='INVALID_SP_CONFIG')
    
    entry_type = (data.get('entry_type') or 'folder').strip().lower()
    if entry_type not in ('folder', 'file'):
        entry_type = 'folder'

    row = {
        'name': name or 'SharePoint Folder',
        'rel_url': rel_url,
        'local_path': local_path,
        'entry_type': entry_type,
        'sync_enabled': data.get('sync_enabled', True) is not False,
    }
    if entry_type == 'folder' and 'local_flat' in data and data.get('local_flat') is not None:
        row['local_flat'] = data['local_flat']
    share = (data.get('sharing_link') or '').strip()
    if share.lower().startswith('http'):
        row['sharing_link'] = share

    configs = load_sp_configs()
    configs.append(row)
    save_sp_configs(configs)
    add_log('sharepoint', f"📁 Folder added: {name}")
    return jsonify({'success': True})


@app.route('/sp/remove_folder', methods=['POST'])
def sp_remove_folder():
    """Remove a SharePoint folder config."""
    data = _request_json()
    try:
        index = int(data.get('index', -1))
    except (TypeError, ValueError):
        return _json_error('Invalid index.', 400, code='INVALID_INDEX')

    configs = load_sp_configs()
    if 0 <= index < len(configs):
        removed = configs.pop(index)
        save_sp_configs(configs)
        add_log('sharepoint', f"🗑️ Folder removed: {removed.get('name', 'unknown')}")
        return jsonify({'success': True})
    return _json_error('Invalid index.', 400, code='INVALID_INDEX')


@app.route('/sp/update_folder', methods=['POST'])
def sp_update_folder():
    """Update an existing SharePoint folder config."""
    data = _request_json()
    try:
        index = int(data.get('index', -1))
    except (TypeError, ValueError):
        return _json_error('Invalid index.', 400, code='INVALID_INDEX')

    name = (data.get('name') or '').strip()
    rel_url = (data.get('rel_url') or '').strip()
    local_path = (data.get('local_path') or '').strip()
    if not rel_url or not local_path:
        return _json_error('Relative URL and local path are required.', 400, code='INVALID_SP_CONFIG')

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
        share = (data.get('sharing_link') or '').strip()
        if share.lower().startswith('http'):
            row['sharing_link'] = share
        elif 'sharing_link' in prev:
            row['sharing_link'] = prev['sharing_link']
        if 'sync_enabled' in prev:
            row['sync_enabled'] = prev['sync_enabled']
        configs[index] = row
        save_sp_configs(configs)
        add_log('sharepoint', f"📝 Folder updated: {name}")
        return jsonify({'success': True})
    return _json_error('Invalid index.', 400, code='INVALID_INDEX')


@app.route('/sp/toggle_sync', methods=['POST'])
def sp_toggle_sync():
    """Enable or disable a SharePoint config in sync runs (order unchanged)."""
    data = request.get_json(silent=True) or {}
    try:
        index = int(data.get('index', -1))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid index.'}), 400

    configs = load_sp_configs()
    if not (0 <= index < len(configs)):
        return jsonify({'success': False, 'error': 'Invalid index.'}), 400

    if 'enabled' in data:
        enabled = data.get('enabled') is not False
    else:
        enabled = not _sp_sync_enabled(configs[index])

    configs[index]['sync_enabled'] = enabled
    save_sp_configs(configs)
    name = configs[index].get('name', 'SharePoint')
    if enabled:
        add_log('sharepoint', f"▶️ Sync enabled: {name}")
    else:
        add_log('sharepoint', f"⏸️ Sync paused: {name}")
    return jsonify({'success': True, 'enabled': enabled, 'index': index})


@app.route('/sp/reorder_folders', methods=['POST'])
def sp_reorder_folders():
    """Persist SharePoint sync folder order (sync runs top-to-bottom)."""
    data = request.get_json(silent=True) or {}
    configs = data.get('configs')
    if isinstance(configs, list) and configs:
        normalized = []
        for raw in configs:
            if not isinstance(raw, dict):
                continue
            n = _normalize_sp_bulk_item(raw)
            if n['rel_url'] and n['local_path']:
                if 'sharing_link' not in n and raw.get('sharing_link'):
                    n['sharing_link'] = (raw.get('sharing_link') or '').strip()
                normalized.append(n)
        if not normalized:
            return jsonify({'success': False, 'error': 'No valid folder entries.'}), 400
        save_sp_configs(normalized)
        add_log('sharepoint', f"📋 Sync order updated ({len(normalized)} item(s))")
        return jsonify({'success': True, 'total': len(normalized)})

    order = data.get('order')
    current = load_sp_configs()
    if not isinstance(order, list) or len(order) != len(current):
        return jsonify({'success': False, 'error': 'Invalid order array.'}), 400
    try:
        indices = [int(i) for i in order]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Order must be integer indices.'}), 400
    if sorted(indices) != list(range(len(current))):
        return jsonify({'success': False, 'error': 'Order must be a permutation of indices.'}), 400
    reordered = [current[i] for i in indices]
    save_sp_configs(reordered)
    add_log('sharepoint', '📋 Sync order updated')
    return jsonify({'success': True, 'total': len(reordered)})


@app.route('/sp/save_guest_link', methods=['POST'])
def sp_save_guest_link():
    """Optional :f:/ guest link used to warm session before sync/browse."""
    from sp_sync.db.store import get_store, KEY_GUEST_LINK

    data = request.get_json(silent=True) or {}
    link = (data.get('guest_link') or data.get('url') or '').strip()
    store = get_store()
    store.set_text(KEY_GUEST_LINK, link)
    if link:
        try:
            configs = store.get_sharepoint_configs()
            updated = False
            for c in configs:
                if c.get('sharing_link'):
                    c['sharing_link'] = link
                    updated = True
            if updated:
                store.set_sharepoint_configs(configs)
        except Exception as e:
            add_log('sharepoint', f"⚠️ Failed to update config sharing links: {e}")
    add_log('sharepoint', 'Guest share link saved' if link else 'Guest share link cleared')
    return jsonify({'success': True})


# ============================================================
# Google Drive Routes
# ============================================================
@app.route('/gdrive/auth')
def gdrive_auth():
    """Redirect to Google Drive auth page."""
    return redirect(url_for('gdrive_auth_page'))


@app.route('/gdrive/auth_page')
def gdrive_auth_page():
    """Interactive Google Drive sign-in page."""
    from sp_sync.gdrive import is_authenticated, get_user_info, get_credentials, has_credentials
    auth = is_authenticated()
    creds = get_credentials() if auth else None
    user = get_user_info(creds) if creds is not None else None
    return render_template('gdrive_auth.html',
        authenticated=auth,
        user=user,
        has_credentials=has_credentials()
    )


@app.route('/gdrive/auth/start', methods=['POST'])
def gdrive_auth_start():
    """Start interactive OAuth2 login in background."""
    from sp_sync.gdrive import start_oauth_flow
    try:
        res = start_oauth_flow(open_browser=True)
        add_log('gdrive', '🔐 Google Drive authentication started in browser...')
        return jsonify(res)
    except FileNotFoundError as exc:
        return jsonify({'success': False, 'error': str(exc), 'need_credentials': True}), 400
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/gdrive/auth/status', methods=['GET'])
def gdrive_auth_status():
    """Check current Google Drive authentication flow status."""
    from sp_sync.gdrive import get_oauth_status
    return jsonify(get_oauth_status())


@app.route('/gdrive/auth/code', methods=['POST'])
def gdrive_auth_code():
    """Submit an authorization code or redirect response URL manually."""
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or data.get('url') or '').strip()
    from sp_sync.gdrive import complete_oauth_with_code
    res = complete_oauth_with_code(code)
    if res.get('success'):
        add_log('gdrive', f"✅ Google Drive connected as {res.get('user_email')}")
    return jsonify(res)


@app.route('/gdrive/auth/disconnect', methods=['POST'])
def gdrive_auth_disconnect():
    """Disconnect Google Drive account."""
    from sp_sync.gdrive import disconnect_gdrive
    disconnect_gdrive()
    add_log('gdrive', '🚪 Google Drive disconnected.')
    return jsonify({'success': True})


@app.route('/gdrive/upload_credentials', methods=['POST'])
def gdrive_upload_credentials():
    """Upload or update credentials.json from UI."""
    from sp_sync.gdrive import save_credentials_content
    content = ""
    if 'file' in request.files:
        f = request.files['file']
        content = f.read().decode('utf-8', errors='replace')
    else:
        data = request.get_json(silent=True) or {}
        content = (data.get('content') or '').strip()

    if not content:
        return jsonify({'success': False, 'error': 'No file or content provided.'}), 400

    try:
        save_credentials_content(content)
        add_log('gdrive', '✅ Google credentials.json saved successfully.')
        return jsonify({'success': True})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400


@app.route('/gdrive/get_folder_info', methods=['POST'])
def gdrive_get_folder_info():
    """Fetch folder name from Google Drive URL."""
    data = _request_json()
    folder_url = (data.get('folder_url') or '').strip()

    from sp_sync.gdrive import extract_folder_id, get_credentials, get_folder_metadata
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return _json_error('Invalid URL.', 400, code='INVALID_GDRIVE_URL')

    creds = get_credentials()
    if not creds or not creds.valid:
        return _json_error('Please sign in first.', 401, code='GDRIVE_NOT_AUTHENTICATED')

    metadata = get_folder_metadata(creds, folder_id)
    if metadata:
        return jsonify({'success': True, 'name': metadata.get('name')})

    return _json_error('Folder not found.', 404, code='GDRIVE_FOLDER_NOT_FOUND')


@app.route('/gdrive/add_folder', methods=['POST'])
def gdrive_add_folder():
    """Add a new Google Drive folder to sync."""
    data = _request_json()
    folder_url = (data.get('folder_url') or '').strip()
    local_path = (data.get('local_path') or '').strip()
    name = (data.get('name') or '').strip()

    if not folder_url or not local_path:
        return _json_error('Folder URL and local path are required.', 400, code='INVALID_GDRIVE_CONFIG')

    from sp_sync.gdrive import extract_folder_id
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return _json_error('Invalid Google Drive URL.', 400, code='INVALID_GDRIVE_URL')
    
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
    data = _request_json()
    try:
        index = int(data.get('index', -1))
    except (TypeError, ValueError):
        return _json_error('Invalid index.', 400, code='INVALID_INDEX')

    folder_url = (data.get('folder_url') or '').strip()
    local_path = (data.get('local_path') or '').strip()
    name = (data.get('name') or '').strip()

    configs = load_gdrive_configs()
    if 0 <= index < len(configs):
        from sp_sync.gdrive import extract_folder_id
        folder_id = extract_folder_id(folder_url)
        if not folder_id:
            return _json_error('Invalid Google Drive URL.', 400, code='INVALID_GDRIVE_URL')
            
        configs[index] = {
            'name': name or f"Google Drive Folder ({folder_id[:8]}...)",
            'folder_url': folder_url,
            'folder_id': folder_id,
            'local_path': local_path
        }
        save_gdrive_configs(configs)
        add_log('gdrive', f"📝 Folder updated: {name or folder_id}")
        return jsonify({'success': True})
    
    return _json_error('Invalid index.', 400, code='INVALID_INDEX')


@app.route('/gdrive/remove_folder', methods=['POST'])
def gdrive_remove_folder():
    """Remove a Google Drive folder config."""
    data = _request_json()
    try:
        index = int(data.get('index', -1))
    except (TypeError, ValueError):
        return _json_error('Invalid index.', 400, code='INVALID_INDEX')

    configs = load_gdrive_configs()
    if 0 <= index < len(configs):
        removed = configs.pop(index)
        save_gdrive_configs(configs)
        add_log('gdrive', f"🗑️ Folder removed: {removed.get('name', 'unknown')}")
        return jsonify({'success': True})

    return _json_error('Invalid index.', 400, code='INVALID_INDEX')


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
        started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        started_ts = time.time()
        add_log('gdrive', '--- Google Drive sync started ---')
        errors = []
        total = 0
        processed = 0
        failed = 0
        config_results: list[dict] = []

        try:
            from sp_sync.gdrive import start_gdrive_sync
            for config in configs:
                cfg_name = config.get('name', 'Google Drive')
                processed += 1
                add_log('gdrive', f"📂 Syncing: {cfg_name}...")
                try:
                    count = start_gdrive_sync(
                        config['folder_url'],
                        config['local_path'],
                        log_callback=lambda msg: add_log('gdrive', msg)
                    )
                    downloaded = int(count or 0)
                    total += downloaded
                    config_results.append({'name': cfg_name, 'status': 'ok', 'downloaded_files': downloaded, 'error': ''})
                except Exception as cfg_exc:
                    failed += 1
                    msg = f"{cfg_name}: {cfg_exc}"
                    errors.append(msg)
                    config_results.append({'name': cfg_name, 'status': 'failed', 'downloaded_files': 0, 'error': str(cfg_exc)})
                    add_log('gdrive', f"❌ Config failed: {msg}")

            if errors:
                result = f"⚠️ Done with errors. Downloaded {total} new file(s). Failed configs: {failed}."
            else:
                result = f"✅ Done. Downloaded {total} new file(s)."
            add_log('gdrive', result)
            sync_status['gdrive']['last_result'] = result
        except Exception as e:
            error_msg = f"❌ Error: {e}"
            add_log('gdrive', error_msg)
            sync_status['gdrive']['last_result'] = error_msg
            errors.append(str(e))
            failed = max(1, failed)
        finally:
            finished_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            elapsed = round(time.time() - started_ts, 3)
            sync_status['gdrive']['running'] = False
            sync_status['gdrive']['last_run'] = finished_at
            _save_sync_report('gdrive', {
                'service': 'gdrive',
                'trigger': 'manual',
                'status': 'success' if not errors else ('partial' if processed > failed else 'error'),
                'started_at': started_at,
                'finished_at': finished_at,
                'elapsed_sec': elapsed,
                'downloaded_files': total,
                'configs_total': len(configs),
                'configs_processed': processed,
                'configs_failed': failed,
                'configs_skipped': 0,
                'skipped_reasons': {},
                'config_results': config_results,
                'errors': errors[:20],
                'message': sync_status['gdrive']['last_result'],
            })

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
            if not sp_site():
                add_log('sharepoint', '⚠️ Cannot start SharePoint sync: site URL is not configured.')
            else:
                sp_configs = load_sp_configs()
                def run_sp():
                    sync_status['sharepoint']['running'] = True
                    set_sync_active(True, service="sharepoint", queue_total=len(sp_configs))
                    started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    started_ts = time.time()
                    add_log('sharepoint', '--- SharePoint sync started ---')
                    total = 0
                    processed = 0
                    failed = 0
                    skipped = 0
                    errors = []
                    try:
                        from sp_sync.sharepoint.sync_engine import start_sync_from_config
                        for config in sp_configs:
                            if not _sp_sync_enabled(config):
                                skipped += 1
                                add_log('sharepoint', f"⏭️ Skipped (sync off): {config.get('name', 'SharePoint')}")
                                continue
                            processed += 1
                            add_log('sharepoint', f"📂 Syncing: {config['name']}...")
                            try:
                                count = start_sync_from_config(
                                    fedauth, rtfa, sp_site(), config,
                                    log_callback=lambda msg: add_log('sharepoint', msg))
                                total += int(count or 0)
                            except Exception as cfg_exc:
                                failed += 1
                                errors.append(str(cfg_exc))
                        result = f"✅ Done. Downloaded {total} new file(s)." if not errors else f"⚠️ Done with errors. Downloaded {total} new file(s). Failed configs: {failed}."
                        add_log('sharepoint', result)
                        sync_status['sharepoint']['last_result'] = result
                    except Exception as e:
                        add_log('sharepoint', f"❌ Error: {e}")
                        sync_status['sharepoint']['last_result'] = f"❌ Error: {e}"
                        errors.append(str(e))
                        failed = max(1, failed)
                    finally:
                        finished_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        sync_status['sharepoint']['running'] = False
                        sync_status['sharepoint']['last_run'] = finished_at
                        set_sync_active(False)
                        _save_sync_report('sharepoint', {
                            'service': 'sharepoint',
                            'trigger': 'sync_all',
                            'status': 'success' if not errors else ('partial' if processed > failed else 'error'),
                            'started_at': started_at,
                            'finished_at': finished_at,
                            'elapsed_sec': round(time.time() - started_ts, 3),
                            'downloaded_files': total,
                            'configs_total': len(sp_configs),
                            'configs_processed': processed,
                            'configs_failed': failed,
                            'configs_skipped': skipped,
                            'errors': errors[:20],
                            'message': sync_status['sharepoint']['last_result'],
                        })
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
                    set_sync_active(True, service="gdrive", queue_total=len(configs))
                    started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    started_ts = time.time()
                    add_log('gdrive', '--- Google Drive sync started ---')
                    total = 0
                    processed = 0
                    failed = 0
                    errors = []
                    try:
                        from sp_sync.gdrive import start_gdrive_sync
                        for config in configs:
                            processed += 1
                            add_log('gdrive', f"📂 Syncing: {config['name']}...")
                            try:
                                count = start_gdrive_sync(
                                    config['folder_url'], config['local_path'],
                                    log_callback=lambda msg: add_log('gdrive', msg))
                                total += int(count or 0)
                            except Exception as cfg_exc:
                                failed += 1
                                errors.append(str(cfg_exc))
                        result = f"✅ Done. Downloaded {total} new file(s)." if not errors else f"⚠️ Done with errors. Downloaded {total} new file(s). Failed configs: {failed}."
                        add_log('gdrive', result)
                        sync_status['gdrive']['last_result'] = result
                    except Exception as e:
                        add_log('gdrive', f"❌ Error: {e}")
                        sync_status['gdrive']['last_result'] = f"❌ Error: {e}"
                        errors.append(str(e))
                        failed = max(1, failed)
                    finally:
                        finished_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        sync_status['gdrive']['running'] = False
                        sync_status['gdrive']['last_run'] = finished_at
                        set_sync_active(False)
                        _save_sync_report('gdrive', {
                            'service': 'gdrive',
                            'trigger': 'sync_all',
                            'status': 'success' if not errors else ('partial' if processed > failed else 'error'),
                            'started_at': started_at,
                            'finished_at': finished_at,
                            'elapsed_sec': round(time.time() - started_ts, 3),
                            'downloaded_files': total,
                            'configs_total': len(configs),
                            'configs_processed': processed,
                            'configs_failed': failed,
                            'configs_skipped': 0,
                            'errors': errors[:20],
                            'message': sync_status['gdrive']['last_result'],
                        })
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


@app.route('/logs/clear', methods=['POST'])
def clear_logs_route():
    """Clear in-memory and persisted log entries (all or one source)."""
    data = request.get_json(silent=True) or {}
    source = (data.get('source') or request.args.get('source') or 'all').strip().lower()
    if source not in ('all', 'sharepoint', 'gdrive', 'system'):
        return jsonify({'success': False, 'error': 'Invalid source.'}), 400

    try:
        from sp_sync.db.store import get_store
        get_store().clear_logs(None if source == 'all' else source)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    if source == 'all':
        log_buffer.clear()
    else:
        kept = [e for e in log_buffer if e.get('source') != source]
        log_buffer.clear()
        log_buffer.extend(kept)

    label = 'all sources' if source == 'all' else source
    add_log('system', f'🧹 Log cleared ({label})')
    return jsonify({'success': True, 'source': source})


@app.route('/status')
def get_status():
    """Get current sync status."""
    return jsonify(sync_status)


@app.route('/sync/report')
def sync_report():
    """Get last sync report for one service or both services."""
    from sp_sync.db.store import get_store

    service = (request.args.get('service') or 'all').strip().lower()
    if service not in ('all', 'sharepoint', 'gdrive'):
        return _json_error('service must be one of: all, sharepoint, gdrive', 400, code='INVALID_SERVICE')

    store = get_store()
    if service == 'all':
        return jsonify({
            'success': True,
            'reports': {
                'sharepoint': store.get_sync_report('sharepoint'),
                'gdrive': store.get_sync_report('gdrive'),
            }
        })

    return jsonify({'success': True, 'report': store.get_sync_report(service), 'service': service})


@app.route('/sync/report/clear', methods=['POST'])
def sync_report_clear():
    """Clear last sync report for one service or both services."""
    from sp_sync.db.store import get_store

    data = _request_json()
    service = (data.get('service') or request.args.get('service') or 'all').strip().lower()
    if service not in ('all', 'sharepoint', 'gdrive'):
        return _json_error('service must be one of: all, sharepoint, gdrive', 400, code='INVALID_SERVICE')

    store = get_store()
    if service in ('all', 'sharepoint'):
        store.set_sync_report('sharepoint', {})
    if service in ('all', 'gdrive'):
        store.set_sync_report('gdrive', {})
    store.clear_sync_report_history(service)

    add_log('system', f"🧹 Sync report cleared ({service})")
    return jsonify({'success': True, 'service': service})


@app.route('/sync/report/history')
def sync_report_history():
    """Get sync report history, optionally filtered by service and query."""
    from sp_sync.db.store import get_store

    service = (request.args.get('service') or 'all').strip().lower()
    if service not in ('all', 'sharepoint', 'gdrive'):
        return _json_error('service must be one of: all, sharepoint, gdrive', 400, code='INVALID_SERVICE')

    raw_limit = (request.args.get('limit') or '20').strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        return _json_error('limit must be an integer', 400, code='INVALID_LIMIT')
    if limit < 1 or limit > 200:
        return _json_error('limit must be between 1 and 200', 400, code='INVALID_LIMIT')

    q = (request.args.get('q') or '').strip()
    items = get_store().get_sync_report_history(service=service, limit=limit, query=q)
    return jsonify({'success': True, 'service': service, 'limit': limit, 'q': q, 'items': items})


@app.route('/sync/report/history/export')
def sync_report_history_export():
    """Export filtered sync report history as JSON file download."""
    from sp_sync.db.store import get_store

    service = (request.args.get('service') or 'all').strip().lower()
    if service not in ('all', 'sharepoint', 'gdrive'):
        return _json_error('service must be one of: all, sharepoint, gdrive', 400, code='INVALID_SERVICE')

    raw_limit = (request.args.get('limit') or '50').strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        return _json_error('limit must be an integer', 400, code='INVALID_LIMIT')
    if limit < 1 or limit > 500:
        return _json_error('limit must be between 1 and 500', 400, code='INVALID_LIMIT')

    q = (request.args.get('q') or '').strip()
    items = get_store().get_sync_report_history(service=service, limit=limit, query=q)
    payload = {
        'exported_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'service': service,
        'limit': limit,
        'q': q,
        'count': len(items),
        'items': items,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"sync_report_history_{service}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    headers = {
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Disposition': f'attachment; filename="{fname}"',
    }
    return Response(body, headers=headers)


@app.route('/api/browse_folder')
def api_browse_folder():
    """Open a native folder selection dialog (when display is available)."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()  # Hide the main tkinter window
        root.attributes("-topmost", True)  # Bring to front

        folder_selected = filedialog.askdirectory()
        root.destroy()

        if folder_selected:
            folder_selected = os.path.normpath(folder_selected)
            return jsonify({'success': True, 'path': folder_selected})

        return jsonify({'success': False})
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Native folder dialog unavailable: {e}',
        }), 200


@app.route('/sync/live_metrics')
def sync_live_metrics():
    """Get real-time download metrics (file, speed, ETA, progress)."""
    return jsonify(get_live_metrics())


@app.route('/scheduler/settings', methods=['GET', 'POST'])
def scheduler_settings_route():
    """Get or update background sync scheduler settings."""
    if request.method == 'POST':
        data = _request_json()
        try:
            updated = save_scheduler_settings(data)
        except ValueError as exc:
            return _json_error(str(exc), 400, code='INVALID_SCHEDULER_SETTINGS')
        except Exception as exc:
            return _json_error(str(exc), 500, code='SCHEDULER_SETTINGS_SAVE_FAILED')

        state_str = "enabled" if updated.get("enabled") else "disabled"
        add_log('system', f"⏰ Scheduler {state_str}: every {updated.get('interval_hours')} hours (target={updated.get('target')})")
        return jsonify({
            'success': True,
            'settings': updated,
            'is_due': is_run_due(),
            'daemon_alive': is_scheduler_daemon_alive(),
        })

    current = get_scheduler_settings()
    return jsonify({
        'success': True,
        'settings': current,
        'is_due': is_run_due(),
        'daemon_alive': is_scheduler_daemon_alive(),
    })


@app.route('/scheduler/run_now', methods=['POST'])
def scheduler_run_now_route():
    """Force trigger scheduler callback now (manual admin/testing action)."""
    data = _request_json()
    target = (data.get('target') or '').strip().lower() or None
    try:
        result = run_scheduler_now(target)
    except ValueError as exc:
        return _json_error(str(exc), 400, code='INVALID_SCHEDULER_TARGET')
    except RuntimeError as exc:
        return _json_error(str(exc), 503, code='SCHEDULER_NOT_READY')
    except Exception as exc:
        return _json_error(str(exc), 500, code='SCHEDULER_RUN_NOW_FAILED')

    add_log('system', f"▶️ Scheduler force-run requested (target={result.get('target')})")
    return jsonify(result)


@app.route('/api/open_local_folder', methods=['POST'])
def api_open_local_folder():
    """Open a local folder path in the OS native file manager."""
    data = request.get_json(silent=True) or {}
    folder_path = (data.get('folder_path') or data.get('path') or '').strip()
    if not folder_path:
        return jsonify({'success': False, 'error': 'No folder path provided.'}), 400

    folder_path = os.path.abspath(os.path.expanduser(folder_path))
    if not os.path.exists(folder_path):
        # Create it if it doesn't exist yet
        try:
            os.makedirs(folder_path, exist_ok=True)
        except Exception as exc:
            return jsonify({'success': False, 'error': f'Folder does not exist and cannot be created: {exc}'}), 400

    try:
        import subprocess
        if sys.platform == 'win32':
            os.startfile(folder_path)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', folder_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(['xdg-open', folder_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'success': True})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


def _trigger_scheduled_sync(target="all"):
    """Callback invoked by background scheduler daemon when an automatic run is due."""
    try:
        add_log('system', f"⏰ Scheduled auto-sync starting (target={target})...")
        if target in ("all", "sharepoint") and not sync_status['sharepoint']['running']:
            fedauth, rtfa = load_sp_cookies()
            if fedauth and sp_site():
                sp_configs = load_sp_configs()
                def _do_sp():
                    sync_status['sharepoint']['running'] = True
                    set_sync_active(True, service="sharepoint", queue_total=len(sp_configs))
                    started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    started_ts = time.time()
                    total = 0
                    errors = []
                    processed = 0
                    failed = 0
                    skipped = 0
                    try:
                        from sp_sync.sharepoint.sync_engine import start_sync_from_config
                        for c in sp_configs:
                            if not _sp_sync_enabled(c):
                                skipped += 1
                                continue
                            processed += 1
                            try:
                                total += int(start_sync_from_config(fedauth, rtfa, sp_site(), c, log_callback=lambda m: add_log('sharepoint', m)) or 0)
                            except Exception as cfg_exc:
                                failed += 1
                                errors.append(str(cfg_exc))
                    except Exception as exc:
                        errors.append(str(exc))
                        failed = max(1, failed)
                    finally:
                        sync_status['sharepoint']['running'] = False
                        set_sync_active(False)
                        finished_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        _save_sync_report('sharepoint', {
                            'service': 'sharepoint',
                            'trigger': 'scheduler',
                            'status': 'success' if not errors else ('partial' if processed > failed else 'error'),
                            'started_at': started_at,
                            'finished_at': finished_at,
                            'elapsed_sec': round(time.time() - started_ts, 3),
                            'downloaded_files': total,
                            'configs_total': len(sp_configs),
                            'configs_processed': processed,
                            'configs_failed': failed,
                            'configs_skipped': skipped,
                            'errors': errors[:20],
                            'message': sync_status['sharepoint'].get('last_result', ''),
                        })
                threading.Thread(target=_do_sp, daemon=True).start()

        if target in ("all", "gdrive") and not sync_status['gdrive']['running']:
            from sp_sync.gdrive import is_authenticated
            if is_authenticated():
                gd_configs = load_gdrive_configs()
                if gd_configs:
                    def _do_gd():
                        sync_status['gdrive']['running'] = True
                        set_sync_active(True, service="gdrive", queue_total=len(gd_configs))
                        started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        started_ts = time.time()
                        total = 0
                        errors = []
                        processed = 0
                        failed = 0
                        try:
                            from sp_sync.gdrive import start_gdrive_sync
                            for c in gd_configs:
                                processed += 1
                                try:
                                    total += int(start_gdrive_sync(c['folder_url'], c['local_path'], log_callback=lambda m: add_log('gdrive', m)) or 0)
                                except Exception as cfg_exc:
                                    failed += 1
                                    errors.append(str(cfg_exc))
                        except Exception as exc:
                            errors.append(str(exc))
                            failed = max(1, failed)
                        finally:
                            sync_status['gdrive']['running'] = False
                            set_sync_active(False)
                            finished_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            _save_sync_report('gdrive', {
                                'service': 'gdrive',
                                'trigger': 'scheduler',
                                'status': 'success' if not errors else ('partial' if processed > failed else 'error'),
                                'started_at': started_at,
                                'finished_at': finished_at,
                                'elapsed_sec': round(time.time() - started_ts, 3),
                                'downloaded_files': total,
                                'configs_total': len(gd_configs),
                                'configs_processed': processed,
                                'configs_failed': failed,
                                'configs_skipped': 0,
                                'errors': errors[:20],
                                'message': sync_status['gdrive'].get('last_result', ''),
                            })
                    threading.Thread(target=_do_gd, daemon=True).start()
    except Exception as exc:
        add_log('system', f"❌ Scheduled sync error: {exc}")


# Start scheduler daemon
try:
    start_scheduler_daemon(_trigger_scheduled_sync)
except Exception as _exc:
    pass


if __name__ == "__main__":
    from sp_sync.single_instance import acquire_app_lock, release_app_lock

    host = os.environ.get("SP_SYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("SP_SYNC_PORT", os.environ.get("PORT", "5000")))
    ok, msg = acquire_app_lock(host=host, port=port)
    if not ok:
        print(f"❌ {msg}")
        raise SystemExit(1)
    print("=" * 50)
    print(f"  Unified Sync Tool — http://{host}:{port}")
    print(f"  {msg}")
    print("  Tip: run from repo root with: python app.py")
    print("=" * 50)
    try:
        app.run(host=host, port=port, debug=False)
    finally:
        release_app_lock()
