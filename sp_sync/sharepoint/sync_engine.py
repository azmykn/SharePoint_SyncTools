"""SharePoint Sync - Hybrid Approach
Uses API for file listing + Playwright SourceBuffer capture for video downloads.
The SourceBuffer hook intercepts DECRYPTED video data from the browser's player,
bypassing DASH SEA (AES-128-CBC) encryption that causes 403 on direct downloads."""
import sys
import requests
import os
import shutil
import urllib.parse
import warnings
import subprocess
import base64
import time
import json
import re
from pathlib import PurePosixPath
from datetime import datetime

# Force UTF-8 via env (must be set before other imports that might use stdout)
os.environ['PYTHONIOENCODING'] = 'utf-8'

# Suppress the chardet/charset_normalizer warning from requests
warnings.filterwarnings("ignore", message="Unable to find acceptable character detection dependency")


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


from sp_sync.paths import project_root
from sp_sync.config.settings import default_guest_share_link


def get_sp_site_url() -> str:
    """Site root URL from SQLite (set via web UI / explorer); empty until configured."""
    try:
        from sp_sync.db.store import get_store

        u = get_store().get_sharepoint_site_url().strip()
        if u:
            return u.rstrip("/")
    except Exception:
        pass
    return ""


def get_default_guest_link() -> str:
    g = default_guest_share_link().strip()
    return g if g.startswith("http") else ""


ROOT = project_root()
TMP_DIR = os.path.join(ROOT, "data", "tmp_segments")

from sp_sync.db.store import get_store


def load_guest_link():
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
    return get_sp_site_url()


def _sharepoint_cookie_host(sp_site_url):
    """Hostname for FedAuth/rtFa cookie domain (e.g. tenant-my.sharepoint.com)."""
    return urllib.parse.urlparse(sp_site_url or get_sp_site_url()).netloc


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

# Extensions that may need browser capture (encrypted DASH / SEA); others use REST /$value
VIDEO_EXTENSIONS = {".mp4", ".webm", ".m4v", ".mov", ".mkv"}

try:
    from sp_sync.security.cookies import CookieValidator

    SECURE_STORAGE_AVAILABLE = True
except ImportError:
    SECURE_STORAGE_AVAILABLE = False
    print("Warning: CookieValidator not available, skipping advanced cookie validation")

FFMPEG_PATH = None
FFMPEG_DIR = None


def resolve_ffmpeg_path(log=print):
    """Resolve ffmpeg binary: $FFMPEG_PATH, PATH, then common Windows install."""
    global FFMPEG_PATH, FFMPEG_DIR
    if FFMPEG_PATH and os.path.isfile(FFMPEG_PATH):
        return FFMPEG_PATH
    env = os.environ.get("FFMPEG_PATH", "").strip()
    if env and os.path.isfile(env):
        FFMPEG_PATH, FFMPEG_DIR = env, os.path.dirname(env)
        return FFMPEG_PATH
    which = shutil.which("ffmpeg")
    if which:
        FFMPEG_PATH, FFMPEG_DIR = which, os.path.dirname(which)
        return FFMPEG_PATH
    win_guess = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft",
        "WinGet",
        "Packages",
    )
    if os.name == "nt" and os.path.isdir(win_guess):
        for root, _dirs, files in os.walk(win_guess):
            if "ffmpeg.exe" in files:
                FFMPEG_PATH = os.path.join(root, "ffmpeg.exe")
                FFMPEG_DIR = os.path.dirname(FFMPEG_PATH)
                return FFMPEG_PATH
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["where", "ffmpeg"], capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                FFMPEG_PATH = r.stdout.strip().split("\n")[0].strip()
                FFMPEG_DIR = os.path.dirname(FFMPEG_PATH)
                return FFMPEG_PATH
        except (OSError, subprocess.TimeoutExpired):
            pass
    log("⚠️ ffmpeg not found — browser capture will not mux audio/video without ffmpeg in PATH")
    return None


resolve_ffmpeg_path()

# JavaScript hook to intercept decrypted video data from SourceBuffer.appendBuffer
HOOK_JS = """
window.__capturedBuffers = { video: [], audio: [] };
window.__captureReady = false;
const origAddSourceBuffer = MediaSource.prototype.addSourceBuffer;
MediaSource.prototype.addSourceBuffer = function(mimeType) {
    const sb = origAddSourceBuffer.call(this, mimeType);
    const trackType = mimeType.includes('video') ? 'video' : 'audio';
    const origAppend = sb.appendBuffer.bind(sb);
    sb.appendBuffer = function(data) {
        const arr = new Uint8Array(data);
        const binary = Array.from(arr).map(b => String.fromCharCode(b)).join('');
        const b64 = btoa(binary);
        window.__capturedBuffers[trackType].push({ size: arr.length, b64: b64 });
        return origAppend(data);
    };
    return sb;
};
window.__captureReady = true;
"""

# --- Secure Cookie Handling ---
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
    """Deprecated: kept name for compatibility; delegates to SQLite."""
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
            
            if not result['valid']:
                log("⚠️ Cookie validation failed:")
                for issue in result['issues']:
                    log(f"   - {issue}")
                log("🔧 Recommendations:")
                for rec in result['recommendations']:
                    log(f"   - {rec}")
                return False
            else:
                log("✅ Cookies look valid")
                return True
        except Exception as e:
            log(f"⚠️ Cookie validation error: {e}")
            return True  # Continue anyway if validation fails
    else:
        log("✅ Basic cookie check passed")
        return True

# --- Enhanced Retry Mechanism with Exponential Backoff ---
def retry_with_backoff(func, max_retries=3, base_delay=1, max_delay=60, 
                      exceptions=(Exception,), log=print, *args, **kwargs):
    """
    Execute function with exponential backoff retry mechanism.
    
    Args:
        func: Function to execute
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        exceptions: Tuple of exceptions to catch and retry on
        log: Logging function
        *args, **kwargs: Arguments to pass to the function
    
    Returns:
        Tuple (success, result)
    """
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
            
            # Calculate delay with exponential backoff and jitter
            delay = min(base_delay * (2 ** attempt) + (attempt * 0.1), max_delay)
            log(f"⚠️ Attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)

# --- Playwright Context Manager (lazy singleton) ---
_pw_instance = None

def _get_playwright_page(
    fedauth_cookie=None,
    rtfa_cookie=None,
    sp_site_url=None,
    guest_link_url=None,
    session=None,
    log=print,
):
    """Create Playwright browser: inject FedAuth+rtFa, open guest link, merge cookies into session."""
    global _pw_instance
    if _pw_instance and _pw_instance.get("page"):
        try:
            _pw_instance["page"].evaluate("() => true")
            return _pw_instance["page"]
        except Exception:
            _cleanup_playwright()

    host = _sharepoint_cookie_host(sp_site_url)
    guest_link_url = guest_link_url or load_guest_link()

    log("🌐 Launching browser...")
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
            if "executable doesn't exist" in err or "playwright install" in err:
                _log_playwright_chromium_missing(log)
            elif "does not support" in err:
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
        cookies_to_add.append(
            {"name": "FedAuth", "value": fedauth_cookie, "domain": host, "path": "/"}
        )
    if rtfa_cookie:
        cookies_to_add.append(
            {"name": "rtFa", "value": rtfa_cookie, "domain": host, "path": "/"}
        )
    if cookies_to_add:
        context.add_cookies(cookies_to_add)
        log(f"  🍪 Injected FedAuth/rtFa for {host}")

    page = context.new_page()

    log("🔗 Warming session (guest link, then site)...")
    session_ok = False

    for attempt in range(2):
        try:
            page.goto(guest_link_url, wait_until="networkidle", timeout=90000)
            page.wait_for_timeout(4000)
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
            page.goto(
                f"{base}/_layouts/15/onedrive.aspx",
                wait_until="networkidle",
                timeout=90000,
            )
            page.wait_for_timeout(4000)
            log(f"  ✅ Direct connection OK: {page.title()[:80]}")
            session_ok = True
        except Exception as e:
            log(f"  ❌ Direct connection failed: {e}")

    if not session_ok:
        log("  ⚠️ Session not fully warmed; continuing with REST anyway.")

    if session is not None:
        _merge_playwright_cookies_into_session(context, session, log)

    _pw_instance = {"pw": pw, "browser": browser, "context": context, "page": page}
    return page


def _cleanup_playwright():
    """Clean up Playwright resources."""
    global _pw_instance
    if _pw_instance:
        try: _pw_instance['browser'].close()
        except: pass
        try: _pw_instance['pw'].stop()
        except: pass
        _pw_instance = None


def download_video_via_capture(
    page, file_rel_url, output_path, log=print, sp_site_url=None
):
    """Download a video by hooking the browser's SourceBuffer to capture decrypted data.
    Returns (success, file_size)."""
    os.makedirs(TMP_DIR, exist_ok=True)

    base = (sp_site_url or get_sp_site_url()).rstrip("/")
    file_viewer_url = (
        f"{base}/_layouts/15/onedrive.aspx"
        f"?id={urllib.parse.quote(file_rel_url)}"
        f"&parent={urllib.parse.quote(os.path.dirname(file_rel_url))}"
    )

    def load_video_page():
        """Load the video page with proper error handling."""
        try:
            page.goto(file_viewer_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(10000)
            return True
        except Exception as e:
            log(f"  ⚠️ First page load failed: {e}")
            try:
                page.goto(file_viewer_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(10000)
                return True
            except Exception as e2:
                log(f"  ❌ Second page load failed: {e2}")
                raise e2

    # Load video page with retry mechanism
    success, _ = retry_with_backoff(load_video_page, max_retries=2, base_delay=2, log=log)
    if not success:
        log(f"  ❌ Failed to load video page: {file_rel_url}")
        return False, 0

    # Check hook
    ready = page.evaluate("() => window.__captureReady === true")
    if not ready:
        page.wait_for_timeout(5000)
        ready = page.evaluate("() => window.__captureReady === true")
        if not ready:
            log("    ⚠️ Failed to inject browser hook")
            return False, 0

    # Wait for video element to appear (up to 30s)
    dur = 0
    for wait in range(15):
        try:
            dur = page.evaluate("() => { const v = document.querySelector('video'); return v ? v.duration : 0; }")
            if dur and dur > 0:
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

    # Proceed with fast playback with enhanced logging
    log(f"    ⏱️ Duration: {dur:.0f}s ({dur/60:.1f} min). Capturing at 16x...")

    # Play video at 16x speed to sequentially capture all segments without skipping
    page.evaluate("() => { const v = document.querySelector('video'); if(v) { v.playbackRate = 16.0; v.muted = true; v.play(); } }")
    
    last_time = 0
    stuck_count = 0
    start_time = time.time()
    last_log_time = start_time
    
    while True:
        time.sleep(2)
        state = page.evaluate("""() => { 
            const v = document.querySelector('video'); 
            return v ? { time: v.currentTime, ended: v.ended, paused: v.paused } : { time: 0, ended: true, paused: true }; 
        }""")
        
        # Enhanced progress logging every 10 seconds
        current_time = time.time()
        if current_time - last_log_time >= 10:
            progress_percent = (state['time'] / dur) * 100 if dur > 0 else 0
            elapsed = current_time - start_time
            buffer_counts = page.evaluate("() => ({ v: window.__capturedBuffers.video.length, a: window.__capturedBuffers.audio.length })")
            
            log(f"    📊 Progress: {progress_percent:.1f}% | time: {state['time']:.0f}/{dur:.0f}s | buffers V={buffer_counts['v']} A={buffer_counts['a']} | elapsed: {elapsed:.0f}s")
            last_log_time = current_time
        
        current_time = state['time']
        
        if state['ended']:
            break
            
        if current_time == last_time:
            stuck_count += 1
            if stuck_count > 15: # 30 seconds stuck
                log("    ⚠️ Playback looks stuck; trying to recover...")
                page.evaluate("() => { const v = document.querySelector('video'); if(v) v.play(); }")
                stuck_count = 0
        else:
            stuck_count = 0
            
        last_time = current_time
        
        if int(current_time) % 60 == 0 or stuck_count == 1:
            c = page.evaluate("() => ({ v: window.__capturedBuffers.video.length, a: window.__capturedBuffers.audio.length })")
            log(f"    📥 Progress: {current_time:.0f}s / {dur:.0f}s (V={c['v']} A={c['a']})")

    final = page.evaluate("() => ({ v: window.__capturedBuffers.video.length, a: window.__capturedBuffers.audio.length })")
    log(f"    📦 Totals: V={final['v']} A={final['a']} buffers")

    # Extract captured data to temp files
    v_file = os.path.join(TMP_DIR, "video.fmp4")
    a_file = os.path.join(TMP_DIR, "audio.fmp4")
    
    # Remove old temp files
    for f in [v_file, a_file]:
        if os.path.exists(f):
            try: os.remove(f)
            except: pass

    for track, track_file in [('video', v_file), ('audio', a_file)]:
        num = page.evaluate(f"() => window.__capturedBuffers.{track}.length")
        if num == 0:
            log(f"    ⚠️ No captured data for track: {track}")
            continue
            
        total_bytes = 0
        with open(track_file, 'wb') as f_out:
            for idx in range(num):
                try:
                    b64_data = page.evaluate(f"() => window.__capturedBuffers.{track}[{idx}].b64")
                    raw = base64.b64decode(b64_data)
                    f_out.write(raw)
                    total_bytes += len(raw)
                except Exception as e:
                    log(f"    ⚠️ Buffer extract error {idx}: {e}")
        log(f"    💾 {track}: {total_bytes/1024/1024:.1f} MB")

    # Reset captured buffers for next file
    page.evaluate("() => { window.__capturedBuffers = { video: [], audio: [] }; }")

    # Muxing Logic: Switch to MPEG-TS as intermediate for robustness
    if not os.path.exists(v_file) or os.path.getsize(v_file) < 100:
        log("    ❌ Video path empty or invalid")
        return False, 0

    log("    🎬 Muxing video and audio with ffmpeg...")

    ff = resolve_ffmpeg_path(log)
    if not ff or not os.path.isfile(ff):
        log("    ❌ ffmpeg not available — install ffmpeg or set FFMPEG_PATH")
        return False, 0

    # Step 1: Convert/Concatenate to a robust format (MPEG-TS) to handle fMP4 discontinuities
    ts_output = os.path.join(TMP_DIR, "merged.ts")
    if os.path.exists(ts_output):
        os.remove(ts_output)

    # Use -f mpegts to force a streamable format that handles appended fragments better
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

    # Step 2: Final conversion to MP4 with faststart
    if res_mux.returncode == 0 and os.path.exists(ts_output):
        cmd_final = [
            ff,
            "-y",
            "-i",
            ts_output,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output_path,
        ]
        res_final = subprocess.run(cmd_final, capture_output=True, text=True, timeout=300)
        
        # Cleanup TS
        if os.path.exists(ts_output): os.remove(ts_output)
        
        if res_final.returncode == 0 and os.path.exists(output_path):
            size = os.path.getsize(output_path)
            return True, size

    # Cleanup temp
    for f in [v_file, a_file, ts_output]:
        if os.path.exists(f):
            try: os.remove(f)
            except: pass

    log(f"    ❌ Final mux failed (exit {res_mux.returncode})")
    if res_mux.stderr:
        log(f"    DEBUG: {res_mux.stderr.splitlines()[-1] if res_mux.stderr.splitlines() else 'Unknown error'}")
            
    return False, 0


def _sp_url_leaf_segment(server_rel_url):
    """Last path segment of a SharePoint server-relative URL (decoded)."""
    parts = [p for p in (server_rel_url or "").replace("\\", "/").split("/") if p]
    if not parts:
        return "SharePoint"
    return urllib.parse.unquote(parts[-1])


def _sanitize_dir_name(name):
    """Make a single path component safe on Windows and common filesystems."""
    if not name or not str(name).strip():
        return "SharePoint"
    bad = '<>:"/\\|?*'
    s = "".join("_" if c in bad else c for c in str(name).strip())
    s = s.rstrip(" .") or "SharePoint"
    if len(s) > 200:
        s = s[:200].rstrip(" .") or "SharePoint"
    return s


def _sp_resolve_normalize_input(pasted_url):
    u = (pasted_url or "").strip()
    if not u:
        return None, ""
    if u.startswith("/"):
        return "server_path", urllib.parse.unquote(u)
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return "url", u


def _sp_split_path_and_query(s):
    s = (s or "").strip()
    if "?" in s:
        path_part, q = s.split("?", 1)
        return path_part.strip(), q.strip()
    return s, ""


def _sp_looks_like_onedrive_sharing_stub(path_no_query):
    """
    /personal/{upn}/{token} where token is a :f:/g sharing resource id (not Documents/...).
    GetFolderByServerRelativeUrl does not accept these; must rebuild full sharing URL.
    """
    p = path_no_query.replace("\\", "/").strip()
    if not p.startswith("/personal/"):
        return False
    parts = [x for x in p.split("/") if x]
    if len(parts) != 3 or parts[0] != "personal":
        return False
    tail = parts[2]
    if tail in (
        "Documents",
        "Shared Documents",
        "Microsoft Teams Chat Files",
        "Attachments",
    ):
        return False
    if len(tail) < 20:
        return False
    if not re.match(r"^[A-Za-z0-9_-]+$", tail):
        return False
    return True


def _sp_share_requests_session(sp_site_url, fedauth_cookie, rtfa_cookie, accept_html=False):
    cookie_host = _sharepoint_cookie_host(sp_site_url)
    session = requests.Session()
    accept = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        if accept_html
        else "application/json;odata=verbose"
    )
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        }
    )
    session.cookies.set("FedAuth", fedauth_cookie, domain=cookie_host, path="/")
    if rtfa_cookie:
        session.cookies.set("rtFa", rtfa_cookie, domain=cookie_host, path="/")
    return session


def prime_sharepoint_rest_session(session, sp_site_url, extra_get_url=None):
    """
    Load guest / OneDrive HTML pages so Set-Cookie merges into session before REST /_api calls.
    Use extra_get_url (e.g. final_url from sharing resolution) when available.
    """
    session.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    try:
        gl = load_guest_link()
        if gl and str(gl).startswith("http"):
            session.get(gl, timeout=45, allow_redirects=True)
        pu = (extra_get_url or "").strip()
        if pu.startswith("http") and "sharepoint.com" in pu.lower():
            h1 = urllib.parse.urlparse(pu).netloc.lower()
            h2 = urllib.parse.urlparse(sp_site_url or get_sp_site_url()).netloc.lower()
            if h1 == h2:
                session.get(pu, timeout=60, allow_redirects=True)
    except Exception:
        pass
    session.headers["Accept"] = "application/json;odata=verbose"


def _sp_extract_onedrive_id_param(url):
    """Extract server-relative path from id= / folder= / RootFolder= query (decoded)."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ("id", "folder", "res", "RootFolder"):
        if key in qs and qs[key][0].strip():
            return urllib.parse.unquote(qs[key][0].strip().replace("+", " "))
    if parsed.fragment:
        m = re.search(r"(?:^|[&?])id=([^&]+)", parsed.fragment)
        if m:
            return urllib.parse.unquote(m.group(1))
    return None


def _sp_server_rel_from_personal_browser_path(parsed_url):
    """If URL path is /personal/... without :f:/ sharing token in path, return server-relative path."""
    path = parsed_url.path or ""
    if "/personal/" not in path:
        return None
    pl = path.lower()
    if ":f:/" in pl or ":w:/" in pl or ":x:/" in pl:
        return None
    if "onedrive.aspx" in pl:
        return None
    if not path.startswith("/"):
        path = "/" + path.lstrip("/")
    return urllib.parse.unquote(path) or None


def _sp_folder_rest_ok(session, sp_base, server_rel):
    enc = urllib.parse.quote(server_rel)
    api = f"{sp_base.rstrip('/')}/_api/web/GetFolderByServerRelativeUrl('{enc}')"
    r = session.get(api, timeout=45)
    return r.status_code == 200


def _sp_ensure_browse_folder(session, sp_base, server_rel, log):
    """Walk up parents until GetFolder succeeds (link may target a file)."""
    cur = (server_rel or "").replace("\\", "/").strip()
    if not cur.startswith("/"):
        cur = "/" + cur
    cur = urllib.parse.unquote(cur)
    seen = set()
    while cur and cur not in seen and len(seen) < 40:
        seen.add(cur)
        if _sp_folder_rest_ok(session, sp_base, cur):
            if len(seen) > 1:
                log(f"  ✓ Using browse folder: {cur}")
            return cur
        parent = str(PurePosixPath(cur).parent)
        if parent == cur:
            break
        log("  ↪ Path is not a listable folder; trying parent...")
        cur = parent
    return None


def resolve_sharepoint_pasted_url(
    fedauth_cookie,
    rtfa_cookie,
    sp_site_url,
    pasted_url,
    log=print,
    *,
    _stub_depth=0,
):
    """
    Resolve pasted SharePoint/OneDrive URL to a server-relative folder path for /sp/browse.

    Returns dict: ok, browse_parent, resolved_target, resolved_from, final_url, error, hint.
    """
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

    session.headers["Accept"] = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    )
    try:
        r = session.get(url, allow_redirects=True, timeout=90)
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
    id_val = _sp_extract_onedrive_id_param(final)
    if not id_val and r.text:
        m = re.search(
            r"(?:onedrive\.aspx|embed\.aspx)\?[^\"'>]*\bid=([^&\"'>\s]+)",
            r.text,
            re.I,
        )
        if m:
            raw_id = m.group(1).replace("&amp;", "&")
            id_val = urllib.parse.unquote(raw_id)
    if not id_val:
        for hist in getattr(r, "history", []) or []:
            id_val = _sp_extract_onedrive_id_param(hist.url)
            if id_val:
                final = hist.url
                break

    if not id_val:
        return {
            "ok": False,
            "browse_parent": None,
            "resolved_target": None,
            "resolved_from": "",
            "final_url": final,
            "error": "Could not extract folder/file path after redirect",
            "hint": "Open the link signed in, copy id= from the address bar (starts with /personal/) and paste as a relative path.",
        }

    if not id_val.startswith("/"):
        id_val = "/" + id_val.lstrip("/")

    session.headers["Accept"] = "application/json;odata=verbose"
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


def _mirror_local_root(download_dir, folder_rel_url, local_flat, log):
    """
    Under download_dir, create a subfolder with the same name as the synced cloud folder
    (last segment of folder_rel_url) so structure is: base / <cloud_folder> / ...mirrored_subfolders.

    If local_flat is True, files stay directly in download_dir (legacy).
    """
    if local_flat:
        return download_dir
    leaf = _sanitize_dir_name(_sp_url_leaf_segment(folder_rel_url))
    target = os.path.join(download_dir, leaf)
    os.makedirs(target, exist_ok=True)
    log(f"📂 Local download root (mirrors cloud folder name): {target}")
    return target


def _sharepoint_session_and_page(fedauth_cookie, rtfa_cookie, sp_site_url, log):
    """Build authenticated REST session and optional Playwright page; merges browser cookies into session."""
    cookie_host = _sharepoint_cookie_host(sp_site_url)
    guest_url = load_guest_link()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json;odata=verbose",
        }
    )
    session.cookies.set("FedAuth", fedauth_cookie, domain=cookie_host, path="/")
    if rtfa_cookie:
        session.cookies.set("rtFa", rtfa_cookie, domain=cookie_host, path="/")
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


def _download_one_file(session, page, sp_site_url, file_rel_url, local_path, log, allow_non_mp4=False):
    """
    Download one SharePoint file. Returns 1 on success, 0 otherwise.
    If allow_non_mp4 is False, only .mp4 files are attempted (recursive folder sync).
    If allow_non_mp4 is True, any file type is tried via REST when response is not HTML;
    Playwright capture is used only for video extensions.
    """
    name = os.path.basename(file_rel_url.rstrip("/"))
    base_site = (sp_site_url or get_sp_site_url()).rstrip("/")

    if os.path.exists(local_path):
        size = os.path.getsize(local_path)
        if size > 1024 * 1024:
            log(f"[-] Skip '{name}' (exists, {size/1024/1024:.1f} MB)")
            return 0
        log(f"⚠️ Re-downloading '{name}' (suspicious size: {size/1024:.0f} KB)")

    if not allow_non_mp4 and not name.lower().endswith(".mp4"):
        log(f"[-] Skip '{name}' (not video)")
        return 0

    log(f"[+] Downloading '{name}'...")

    rel_url_enc = urllib.parse.quote(file_rel_url)
    file_content_url = f"{base_site}/_api/web/GetFileByServerRelativeUrl('{rel_url_enc}')/$value"

    try:
        file_res = session.get(file_content_url, stream=True, timeout=30)
        if file_res.status_code == 200:
            ct = file_res.headers.get("Content-Type", "")
            if "html" not in ct.lower():
                if allow_non_mp4:
                    use_stream = True
                else:
                    cl = int(file_res.headers.get("Content-Length", "0"))
                    use_stream = cl > 1024 * 1024
                if use_stream:
                    with open(local_path, "wb") as f_out:
                        for chunk in file_res.iter_content(chunk_size=65536):
                            f_out.write(chunk)
                    log(
                        f"✅ Downloaded '{name}' via API ({os.path.getsize(local_path)/1024/1024:.1f} MB)"
                    )
                    return 1
    except Exception:
        pass

    ext = os.path.splitext(name)[1].lower()
    use_capture = ext == ".mp4" or ext in VIDEO_EXTENSIONS
    if not use_capture:
        log(f"    ❌ API download failed; skipping capture (not video): '{name}'")
        return 0

    if page is None:
        log(
            "    ❌ No Chromium — install Playwright and retry. Skipping file."
        )
        return 0
    log(f"    🔒 Protected file; downloading via browser...")
    success, size = download_video_via_capture(
        page, file_rel_url, local_path, log, sp_site_url=sp_site_url
    )
    if success:
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
):
    """Recursively sync a SharePoint folder using API listing + Playwright capture for downloads.

    By default, creates ``download_dir / <last_segment_of_folder_rel_url> /`` and mirrors
    subfolders under it (folder-for-folder under that root).
    Set ``local_flat=True`` to download directly into ``download_dir`` (legacy behavior).
    """
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    # Load cookies securely if not provided
    if not fedauth_cookie or not rtfa_cookie:
        fedauth_cookie, rtfa_cookie = get_cookies_secure(log)
        if not fedauth_cookie or not rtfa_cookie:
            log("❌ No valid cookies. Extract them from the browser (web UI) first.")
            return 0

    # Validate cookies before starting sync
    if not validate_cookies_before_sync(fedauth_cookie, rtfa_cookie, log):
        log("❌ Cookie validation failed. Aborting sync.")
        return 0

    os.makedirs(download_dir, exist_ok=True)

    session, page = _sharepoint_session_and_page(
        fedauth_cookie, rtfa_cookie, sp_site_url, log
    )

    target_dir = _mirror_local_root(
        download_dir, folder_rel_url, bool(local_flat), log
    )
    return _sync_folder_recursive(session, page, sp_site_url, folder_rel_url, target_dir, log)


def sync_sharepoint_single_file(
    fedauth_cookie=None,
    rtfa_cookie=None,
    sp_site_url=None,
    file_rel_url=None,
    local_path=None,
    log_callback=None,
):
    """Download a single SharePoint file (any type via REST; capture fallback for video). Returns 0 or 1."""
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

    session, page = _sharepoint_session_and_page(
        fedauth_cookie, rtfa_cookie, sp_site_url, log
    )

    local_path = os.path.abspath(os.path.expanduser(str(local_path).strip()))
    parent = os.path.dirname(local_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    return _download_one_file(
        session,
        page,
        sp_site_url,
        file_rel_url,
        local_path,
        log,
        allow_non_mp4=True,
    )


def _sync_folder_recursive(session, page, sp_site_url, folder_rel_url, download_dir, log):
    """Internal recursive folder sync."""
    os.makedirs(download_dir, exist_ok=True)

    api_headers = {"Accept": "application/json;odata=verbose"}
    base_site = (sp_site_url or get_sp_site_url()).rstrip("/")
    folder_url_enc = urllib.parse.quote(folder_rel_url)
    folder_api_url = f"{base_site}/_api/web/GetFolderByServerRelativeUrl('{folder_url_enc}')"

    # 1. Fetch files
    response = session.get(folder_api_url + "/Files", headers=api_headers)
    if response.status_code != 200:
        log(f"HTTP {response.status_code} for folder {folder_rel_url}")
        if response.status_code == 403:
            log("Cookies may be expired — paste fresh FedAuth/rtFa in settings.")
        return 0

    files = response.json()['d']['results']
    downloaded_count = 0

    for f in files:
        name = f['Name']
        rel_url = f['ServerRelativeUrl']
        local_path = os.path.join(download_dir, name)
        downloaded_count += _download_one_file(
            session,
            page,
            sp_site_url,
            rel_url,
            local_path,
            log,
            allow_non_mp4=False,
        )
    response_folders = session.get(folder_api_url + "/Folders", headers=api_headers)
    if response_folders.status_code == 200:
        folders = response_folders.json()['d']['results']
        for folder in folders:
            folder_name = folder['Name']
            if folder_name == "Forms" or folder_name.startswith('_'):
                continue
            sub_rel_url = folder['ServerRelativeUrl']
            sub_dir = os.path.join(download_dir, folder_name)
            log(f"\n📁 {folder_name}")
            downloaded_count += _sync_folder_recursive(session, page, sp_site_url, sub_rel_url, sub_dir, log)

    return downloaded_count


def start_sync_from_config(fedauth_cookie, rtfa_cookie, sp_site_url, config, log_callback=None):
    """Run sync for one config dict: folder (recursive) or single file via entry_type."""
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    log("==================================================")
    log("⏳ Connecting and scanning files and folders...")
    try:
        et = str(config.get("entry_type") or "folder").strip().lower()
        if et == "file":
            count = sync_sharepoint_single_file(
                fedauth_cookie,
                rtfa_cookie,
                sp_site_url,
                config["rel_url"],
                config["local_path"],
                log_callback,
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
            )
    finally:
        _cleanup_playwright()
    log("==================================================")
    log(f"🎉 Sync finished. Downloaded {count} new file(s).")
    return count


def start_sync(fedauth_cookie, rtfa_cookie, sp_site_url, folder_rel_url, download_dir, log_callback=None):
    """Entry point called by app.py web interface."""
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
    print("  SharePoint sync (hybrid API + browser capture)")
    print("=" * 60)
    print("Use the Flask UI or call start_sync_from_config() from your own script.")
    print("Example SYNC_CONFIG entries belong in SQLite via the dashboard, not hardcoded here.")
    raise SystemExit(0)
