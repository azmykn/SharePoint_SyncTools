"""Shared ffmpeg / ffprobe resolution, MP4 validation, and duration checks.

Extracted from ``sp_sync.sharepoint.sync_engine`` to eliminate the circular import
between ``sync_engine`` and ``silence_trim`` and to provide clean media utilities
for health checks and post-download verification.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, Optional

from sp_sync.config.settings import duration_tolerance
from sp_sync.paths import project_root

LogFn = Callable[[str], None]

FFMPEG_PATH: Optional[str] = None
FFMPEG_DIR: Optional[str] = None


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


def resolve_ffmpeg_path(log: LogFn = print) -> Optional[str]:
    """Resolve ffmpeg: $FFMPEG_PATH, PATH, project bin, imageio-ffmpeg bundle, Windows."""
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


def _resolve_ffprobe_path(ffmpeg_path: Optional[str] = None) -> Optional[str]:
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


def _ffprobe_duration_seconds(
    file_path: str, ffprobe_path: Optional[str] = None
) -> Optional[float]:
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


def _ffmpeg_stderr_tail(text: Optional[str], max_lines: int = 8) -> str:
    """Last non-empty ffmpeg log lines, for debugging without dumping the full log."""
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-max_lines:])


def _duration_outside_tolerance(
    actual,
    expected,
    pct=None,
    abs_sec=None,
    *,
    tolerance_fn=None,
) -> bool:
    """True when |actual-expected| exceeds ``max(abs_sec, pct*expected)``."""
    if actual is None or expected is None:
        return False
    try:
        actual_f = float(actual)
        expected_f = float(expected)
    except (TypeError, ValueError):
        return False
    if actual_f <= 0 or expected_f <= 0:
        return False
    if pct is None or abs_sec is None:
        fn = tolerance_fn or duration_tolerance
        pct, abs_sec = fn()
    diff = abs(actual_f - expected_f)
    allowed = max(float(abs_sec), float(expected_f) * float(pct))
    return diff > allowed


def _verify_output_duration(
    file_path: str,
    expected_duration,
    log: LogFn = print,
    ffmpeg_path: Optional[str] = None,
    *,
    resolve_ffprobe_fn=None,
    ffprobe_duration_fn=None,
    tolerance_fn=None,
):
    """ffprobe the file and compare to expected. Returns (ok, actual_duration)."""
    if expected_duration is None:
        return True, None
    try:
        expected_f = float(expected_duration)
    except (TypeError, ValueError):
        return True, None
    if expected_f <= 0:
        return True, None

    probe_resolver = resolve_ffprobe_fn or _resolve_ffprobe_path
    dur_reader = ffprobe_duration_fn or _ffprobe_duration_seconds
    tol_reader = tolerance_fn or duration_tolerance

    probe = probe_resolver(ffmpeg_path)
    actual = dur_reader(file_path, probe) if probe else None
    if actual is None:
        log("    ❌ Duration verification failed: ffprobe could not read output")
        return False, None
    if _duration_outside_tolerance(actual, expected_f, tolerance_fn=tol_reader):
        pct, abs_sec = tol_reader()
        allowed = max(abs_sec, expected_f * pct)
        log(
            f"    ❌ Duration mismatch: got {actual:.1f}s, expected {expected_f:.1f}s "
            f"(tolerance ±{allowed:.1f}s)"
        )
        return False, actual
    log(f"    ✅ Duration verified: {actual:.1f}s (expected {expected_f:.1f}s)")
    return True, actual


def _ts_to_mp4_part_cmd(ffmpeg_path: str, ts_output: str, part_path: str) -> list[str]:
    """Remux MPEG-TS into a *.mp4.part file. -f mp4 is required: ffmpeg cannot guess the muxer from .part."""
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
