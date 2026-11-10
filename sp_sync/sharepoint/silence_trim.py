"""Trim leading/trailing silence by keeping the longest continuous speech region.

Uses ffmpeg ``silencedetect`` only to find speech bounds. Silence in the middle of
the main lecture is never cut. Short isolated blips before/after the main block
are dropped when speech segments are merged across gaps shorter than a threshold.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Callable, List, Optional, Tuple

from sp_sync.config.settings import load_settings
from sp_sync.sharepoint.sync_engine import (
    _ffprobe_duration_seconds,
    _is_valid_mp4,
    _resolve_ffprobe_path,
    resolve_ffmpeg_path,
)

LogFn = Callable[[str], None]

_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


def silence_trim_settings() -> dict:
    s = load_settings()
    return {
        "enabled": bool(s.get("trim_silence_enabled", False)),
        "noise_db": float(s.get("trim_silence_noise_db", -70.0)),
        "min_silence_dur": float(s.get("trim_silence_min_duration", 2.0)),
        "margin_sec": float(s.get("trim_silence_margin_sec", 3.0)),
        "merge_gap_sec": float(s.get("trim_silence_merge_gap_sec", 120.0)),
    }


def parse_silencedetect_stderr(stderr_text: str) -> List[Tuple[float, float]]:
    """Return list of (silence_start, silence_end) from ffmpeg silencedetect log."""
    starts: List[float] = []
    ends: List[float] = []
    for line in (stderr_text or "").splitlines():
        m_s = _SILENCE_START_RE.search(line)
        if m_s:
            starts.append(float(m_s.group(1)))
        m_e = _SILENCE_END_RE.search(line)
        if m_e:
            ends.append(float(m_e.group(1)))
    pairs: List[Tuple[float, float]] = []
    # Pair in order; if file ends in silence, last start may lack end.
    i = j = 0
    while i < len(starts):
        st = starts[i]
        if j < len(ends) and ends[j] >= st:
            pairs.append((st, ends[j]))
            j += 1
            i += 1
        else:
            # Unclosed trailing silence — caller supplies duration as end.
            pairs.append((st, -1.0))
            i += 1
            break
    return pairs


def silence_intervals_to_speech(
    silence_pairs: List[Tuple[float, float]], duration: float
) -> List[Tuple[float, float]]:
    """Invert silence intervals into speech intervals within ``[0, duration]``."""
    if duration <= 0:
        return []
    cleaned: List[Tuple[float, float]] = []
    for st, en in silence_pairs:
        end = duration if en < 0 else min(en, duration)
        start = max(0.0, min(st, duration))
        if end > start:
            cleaned.append((start, end))
    cleaned.sort()
    speech: List[Tuple[float, float]] = []
    cursor = 0.0
    for st, en in cleaned:
        if st > cursor:
            speech.append((cursor, st))
        cursor = max(cursor, en)
    if cursor < duration:
        speech.append((cursor, duration))
    return [(a, b) for a, b in speech if b - a > 1e-6]


def merge_speech_segments(
    segments: List[Tuple[float, float]], merge_gap_sec: float
) -> List[Tuple[float, float]]:
    """Merge neighboring speech segments separated by a gap shorter than ``merge_gap_sec``."""
    if not segments:
        return []
    ordered = sorted(segments, key=lambda x: x[0])
    merged = [list(ordered[0])]
    for st, en in ordered[1:]:
        prev = merged[-1]
        gap = st - prev[1]
        if gap <= merge_gap_sec:
            prev[1] = max(prev[1], en)
        else:
            merged.append([st, en])
    return [(a, b) for a, b in merged]


def longest_speech_region(
    segments: List[Tuple[float, float]],
) -> Optional[Tuple[float, float]]:
    if not segments:
        return None
    return max(segments, key=lambda se: se[1] - se[0])


def compute_trim_window(
    silence_pairs: List[Tuple[float, float]],
    duration: float,
    *,
    merge_gap_sec: float = 120.0,
    margin_sec: float = 3.0,
) -> Optional[Tuple[float, float]]:
    """Return ``(start, end)`` cut points for the longest merged speech region + margin."""
    speech = silence_intervals_to_speech(silence_pairs, duration)
    merged = merge_speech_segments(speech, merge_gap_sec)
    region = longest_speech_region(merged)
    if region is None:
        return None
    start = max(0.0, region[0] - margin_sec)
    end = min(duration, region[1] + margin_sec)
    if end - start < 1.0:
        return None
    # No meaningful trim.
    if start <= 0.05 and end >= duration - 0.05:
        return None
    return start, end


def _run_silencedetect(ffmpeg_path: str, file_path: str, noise_db: float, min_dur: float) -> str:
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-i",
        file_path,
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-f",
        "null",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return (res.stderr or "") + (res.stdout or "")


def trim_silence_edges(
    file_path: str,
    log: LogFn = print,
    *,
    ffmpeg_path: Optional[str] = None,
    settings: Optional[dict] = None,
) -> bool:
    """Trim leading/trailing silence in-place after verifying the trimmed copy.

    Keeps the original until the trimmed file is duration-sane and playable, then
    replaces. Returns True when a trim was applied (or nothing to trim); False on
    hard failure (original left intact).
    """
    cfg = settings or silence_trim_settings()
    if not cfg.get("enabled"):
        return True
    if not os.path.isfile(file_path):
        return False

    ff = ffmpeg_path or resolve_ffmpeg_path(log)
    if not ff or not os.path.isfile(ff):
        log("    ⚠️ Silence trim skipped: ffmpeg not available")
        return True

    probe = _resolve_ffprobe_path(ff)
    duration = _ffprobe_duration_seconds(file_path, probe) if probe else None
    if duration is None or duration <= 0:
        log("    ⚠️ Silence trim skipped: could not read duration")
        return True

    noise_db = float(cfg.get("noise_db", -70.0))
    min_dur = float(cfg.get("min_silence_dur", 2.0))
    margin = float(cfg.get("margin_sec", 3.0))
    merge_gap = float(cfg.get("merge_gap_sec", 120.0))

    log(
        f"    ✂️ Detecting silence (noise={noise_db}dB, min={min_dur}s, "
        f"merge_gap={merge_gap}s, margin={margin}s)..."
    )
    try:
        stderr = _run_silencedetect(ff, file_path, noise_db, min_dur)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"    ⚠️ Silence detect failed: {exc}")
        return True

    pairs = parse_silencedetect_stderr(stderr)
    # Close trailing silence with media duration.
    pairs = [(st, duration if en < 0 else en) for st, en in pairs]
    window = compute_trim_window(
        pairs, duration, merge_gap_sec=merge_gap, margin_sec=margin
    )
    if window is None:
        log("    ✂️ No leading/trailing silence trim needed")
        return True

    start, end = window
    log(f"    ✂️ Trimming to main speech {start:.1f}s–{end:.1f}s (was 0–{duration:.1f}s)")

    parent = os.path.dirname(os.path.abspath(file_path)) or "."
    fd, trimmed_path = tempfile.mkstemp(prefix="trim_", suffix=".mp4", dir=parent)
    os.close(fd)
    try:
        cmd = [
            ff,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-to",
            f"{end:.3f}",
            "-i",
            file_path,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            trimmed_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode != 0 or not os.path.isfile(trimmed_path):
            log("    ⚠️ Silence trim remux failed; keeping original")
            return True
        if not _is_valid_mp4(trimmed_path) or os.path.getsize(trimmed_path) < 1024:
            log("    ⚠️ Trimmed file invalid; keeping original")
            return True
        new_dur = _ffprobe_duration_seconds(trimmed_path, probe) if probe else None
        expected_len = end - start
        if new_dur is None or new_dur < 1.0:
            log("    ⚠️ Trimmed duration unreadable; keeping original")
            return True
        # Allow container/keyframe slack from stream copy.
        if abs(new_dur - expected_len) > max(5.0, expected_len * 0.05):
            log(
                f"    ⚠️ Trimmed duration {new_dur:.1f}s vs expected ~{expected_len:.1f}s; "
                "keeping original"
            )
            return True
        os.replace(trimmed_path, file_path)
        log(f"    ✅ Silence trimmed; new duration {new_dur:.1f}s")
        return True
    finally:
        if os.path.exists(trimmed_path):
            try:
                os.remove(trimmed_path)
            except OSError:
                pass
