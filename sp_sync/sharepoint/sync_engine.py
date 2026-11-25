"""SharePoint Sync — Hybrid REST + Lazy Playwright SourceBuffer Capture Engine.

Uses SharePoint REST / v2.0 Shares API for listing and direct downloads, and lazily launches
Playwright with a memory-bounded SourceBuffer hook only when protected DASH/SEA video streams
cannot be downloaded directly via REST.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import warnings
from typing import Any, Callable, Dict, Optional, Tuple

import requests

# Force UTF-8 via env
os.environ["PYTHONIOENCODING"] = "utf-8"
warnings.filterwarnings("ignore", message="Unable to find acceptable character detection dependency")

_logger = logging.getLogger(__name__)

from sp_sync.config.settings import default_guest_share_link, duration_tolerance
from sp_sync.db.store import get_store
from sp_sync.media.ffmpeg_utils import (
    _duration_outside_tolerance as _media_duration_outside_tolerance,
    _ffmpeg_stderr_tail,
    _ffprobe_duration_seconds,
    _is_valid_mp4,
    _resolve_ffprobe_path,
    _ts_to_mp4_part_cmd,
    _verify_output_duration as _media_verify_output_duration,
    resolve_ffmpeg_path,
)
from sp_sync.paths import project_root
from sp_sync.sharepoint.session_client import (
    _sharepoint_cookie_host as _client_cookie_host,
    _sp_share_requests_session as _client_share_session,
    apply_sharepoint_chromium_security_headers,
    apply_sharepoint_page_digest,
    attach_sharepoint_browser_rest_hints,
    extract_request_digest_from_html,
    stream_get_with_retry,
    try_attach_sharepoint_form_digest,
)
from sp_sync.sharepoint.url_resolver import (
    _sanitize_dir_name,
    _sp_body_indicates_sharing_link_removed,
    _sp_ensure_browse_folder,
    _sp_extract_onedrive_id_param,
    _sp_extract_server_rel_from_sharepoint_html,
    _sp_folder_rest_ok,
    _sp_looks_like_onedrive_sharing_stub,
    _sp_resolve_normalize_input,
    _sp_resolve_via_v2_share_driveitem,
    _sp_server_rel_from_personal_browser_path,
    _sp_sharing_url_has_stream_path,
    _sp_split_path_and_query,
    _sp_unquote_path_value,
    _sp_url_leaf_segment,
)
from sp_sync.sync_metrics import (
    complete_file_progress,
    start_file_progress,
    update_file_progress,
)

ROOT = project_root()
TMP_DIR = os.path.join(ROOT, "data", "tmp_segments")

# Extensions that may need browser capture (encrypted DASH / SEA); others use REST /$value
VIDEO_EXTENSIONS = {".mp4", ".webm", ".m4v", ".mov", ".mkv"}

try:
    from sp_sync.security.cookies import CookieValidator

    SECURE_STORAGE_AVAILABLE = True
except ImportError:
    SECURE_STORAGE_AVAILABLE = False
    _logger.warning("CookieValidator not available, skipping advanced cookie validation")


def _coerce_int(raw: Any, default: int = 0) -> int:
    if isinstance(raw, bool) or raw is None:
        return default
    if isinstance(raw, int):
        return raw
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


# Thin wrappers so existing unittest.mock.patch targets on sp_sync.sharepoint.sync_engine.* work 100%.
def _duration_outside_tolerance(actual, expected, pct=None, abs_sec=None):
    return _media_duration_outside_tolerance(
        actual, expected, pct=pct, abs_sec=abs_sec, tolerance_fn=duration_tolerance
    )


def _verify_output_duration(file_path, expected_duration, log=print, ffmpeg_path=None):
    return _media_verify_output_duration(
        file_path,
        expected_duration,
        log=log,
        ffmpeg_path=ffmpeg_path,
        resolve_ffprobe_fn=_resolve_ffprobe_path,
        ffprobe_duration_fn=_ffprobe_duration_seconds,
        tolerance_fn=duration_tolerance,
    )


def _log_final_mux_failure(res_final, res_mux, log=print):
    """Log the failing ffmpeg step. Prefer cmd_final (res_final) over step-1 res_mux."""
    if res_final is not None:
        rc = res_final.returncode
        log(f"    ❌ Final mux failed (exit {rc})")
        tail = _ffmpeg_stderr_tail(getattr(res_final, "stderr", None))
        if tail:
            log(f"    DEBUG res_final stderr: {tail}")
        elif rc == 0:
            log("    DEBUG res_final: process exited 0 but output failed validation")
        return rc
    rc = res_mux.returncode if res_mux is not None else -1
    log(f"    ❌ Final mux failed (exit {rc})")
    if res_mux is not None:
        tail = _ffmpeg_stderr_tail(getattr(res_mux, "stderr", None))
        if tail:
            log(f"    DEBUG res_mux stderr: {tail}")
    return rc


def _remove_empty_dir(path: str) -> None:
    try:
        if path and os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
    except OSError:
        pass


def _attempt_mux_captured_tracks(ff, v_file, a_file, output_path, log=print, tmp_dir=None):
    """Single mux attempt. Never deletes ``v_file``/``a_file`` on failure."""
    work_dir = tmp_dir or os.path.dirname(v_file) or TMP_DIR
    os.makedirs(work_dir, exist_ok=True)
    ts_output = os.path.join(work_dir, "merged.ts")
    if os.path.exists(ts_output):
        try:
            os.remove(ts_output)
        except OSError:
            pass

    cmd_mux = [
        ff,
        "-y",
        "-i",
        v_file,
        "-i",
        a_file,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-c:v",
        "copy",
        "-c:a:0",
        "aac",
        "-f",
        "mpegts",
        ts_output,
    ]
    res_mux = subprocess.run(cmd_mux, capture_output=True, text=True, timeout=600)

    if res_mux.returncode != 0:
        log("    🔄 Retrying mux without audio...")
        cmd_v_only = [ff, "-y", "-i", v_file, "-c:v", "copy", "-f", "mpegts", ts_output]
        res_mux = subprocess.run(cmd_v_only, capture_output=True, text=True, timeout=300)

    res_final = None
    part_path = output_path + ".part"
    if res_mux.returncode == 0 and os.path.exists(ts_output):
        cmd_final = _ts_to_mp4_part_cmd(ff, ts_output, part_path)
        res_final = subprocess.run(cmd_final, capture_output=True, text=True, timeout=300)

        ok = (
            res_final.returncode == 0
            and os.path.exists(part_path)
            and os.path.getsize(part_path) > 1024
            and _is_valid_mp4(part_path)
        )
        duration = None
        probe = _resolve_ffprobe_path(ff)
        if ok and probe:
            duration = _ffprobe_duration_seconds(part_path, probe)
            if duration is None:
                ok = False
                log("    ❌ ffprobe could not read duration of muxed MP4")

        if ok:
            os.replace(part_path, output_path)
            size = os.path.getsize(output_path)
            for tmp in (v_file, a_file, ts_output):
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            if duration is not None:
                log(f"    ✅ Muxed MP4 duration: {duration:.1f}s")
            return True, size, res_final, res_mux, ts_output, part_path

    return False, 0, res_final, res_mux, ts_output, part_path


def _mux_captured_tracks(ff, v_file, a_file, output_path, log=print, tmp_dir=None):
    """Mux captured fMP4 video/audio tracks into ``output_path`` (retries once on failure)."""
    ok, size, res_final, res_mux, ts_output, part_path = _attempt_mux_captured_tracks(
        ff, v_file, a_file, output_path, log=log, tmp_dir=tmp_dir
    )
    if ok:
        return True, size

    if os.path.exists(v_file) and os.path.getsize(v_file) >= 100:
        log("    🔁 Mux failed — keeping capture temps and retrying mux once...")
        ok, size, res_final, res_mux, ts_output, part_path = _attempt_mux_captured_tracks(
            ff, v_file, a_file, output_path, log=log, tmp_dir=tmp_dir
        )
        if ok:
            log("    ✅ Mux succeeded on retry")
            return True, size

    _log_final_mux_failure(res_final, res_mux, log=log)
    kept = [p for p in (v_file, a_file, ts_output, part_path) if os.path.exists(p)]
    if kept:
        log("    📁 Kept temp files for recovery: " + ", ".join(kept))
    else:
        log("    📁 No temp files remained to keep")
    return False, 0


def _log_playwright_chromium_missing(log):
    """User-friendly hint when Playwright browsers are not installed or OS unsupported."""
    py = sys.executable or "python3"
    log("Chromium for Playwright was not found.")
    log("On most systems install the bundled browser:")
    log(f"   {py} -m playwright install chromium")
    log("On Ubuntu 26.04 you may see: Playwright does not support chromium — install system Chromium:")
    log("   sudo apt install chromium (or chromium-browser)")
    log("Then set the executable (optional if auto-detected):")
    log("   export PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium")
    log("Restart the app and try sync again.")


def _find_system_chromium_executable():
    """Prefer env override, then common distro paths (Linux / WSL)."""
    candidates = [
        os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip(),
        os.environ.get("CHROMIUM_PATH", "").strip(),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/snap/bin/chromium",
    ]
    for p in candidates:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def get_sp_site_url() -> str:
    """Site root URL from SQLite (set via web UI / explorer); empty until configured."""
    try:
        u = get_store().get_sharepoint_site_url().strip()
        if u:
            return u.rstrip("/")
    except Exception:
        pass
    return ""


def get_default_guest_link() -> str:
    g = default_guest_share_link().strip()
    return g if g.startswith("http") else ""


def load_guest_link() -> str:
    """Guest share URL used to seed browser session before REST calls."""
    line = get_store().get_text("sharepoint_guest_link", "").strip()
    if line.startswith("http"):
        return line
    path = os.path.join(ROOT, ".sp_guest_link")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                line = f.readline().strip()
            if line.startswith("http"):
                return line
        except OSError:
            pass
    env = os.environ.get("SP_GUEST_LINK", "").strip()
    if env.startswith("http"):
        return env
    g = get_default_guest_link()
    if g.startswith("http"):
        return g
    return ""


def _sharepoint_cookie_host(sp_site_url: Optional[str]) -> str:
    return _client_cookie_host(sp_site_url or "", get_sp_site_url())


def _sp_share_requests_session(sp_site_url, fedauth_cookie, rtfa_cookie, accept_html=False):
    return _client_share_session(
        sp_site_url,
        fedauth_cookie,
        rtfa_cookie,
        accept_html=accept_html,
        fallback_site_url=get_sp_site_url(),
    )


def prime_sharepoint_rest_session(session, sp_site_url, extra_get_url=None):
    """Load guest / OneDrive HTML pages so Set-Cookie merges into session before REST /_api calls."""
    html_headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    try:
        gl = load_guest_link()
        if gl and str(gl).startswith("http"):
            session.get(gl, timeout=45, allow_redirects=True, headers=html_headers)
        pu = (extra_get_url or "").strip()
        if pu.startswith("http") and "sharepoint.com" in pu.lower():
            h1 = urllib.parse.urlparse(pu).netloc.lower()
            h2 = urllib.parse.urlparse(sp_site_url or get_sp_site_url()).netloc.lower()
            if h1 == h2:
                session.get(pu, timeout=60, allow_redirects=True, headers=html_headers)
    except Exception as exc:
        _logger.debug("REST session prime warning: %s", exc)


def prime_sharepoint_session_for_browse(
    session,
    sp_site_url: str,
    *,
    prime_url: Optional[str] = None,
    fedauth_cookie: Optional[str] = None,
    rtfa_cookie: Optional[str] = None,
) -> None:
    """Full warm-up for REST browse: HTML prime, browser-like headers, page digest, context digest fallback."""
    prime_sharepoint_rest_session(session, sp_site_url, extra_get_url=prime_url)
    attach_sharepoint_browser_rest_hints(session, sp_site_url, referer_url=prime_url)
    apply_sharepoint_chromium_security_headers(session, sp_site_url)
    pu = (prime_url or "").strip()
    if pu.startswith("http") and "sharepoint.com" in pu.lower():
        apply_sharepoint_page_digest(session, pu)
    else:
        apply_sharepoint_page_digest(session, (sp_site_url or "").strip().rstrip("/") + "/")
    if not (session.headers.get("X-RequestDigest") or "").strip():
        try_attach_sharepoint_form_digest(session, sp_site_url)
    if fedauth_cookie and rtfa_cookie:
        try:
            host = urllib.parse.urlparse(sp_site_url or "").netloc.lower()
            if host.endswith(".sharepoint.com"):
                session.cookies.set("FedAuth", fedauth_cookie, domain=".sharepoint.com", path="/")
                session.cookies.set("rtFa", rtfa_cookie, domain=".sharepoint.com", path="/")
        except Exception:
            pass


def _playwright_page_is_login_wall(page) -> bool:
    """True when Playwright landed on Microsoft sign-in instead of SharePoint content."""
    try:
        url = (page.url or "").lower()
        if "login.microsoftonline.com" in url or "login.live.com" in url:
            return True
        title = (page.title() or "").lower()
        if "sign in" in title or "تسجيل الدخول" in title:
            return True
        body = page.content()[:8000].lower()
        if "login.microsoftonline.com" in body and "fedauth" not in body[:4000]:
            return True
    except Exception:
        pass
    return False


def _merge_playwright_cookies_into_session(context, session, log):
    """Copy cookies from Playwright context into requests.Session (after opening guest link)."""
    merged = 0
    try:
        for c in context.cookies():
            name = c.get("name")
            if not name:
                continue
            value = c.get("value", "")
            domain = (c.get("domain") or "").lstrip(".").strip()
            path = c.get("path") or "/"
            if not domain:
                continue
            try:
                session.cookies.set(name, value, domain=domain, path=path)
                merged += 1
            except Exception as ex:
                log(f"  (skip cookie {name}): {ex}")
    except Exception as e:
        log(f"  ⚠️ Could not merge all browser cookies: {e}")
    if merged:
        log(f"  🔄 Merged {merged} browser cookie(s) into REST session")


# Resolve ffmpeg quietly once at import (keep compatibility with FFMPEG_PATH callers)
resolve_ffmpeg_path(log=lambda *_args, **_kwargs: None)

# Optimized SourceBuffer capture hook: fast chunked Base64 encoding (32 KB slices)
# without O(N) per-byte JS array allocations; Python flush nulls flushed entries to bound RAM.
HOOK_JS = """
window.__capturedBuffers = { video: [], audio: [] };
window.__captureReady = false;
function __u8ToBase64(arr) {
    const CHUNK = 0x8000;
    const parts = [];
    for (let i = 0; i < arr.length; i += CHUNK) {
        parts.push(String.fromCharCode.apply(null, arr.subarray(i, i + CHUNK)));
    }
    return btoa(parts.join(''));
}
const origAddSourceBuffer = MediaSource.prototype.addSourceBuffer;
MediaSource.prototype.addSourceBuffer = function(mimeType) {
    const sb = origAddSourceBuffer.call(this, mimeType);
    const trackType = mimeType.includes('video') ? 'video' : 'audio';
    const origAppend = sb.appendBuffer.bind(sb);
    sb.appendBuffer = function(data) {
        try {
            const arr = data instanceof Uint8Array
                ? data
                : (ArrayBuffer.isView(data)
                    ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength)
                    : new Uint8Array(data));
            window.__capturedBuffers[trackType].push({ size: arr.length, b64: __u8ToBase64(arr) });
        } catch (_err) {}
        return origAppend(data);
    };
    return sb;
};
window.__captureReady = true;
"""


def get_cookies_secure(log=print):
    """Load SharePoint cookies from SQLite."""
    try:
        c = get_store().get_sharepoint_cookies()
        fed = c.get("FedAuth") or ""
        rt = c.get("rtFa") or ""
        if fed:
            log("🔐 Loaded cookies from local SQLite")
            return fed, rt
        log("⚠️ No cookies in storage")
        return None, None
    except Exception as e:
        log(f"⚠️ Error reading cookies: {e}")
        return None, None


def get_cookies_legacy(log=print):
    """Deprecated alias kept for compatibility."""
    return get_cookies_secure(log)


def validate_cookies_before_sync(fedauth, rtfa, log=print):
    """Validate cookies before starting sync operations."""
    if not fedauth or not rtfa:
        log("❌ Cookies missing")
        return False

    if SECURE_STORAGE_AVAILABLE:
        try:
            validator = CookieValidator()
            result = validator.validate_cookies(fedauth, rtfa)
            if not result["valid"]:
                log("⚠️ Cookie validation failed:")
                for issue in result["issues"]:
                    log(f"   - {issue}")
                log("🔧 Recommendations:")
                for rec in result["recommendations"]:
                    log(f"   - {rec}")
                return False
            log("✅ Cookies look valid")
            return True
        except Exception as e:
            log(f"⚠️ Cookie validation error: {e}")
            return True
    log("✅ Basic cookie check passed")
    return True


def retry_with_backoff(
    func,
    max_retries=3,
    base_delay=1,
    max_delay=60,
    exceptions=(Exception,),
    log=print,
    *args,
    **kwargs,
):
    """Execute function with exponential backoff retry mechanism. Returns (success, result)."""
    for attempt in range(max_retries + 1):
        try:
            result = func(*args, **kwargs)
            if attempt > 0:
                log(f"✅ Succeeded on attempt {attempt + 1}")
            return True, result
        except exceptions as e:
            if attempt == max_retries:
                log(f"❌ All attempts failed ({max_retries + 1}). Last error: {e}")
                return False, e
            delay = min(base_delay * (2 ** attempt) + (attempt * 0.1), max_delay)
            log(f"⚠️ Attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)


# --- Playwright Context Manager (thread-safe lazy singleton) ---
_pw_lock = threading.RLock()
_pw_instance = None


def _get_playwright_page(
    fedauth_cookie=None,
    rtfa_cookie=None,
    sp_site_url=None,
    guest_link_url=None,
    session=None,
    log=print,
):
    """Create or reuse Playwright browser: inject FedAuth+rtFa, warm session, merge cookies."""
    global _pw_instance
    with _pw_lock:
        if _pw_instance and _pw_instance.get("page"):
            try:
                _pw_instance["page"].evaluate("() => true")
                if session is not None:
                    _merge_playwright_cookies_into_session(_pw_instance["context"], session, log)
                return _pw_instance["page"]
            except Exception:
                _cleanup_playwright()

        host = _sharepoint_cookie_host(sp_site_url)
        guest_link_url = (guest_link_url or load_guest_link() or "").strip()
        if guest_link_url and not _sp_sharing_url_has_stream_path(guest_link_url):
            if guest_link_url.rstrip("/") == (sp_site_url or get_sp_site_url()).rstrip("/"):
                guest_link_url = ""

        log("🌐 Launching browser (needed for protected video / session warm-up)...")
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = None
        launch_err = None
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as e:
            launch_err = e
            sys_exe = _find_system_chromium_executable()
            if sys_exe:
                log(f"  🔄 Using system Chromium: {sys_exe}")
                try:
                    browser = pw.chromium.launch(
                        headless=True,
                        executable_path=sys_exe,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                except Exception as e2:
                    log(f"  ❌ Failed to launch system Chromium: {e2}")
                    launch_err = e2
            if browser is None:
                try:
                    pw.stop()
                except Exception:
                    pass
                err = (str(launch_err) or "").lower()
                if "executable doesn't exist" in err or "playwright install" in err or "does not support" in err:
                    _log_playwright_chromium_missing(log)
                else:
                    log(f"❌ Failed to launch Chromium: {launch_err}")
                return None

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            )
        )
        context.add_init_script(HOOK_JS)

        cookies_to_add = []
        if fedauth_cookie:
            cookies_to_add.append({"name": "FedAuth", "value": fedauth_cookie, "domain": host, "path": "/"})
        if rtfa_cookie:
            cookies_to_add.append({"name": "rtFa", "value": rtfa_cookie, "domain": host, "path": "/"})
        if cookies_to_add:
            context.add_cookies(cookies_to_add)
            log(f"  🍪 Injected FedAuth/rtFa for {host}")

        page = context.new_page()

        log("🔗 Warming browser session...")
        session_ok = False
        if guest_link_url.startswith("http"):
            for attempt in range(2):
                try:
                    page.goto(guest_link_url, wait_until="networkidle", timeout=90000)
                    page.wait_for_timeout(3000)
                    if _playwright_page_is_login_wall(page):
                        log(
                            f"  ⚠️ Guest link opened sign-in page (attempt {attempt + 1}/2). "
                            "Refresh FedAuth/rtFa or paste a valid :f:/ sharing link."
                        )
                        continue
                    title = page.title()
                    if title and "error" not in title.lower():
                        log(f"  ✅ Connected via guest link: {title[:80]}")
                        session_ok = True
                        break
                    log(f"  ⚠️ Guest link attempt {attempt + 1}/2: ({title})")
                except Exception as e:
                    log(f"  ⚠️ Guest link attempt {attempt + 1}/2: {e}")

        if not session_ok and fedauth_cookie:
            log("  🔄 Trying direct OneDrive connection...")
            try:
                base = (sp_site_url or get_sp_site_url()).rstrip("/")
                page.goto(f"{base}/_layouts/15/onedrive.aspx", wait_until="networkidle", timeout=90000)
                page.wait_for_timeout(3000)
                if _playwright_page_is_login_wall(page):
                    log("  ❌ Direct OneDrive connection still on sign-in page — update cookies")
                else:
                    log(f"  ✅ Direct connection OK: {page.title()[:80]}")
                    session_ok = True
            except Exception as e:
                log(f"  ❌ Direct connection failed: {e}")

        if not session_ok:
            log("  ⚠️ Browser session not fully warmed; continuing anyway.")

        if session is not None:
            _merge_playwright_cookies_into_session(context, session, log)

        _pw_instance = {"pw": pw, "browser": browser, "context": context, "page": page}
        return page


def _cleanup_playwright():
    """Clean up Playwright resources safely."""
    global _pw_instance
    with _pw_lock:
        if _pw_instance:
            try:
                _pw_instance["browser"].close()
            except Exception:
                pass
            try:
                _pw_instance["pw"].stop()
            except Exception:
                pass
            _pw_instance = None


def _capture_work_dir(output_path: str) -> str:
    """Per-output temp directory so interrupted captures can resume independently."""
    abs_out = os.path.abspath(output_path)
    digest = hashlib.sha1(abs_out.encode("utf-8")).hexdigest()[:12]
    stem = _sanitize_dir_name(os.path.splitext(os.path.basename(output_path))[0])[:48]
    return os.path.join(TMP_DIR, f"{stem}_{digest}")


def _progress_meta_path(work_dir: str) -> str:
    return os.path.join(work_dir, "capture_progress.json")


def _load_capture_progress(work_dir: str) -> Optional[dict]:
    path = _progress_meta_path(work_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _save_capture_progress(work_dir: str, meta: dict) -> None:
    os.makedirs(work_dir, exist_ok=True)
    path = _progress_meta_path(work_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, path)


def _clear_capture_progress(work_dir: str) -> None:
    path = _progress_meta_path(work_dir)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    _remove_empty_dir(work_dir)


def _try_promote_part_file(output_path: str, log=print) -> Optional[int]:
    """If ``output_path.part`` is a valid MP4, promote it to ``output_path``."""
    part_path = output_path + ".part"
    if not os.path.isfile(part_path):
        return None
    if not _is_valid_mp4(part_path) or os.path.getsize(part_path) < 1024:
        return None
    probe = _resolve_ffprobe_path()
    if probe and _ffprobe_duration_seconds(part_path, probe) is None:
        return None
    os.replace(part_path, output_path)
    size = os.path.getsize(output_path)
    log(f"    ♻️ Resumed incomplete download: promoted .part ({size/1024/1024:.1f} MB)")
    return size


def _flush_capture_buffers(page, v_file: str, a_file: str, cursors: dict, log=print) -> dict:
    """Append newly captured SourceBuffer chunks to disk and release browser RAM immediately."""
    written = {"video": 0, "audio": 0}
    for track, track_file in (("video", v_file), ("audio", a_file)):
        try:
            num = page.evaluate(f"() => window.__capturedBuffers.{track}.length")
        except Exception as exc:
            log(f"    ⚠️ Buffer length error ({track}): {exc}")
            continue
        start = int(cursors.get(track, 0) or 0)
        if not num or num <= start:
            continue
        batch_size = 50
        total_bytes = 0
        os.makedirs(os.path.dirname(track_file) or ".", exist_ok=True)
        with open(track_file, "ab") as f_out:
            for start_idx in range(start, num, batch_size):
                end_idx = min(start_idx + batch_size, num)
                try:
                    # Extract base64 strings AND immediately null flushed slots to prevent Chromium OOM!
                    batch = page.evaluate(
                        """([t, s, e]) => {
                            const buf = window.__capturedBuffers[t];
                            const out = [];
                            for (let i = s; i < e; i++) {
                                const item = buf[i];
                                if (item && item.b64) out.push(item.b64);
                                buf[i] = null;
                            }
                            return out;
                        }""",
                        [track, start_idx, end_idx],
                    )
                    for b64_data in batch:
                        raw = base64.b64decode(b64_data)
                        f_out.write(raw)
                        total_bytes += len(raw)
                except Exception as e:
                    log(f"    ⚠️ Buffer extract error near batch {start_idx}: {e}")
        cursors[track] = num
        written[track] = total_bytes
    return written


def _try_resume_from_existing_capture(output_path: str, log=print, ffmpeg_path=None):
    """Reuse an existing .part or completed capture temps. Returns (ok, size) or None."""
    promoted = _try_promote_part_file(output_path, log=log)
    if promoted is not None:
        return True, promoted

    work_dir = _capture_work_dir(output_path)
    v_file = os.path.join(work_dir, "video.fmp4")
    a_file = os.path.join(work_dir, "audio.fmp4")
    meta = _load_capture_progress(work_dir) or {}
    if not (os.path.exists(v_file) and os.path.getsize(v_file) >= 100):
        return None
    if not meta.get("capture_complete") and not meta.get("force_mux"):
        return None

    ff = ffmpeg_path or resolve_ffmpeg_path(log)
    if not ff or not os.path.isfile(ff):
        log("    ❌ ffmpeg not available — cannot remux resumed capture")
        return False, 0
    log("    ♻️ Resuming: remuxing previously captured tracks (skipping browser capture)...")
    ok, size = _mux_captured_tracks(ff, v_file, a_file, output_path, log=log, tmp_dir=work_dir)
    if not ok:
        return False, 0
    expected = meta.get("duration")
    verified, _actual = _verify_output_duration(output_path, expected, log=log, ffmpeg_path=ff)
    if not verified:
        log("    ⚠️ Resumed mux failed duration check; discarding output for re-download")
        try:
            os.remove(output_path)
        except OSError:
            pass
        _clear_capture_progress(work_dir)
        for tmp in (v_file, a_file):
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        _remove_empty_dir(work_dir)
        return False, 0
    _clear_capture_progress(work_dir)
    return True, size


def download_video_via_capture(
    page,
    file_rel_url,
    output_path,
    log=print,
    sp_site_url=None,
    *,
    cancel_event: Optional[threading.Event] = None,
    max_recovery_attempts: int = 6,
):
    """Download a video by hooking SourceBuffer with bounded RAM, deadlock protection, and cancellation."""
    if cancel_event and cancel_event.is_set():
        return False, 0

    os.makedirs(TMP_DIR, exist_ok=True)
    work_dir = _capture_work_dir(output_path)
    os.makedirs(work_dir, exist_ok=True)
    v_file = os.path.join(work_dir, "video.fmp4")
    a_file = os.path.join(work_dir, "audio.fmp4")

    resumed = _try_resume_from_existing_capture(output_path, log=log)
    if resumed is not None:
        return resumed

    meta = _load_capture_progress(work_dir) or {}
    resume_from = float(meta.get("last_time") or 0)
    reuse_temps = (
        resume_from > 5
        and os.path.exists(v_file)
        and os.path.getsize(v_file) >= 100
        and not meta.get("capture_complete")
    )

    base = (sp_site_url or get_sp_site_url()).rstrip("/")
    file_viewer_url = (
        f"{base}/_layouts/15/onedrive.aspx"
        f"?id={urllib.parse.quote(file_rel_url)}"
        f"&parent={urllib.parse.quote(os.path.dirname(file_rel_url))}"
    )

    def load_video_page():
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("SharePoint sync cancelled by user")
        try:
            page.goto(file_viewer_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(8000)
            return True
        except Exception as e:
            log(f"  ⚠️ First page load failed: {e}")
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("SharePoint sync cancelled by user")
            try:
                page.goto(file_viewer_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(8000)
                return True
            except Exception as e2:
                log(f"  ❌ Second page load failed: {e2}")
                raise e2

    success, _ = retry_with_backoff(load_video_page, max_retries=2, base_delay=2, log=log)
    if not success:
        log(f"  ❌ Failed to load video page: {file_rel_url}")
        return False, 0

    ready = page.evaluate("() => window.__captureReady === true")
    if not ready:
        page.wait_for_timeout(5000)
        ready = page.evaluate("() => window.__captureReady === true")
        if not ready:
            log("    ⚠️ Failed to inject browser hook")
            return False, 0

    dur = 0.0
    for _wait in range(15):
        if cancel_event and cancel_event.is_set():
            log("    🛑 Video capture cancelled before start.")
            return False, 0
        try:
            dur = float(
                page.evaluate("() => { const v = document.querySelector('video'); return v ? (v.duration || 0) : 0; }")
                or 0
            )
            if dur > 0 and dur != float("inf"):
                log(f"    📺 Video detected: {dur:.0f}s")
                break
            page.wait_for_timeout(2000)
        except Exception as e:
            log(f"    ⚠️ Video check error: {e}")
            page.wait_for_timeout(2000)

    counts = page.evaluate("() => ({ v: window.__capturedBuffers.video.length, a: window.__capturedBuffers.audio.length })")
    log(f"    📡 Hook active. Initial buffers: V={counts['v']} A={counts['a']}")

    if not dur or dur <= 0:
        log("    ❌ Video player not found after several tries")
        return False, 0

    cursors = {"video": 0, "audio": 0}
    if reuse_temps:
        log(
            f"    ♻️ Resuming capture from ~{resume_from:.0f}s "
            f"(reusing {os.path.getsize(v_file)/1024/1024:.1f} MB video temp)"
        )
        seek_to = min(resume_from, max(0, dur - 1))
        page.evaluate(
            """(t) => {
                const v = document.querySelector('video');
                if (!v) return;
                v.currentTime = t;
                v.playbackRate = 16.0;
                v.muted = true;
                v.play();
            }""",
            seek_to,
        )
        last_time = seek_to
    else:
        for f in (v_file, a_file):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        _clear_capture_progress(work_dir)
        os.makedirs(work_dir, exist_ok=True)
        resume_from = 0.0
        log(f"    ⏱️ Duration: {dur:.0f}s ({dur/60:.1f} min). Capturing at 16x...")
        page.evaluate(
            "() => { const v = document.querySelector('video'); if(v) { v.playbackRate = 16.0; v.muted = true; v.play(); } }"
        )
        last_time = 0.0

    stuck_ticks = 0
    recovery_attempts = 0
    start_time = time.time()
    max_capture_seconds = max(900.0, dur * 1.6)
    last_log_time = start_time
    last_flush_time = start_time
    aborted = False

    while True:
        if cancel_event:
            if cancel_event.wait(2.0):
                log("    🛑 Video capture cancelled by user (flushing partial buffers for future resume)...")
                _flush_capture_buffers(page, v_file, a_file, cursors, log=log)
                _save_capture_progress(
                    work_dir,
                    {
                        "output_path": os.path.abspath(output_path),
                        "last_time": float(last_time or 0),
                        "duration": float(dur or 0),
                        "capture_complete": False,
                        "video_bytes": os.path.getsize(v_file) if os.path.exists(v_file) else 0,
                        "audio_bytes": os.path.getsize(a_file) if os.path.exists(a_file) else 0,
                    },
                )
                return False, 0
        else:
            time.sleep(2.0)

        try:
            state = page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    return v ? { time: v.currentTime || 0, ended: !!v.ended, paused: !!v.paused }
                             : { time: 0, ended: true, paused: true };
                }"""
            )
        except Exception as exc:
            log(f"    ❌ Browser evaluation lost during capture: {exc}")
            aborted = True
            break

        now = time.time()
        current_time = float(state.get("time") or 0)
        progress_pct = min(99, int((current_time / dur) * 100)) if dur > 0 else 0

        # Update central live metrics during browser capture so the UI progress bar moves smoothly!
        v_bytes = os.path.getsize(v_file) if os.path.exists(v_file) else 0
        a_bytes = os.path.getsize(a_file) if os.path.exists(a_file) else 0
        flushed_bytes = v_bytes + a_bytes
        if flushed_bytes > 0 and current_time > 1:
            est_total = max(flushed_bytes, int(flushed_bytes / max(0.01, current_time / dur)))
            update_file_progress(flushed_bytes, est_total, service="sharepoint", percent_override=progress_pct)
        else:
            update_file_progress(int(current_time), int(dur), service="sharepoint", percent_override=progress_pct)

        if now - last_log_time >= 10:
            elapsed = now - start_time
            buffer_counts = page.evaluate(
                "() => ({ v: window.__capturedBuffers.video.length, a: window.__capturedBuffers.audio.length })"
            )
            log(
                f"    📊 Progress: {progress_pct:.1f}% | time: {current_time:.0f}/{dur:.0f}s "
                f"| buffers V={buffer_counts['v']} A={buffer_counts['a']} | elapsed: {elapsed:.0f}s"
            )
            last_log_time = now

        # Flush buffers every 20s (keeps Chromium RAM low and preserves resumable progress)
        if now - last_flush_time >= 20 or state["ended"]:
            _flush_capture_buffers(page, v_file, a_file, cursors, log=log)
            _save_capture_progress(
                work_dir,
                {
                    "output_path": os.path.abspath(output_path),
                    "last_time": float(current_time or last_time or 0),
                    "duration": float(dur or 0),
                    "capture_complete": bool(state["ended"]),
                    "video_bytes": os.path.getsize(v_file) if os.path.exists(v_file) else 0,
                    "audio_bytes": os.path.getsize(a_file) if os.path.exists(a_file) else 0,
                },
            )
            last_flush_time = now

        if state["ended"]:
            break

        if (now - start_time) > max_capture_seconds:
            log(f"    ❌ Capture timed out after {int(now - start_time)}s (max {int(max_capture_seconds)}s)")
            aborted = True
            break

        if abs(current_time - last_time) < 0.05:
            stuck_ticks += 1
            if stuck_ticks > 15:  # 30 seconds stuck
                recovery_attempts += 1
                if recovery_attempts > max_recovery_attempts:
                    log(
                        f"    ❌ Playback remained frozen after {max_recovery_attempts} recovery attempts; "
                        "aborting capture to avoid infinite hang."
                    )
                    aborted = True
                    break
                log(
                    f"    ⚠️ Playback looks stuck ({recovery_attempts}/{max_recovery_attempts}); "
                    "trying to recover..."
                )
                try:
                    page.evaluate(
                        "() => { const v = document.querySelector('video'); if(v) { v.playbackRate = 16.0; v.play(); } }"
                    )
                except Exception:
                    pass
                stuck_ticks = 0
        else:
            stuck_ticks = 0
            recovery_attempts = 0

        last_time = current_time

    if aborted:
        _flush_capture_buffers(page, v_file, a_file, cursors, log=log)
        _save_capture_progress(
            work_dir,
            {
                "output_path": os.path.abspath(output_path),
                "last_time": float(last_time or 0),
                "duration": float(dur or 0),
                "capture_complete": False,
                "video_bytes": os.path.getsize(v_file) if os.path.exists(v_file) else 0,
                "audio_bytes": os.path.getsize(a_file) if os.path.exists(a_file) else 0,
            },
        )
        try:
            page.evaluate("() => { window.__capturedBuffers = { video: [], audio: [] }; }")
        except Exception:
            pass
        return False, 0

    final = page.evaluate("() => ({ v: window.__capturedBuffers.video.length, a: window.__capturedBuffers.audio.length })")
    log(f"    📦 Totals: V={final['v']} A={final['a']} buffers")

    _flush_capture_buffers(page, v_file, a_file, cursors, log=log)
    _save_capture_progress(
        work_dir,
        {
            "output_path": os.path.abspath(output_path),
            "last_time": float(dur or last_time or 0),
            "duration": float(dur or 0),
            "capture_complete": True,
            "video_bytes": os.path.getsize(v_file) if os.path.exists(v_file) else 0,
            "audio_bytes": os.path.getsize(a_file) if os.path.exists(a_file) else 0,
        },
    )

    for track, track_file in (("video", v_file), ("audio", a_file)):
        if os.path.exists(track_file):
            log(f"    💾 {track}: {os.path.getsize(track_file)/1024/1024:.1f} MB")
        else:
            log(f"    ⚠️ No captured data for track: {track}")

    page.evaluate("() => { window.__capturedBuffers = { video: [], audio: [] }; }")

    if not os.path.exists(v_file) or os.path.getsize(v_file) < 100:
        log("    ❌ Video path empty or invalid")
        return False, 0

    log("    🎬 Muxing video and audio with ffmpeg...")
    ff = resolve_ffmpeg_path(log)
    if not ff or not os.path.isfile(ff):
        log("    ❌ ffmpeg not available — install ffmpeg or set FFMPEG_PATH")
        return False, 0

    ok, size = _mux_captured_tracks(ff, v_file, a_file, output_path, log=log, tmp_dir=work_dir)
    if not ok:
        return False, 0

    verified, _actual = _verify_output_duration(output_path, dur, log=log, ffmpeg_path=ff)
    if not verified:
        log("    ⚠️ Marking download failed due to duration mismatch; will re-download")
        try:
            os.remove(output_path)
        except OSError:
            pass
        _clear_capture_progress(work_dir)
        for tmp in (v_file, a_file):
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        _remove_empty_dir(work_dir)
        return False, 0

    _clear_capture_progress(work_dir)
    return True, size


def resolve_sharepoint_pasted_url(
    fedauth_cookie,
    rtfa_cookie,
    sp_site_url,
    pasted_url,
    log=print,
    *,
    _stub_depth=0,
):
    """Resolve pasted SharePoint/OneDrive URL to a server-relative folder path for /sp/browse."""
    if _stub_depth > 3:
        return {
            "ok": False,
            "browse_parent": None,
            "resolved_target": None,
            "resolved_from": "",
            "final_url": "",
            "error": "Could not resolve URL (retry depth exceeded)",
            "hint": "",
        }

    base = (sp_site_url or get_sp_site_url()).rstrip("/")
    kind, payload = _sp_resolve_normalize_input(pasted_url)
    if kind is None:
        return {
            "ok": False,
            "browse_parent": None,
            "resolved_target": None,
            "resolved_from": "",
            "final_url": "",
            "error": "Empty URL or path",
            "hint": "",
        }

    session = _sp_share_requests_session(sp_site_url, fedauth_cookie, rtfa_cookie, accept_html=False)
    prime_sharepoint_rest_session(session, sp_site_url or get_sp_site_url())

    if kind == "server_path":
        path_only, query = _sp_split_path_and_query(payload)
        path_only = urllib.parse.unquote(path_only.replace("\\", "/").strip())
        if not path_only.startswith("/"):
            path_only = "/" + path_only.lstrip("/")

        if _stub_depth < 2 and _sp_looks_like_onedrive_sharing_stub(path_only):
            host = urllib.parse.urlparse(sp_site_url or get_sp_site_url()).netloc
            rebuilt = f"https://{host}/:f:/g{path_only}"
            if query:
                rebuilt += "?" + query
            log("  ↪ Path looks like a sharing stub (:f:) — rebuilding full URL...")
            return resolve_sharepoint_pasted_url(
                fedauth_cookie,
                rtfa_cookie,
                sp_site_url,
                rebuilt,
                log=log,
                _stub_depth=_stub_depth + 1,
            )

        browse = _sp_ensure_browse_folder(session, base, path_only, log)
        if not browse:
            return {
                "ok": False,
                "browse_parent": None,
                "resolved_target": path_only,
                "resolved_from": "server_path",
                "final_url": "",
                "error": "No listable folder found for this path",
                "hint": "If this came from a sharing link, paste the full https URL or /personal/.../id without ?e=, then use Resolve link on the explorer page.",
            }
        return {
            "ok": True,
            "browse_parent": browse,
            "resolved_target": path_only,
            "resolved_from": "server_path",
            "final_url": "",
            "error": "",
            "hint": "",
        }

    url = payload
    parsed = urllib.parse.urlparse(url)
    if "sharepoint.com" not in (parsed.netloc or "").lower():
        return {
            "ok": False,
            "browse_parent": None,
            "resolved_target": None,
            "resolved_from": "",
            "final_url": url,
            "error": "URL is not a SharePoint domain",
            "hint": "",
        }

    phost = parsed.netloc.lower()
    config_host = urllib.parse.urlparse(sp_site_url or get_sp_site_url()).netloc.lower()
    if phost != config_host:
        return {
            "ok": False,
            "browse_parent": None,
            "resolved_target": None,
            "resolved_from": "",
            "final_url": url,
            "error": "URL host does not match configured site URL",
            "hint": f"Configured host: {config_host}",
        }

    v2_fail_status = None
    if _sp_sharing_url_has_stream_path(url):
        v2_path, v2_st = _sp_resolve_via_v2_share_driveitem(session, url)
        if v2_st is not None and v2_st != 200:
            v2_fail_status = v2_st
        if v2_path:
            id_v2 = v2_path if v2_path.startswith("/") else "/" + v2_path.lstrip("/")
            browse = _sp_ensure_browse_folder(session, base, id_v2, log)
            if browse:
                return {
                    "ok": True,
                    "browse_parent": browse,
                    "resolved_target": id_v2,
                    "resolved_from": "v2_share_driveitem",
                    "final_url": url,
                    "error": "",
                    "hint": "",
                }

    id_early = _sp_extract_onedrive_id_param(url)
    if id_early:
        if not id_early.startswith("/"):
            id_early = "/" + id_early.lstrip("/")
        browse = _sp_ensure_browse_folder(session, base, id_early, log)
        if browse:
            return {
                "ok": True,
                "browse_parent": browse,
                "resolved_target": id_early,
                "resolved_from": "query_id",
                "final_url": url,
                "error": "",
                "hint": "",
            }

    direct = _sp_server_rel_from_personal_browser_path(parsed)
    if direct:
        browse = _sp_ensure_browse_folder(session, base, direct, log)
        if browse:
            return {
                "ok": True,
                "browse_parent": browse,
                "resolved_target": direct,
                "resolved_from": "url_path",
                "final_url": url,
                "error": "",
                "hint": "",
            }

    try:
        r = session.get(
            url,
            allow_redirects=True,
            timeout=90,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
    except requests.RequestException as e:
        return {
            "ok": False,
            "browse_parent": None,
            "resolved_target": None,
            "resolved_from": "",
            "final_url": url,
            "error": f"Request failed: {e}",
            "hint": "Check network and cookies.",
        }
    final = r.url or url
    body = r.text or ""
    urls_chain = []
    for hist in getattr(r, "history", []) or []:
        hu = getattr(hist, "url", None) or ""
        if hu and hu not in urls_chain:
            urls_chain.append(hu)
    if final and final not in urls_chain:
        urls_chain.append(final)

    id_val = None
    for u in urls_chain:
        id_val = _sp_extract_onedrive_id_param(u)
        if id_val:
            final = u
            break

    if not id_val and body:
        m = re.search(r"(?:onedrive\.aspx|embed\.aspx)\?[^\"'>]*\bid=([^&\"'>\s]+)", body, re.I)
        if m:
            id_val = _sp_unquote_path_value(m.group(1).replace("&amp;", "&"))

    if not id_val and body:
        id_val = _sp_extract_server_rel_from_sharepoint_html(body)

    if not id_val:
        for u in reversed(urls_chain):
            sp = _sp_server_rel_from_personal_browser_path(urllib.parse.urlparse(u))
            if sp:
                id_val = sp
                final = u
                break

    if not id_val:
        if v2_fail_status == 404 or _sp_body_indicates_sharing_link_removed(body):
            return {
                "ok": False,
                "browse_parent": None,
                "resolved_target": None,
                "resolved_from": "",
                "final_url": final,
                "error": "Sharing link is invalid, expired, or was removed by the owner",
                "hint": (
                    "Microsoft returned a removed/invalid link (or 404 on shares API). Ask the owner for a new link, "
                    "or open the folder in the browser while signed in and paste the server path (id= / /personal/…)."
                ),
            }
        if v2_fail_status == 403:
            return {
                "ok": False,
                "browse_parent": None,
                "resolved_target": None,
                "resolved_from": "",
                "final_url": final,
                "error": "Could not access this sharing link with the current session (403)",
                "hint": (
                    "Paste FedAuth and rtFa from the same browser where the link opens while you are signed in, "
                    "then try Resolve again. Ensure the configured site URL host matches this link."
                ),
            }
        return {
            "ok": False,
            "browse_parent": None,
            "resolved_target": None,
            "resolved_from": "",
            "final_url": final,
            "error": "Could not extract folder/file path after redirect",
            "hint": (
                "Open the link while signed in, then copy the server path: from the address bar use id=, folder=, or "
                "RootFolder= (decoded path starting with /personal/…), or paste that path directly as a relative path."
            ),
        }

    if not id_val.startswith("/"):
        id_val = "/" + id_val.lstrip("/")

    browse = _sp_ensure_browse_folder(session, base, id_val, log)
    if not browse:
        return {
            "ok": False,
            "browse_parent": None,
            "resolved_target": id_val,
            "resolved_from": "redirect_or_html",
            "final_url": final,
            "error": "Resolved path is not a REST-listable folder",
            "hint": "Try a folder link or copy the server-relative path from the browser.",
        }
    return {
        "ok": True,
        "browse_parent": browse,
        "resolved_target": id_val,
        "resolved_from": "sharing_link",
        "final_url": final,
        "error": "",
        "hint": "",
    }


def _mirror_local_root(download_dir, folder_rel_url, local_flat, log, *, dry_run: bool = False):
    """Create a subfolder matching the cloud folder leaf name unless local_flat is True."""
    if local_flat:
        return download_dir
    leaf = _sanitize_dir_name(_sp_url_leaf_segment(folder_rel_url))
    target = os.path.join(download_dir, leaf)
    if not dry_run:
        os.makedirs(target, exist_ok=True)
    log(f"📂 Local download root (mirrors cloud folder name): {target}")
    return target


def _build_rest_session(fedauth_cookie, rtfa_cookie, sp_site_url):
    cookie_host = _sharepoint_cookie_host(sp_site_url)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json;odata=verbose",
        }
    )
    if fedauth_cookie:
        session.cookies.set("FedAuth", fedauth_cookie, domain=cookie_host, path="/")
    if rtfa_cookie:
        session.cookies.set("rtFa", rtfa_cookie, domain=cookie_host, path="/")
    return session


def _sharepoint_session_and_page(fedauth_cookie, rtfa_cookie, sp_site_url, log, *, lazy_browser: bool = True):
    """Build authenticated REST session and (optionally lazy) Playwright page."""
    session = _build_rest_session(fedauth_cookie, rtfa_cookie, sp_site_url)
    if lazy_browser:
        return session, None
    guest_url = load_guest_link()
    page = _get_playwright_page(
        fedauth_cookie=fedauth_cookie,
        rtfa_cookie=rtfa_cookie,
        sp_site_url=sp_site_url,
        guest_link_url=guest_url,
        session=session,
        log=log,
    )
    if page is None:
        log(
            "⚠️ Continuing with REST only (listing and /$value when allowed). "
            "Protected video needs Chromium after installing Playwright."
        )
    return session, page


def _stream_response_to_file(
    resp: requests.Response,
    part_path: str,
    expected_size: int,
    is_video: bool,
    *,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[bool, int, str]:
    """Stream *resp* into *part_path* with cancellation, length verification, and MP4 check."""
    cl = _coerce_int(resp.headers.get("Content-Length"), 0)
    tot = cl if cl > 0 else max(0, expected_size)
    dl = 0
    try:
        with open(part_path, "wb") as f_out:
            for chunk in resp.iter_content(chunk_size=65536):
                if cancel_event and cancel_event.is_set():
                    return False, dl, "cancelled"
                if not chunk:
                    continue
                f_out.write(chunk)
                dl += len(chunk)
                update_file_progress(dl, tot, service="sharepoint")
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if dl == 0:
        return False, 0, "empty response body"
    if cl > 0 and dl < cl:
        return False, dl, f"truncated stream ({dl}/{cl} bytes)"
    if is_video and not _is_valid_mp4(part_path):
        return False, dl, "downloaded video failed MP4 header validation"
    return True, dl, ""


def _download_one_file(
    session,
    page,
    sp_site_url,
    file_rel_url,
    local_path,
    log,
    allow_non_mp4=False,
    *,
    sharing_link_url: Optional[str] = None,
    item_id: Optional[str] = None,
    share_root_norm: Optional[str] = None,
    remote_size: Any = 0,
    cancel_event: Optional[threading.Event] = None,
    page_factory: Optional[Callable[[], Any]] = None,
):
    """Download one SharePoint file with retries, integrity verification, and lazy Playwright fallback."""
    if cancel_event and cancel_event.is_set():
        return 0

    remote_size_int = _coerce_int(remote_size, 0)
    name = os.path.basename(file_rel_url.rstrip("/"))
    base_site = (sp_site_url or get_sp_site_url()).rstrip("/")
    ext = os.path.splitext(name)[1].lower()
    is_video = ext == ".mp4" or ext in VIDEO_EXTENSIONS

    if os.path.exists(local_path):
        size = os.path.getsize(local_path)
        is_corrupt = False
        if is_video:
            if not _is_valid_mp4(local_path) or size < 1024 * 1024:
                is_corrupt = True
        elif remote_size_int > 0 and size != remote_size_int:
            is_corrupt = True

        if not is_corrupt:
            log(f"[-] Skip '{name}' (exists, {size/1024/1024:.1f} MB)")
            return 0
        log(f"⚠️ Re-downloading '{name}' (corrupted or incomplete size: {size/1024:.0f} KB)")
    else:
        promoted = _try_promote_part_file(local_path, log=log)
        if promoted is not None:
            complete_file_progress(name, service="sharepoint")
            return 1

    if not allow_non_mp4 and not is_video:
        log(f"[-] Skip '{name}' (not video)")
        return 0

    log(f"[+] Downloading '{name}'...")
    parent_folder_name = os.path.basename(os.path.dirname(local_path))
    start_file_progress(parent_folder_name, name, remote_size_int, service="sharepoint")

    def _finish_success(msg_size_path=local_path):
        size_now = os.path.getsize(msg_size_path) if os.path.isfile(msg_size_path) else 0
        if is_video:
            try:
                from sp_sync.sharepoint.silence_trim import trim_silence_edges

                trim_silence_edges(msg_size_path, log=log)
                if os.path.isfile(msg_size_path):
                    size_now = os.path.getsize(msg_size_path)
            except Exception as trim_exc:
                log(f"    ⚠️ Silence trim skipped due to error: {trim_exc}")
        complete_file_progress(name, service="sharepoint")
        return size_now

    from sp_sync.sharepoint.rest_folder_list import (
        _should_try_v2_shares,
        _v2_share_token_from_url,
        encode_path_for_get_folder_param,
    )

    # Strategy 1: v2.0/shares API if sharing link is provided
    if sharing_link_url and _should_try_v2_shares(sharing_link_url):
        part_path = local_path + ".part"
        try:
            p = urllib.parse.urlparse(base_site)
            netloc = (p.netloc or "").lower()
            if netloc and "sharepoint.com" in netloc:
                root = f"{p.scheme}://{netloc}"
                sid = _v2_share_token_from_url(sharing_link_url)
                v2_content_url = None
                if item_id:
                    v2_content_url = (
                        f"{root}/_api/v2.0/shares/{sid}/driveItem/"
                        f"{urllib.parse.quote(str(item_id).strip(), safe='')}/content"
                    )
                elif share_root_norm and file_rel_url.startswith(share_root_norm.rstrip("/") + "/"):
                    rel_under = file_rel_url[len(share_root_norm.rstrip("/")) :].lstrip("/")
                    if rel_under:
                        enc_rel = "/".join(urllib.parse.quote(s, safe="") for s in rel_under.split("/") if s)
                        v2_content_url = f"{root}/_api/v2.0/shares/{sid}/driveItem:/{enc_rel}:/content"

                if v2_content_url:
                    v2_res = stream_get_with_retry(
                        session,
                        v2_content_url,
                        headers={"Accept": "*/*"},
                        timeout=60,
                        allow_redirects=True,
                        retries=2,
                        cancel_event=cancel_event,
                        log=log,
                    )
                    if v2_res.status_code in (200, 206):
                        ct = (v2_res.headers.get("Content-Type") or "").lower()
                        if "html" not in ct:
                            ok_dl, _dl, reason = _stream_response_to_file(
                                v2_res, part_path, remote_size_int, is_video, cancel_event=cancel_event
                            )
                            if ok_dl:
                                os.replace(part_path, local_path)
                                size_now = _finish_success(local_path)
                                log(f"✅ Downloaded '{name}' via shares API ({size_now/1024/1024:.1f} MB)")
                                return 1
                            if reason == "cancelled":
                                return 0
                            log(f"    ⚠️ Shares API download rejected ({reason}); trying next method...")
                        else:
                            v2_res.close()
                    else:
                        _logger.debug("Shares API download HTTP %s for %s", v2_res.status_code, name)
                        v2_res.close()
        except InterruptedError:
            log(f"    🛑 Cancelled download of '{name}'")
            return 0
        except Exception as exc:
            _logger.debug("Shares API download error for %s: %s", name, exc)
        finally:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass

    # Strategy 2: Classic SharePoint REST API (with OData single-quote escaping + retry + integrity verify)
    rel_url_enc = encode_path_for_get_folder_param(file_rel_url)
    file_content_url = f"{base_site}/_api/web/GetFileByServerRelativeUrl('{rel_url_enc}')/$value"
    part_path = local_path + ".part"
    try:
        file_res = stream_get_with_retry(
            session,
            file_content_url,
            timeout=60,
            retries=2,
            cancel_event=cancel_event,
            log=log,
        )
        if file_res.status_code == 200:
            ct = (file_res.headers.get("Content-Type") or "").lower()
            if "html" not in ct:
                if allow_non_mp4:
                    use_stream = True
                else:
                    cl = _coerce_int(file_res.headers.get("Content-Length"), 0)
                    use_stream = cl > 1024 * 1024 or cl == 0
                if use_stream:
                    ok_dl, _dl, reason = _stream_response_to_file(
                        file_res, part_path, remote_size_int, is_video, cancel_event=cancel_event
                    )
                    if ok_dl:
                        os.replace(part_path, local_path)
                        size_now = _finish_success(local_path)
                        log(f"✅ Downloaded '{name}' via API ({size_now/1024/1024:.1f} MB)")
                        return 1
                    if reason == "cancelled":
                        return 0
                    log(f"    ⚠️ REST API download rejected ({reason}); trying next method...")
                else:
                    file_res.close()
            else:
                file_res.close()
        else:
            _logger.debug("Classic REST download HTTP %s for %s", file_res.status_code, name)
            file_res.close()
    except InterruptedError:
        log(f"    🛑 Cancelled download of '{name}'")
        return 0
    except Exception as exc:
        _logger.debug("Classic REST download error for %s: %s", name, exc)
    finally:
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass

    # Strategy 3: Lazy Playwright browser video capture (only for protected streams)
    if not is_video:
        log(f"    ❌ API download failed; skipping capture (not video): '{name}'")
        return 0

    if cancel_event and cancel_event.is_set():
        return 0

    if page is None and page_factory is not None:
        page = page_factory()

    if page is None:
        log("    ❌ No Chromium — install Playwright and retry. Skipping file.")
        return 0

    log("    🔒 Protected file; downloading via browser...")
    success, size = download_video_via_capture(
        page, file_rel_url, local_path, log, sp_site_url=sp_site_url, cancel_event=cancel_event
    )
    if not success and not (cancel_event and cancel_event.is_set()):
        log(f"    🔁 Retrying browser capture for '{name}' once...")
        success, size = download_video_via_capture(
            page, file_rel_url, local_path, log, sp_site_url=sp_site_url, cancel_event=cancel_event
        )
    if success:
        size = _finish_success(local_path)
        log(f"✅ Finished '{name}' ({size/1024/1024:.1f} MB)")
        return 1
    log(f"❌ Failed '{name}'")
    if os.path.exists(local_path) and os.path.getsize(local_path) < 1024 * 1024:
        try:
            os.remove(local_path)
        except OSError:
            pass
    return 0


def sync_sharepoint_files(
    fedauth_cookie=None,
    rtfa_cookie=None,
    sp_site_url=None,
    folder_rel_url=None,
    download_dir=None,
    log_callback=None,
    *,
    local_flat=False,
    sharing_link: Optional[str] = None,
    allow_non_mp4: bool = True,
    cancel_event: Optional[threading.Event] = None,
    dry_run: bool = False,
    stats: Optional[Dict[str, Any]] = None,
):
    """Recursively sync a SharePoint folder using REST listing + lazy Playwright capture."""
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    if not fedauth_cookie or not rtfa_cookie:
        fedauth_cookie, rtfa_cookie = get_cookies_secure(log)
        if not fedauth_cookie or not rtfa_cookie:
            log("❌ No valid cookies. Extract them from the browser (web UI) first.")
            return 0

    if not validate_cookies_before_sync(fedauth_cookie, rtfa_cookie, log):
        log("❌ Cookie validation failed. Aborting sync.")
        return 0

    if not dry_run:
        os.makedirs(download_dir, exist_ok=True)
        try:
            _, _, free = shutil.disk_usage(download_dir)
            if free < 2 * 1024 * 1024 * 1024:
                log(f"⚠️ Warning: Low disk space! Only {free / (1024**3):.1f} GB remaining on target drive.")
        except Exception:
            pass

    session, page = _sharepoint_session_and_page(
        fedauth_cookie, rtfa_cookie, sp_site_url, log, lazy_browser=True
    )

    page_holder: Dict[str, Any] = {"page": page, "attempted": False}

    def lazy_page_factory():
        if page_holder["page"] is not None:
            return page_holder["page"]
        if page_holder["attempted"]:
            return None
        page_holder["attempted"] = True
        page_holder["page"] = _get_playwright_page(
            fedauth_cookie=fedauth_cookie,
            rtfa_cookie=rtfa_cookie,
            sp_site_url=sp_site_url,
            guest_link_url=share_u or load_guest_link(),
            session=session,
            log=log,
        )
        return page_holder["page"]

    cfg_share = (sharing_link or "").strip()
    guest = load_guest_link()
    share_u = ""
    if _sp_sharing_url_has_stream_path(cfg_share):
        share_u = cfg_share
    elif _sp_sharing_url_has_stream_path(guest):
        share_u = guest.strip()
    share_root = ""
    if share_u:
        root_path, _st = _sp_resolve_via_v2_share_driveitem(session, share_u)
        if root_path:
            share_root = root_path
        elif guest.strip() and guest.strip() != share_u and _sp_sharing_url_has_stream_path(guest):
            log(f"  ⚠️ Configured sharing link failed to resolve (HTTP {_st}). Falling back to active guest link.")
            share_u = guest.strip()
            root_path, _st = _sp_resolve_via_v2_share_driveitem(session, share_u)
            if root_path:
                share_root = root_path

    base = (sp_site_url or get_sp_site_url()).rstrip("/")
    warm_urls = []
    if share_u:
        warm_urls.append(share_u)
    if guest.strip() and guest.strip() not in warm_urls:
        warm_urls.append(guest.strip())
    warm_urls.append(base + "/")
    for warm in warm_urls:
        if warm.startswith("http"):
            prime_sharepoint_session_for_browse(
                session,
                base,
                prime_url=warm,
                fedauth_cookie=fedauth_cookie,
                rtfa_cookie=rtfa_cookie,
            )

    if not share_u:
        log(
            "⚠️ No :f:/ sharing link for this sync. REST listing may return 403. "
            "Add a sharing link in Connection settings or re-add the folder from explorer."
        )

    target_dir = _mirror_local_root(download_dir, folder_rel_url, bool(local_flat), log, dry_run=dry_run)
    try:
        return _sync_folder_recursive(
            session,
            page_holder["page"],
            sp_site_url,
            folder_rel_url,
            target_dir,
            log,
            sharing_link_url=share_u or None,
            share_root_norm=share_root or None,
            allow_non_mp4=allow_non_mp4,
            cancel_event=cancel_event,
            dry_run=dry_run,
            stats=stats,
            page_factory=lazy_page_factory,
        )
    finally:
        session.close()


def sync_sharepoint_single_file(
    fedauth_cookie=None,
    rtfa_cookie=None,
    sp_site_url=None,
    file_rel_url=None,
    local_path=None,
    log_callback=None,
    *,
    sharing_link: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    dry_run: bool = False,
    stats: Optional[Dict[str, Any]] = None,
):
    """Download a single SharePoint file (with session warm-up, ledger tracking, and lazy Playwright)."""
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    if not fedauth_cookie or not rtfa_cookie:
        fedauth_cookie, rtfa_cookie = get_cookies_secure(log)
        if not fedauth_cookie or not rtfa_cookie:
            log("❌ No valid cookies. Extract them from the browser (web UI) first.")
            return 0

    if not validate_cookies_before_sync(fedauth_cookie, rtfa_cookie, log):
        log("❌ Cookie validation failed. Aborting sync.")
        return 0

    local_path = os.path.abspath(os.path.expanduser(str(local_path).strip()))
    parent = os.path.dirname(local_path)
    if not dry_run and parent:
        os.makedirs(parent, exist_ok=True)
        try:
            _, _, free = shutil.disk_usage(parent)
            if free < 2 * 1024 * 1024 * 1024:
                log(f"⚠️ Warning: Low disk space! Only {free / (1024**3):.1f} GB remaining on target drive.")
        except Exception:
            pass

    from sp_sync.sharepoint import ledger as _ledger

    store = get_store()
    row = {
        "name": os.path.basename((file_rel_url or "").rstrip("/")),
        "rel_url": file_rel_url,
        "item_id": None,
        "size": 0,
    }
    entry = store.ledger_get(_ledger._row_key(row))
    if entry and not entry.get("redownload_requested"):
        log(f"[-] Skip '{row['name']}' (in download ledger)")
        if stats is not None:
            stats["skipped"] = int(stats.get("skipped", 0)) + 1
        return 0

    if dry_run:
        log(f"[DRY-RUN] Would download single file '{row['name']}' -> {local_path}")
        if stats is not None:
            stats.setdefault("planned_files", []).append({"name": row["name"], "local_path": local_path, "size": 0})
        return 1

    session, _ = _sharepoint_session_and_page(
        fedauth_cookie, rtfa_cookie, sp_site_url, log, lazy_browser=True
    )
    base = (sp_site_url or get_sp_site_url()).rstrip("/")
    prime_sharepoint_session_for_browse(
        session,
        base,
        prime_url=sharing_link or load_guest_link() or (base + "/"),
        fedauth_cookie=fedauth_cookie,
        rtfa_cookie=rtfa_cookie,
    )

    page_holder: Dict[str, Any] = {"page": None, "attempted": False}

    def lazy_page_factory():
        if page_holder["page"] is not None:
            return page_holder["page"]
        if page_holder["attempted"]:
            return None
        page_holder["attempted"] = True
        page_holder["page"] = _get_playwright_page(
            fedauth_cookie=fedauth_cookie,
            rtfa_cookie=rtfa_cookie,
            sp_site_url=sp_site_url,
            guest_link_url=sharing_link or load_guest_link(),
            session=session,
            log=log,
        )
        return page_holder["page"]

    try:
        existed_before = os.path.isfile(local_path)
        got = _download_one_file(
            session,
            None,
            sp_site_url,
            file_rel_url,
            local_path,
            log,
            allow_non_mp4=True,
            sharing_link_url=sharing_link or None,
            cancel_event=cancel_event,
            page_factory=lazy_page_factory,
        )
        try:
            _ledger.record_after_download(
                store,
                row,
                local_path,
                got,
                existed_before,
                is_valid=lambda p: (not _ledger.is_video_name(p))
                or (_is_valid_mp4(p) and os.path.getsize(p) >= 1024 * 1024),
            )
        except Exception as exc:
            log(f"  ⚠️ Could not record '{row['name']}' in download ledger: {exc}")
        if stats is not None:
            if got:
                stats["downloaded"] = int(stats.get("downloaded", 0)) + 1
                stats.setdefault("new_files", []).append(os.path.basename(local_path))
            elif not existed_before and not (cancel_event and cancel_event.is_set()):
                stats["failed"] = int(stats.get("failed", 0)) + 1
        return got
    finally:
        session.close()


def _sync_folder_recursive(
    session,
    page,
    sp_site_url,
    folder_rel_url,
    download_dir,
    log,
    *,
    sharing_link_url: Optional[str] = None,
    share_root_norm: Optional[str] = None,
    allow_non_mp4: bool = True,
    parent_item_id: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    dry_run: bool = False,
    stats: Optional[Dict[str, Any]] = None,
    page_factory: Optional[Callable[[], Any]] = None,
):
    """Internal recursive folder sync (same listing stack as /sp/browse)."""
    from sp_sync.sharepoint.rest_folder_list import list_folder_children_rest

    if cancel_event and cancel_event.is_set():
        if stats is not None:
            stats["cancelled"] = True
        return 0

    if not dry_run:
        os.makedirs(download_dir, exist_ok=True)
        try:
            _, _, free = shutil.disk_usage(download_dir)
            if free < 2 * 1024 * 1024 * 1024:
                log(f"⚠️ Warning: Low disk space! Only {free / (1024**3):.1f} GB remaining on target drive.")
        except Exception:
            pass

    base_site = (sp_site_url or get_sp_site_url()).rstrip("/")
    share_for_list = (sharing_link_url or "").strip()
    if share_for_list and not _sp_sharing_url_has_stream_path(share_for_list):
        share_for_list = ""

    listing = list_folder_children_rest(
        session,
        base_site,
        folder_rel_url,
        timeout=120,
        sharing_link_url=share_for_list or None,
        share_root_norm=share_root_norm,
        parent_item_id=parent_item_id,
    )
    if not listing.get("ok"):
        guest = load_guest_link().strip()
        if guest and guest != share_for_list and _sp_sharing_url_has_stream_path(guest):
            log(f"  ⚠️ Folder listing failed ({listing.get('error')}). Retrying with active guest link...")
            root_path, _ = _sp_resolve_via_v2_share_driveitem(session, guest)
            listing = list_folder_children_rest(
                session,
                base_site,
                folder_rel_url,
                timeout=120,
                sharing_link_url=guest,
                share_root_norm=root_path or share_root_norm,
                parent_item_id=parent_item_id,
            )
            if listing.get("ok"):
                share_for_list = guest
                share_root_norm = root_path or share_root_norm

    if not listing.get("ok"):
        err = listing.get("error") or "Folder listing failed"
        log(f"❌ Cannot list {folder_rel_url}: {err}")
        st = listing.get("upstream_http_status")
        if st:
            log(f"   HTTP {st}")
        snippet = (listing.get("body_snippet") or "").strip()
        if snippet:
            log(f"   Server: {snippet[:240]}")
        if st in (401, 403) or "403" in err or "401" in err:
            if not share_for_list:
                log(
                    "   No :f:/ sharing link on this folder. Resolve the link in SharePoint explorer, "
                    "save the guest link under Connection settings, or re-add the folder to sync."
                )
            else:
                log(
                    "   Paste fresh FedAuth/rtFa from the browser that opens this folder, "
                    "or use a valid :f:/ sharing link."
                )
        if stats is not None:
            stats["failed"] = int(stats.get("failed", 0)) + 1
            stats.setdefault("errors", []).append(f"List {folder_rel_url}: {err}")
        return 0

    method = listing.get("method") or "rest"
    n_f = len(listing.get("folders") or [])
    n_files = len(listing.get("files") or [])
    ext_counts: Dict[str, int] = {}
    for _row in listing.get("files") or []:
        _ext = os.path.splitext(_row.get("name") or "")[1].lower() or "(none)"
        ext_counts[_ext] = ext_counts.get(_ext, 0) + 1
    _types = ", ".join(f"{n} {ext}" for ext, n in sorted(ext_counts.items()))
    if _types:
        log(f"📋 Listed via {method}: {n_f} folder(s), {n_files} file(s) ({_types})")
    else:
        log(f"📋 Listed via {method}: {n_f} folder(s), {n_files} file(s)")

    downloaded_count = 0
    from sp_sync.sharepoint import ledger as _ledger

    _store = get_store()
    if not dry_run:
        try:
            _ledger.backfill_folder(_store, download_dir, listing.get("files") or [], log=log)
        except Exception as exc:
            log(f"  ⚠️ Ledger backfill failed: {exc}")

    for file_row in listing.get("files") or []:
        if cancel_event and cancel_event.is_set():
            log("🛑 SharePoint sync cancelled by user.")
            if stats is not None:
                stats["cancelled"] = True
            return downloaded_count

        name = file_row.get("name") or ""
        rel_url = file_row.get("rel_url") or ""
        if not name or not rel_url:
            continue
        file_item_id = file_row.get("item_id")
        sz = _coerce_int(file_row.get("size"), 0)

        plan_meta: Dict[str, Any] = {}
        local_path = _ledger.plan_download(
            _store, download_dir, file_row, log=log, dry_run=dry_run, out_meta=plan_meta
        )
        if local_path is None:
            if stats is not None:
                stats["skipped"] = int(stats.get("skipped", 0)) + 1
            continue

        if dry_run:
            ext = os.path.splitext(name)[1].lower()
            is_vid = ext == ".mp4" or ext in VIDEO_EXTENSIONS
            if not allow_non_mp4 and not is_vid:
                if stats is not None:
                    stats["skipped"] = int(stats.get("skipped", 0)) + 1
                continue
            log(f"[DRY-RUN] Would download '{name}' -> {local_path} ({sz/1024/1024:.1f} MB)")
            if stats is not None:
                stats.setdefault("planned_files", []).append(
                    {"name": name, "local_path": local_path, "size": sz}
                )
            downloaded_count += 1
            continue

        existed_before = os.path.isfile(local_path)
        got = _download_one_file(
            session,
            page,
            sp_site_url,
            rel_url,
            local_path,
            log,
            allow_non_mp4=allow_non_mp4,
            sharing_link_url=share_for_list or None,
            item_id=file_item_id,
            share_root_norm=share_root_norm,
            remote_size=sz,
            cancel_event=cancel_event,
            page_factory=page_factory,
        )
        downloaded_count += got

        if not got and plan_meta.get("aside_path"):
            _ledger.restore_redownload_aside(local_path, plan_meta["aside_path"], log=log)

        try:
            _ledger.record_after_download(
                _store,
                file_row,
                local_path,
                got,
                existed_before,
                is_valid=lambda p: (not _ledger.is_video_name(p))
                or (_is_valid_mp4(p) and os.path.getsize(p) >= 1024 * 1024),
            )
        except Exception as exc:
            log(f"  ⚠️ Could not record '{name}' in download ledger: {exc}")

        if stats is not None:
            if got:
                stats["downloaded"] = int(stats.get("downloaded", 0)) + 1
                stats.setdefault("new_files", []).append(os.path.basename(local_path))
            elif existed_before:
                stats["skipped"] = int(stats.get("skipped", 0)) + 1
            elif not (cancel_event and cancel_event.is_set()):
                ext = os.path.splitext(name)[1].lower()
                is_vid = ext == ".mp4" or ext in VIDEO_EXTENSIONS
                if allow_non_mp4 or is_vid:
                    stats["failed"] = int(stats.get("failed", 0)) + 1
                    stats.setdefault("errors", []).append(f"Failed download: {name}")
                else:
                    stats["skipped"] = int(stats.get("skipped", 0)) + 1

    for folder_row in listing.get("folders") or []:
        if cancel_event and cancel_event.is_set():
            if stats is not None:
                stats["cancelled"] = True
            break
        folder_name = folder_row.get("name") or ""
        sub_rel_url = folder_row.get("rel_url") or ""
        if not folder_name or not sub_rel_url:
            continue
        if folder_name == "Forms" or folder_name.startswith("_"):
            continue
        safe_sub = _sanitize_dir_name(folder_name)
        sub_dir = os.path.join(download_dir, safe_sub)
        log(f"\n📁 {folder_name}")
        downloaded_count += _sync_folder_recursive(
            session,
            page,
            sp_site_url,
            sub_rel_url,
            sub_dir,
            log,
            sharing_link_url=share_for_list or sharing_link_url,
            share_root_norm=share_root_norm,
            allow_non_mp4=allow_non_mp4,
            parent_item_id=folder_row.get("item_id"),
            cancel_event=cancel_event,
            dry_run=dry_run,
            stats=stats,
            page_factory=page_factory,
        )

    return downloaded_count


def start_sync_from_config(
    fedauth_cookie,
    rtfa_cookie,
    sp_site_url,
    config,
    log_callback=None,
    *,
    cancel_event: Optional[threading.Event] = None,
    dry_run: bool = False,
    keep_browser_open: bool = False,
    stats: Optional[Dict[str, Any]] = None,
):
    """Run sync for one config dict: folder (recursive) or single file via entry_type."""
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    log("==================================================")
    log("🔍 Dry-run scanning SharePoint..." if dry_run else "⏳ Connecting and scanning files and folders...")
    count = 0
    try:
        et = str(config.get("entry_type") or "folder").strip().lower()
        share_cfg = (config.get("sharing_link") or "").strip()
        allow_all = config.get("allow_non_mp4", True) if not config.get("video_only", False) else False
        if et == "file":
            count = sync_sharepoint_single_file(
                fedauth_cookie,
                rtfa_cookie,
                sp_site_url,
                config["rel_url"],
                config["local_path"],
                log_callback,
                sharing_link=share_cfg or None,
                cancel_event=cancel_event,
                dry_run=dry_run,
                stats=stats,
            )
        else:
            count = sync_sharepoint_files(
                fedauth_cookie,
                rtfa_cookie,
                sp_site_url,
                config["rel_url"],
                config["local_path"],
                log_callback,
                local_flat=bool(config.get("local_flat", False)),
                sharing_link=share_cfg or None,
                allow_non_mp4=allow_all,
                cancel_event=cancel_event,
                dry_run=dry_run,
                stats=stats,
            )
    except json.JSONDecodeError as exc:
        log(f"❌ Server returned non-JSON while listing folder: {exc}")
        log(
            "   Refresh FedAuth/rtFa, or add the original :f:/ sharing link to this folder "
            "(edit config / re-add from explorer)."
        )
        if stats is not None:
            stats["failed"] = int(stats.get("failed", 0)) + 1
            stats.setdefault("errors", []).append(str(exc))
        count = 0
    except InterruptedError:
        log("🛑 Sync cancelled.")
        if stats is not None:
            stats["cancelled"] = True
        count = 0
    except Exception as exc:
        log(f"❌ Error: {exc}")
        if stats is not None:
            stats["failed"] = int(stats.get("failed", 0)) + 1
            stats.setdefault("errors", []).append(str(exc))
        count = 0
    finally:
        if not keep_browser_open:
            _cleanup_playwright()
    log("==================================================")
    if dry_run:
        log(f"🔍 Dry-run finished. {count} file(s) would be downloaded.")
    else:
        log(f"🎉 Sync finished. Downloaded {count} new file(s).")
    return count


def start_sync(fedauth_cookie, rtfa_cookie, sp_site_url, folder_rel_url, download_dir, log_callback=None):
    """Entry point called by legacy callers."""
    return start_sync_from_config(
        fedauth_cookie,
        rtfa_cookie,
        sp_site_url,
        {
            "name": "",
            "rel_url": folder_rel_url,
            "local_path": download_dir,
            "entry_type": "folder",
            "local_flat": True,
        },
        log_callback,
    )


if __name__ == "__main__":
    fedauth, rtfa = get_cookies_secure(print)
    if not fedauth:
        print("No cookies in SQLite. Run app.py and save cookies from the web UI.")
        raise SystemExit(1)
    site = get_sp_site_url()
    if not site:
        print("Set SharePoint site URL from the web UI (Connection settings or paste a full link in Explorer).")
        raise SystemExit(1)
    print("=" * 60)
    print("  SharePoint sync (hybrid API + lazy browser capture)")
    print("=" * 60)
    print("Use the Flask UI or call start_sync_from_config() from your own script.")
    raise SystemExit(0)
