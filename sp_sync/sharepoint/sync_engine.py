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

from sp_sync.sync_metrics import (
    start_file_progress,
    update_file_progress,
    complete_file_progress,
)

def _is_valid_mp4(file_path: str) -> bool:
    """Quick validation to check if an MP4 file has a valid container header and size."""
    try:
        if not os.path.isfile(file_path) or os.path.getsize(file_path) < 64:
            return False
        with open(file_path, "rb") as f:
            head = f.read(128)
            return b"ftyp" in head or b"moov" in head
    except Exception:
        return False


def _resolve_ffprobe_path(ffmpeg_path=None):
    """Prefer ffprobe next to ffmpeg, then PATH."""
    candidates = []
    if ffmpeg_path:
        d = os.path.dirname(os.path.abspath(ffmpeg_path))
        base = os.path.basename(ffmpeg_path)
        probe_name = "ffprobe.exe" if base.lower().endswith(".exe") else "ffprobe"
        candidates.append(os.path.join(d, probe_name))
    which = shutil.which("ffprobe")
    if which:
        candidates.append(which)
    for p in candidates:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _ffprobe_duration_seconds(file_path, ffprobe_path=None):
    """Return media duration in seconds, or None if unreadable."""
    probe = ffprobe_path or _resolve_ffprobe_path()
    if not probe or not os.path.isfile(file_path):
        return None
    try:
        res = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if res.returncode != 0:
            return None
        raw = (res.stdout or "").strip()
        if not raw or raw.upper() == "N/A":
            return None
        duration = float(raw)
        return duration if duration > 0 else None
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        return None


def _ffmpeg_stderr_tail(text, max_lines=8):
    """Last non-empty ffmpeg log lines, for debugging without dumping the full log."""
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-max_lines:])


def _ts_to_mp4_part_cmd(ffmpeg_path, ts_output, part_path):
    """Remux MPEG-TS into a *.mp4.part file. -f mp4 is required: ffmpeg cannot guess the muxer from a .part extension."""
    return [
        ffmpeg_path,
        "-y",
        "-i",
        ts_output,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        part_path,
    ]


def _log_final_mux_failure(res_final, res_mux, log=print):
    """Log the failing ffmpeg step. Prefer cmd_final (res_final) over step-1 res_mux.

    Historically failures logged ``res_mux.returncode`` (often 0) even when the
    final remux failed — always report the step-2 exit code and stderr when present.
    """
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


def _attempt_mux_captured_tracks(ff, v_file, a_file, output_path, log=print, tmp_dir=None):
    """Single mux attempt. Never deletes ``v_file``/``a_file`` on failure.

    Returns ``(ok, size_bytes, res_final, res_mux, ts_output, part_path)``.
    """
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
        res_final = subprocess.run(
            cmd_final, capture_output=True, text=True, timeout=300
        )

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
            # Only delete capture temps after a verified success.
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
    """Mux captured fMP4 video/audio tracks into ``output_path``.

    Returns ``(ok, size_bytes)``. On failure keeps ``v_file``/``a_file``, retries
    the mux once, and if still failing leaves them for recovery with paths logged.
    """
    ok, size, res_final, res_mux, ts_output, part_path = _attempt_mux_captured_tracks(
        ff, v_file, a_file, output_path, log=log, tmp_dir=tmp_dir
    )
    if ok:
        return True, size

    # Keep capture inputs; retry the full mux once before giving up.
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
    return ""


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


def _project_ffmpeg_candidates():
    """Bundled ffmpeg under repo data/bin (optional manual install)."""
    root = project_root()
    names = ("ffmpeg", "ffmpeg.exe")
    for sub in ("data/bin", "bin", "tools/ffmpeg"):
        folder = os.path.join(root, sub)
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                yield path


def resolve_ffmpeg_path(log=print):
    """Resolve ffmpeg: $FFMPEG_PATH, PATH, imageio-ffmpeg bundle, project bin, Windows."""
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
    for bundled in _project_ffmpeg_candidates():
        FFMPEG_PATH, FFMPEG_DIR = bundled, os.path.dirname(bundled)
        return FFMPEG_PATH
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.isfile(bundled):
            FFMPEG_PATH, FFMPEG_DIR = bundled, os.path.dirname(bundled)
            return FFMPEG_PATH
    except ImportError:
        pass
    except Exception:
        pass
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
    log(
        "⚠️ ffmpeg not found — install system ffmpeg (sudo apt install ffmpeg), "
        "pip install imageio-ffmpeg, or set FFMPEG_PATH"
    )
    return None


# Resolve once at import without printing (avoids noisy startup when mux is not used yet).
resolve_ffmpeg_path(log=lambda *_args, **_kwargs: None)

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
    guest_link_url = (guest_link_url or load_guest_link() or "").strip()
    if guest_link_url and not _sp_sharing_url_has_stream_path(guest_link_url):
        if guest_link_url.rstrip("/") == (sp_site_url or get_sp_site_url()).rstrip("/"):
            guest_link_url = ""

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

    if guest_link_url.startswith("http"):
        for attempt in range(2):
            try:
                page.goto(guest_link_url, wait_until="networkidle", timeout=90000)
                page.wait_for_timeout(4000)
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
    elif not session_ok:
        log("  ℹ️ No :f:/ guest link configured — skipping guest warm-up")

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
            if _playwright_page_is_login_wall(page):
                log("  ❌ Direct OneDrive connection still on sign-in page — update cookies")
            else:
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


def _capture_work_dir(output_path):
    """Per-output temp directory so interrupted captures can resume independently."""
    import hashlib

    abs_out = os.path.abspath(output_path)
    digest = hashlib.sha1(abs_out.encode("utf-8")).hexdigest()[:12]
    stem = _sanitize_dir_name(os.path.splitext(os.path.basename(output_path))[0])[:48]
    return os.path.join(TMP_DIR, f"{stem}_{digest}")


def _progress_meta_path(work_dir):
    return os.path.join(work_dir, "capture_progress.json")


def _load_capture_progress(work_dir):
    path = _progress_meta_path(work_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _save_capture_progress(work_dir, meta):
    os.makedirs(work_dir, exist_ok=True)
    path = _progress_meta_path(work_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _clear_capture_progress(work_dir):
    path = _progress_meta_path(work_dir)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _try_promote_part_file(output_path, log=print):
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


def _flush_capture_buffers(page, v_file, a_file, cursors, log=print):
    """Append newly captured SourceBuffer chunks to disk; update ``cursors`` in place."""
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
                try:
                    batch = page.evaluate(
                        "([t, s, e]) => window.__capturedBuffers[t].slice(s, e).map(x => x.b64)",
                        [track, start_idx, min(start_idx + batch_size, num)],
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


def _try_resume_from_existing_capture(output_path, log=print, ffmpeg_path=None):
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
        # Incomplete capture: caller may continue from last_time.
        return None

    ff = ffmpeg_path or resolve_ffmpeg_path(log)
    if not ff or not os.path.isfile(ff):
        log("    ❌ ffmpeg not available — cannot remux resumed capture")
        return False, 0
    log("    ♻️ Resuming: remuxing previously captured tracks (skipping browser capture)...")
    ok, size = _mux_captured_tracks(ff, v_file, a_file, output_path, log=log, tmp_dir=work_dir)
    if ok:
        _clear_capture_progress(work_dir)
    return ok, size


def download_video_via_capture(
    page, file_rel_url, output_path, log=print, sp_site_url=None
):
    """Download a video by hooking the browser's SourceBuffer to capture decrypted data.
    Returns (success, file_size).

    Interrupted runs resume from ``output_path.part`` or per-file capture temps under
    ``TMP_DIR`` when possible; otherwise capture continues from the last saved time.
    """
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
        # Fresh capture — drop stale temps for this output.
        for f in (v_file, a_file):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        _clear_capture_progress(work_dir)
        resume_from = 0
        log(f"    ⏱️ Duration: {dur:.0f}s ({dur/60:.1f} min). Capturing at 16x...")
        page.evaluate(
            "() => { const v = document.querySelector('video'); if(v) { v.playbackRate = 16.0; v.muted = true; v.play(); } }"
        )
        last_time = 0

    stuck_count = 0
    start_time = time.time()
    last_log_time = start_time
    last_flush_time = start_time

    while True:
        time.sleep(2)
        state = page.evaluate("""() => { 
            const v = document.querySelector('video'); 
            return v ? { time: v.currentTime, ended: v.ended, paused: v.paused } : { time: 0, ended: true, paused: true }; 
        }""")

        now = time.time()
        if now - last_log_time >= 10:
            progress_percent = (state['time'] / dur) * 100 if dur > 0 else 0
            elapsed = now - start_time
            buffer_counts = page.evaluate("() => ({ v: window.__capturedBuffers.video.length, a: window.__capturedBuffers.audio.length })")
            log(f"    📊 Progress: {progress_percent:.1f}% | time: {state['time']:.0f}/{dur:.0f}s | buffers V={buffer_counts['v']} A={buffer_counts['a']} | elapsed: {elapsed:.0f}s")
            last_log_time = now

        current_time = state['time']

        # Periodically flush buffers so a crash mid-lecture keeps reusable parts.
        if now - last_flush_time >= 30 or state['ended']:
            _flush_capture_buffers(page, v_file, a_file, cursors, log=log)
            _save_capture_progress(
                work_dir,
                {
                    "output_path": os.path.abspath(output_path),
                    "last_time": float(current_time or last_time or 0),
                    "duration": float(dur or 0),
                    "capture_complete": bool(state['ended']),
                    "video_bytes": os.path.getsize(v_file) if os.path.exists(v_file) else 0,
                    "audio_bytes": os.path.getsize(a_file) if os.path.exists(a_file) else 0,
                },
            )
            last_flush_time = now

        if state['ended']:
            break

        if current_time == last_time:
            stuck_count += 1
            if stuck_count > 15:  # 30 seconds stuck
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

    # Final flush of any remaining chunks.
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

    # Reset captured buffers for next file
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
    if ok:
        _clear_capture_progress(work_dir)
    return ok, size



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


def attach_sharepoint_browser_rest_hints(session, sp_site_url, referer_url=None):
    """
    Set headers many SharePoint Online endpoints expect for XHR-style REST calls
    (reduces 403 UnauthorizedAccessException on /_api/web/.../Folders for some tenants).
    """
    ref = (referer_url or "").strip()
    if not ref.startswith("http"):
        ref = (sp_site_url or "").strip().rstrip("/") + "/"
    session.headers["Referer"] = ref
    session.headers["X-Requested-With"] = "XMLHttpRequest"


def try_attach_sharepoint_form_digest(session, sp_site_url, timeout=30):
    """POST /_api/contextinfo and set X-RequestDigest on session when SharePoint allows it."""
    base = (sp_site_url or "").strip().rstrip("/")
    url = base + "/_api/contextinfo"
    try:
        r = session.post(
            url,
            data="{}",
            headers={
                "Accept": "application/json;odata=verbose",
                "Content-Type": "application/json;odata=verbose",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return
        j = r.json()
        d = j.get("d") or {}
        gci = d.get("GetContextWebInformation")
        if isinstance(gci, dict):
            digest = gci.get("FormDigestValue")
            if digest:
                session.headers["X-RequestDigest"] = digest
    except Exception:
        pass


def extract_request_digest_from_html(html: str) -> str | None:
    """Parse SharePoint / OneDrive __REQUESTDIGEST from HTML (matches browser X-RequestDigest)."""
    if not html:
        return None
    m = re.search(
        r'name=["\']__REQUESTDIGEST["\']\s+value=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r'id=["\']__REQUESTDIGEST["\'][^>]*value=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if m:
        return m.group(1).strip()
    return None


def apply_sharepoint_page_digest(session, page_url: str, timeout=45) -> None:
    """GET a SharePoint HTML page and copy __REQUESTDIGEST into session headers when present."""
    u = (page_url or "").strip()
    if not u.startswith("http"):
        return
    try:
        prev = session.headers.get("Accept")
        session.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        r = session.get(u, timeout=timeout, allow_redirects=True)
        if prev:
            session.headers["Accept"] = prev
        else:
            session.headers["Accept"] = "application/json;odata=verbose"
        d = extract_request_digest_from_html(r.text or "")
        if d:
            session.headers["X-RequestDigest"] = d
    except Exception:
        try:
            session.headers["Accept"] = "application/json;odata=verbose"
        except Exception:
            pass


def apply_sharepoint_chromium_security_headers(session, sp_site_url: str) -> None:
    """Optional Sec-Fetch-* / Origin headers some SPO WAFs correlate with real browsers."""
    try:
        p = urllib.parse.urlparse((sp_site_url or "").strip())
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
        if origin:
            session.headers["Origin"] = origin
        session.headers.setdefault("Sec-Fetch-Dest", "empty")
        session.headers.setdefault("Sec-Fetch-Mode", "cors")
        session.headers.setdefault("Sec-Fetch-Site", "same-origin")
    except Exception:
        pass


def prime_sharepoint_session_for_browse(
    session,
    sp_site_url: str,
    *,
    prime_url: str | None = None,
    fedauth_cookie: str | None = None,
    rtfa_cookie: str | None = None,
) -> None:
    """
    Full warm-up for REST browse: HTML prime, browser-like headers, page digest, context digest fallback.

    Resolve only checks GetFolder metadata; listing /Folders is stricter on some tenants.
    """
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
    # Mirror auth cookies to parent .sharepoint.com (matches many browser FedAuth domains)
    if fedauth_cookie and rtfa_cookie:
        try:
            host = urllib.parse.urlparse(sp_site_url or "").netloc.lower()
            if host.endswith(".sharepoint.com"):
                session.cookies.set("FedAuth", fedauth_cookie, domain=".sharepoint.com", path="/")
                session.cookies.set("rtFa", rtfa_cookie, domain=".sharepoint.com", path="/")
        except Exception:
            pass


def _sp_unquote_path_value(raw: str) -> str:
    """Decode id/folder query values; some tenants double-encode %2F."""
    s = (raw or "").strip().replace("+", " ")
    if not s:
        return ""
    s = urllib.parse.unquote(s)
    if "%" in s:
        s2 = urllib.parse.unquote(s)
        if s2 != s:
            s = s2
    return s


def _sp_extract_onedrive_id_param(url):
    """
    Extract server-relative path from id= / folder= / RootFolder= / res= query (decoded).

    Keys are matched case-insensitively; fragment is parsed like a query string when it contains '='.
    """
    if not (isinstance(url, str) and url.strip()):
        return None
    parsed = urllib.parse.urlparse(url.strip())
    chunks = []
    if parsed.query:
        chunks.append(parsed.query)
    frag = (parsed.fragment or "").lstrip("?")
    frag_unq = urllib.parse.unquote(frag) if frag else ""
    if frag_unq and ("=" in frag_unq or "%3d" in frag.lower()):
        chunks.append(frag_unq)
    combined = "&".join([c for c in chunks if c])
    if not combined:
        return None
    qs = urllib.parse.parse_qs(combined, keep_blank_values=False)
    for k, vals in qs.items():
        lk = (k or "").lower()
        if lk not in ("id", "folder", "res", "rootfolder"):
            continue
        if not vals or not str(vals[0]).strip():
            continue
        decoded = _sp_unquote_path_value(str(vals[0]))
        if decoded:
            return decoded
    if frag_unq:
        for pat in (r"(?:^|[?&])id=([^&]+)", r"(?:^|[?&])folder=([^&]+)", r"(?:^|[?&])rootfolder=([^&]+)"):
            m = re.search(pat, frag_unq, re.I)
            if m:
                decoded = _sp_unquote_path_value(m.group(1))
                if decoded:
                    return decoded
    return None


def _sp_extract_server_rel_from_sharepoint_html(html: str) -> str | None:
    """
    Best-effort path extraction from OneDrive/SPO HTML after redirects (SPA / mixed markup).

    Looks for serverRelativeUrl / listUrl JSON, og:url / canonical links, and onedrive.aspx id=.
    """
    if not html or len(html) < 20:
        return None

    def _norm_path(s: str) -> str | None:
        s = (s or "").strip().replace("&amp;", "&").replace("\\/", "/")
        if not s:
            return None
        s = _sp_unquote_path_value(s)
        if s.startswith("http"):
            try:
                p = urllib.parse.urlparse(s)
                s = _sp_unquote_path_value(p.path or "")
            except Exception:
                return None
        if not s.startswith("/"):
            s = "/" + s.lstrip("/")
        low = s.lower()
        if "/personal/" not in low and "/sites/" not in low:
            return None
        return s

    for pat in (
        r'<meta\s+property=["\']og:url["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:url["\']',
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
    ):
        m = re.search(pat, html, re.I)
        if not m:
            continue
        href = m.group(1).strip()
        cand = _sp_extract_onedrive_id_param(href) or _norm_path(href)
        if cand:
            return cand

    for pat in (
        r'"serverRelativeUrl"\s*:\s*"([^"]+)"',
        r"'serverRelativeUrl'\s*:\s*'([^']+)'",
        r'"listUrl"\s*:\s*"([^"]+)"',
        r"'listUrl'\s*:\s*'([^']+)'",
    ):
        m = re.search(pat, html, re.I)
        if not m:
            continue
        cand = _norm_path(m.group(1))
        if cand:
            return cand

    for pat in (
        r'(?:onedrive|embed)\.aspx[^"\'<>\s]*[?&]id=([^&"\'<>\s]+)',
        r'(?:onedrive|embed)\.aspx[^"\'<>\s]*[?&]folder=([^&"\'<>\s]+)',
        r'[?&]RootFolder=([^&"\'<>\s]+)',
    ):
        m = re.search(pat, html, re.I)
        if not m:
            continue
        cand = _norm_path(m.group(1))
        if cand:
            return cand

    return None


def _sp_sharing_url_has_stream_path(url: str) -> bool:
    """OneDrive-style sharing URL path contains :f:/, :w:/, or :x:/ (not plain /personal/...)."""
    low = (url or "").lower()
    return ":f:/" in low or ":w:/" in low or ":x:/" in low


def _sp_body_indicates_sharing_link_removed(html: str) -> bool:
    """Detect SharePoint 'link removed / access removed' HTML (EN and common AR phrases)."""
    if not html:
        return False
    low = html.lower()
    if "this link has been removed" in low:
        return True
    if "access to this document has been removed" in low:
        return True
    if "the link has been removed" in low:
        return True
    if "link has been removed" in low and "sharepoint" in low:
        return True
    # Arabic (Microsoft sometimes serves AR pages)
    if "تمت إزالة هذا الرابط" in html or "تم إزالة هذا الرابط" in html:
        return True
    if "لم يعد بإمكانك الوصول" in html and "رابط" in html:
        return True
    return False


def _sp_resolve_via_v2_share_driveitem(session, sharing_url: str) -> tuple[str | None, int | None]:
    """
    Resolve :f:/… / :w:/… sharing URLs via _api/v2.0/shares/u!…/driveItem (no HTML redirect).

    Returns (server_relative_path_or_None, http_status_or_None_on_error).
    """
    from sp_sync.sharepoint.rest_folder_list import _v2_share_token_from_url

    u = (sharing_url or "").strip()
    if not u.startswith("http") or not _sp_sharing_url_has_stream_path(u):
        return None, None
    try:
        p = urllib.parse.urlparse(u)
        host = (p.netloc or "").lower()
        if "sharepoint.com" not in host:
            return None, None
        sid = _v2_share_token_from_url(u)
        api = f"{p.scheme}://{p.netloc}/_api/v2.0/shares/{sid}/driveItem"
        prev_accept = session.headers.get("Accept")
        session.headers["Accept"] = "application/json"
        r = session.get(api, timeout=60)
        st = r.status_code
        if prev_accept:
            session.headers["Accept"] = prev_accept
        else:
            session.headers["Accept"] = "application/json;odata=verbose"
        if st != 200:
            return None, st
        j = r.json()
    except Exception:
        try:
            session.headers["Accept"] = "application/json;odata=verbose"
        except Exception:
            pass
        return None, None

    if not isinstance(j, dict):
        return None, None

    for key in ("webUrl", "webDavUrl"):
        val = j.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        ex = _sp_extract_onedrive_id_param(val)
        if ex:
            return ex, 200
        pp = urllib.parse.urlparse(val.strip())
        sp = _sp_server_rel_from_personal_browser_path(pp)
        if sp:
            return sp, 200

    spids = j.get("sharepointIds")
    if isinstance(spids, dict):
        for sk in ("listPath", "sitePath"):
            sp_path = spids.get(sk)
            if isinstance(sp_path, str) and "/personal/" in sp_path.lower():
                s = sp_path.strip()
                if not s.startswith("/"):
                    s = "/" + s.lstrip("/")
                return urllib.parse.unquote(s), 200

    return None, 200


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
    from sp_sync.sharepoint.rest_folder_list import (
        encode_path_for_get_folder_param,
        normalize_server_relative_folder_path,
    )

    norm = normalize_server_relative_folder_path(server_rel)
    enc = encode_path_for_get_folder_param(norm)
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
        m = re.search(
            r"(?:onedrive\.aspx|embed\.aspx)\?[^\"'>]*\bid=([^&\"'>\s]+)",
            body,
            re.I,
        )
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


def _download_one_file(
    session,
    page,
    sp_site_url,
    file_rel_url,
    local_path,
    log,
    allow_non_mp4=False,
    *,
    sharing_link_url: str | None = None,
    item_id: str | None = None,
    share_root_norm: str | None = None,
    remote_size: int = 0,
):
    """
    Download one SharePoint file. Returns 1 on success, 0 otherwise.
    If allow_non_mp4 is False, only .mp4 files are attempted (recursive folder sync).
    If allow_non_mp4 is True, any file type is tried via REST/shares when response is not HTML;
    Playwright capture is used only for video extensions.
    """
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
        elif remote_size and remote_size > 0 and size != remote_size:
            is_corrupt = True

        if not is_corrupt:
            log(f"[-] Skip '{name}' (exists, {size/1024/1024:.1f} MB)")
            return 0
        log(f"⚠️ Re-downloading '{name}' (corrupted or incomplete size: {size/1024:.0f} KB)")
    else:
        # Interrupted previous run left a valid .part — promote instead of starting over.
        promoted = _try_promote_part_file(local_path, log=log)
        if promoted is not None:
            complete_file_progress(name)
            return 1

    if not allow_non_mp4 and not is_video:
        log(f"[-] Skip '{name}' (not video)")
        return 0

    log(f"[+] Downloading '{name}'...")
    parent_folder_name = os.path.basename(os.path.dirname(local_path))
    start_file_progress(parent_folder_name, name, remote_size or 0)

    # Strategy 1: v2.0/shares API if sharing link is provided
    from sp_sync.sharepoint.rest_folder_list import (
        _should_try_v2_shares,
        _v2_share_token_from_url,
    )

    if sharing_link_url and _should_try_v2_shares(sharing_link_url):
        try:
            p = urllib.parse.urlparse(base_site)
            netloc = (p.netloc or "").lower()
            if netloc and "sharepoint.com" in netloc:
                root = f"{p.scheme}://{netloc}"
                sid = _v2_share_token_from_url(sharing_link_url)
                v2_content_url = None
                if item_id:
                    v2_content_url = f"{root}/_api/v2.0/shares/{sid}/driveItem/{urllib.parse.quote(str(item_id).strip(), safe='')}/content"
                elif share_root_norm and file_rel_url.startswith(share_root_norm.rstrip("/") + "/"):
                    rel_under = file_rel_url[len(share_root_norm.rstrip("/")) :].lstrip("/")
                    if rel_under:
                        enc_rel = "/".join(urllib.parse.quote(s, safe="") for s in rel_under.split("/") if s)
                        v2_content_url = f"{root}/_api/v2.0/shares/{sid}/driveItem:/{enc_rel}:/content"

                if v2_content_url:
                    prev_accept = session.headers.get("Accept")
                    session.headers["Accept"] = "*/*"
                    part_path = local_path + ".part"
                    try:
                        v2_res = session.get(v2_content_url, stream=True, timeout=60, allow_redirects=True)
                        if v2_res.status_code in (200, 206):
                            ct = (v2_res.headers.get("Content-Type") or "").lower()
                            if "html" not in ct:
                                tot = int(v2_res.headers.get("Content-Length", 0) or remote_size or 0)
                                dl = 0
                                with open(part_path, "wb") as f_out:
                                    for chunk in v2_res.iter_content(chunk_size=65536):
                                        f_out.write(chunk)
                                        dl += len(chunk)
                                        update_file_progress(dl, tot)
                                os.replace(part_path, local_path)
                                complete_file_progress(name)
                                log(
                                    f"✅ Downloaded '{name}' via shares API ({os.path.getsize(local_path)/1024/1024:.1f} MB)"
                                )
                                return 1
                    finally:
                        if prev_accept:
                            session.headers["Accept"] = prev_accept
                        if os.path.exists(part_path):
                            try:
                                os.remove(part_path)
                            except Exception:
                                pass
        except Exception:
            pass

    # Strategy 2: Classic SharePoint REST API
    rel_url_enc = urllib.parse.quote(file_rel_url, safe="/")
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
                    part_path = local_path + ".part"
                    tot = int(file_res.headers.get("Content-Length", 0) or remote_size or 0)
                    dl = 0
                    try:
                        with open(part_path, "wb") as f_out:
                            for chunk in file_res.iter_content(chunk_size=65536):
                                f_out.write(chunk)
                                dl += len(chunk)
                                update_file_progress(dl, tot)
                        os.replace(part_path, local_path)
                        complete_file_progress(name)
                        log(
                            f"✅ Downloaded '{name}' via API ({os.path.getsize(local_path)/1024/1024:.1f} MB)"
                        )
                        return 1
                    finally:
                        if os.path.exists(part_path):
                            try:
                                os.remove(part_path)
                            except Exception:
                                pass
    except Exception:
        pass

    # Strategy 3: Playwright browser video capture (for protected streams)
    use_capture = is_video
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
        complete_file_progress(name)
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
    sharing_link: str | None = None,
    allow_non_mp4: bool = True,
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
    try:
        _, _, free = shutil.disk_usage(download_dir)
        if free < 2 * 1024 * 1024 * 1024:
            log(f"⚠️ Warning: Low disk space! Only {free / (1024**3):.1f} GB remaining on target drive.")
    except Exception:
        pass

    session, page = _sharepoint_session_and_page(
        fedauth_cookie, rtfa_cookie, sp_site_url, log
    )

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

    target_dir = _mirror_local_root(
        download_dir, folder_rel_url, bool(local_flat), log
    )
    return _sync_folder_recursive(
        session,
        page,
        sp_site_url,
        folder_rel_url,
        target_dir,
        log,
        sharing_link_url=share_u or None,
        share_root_norm=share_root or None,
        allow_non_mp4=allow_non_mp4,
    )


def sync_sharepoint_single_file(
    fedauth_cookie=None,
    rtfa_cookie=None,
    sp_site_url=None,
    file_rel_url=None,
    local_path=None,
    log_callback=None,
    *,
    sharing_link: str | None = None,
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
        sharing_link_url=sharing_link or None,
    )


def _sync_folder_recursive(
    session,
    page,
    sp_site_url,
    folder_rel_url,
    download_dir,
    log,
    *,
    sharing_link_url: str | None = None,
    share_root_norm: str | None = None,
    allow_non_mp4: bool = True,
    parent_item_id: str | None = None,
):
    """Internal recursive folder sync (same listing stack as /sp/browse)."""
    from sp_sync.sharepoint.rest_folder_list import list_folder_children_rest

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
        return 0

    method = listing.get("method") or "rest"
    n_f = len(listing.get("folders") or [])
    n_files = len(listing.get("files") or [])
    ext_counts: dict[str, int] = {}
    for _row in listing.get("files") or []:
        _ext = os.path.splitext(_row.get("name") or "")[1].lower() or "(none)"
        ext_counts[_ext] = ext_counts.get(_ext, 0) + 1
    _types = ", ".join(f"{n} {ext}" for ext, n in sorted(ext_counts.items()))
    if _types:
        log(f"📋 Listed via {method}: {n_f} folder(s), {n_files} file(s) ({_types})")
    else:
        log(f"📋 Listed via {method}: {n_f} folder(s), {n_files} file(s)")

    downloaded_count = 0
    for file_row in listing.get("files") or []:
        name = file_row.get("name") or ""
        rel_url = file_row.get("rel_url") or ""
        if not name or not rel_url:
            continue
        file_item_id = file_row.get("item_id")
        sz = file_row.get("size") or 0
        local_path = os.path.join(download_dir, name)
        downloaded_count += _download_one_file(
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
        )

    for folder_row in listing.get("folders") or []:
        folder_name = folder_row.get("name") or ""
        sub_rel_url = folder_row.get("rel_url") or ""
        if not folder_name or not sub_rel_url:
            continue
        if folder_name == "Forms" or folder_name.startswith("_"):
            continue
        sub_dir = os.path.join(download_dir, folder_name)
        log(f"\n📁 {folder_name}")
        downloaded_count += _sync_folder_recursive(
            session,
            page,
            sp_site_url,
            sub_rel_url,
            sub_dir,
            log,
            sharing_link_url=sharing_link_url,
            share_root_norm=share_root_norm,
            allow_non_mp4=allow_non_mp4,
            parent_item_id=folder_row.get("item_id"),
        )

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
            )
    except json.JSONDecodeError as exc:
        log(f"❌ Server returned non-JSON while listing folder: {exc}")
        log(
            "   Refresh FedAuth/rtFa, or add the original :f:/ sharing link to this folder "
            "(edit config / re-add from explorer)."
        )
        count = 0
    except Exception as exc:
        log(f"❌ Error: {exc}")
        count = 0
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
